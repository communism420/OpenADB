"""Small thread-safe registry for cancellable background operations."""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from .device_context import DeviceContext


class OperationConflictError(RuntimeError):
    """Raised when a live operation already owns a conflict group."""


@dataclass(slots=True)
class OperationToken:
    operation_id: str
    owner_key: str
    device_context: DeviceContext | None
    cancel_event: threading.Event
    conflict_group: str
    conflict_groups: frozenset[str] = field(default_factory=frozenset)
    _cancellation_reason: str = ""
    _reason_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    @property
    def cancellation_reason(self) -> str:
        with self._reason_lock:
            return self._cancellation_reason

    def cancel(self, reason: str) -> bool:
        """Request cancellation once and retain the first useful reason."""

        with self._reason_lock:
            first_request = not self.cancel_event.is_set()
            if first_request or not self._cancellation_reason:
                self._cancellation_reason = str(reason or "cancelled")
            self.cancel_event.set()
            return first_request


class OperationRegistry:
    """Track operation ownership, conflicts, cancellation, and cleanup."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tokens: dict[str, OperationToken] = {}
        self._shutting_down = False

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._tokens)

    @property
    def shutting_down(self) -> bool:
        with self._lock:
            return self._shutting_down

    def active_tokens(self) -> tuple[OperationToken, ...]:
        with self._lock:
            return tuple(self._tokens.values())

    def register(
        self,
        owner_key: str,
        *,
        device_context: DeviceContext | None = None,
        conflict_group: str = "",
        conflict_groups: Iterable[str] = (),
        cancel_event: threading.Event | None = None,
        operation_id: str | None = None,
    ) -> OperationToken:
        owner_key = str(owner_key or "").strip()
        if not owner_key:
            raise ValueError("owner_key is required")
        conflict_group = str(conflict_group or "").strip()
        normalized_groups = {
            str(group or "").strip()
            for group in conflict_groups
            if str(group or "").strip()
        }
        if conflict_group:
            normalized_groups.add(conflict_group)
        frozen_groups = frozenset(normalized_groups)
        with self._lock:
            if self._shutting_down:
                raise RuntimeError("Operation registry is shutting down")
            if frozen_groups:
                conflict = next(
                    (
                        token
                        for token in self._tokens.values()
                        if token.conflict_groups.intersection(frozen_groups)
                    ),
                    None,
                )
                if conflict is not None:
                    shared_groups = ", ".join(sorted(conflict.conflict_groups.intersection(frozen_groups)))
                    raise OperationConflictError(
                        f"Operation conflict group '{shared_groups}' is already owned by {conflict.owner_key}"
                    )
            token_id = operation_id or uuid.uuid4().hex
            if token_id in self._tokens:
                raise ValueError(f"Duplicate operation_id: {token_id}")
            token = OperationToken(
                operation_id=token_id,
                owner_key=owner_key,
                device_context=device_context,
                cancel_event=cancel_event or threading.Event(),
                conflict_group=conflict_group,
                conflict_groups=frozen_groups,
            )
            self._tokens[token_id] = token
            return token

    def contains(self, token: OperationToken) -> bool:
        with self._lock:
            return self._tokens.get(token.operation_id) is token

    def finish(self, token: OperationToken) -> bool:
        with self._lock:
            if self._tokens.get(token.operation_id) is not token:
                return False
            del self._tokens[token.operation_id]
            return True

    def cancel_owner(self, owner_key: str, reason: str = "owner cancelled") -> int:
        owner_key = str(owner_key or "").strip()
        with self._lock:
            tokens = [token for token in self._tokens.values() if token.owner_key == owner_key]
        return sum(1 for token in tokens if token.cancel(reason))

    def cancel_stale(self, current_generation: int, reason: str = "device context changed") -> int:
        with self._lock:
            tokens = [
                token
                for token in self._tokens.values()
                if token.device_context is not None
                and token.device_context.generation < current_generation
            ]
        return sum(1 for token in tokens if token.cancel(reason))

    def cancel_all(self, reason: str = "application shutdown", *, remove: bool = False) -> int:
        with self._lock:
            tokens = tuple(self._tokens.values())
            if remove:
                self._tokens.clear()
        for token in tokens:
            token.cancel(reason)
        return len(tokens)

    def shutdown(self, reason: str = "application shutdown") -> int:
        with self._lock:
            self._shutting_down = True
        return self.cancel_all(reason, remove=True)

    @contextmanager
    def tracked(
        self,
        owner_key: str,
        *,
        device_context: DeviceContext | None = None,
        conflict_group: str = "",
        conflict_groups: Iterable[str] = (),
        cancel_event: threading.Event | None = None,
    ) -> Iterator[OperationToken]:
        token = self.register(
            owner_key,
            device_context=device_context,
            conflict_group=conflict_group,
            conflict_groups=conflict_groups,
            cancel_event=cancel_event,
        )
        try:
            yield token
        finally:
            self.finish(token)
