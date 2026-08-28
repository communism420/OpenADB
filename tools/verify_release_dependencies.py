"""Fail-closed verification for the hashed Windows release environment."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from pip._vendor.packaging.markers import default_environment
from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import canonicalize_name


TARGET_PYTHON_VERSION = "3.12.10"
TARGET_ENVIRONMENT = {
    **default_environment(),
    "implementation_name": "cpython",
    "implementation_version": TARGET_PYTHON_VERSION,
    "os_name": "nt",
    "platform_machine": "AMD64",
    "platform_python_implementation": "CPython",
    "platform_system": "Windows",
    "python_full_version": TARGET_PYTHON_VERSION,
    "python_version": "3.12",
    "sys_platform": "win32",
}
BOOTSTRAP_OPTIONS = ("--only-binary=:all:",)
BUILD_OPTIONS = ("--only-binary=:all:", "--no-binary=apkutils2")
BOOTSTRAP_VERSIONS = {
    "pip": "26.2.1",
    "setuptools": "83.0.0",
    "wheel": "0.46.2",
}
HASH_PATTERN = re.compile(r"^--hash=sha256:([0-9a-f]{64})$")


@dataclass(frozen=True)
class LockedRequirement:
    version: str
    extras: frozenset[str]
    artifact_hash: str


@dataclass(frozen=True)
class ReviewedRequirement:
    version: str
    extras: frozenset[str]


def _logical_lines(path: Path) -> list[str]:
    lines: list[str] = []
    pending = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        content = raw_line.split("#", 1)[0].strip()
        if not content:
            continue
        pending = f"{pending} {content}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        lines.append(pending)
        pending = ""
    if pending:
        raise ValueError(f"Unterminated continuation in {path}")
    return lines


def _extras(requirement: Requirement) -> frozenset[str]:
    return frozenset(canonicalize_name(extra) for extra in requirement.extras)


def load_hash_lock(
    path: Path,
    *,
    expected_options: tuple[str, ...],
) -> dict[str, LockedRequirement]:
    """Parse an exact artifact lock and require its complete option contract."""

    expected: dict[str, LockedRequirement] = {}
    options: list[str] = []
    for line in _logical_lines(path):
        if line.startswith("--"):
            options.append(line)
            continue
        tokens = line.split()
        if len(tokens) != 2:
            raise ValueError(f"Lock entry must contain one requirement and one hash: {line}")
        requirement = Requirement(tokens[0])
        specifiers = list(requirement.specifier)
        if requirement.url or requirement.marker or len(specifiers) != 1:
            raise ValueError(f"Lock entry is not an unconditional exact pin: {line}")
        specifier = specifiers[0]
        if specifier.operator != "==" or "*" in specifier.version:
            raise ValueError(f"Lock entry is not exactly pinned: {line}")
        hash_match = HASH_PATTERN.fullmatch(tokens[1])
        if hash_match is None:
            raise ValueError(f"Lock entry does not contain one lowercase SHA-256: {line}")
        name = canonicalize_name(requirement.name)
        candidate = LockedRequirement(
            version=specifier.version,
            extras=_extras(requirement),
            artifact_hash=hash_match.group(1),
        )
        if name in expected:
            raise ValueError(f"Duplicate distribution in release lock: {requirement.name}")
        expected[name] = candidate
    if tuple(options) != expected_options:
        raise ValueError(
            f"Release lock option contract mismatch in {path}: "
            f"expected={list(expected_options)}, actual={options}"
        )
    if not expected:
        raise ValueError(f"Release lock is empty: {path}")
    return expected


def load_active_requirements(
    path: Path,
    seen: set[Path] | None = None,
) -> dict[str, ReviewedRequirement]:
    """Resolve exact pins, extras, and includes for the fixed release target."""

    expected: dict[str, ReviewedRequirement] = {}
    seen = set() if seen is None else seen
    resolved = path.resolve()
    if resolved in seen:
        return expected
    seen.add(resolved)
    for line in _logical_lines(resolved):
        if line.startswith(("-r ", "--requirement ")):
            included = line.split(maxsplit=1)[1]
            included_requirements = load_active_requirements(
                resolved.parent / included,
                seen,
            )
            for name, candidate in included_requirements.items():
                if name in expected and expected[name] != candidate:
                    raise ValueError(f"Conflicting requirement for {name}")
                expected[name] = candidate
            continue
        if line.startswith("--"):
            raise ValueError(f"Unexpected option in reviewed requirements: {line}")
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate(TARGET_ENVIRONMENT):
            continue
        specifiers = list(requirement.specifier)
        if requirement.url or len(specifiers) != 1:
            raise ValueError(f"Release requirement is not exactly pinned: {line}")
        specifier = specifiers[0]
        if specifier.operator != "==" or "*" in specifier.version:
            raise ValueError(f"Release requirement is not exactly pinned: {line}")
        name = canonicalize_name(requirement.name)
        candidate = ReviewedRequirement(
            version=specifier.version,
            extras=_extras(requirement),
        )
        if name in expected and expected[name] != candidate:
            raise ValueError(f"Conflicting requirement for {requirement.name}")
        expected[name] = candidate
    return expected


def installed_distributions() -> dict[str, str]:
    installed: dict[str, str] = {}
    for distribution in metadata.distributions():
        raw_name = distribution.metadata.get("Name")
        if not raw_name:
            raise ValueError(f"Installed distribution has no Name metadata: {distribution}")
        name = canonicalize_name(raw_name)
        if name in installed:
            raise ValueError(f"Installed distribution appears more than once: {raw_name}")
        installed[name] = distribution.version
    return installed


def _require_target_python(python_version: str) -> None:
    if python_version != TARGET_PYTHON_VERSION:
        raise ValueError(
            f"Expected CPython {TARGET_PYTHON_VERSION}, found {python_version}"
        )


def validate_bootstrap_lock(lock: dict[str, LockedRequirement]) -> None:
    locked_versions = {name: value.version for name, value in lock.items()}
    if locked_versions != BOOTSTRAP_VERSIONS:
        raise ValueError(
            "Bootstrap lock must contain only the reviewed build tools: "
            f"expected={BOOTSTRAP_VERSIONS}, actual={locked_versions}"
        )
    with_extras = sorted(name for name, value in lock.items() if value.extras)
    if with_extras:
        raise ValueError(f"Bootstrap requirements must not have extras: {with_extras}")


def _require_exact_installed(
    expected: dict[str, str],
    installed: dict[str, str],
    *,
    label: str,
) -> None:
    if installed == expected:
        return
    missing = sorted(expected.keys() - installed.keys())
    unexpected = sorted(installed.keys() - expected.keys())
    mismatched = sorted(
        name
        for name in installed.keys() & expected.keys()
        if installed[name] != expected[name]
    )
    raise ValueError(
        f"Installed {label} environment differs from its lock: "
        f"missing={missing}, unexpected={unexpected}, mismatched={mismatched}"
    )


def validate_bootstrap_environment(
    *,
    lock: dict[str, LockedRequirement],
    installed: dict[str, str],
    python_version: str,
) -> None:
    _require_target_python(python_version)
    validate_bootstrap_lock(lock)
    _require_exact_installed(BOOTSTRAP_VERSIONS, installed, label="bootstrap")


def validate_release_environment(
    *,
    lock: dict[str, LockedRequirement],
    bootstrap_lock: dict[str, LockedRequirement],
    requirements: dict[str, ReviewedRequirement],
    installed: dict[str, str],
    python_version: str,
) -> None:
    _require_target_python(python_version)
    validate_bootstrap_lock(bootstrap_lock)

    reviewed = dict(requirements)
    reviewed["pip"] = ReviewedRequirement(version="26.2.1", extras=frozenset())
    locked_review = {
        name: ReviewedRequirement(version=value.version, extras=value.extras)
        for name, value in lock.items()
    }
    if locked_review != reviewed:
        missing = sorted(reviewed.keys() - locked_review.keys())
        unexpected = sorted(locked_review.keys() - reviewed.keys())
        mismatched = sorted(
            name
            for name in locked_review.keys() & reviewed.keys()
            if locked_review[name] != reviewed[name]
        )
        raise ValueError(
            "Hash lock does not match reviewed requirements: "
            f"missing={missing}, unexpected={unexpected}, mismatched={mismatched}"
        )

    bootstrap_drift = sorted(
        name
        for name, value in bootstrap_lock.items()
        if lock.get(name) != value
    )
    if bootstrap_drift:
        raise ValueError(
            "Full release lock does not preserve the verified bootstrap artifacts: "
            f"{bootstrap_drift}"
        )

    locked_versions = {name: value.version for name, value in lock.items()}
    _require_exact_installed(locked_versions, installed, label="release")


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("bootstrap", "build"), required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--bootstrap-lock", type=Path)
    parser.add_argument("--requirements", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)

    try:
        if arguments.phase == "bootstrap":
            if arguments.bootstrap_lock is not None or arguments.requirements is not None:
                raise ValueError(
                    "Bootstrap verification accepts only --phase bootstrap and --lock"
                )
            lock = load_hash_lock(
                arguments.lock,
                expected_options=BOOTSTRAP_OPTIONS,
            )
            validate_bootstrap_environment(
                lock=lock,
                installed=installed_distributions(),
                python_version=sys.version.split()[0],
            )
        else:
            if arguments.bootstrap_lock is None or arguments.requirements is None:
                raise ValueError(
                    "Build verification requires --bootstrap-lock and --requirements"
                )
            lock = load_hash_lock(arguments.lock, expected_options=BUILD_OPTIONS)
            bootstrap_lock = load_hash_lock(
                arguments.bootstrap_lock,
                expected_options=BOOTSTRAP_OPTIONS,
            )
            requirements = load_active_requirements(arguments.requirements)
            validate_release_environment(
                lock=lock,
                bootstrap_lock=bootstrap_lock,
                requirements=requirements,
                installed=installed_distributions(),
                python_version=sys.version.split()[0],
            )
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1

    for name, requirement in sorted(lock.items()):
        extras = f"[{','.join(sorted(requirement.extras))}]" if requirement.extras else ""
        print(
            f"{name}{extras}=={requirement.version} "
            f"sha256:{requirement.artifact_hash}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
