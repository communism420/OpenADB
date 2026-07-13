"""Immutable input model for device-bound file transfers."""

from __future__ import annotations

from dataclasses import dataclass
from operator import index
from os import PathLike, fspath

from .acbridge_p2p import (
    ADB_TRANSPORT,
    P2P_MAX_PARALLELISM,
    P2P_TRANSPORT,
)
from .device_context import DeviceContext


PUSH_DIRECTION = "pc_to_android"
PULL_DIRECTION = "android_to_pc"
ADB_TRANSFER = ADB_TRANSPORT
P2P_TRANSFER = P2P_TRANSPORT
FIXED_PARALLELISM = "fixed"
AUTO_PARALLELISM = "auto"
MAX_REQUESTED_PARALLELISM = P2P_MAX_PARALLELISM


class TransferPlanError(ValueError):
    """Raised before a transfer starts when its captured inputs are invalid."""


@dataclass(frozen=True, slots=True)
class TransferPlan:
    """A complete transfer request that cannot follow later UI changes.

    All path-like inputs are copied to strings and ``sources`` is copied to a
    tuple during construction.  The contained :class:`DeviceContext` is itself
    immutable, so a plan remains bound to the exact device generation that was
    active when the user started the operation.
    """

    direction: str
    transport: str
    sources: tuple[str, ...]
    destination: str
    device_context: DeviceContext
    use_root: bool = False
    parallelism_mode: str = FIXED_PARALLELISM
    requested_parallelism: int | None = 1

    def __post_init__(self) -> None:
        if not isinstance(self.device_context, DeviceContext):
            raise TransferPlanError("A transfer plan requires an immutable DeviceContext")
        if not isinstance(self.use_root, bool):
            raise TransferPlanError("use_root must be a boolean")

        direction = _normalize_direction(self.direction)
        transport = _normalize_transport(self.transport)
        sources = _copy_sources(self.sources)
        destination = _copy_path(self.destination, field_name="destination")
        parallelism_mode = _normalize_parallelism_mode(self.parallelism_mode)
        requested_parallelism = _normalize_parallelism(
            self.requested_parallelism,
            mode=parallelism_mode,
        )

        if direction == PULL_DIRECTION and transport == P2P_TRANSFER:
            raise TransferPlanError("ACBridge P2P currently supports PC to Android transfers only")

        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "transport", transport)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "destination", destination)
        object.__setattr__(self, "parallelism_mode", parallelism_mode)
        object.__setattr__(self, "requested_parallelism", requested_parallelism)

    @property
    def context(self) -> DeviceContext:
        """Compatibility alias for callers that use the shorter name."""

        return self.device_context

    @property
    def is_upload(self) -> bool:
        return self.direction == PUSH_DIRECTION

    @property
    def is_download(self) -> bool:
        return self.direction == PULL_DIRECTION

    @property
    def is_p2p(self) -> bool:
        return self.transport == P2P_TRANSFER

    def fixed_parallelism(self, *, automatic_default: int = 1) -> int:
        """Return the captured stream count for a strategy ready to execute.

        Automatic stream selection is deliberately left to the P2P strategy,
        after it has collected immutable file-count and size statistics.  It
        does not probe or guess network speed.  Until then,
        ``automatic_default`` is used by legacy callers.
        """

        if self.parallelism_mode == FIXED_PARALLELISM:
            assert self.requested_parallelism is not None
            return self.requested_parallelism
        if self.requested_parallelism is not None:
            return self.requested_parallelism
        normalized = _normalize_parallelism(automatic_default, mode=FIXED_PARALLELISM)
        assert normalized is not None
        return normalized


def _copy_sources(values: object) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, PathLike)):
        values = (values,)
    try:
        copied = tuple(_copy_path(value, field_name="source") for value in values)  # type: ignore[union-attr]
    except TypeError as exc:
        raise TransferPlanError("sources must be an iterable of paths") from exc
    if not copied:
        raise TransferPlanError("At least one transfer source is required")
    return copied


def _copy_path(value: object, *, field_name: str) -> str:
    if value is None:
        raise TransferPlanError(f"{field_name} must be a valid path")
    if isinstance(value, bytes):
        raise TransferPlanError(f"{field_name} must be a text path")
    try:
        copied = fspath(value) if isinstance(value, PathLike) else str(value)
    except (TypeError, ValueError) as exc:
        raise TransferPlanError(f"{field_name} must be a valid path") from exc
    if isinstance(copied, bytes):
        raise TransferPlanError(f"{field_name} must be a text path")
    if not copied:
        raise TransferPlanError(f"{field_name} must not be empty")
    if any(ord(character) < 32 for character in copied):
        raise TransferPlanError(f"{field_name} contains an unsafe control character")
    return copied


def _normalize_direction(value: object) -> str:
    normalized = str(value or "").strip().casefold().replace("→", "->")
    aliases = {
        "push": PUSH_DIRECTION,
        "upload": PUSH_DIRECTION,
        "pc_to_android": PUSH_DIRECTION,
        "pc-to-android": PUSH_DIRECTION,
        "pc -> android": PUSH_DIRECTION,
        "pull": PULL_DIRECTION,
        "download": PULL_DIRECTION,
        "android_to_pc": PULL_DIRECTION,
        "android-to-pc": PULL_DIRECTION,
        "android -> pc": PULL_DIRECTION,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise TransferPlanError(f"Unsupported transfer direction: {value!r}") from exc


def _normalize_transport(value: object) -> str:
    normalized = str(value or "").strip().casefold().replace("-", "_")
    aliases = {
        "adb": ADB_TRANSFER,
        "platform_tools": ADB_TRANSFER,
        "platform tools": ADB_TRANSFER,
        "p2p": P2P_TRANSFER,
        "acbridge_p2p": P2P_TRANSFER,
        "acbridge p2p": P2P_TRANSFER,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise TransferPlanError(f"Unsupported transfer transport: {value!r}") from exc


def _normalize_parallelism_mode(value: object) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized in {FIXED_PARALLELISM, "manual"}:
        return FIXED_PARALLELISM
    if normalized in {AUTO_PARALLELISM, "automatic"}:
        return AUTO_PARALLELISM
    raise TransferPlanError(f"Unsupported parallelism mode: {value!r}")


def _normalize_parallelism(value: object, *, mode: str) -> int | None:
    if value is None:
        if mode == AUTO_PARALLELISM:
            return None
        raise TransferPlanError("Fixed parallelism requires a stream count")
    if isinstance(value, bool):
        raise TransferPlanError("Parallelism must be a positive integer")
    try:
        result = index(value)  # type: ignore[arg-type]
    except TypeError:
        if isinstance(value, str) and value.strip().isdecimal():
            result = int(value.strip())
        else:
            raise TransferPlanError("Parallelism must be a positive integer") from None
    if result < 1 or result > MAX_REQUESTED_PARALLELISM:
        raise TransferPlanError(
            f"Parallelism must be between 1 and {MAX_REQUESTED_PARALLELISM}"
        )
    return result
