"""Verify the deterministic legal bundle shipped with Windows releases."""

from __future__ import annotations

import argparse
import stat
import sys
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Sequence


PLATFORM_TOOLS_NOTICE_ENTRY = "LICENSES/Android-Platform-Tools-37.0.0-NOTICE.txt"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ZIP_CREATE_SYSTEM = 3
ZIP_FILE_MODE = stat.S_IFREG | 0o644
ZIP_EXTERNAL_ATTR = ZIP_FILE_MODE << 16


class LicenseBundleError(RuntimeError):
    """Raised when legal sources or their ZIP bundle violate the contract."""


def expected_license_files(
    licenses_root: Path,
    platform_tools_notice: Path,
) -> dict[str, bytes]:
    """Return the exact, canonically named payload expected in the bundle."""

    if licenses_root.is_symlink():
        raise LicenseBundleError(
            f"Required license directory must not be a symlink: {licenses_root}"
        )
    licenses_root = licenses_root.resolve()
    if not licenses_root.is_dir():
        raise LicenseBundleError(
            f"Required license directory is missing or invalid: {licenses_root}"
        )

    source_files: list[Path] = []
    for source in licenses_root.rglob("*"):
        if source.is_symlink():
            raise LicenseBundleError(f"License sources must not contain symlinks: {source}")
        if source.is_file():
            source_files.append(source)
    source_files.sort(key=lambda path: path.relative_to(licenses_root).as_posix())
    if not source_files:
        raise LicenseBundleError(
            f"Required license directory is missing or empty: {licenses_root}"
        )

    expected: dict[str, bytes] = {}
    casefolded_names: set[str] = set()
    for source in source_files:
        relative = source.relative_to(licenses_root)
        name = PurePosixPath("LICENSES", *relative.parts).as_posix()
        _validate_entry_name(name)
        folded_name = name.casefold()
        if name in expected or folded_name in casefolded_names:
            raise LicenseBundleError(f"Duplicate license bundle entry: {name}")
        data = source.read_bytes()
        if not data:
            raise LicenseBundleError(f"License file is empty: {source}")
        expected[name] = data
        casefolded_names.add(folded_name)

    if platform_tools_notice.is_symlink():
        raise LicenseBundleError(
            f"Platform Tools NOTICE.txt must not be a symlink: {platform_tools_notice}"
        )
    platform_tools_notice = platform_tools_notice.resolve()
    if not platform_tools_notice.is_file():
        raise LicenseBundleError(
            f"Platform Tools NOTICE.txt is missing or invalid: {platform_tools_notice}"
        )
    platform_notice_data = platform_tools_notice.read_bytes()
    if not platform_notice_data:
        raise LicenseBundleError(
            f"Platform Tools NOTICE.txt is empty: {platform_tools_notice}"
        )
    if PLATFORM_TOOLS_NOTICE_ENTRY.casefold() in casefolded_names:
        raise LicenseBundleError(
            f"Reserved license bundle entry already exists: {PLATFORM_TOOLS_NOTICE_ENTRY}"
        )
    expected[PLATFORM_TOOLS_NOTICE_ENTRY] = platform_notice_data
    return expected


def verify_license_bundle(
    licenses_root: Path,
    platform_tools_notice: Path,
    bundle_path: Path,
) -> None:
    """Require a canonical ZIP_STORED bundle matching every source byte."""

    expected = expected_license_files(licenses_root, platform_tools_notice)
    bundle_path = bundle_path.resolve()
    if not bundle_path.is_file() or bundle_path.stat().st_size <= 0:
        raise LicenseBundleError(f"License bundle is missing or empty: {bundle_path}")

    try:
        with zipfile.ZipFile(bundle_path, "r") as archive:
            if archive.comment:
                raise LicenseBundleError("License bundle must not have an archive comment")
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            duplicate_names = sorted(
                name for name, count in Counter(names).items() if count != 1
            )
            if duplicate_names:
                raise LicenseBundleError(
                    f"License bundle contains duplicate entries: {duplicate_names}"
                )
            folded_names = [name.casefold() for name in names]
            duplicate_folded_names = sorted(
                name
                for name, count in Counter(folded_names).items()
                if count != 1
            )
            if duplicate_folded_names:
                raise LicenseBundleError(
                    "License bundle contains case-insensitive duplicate entries: "
                    f"{duplicate_folded_names}"
                )

            expected_names = sorted(expected)
            missing = sorted(set(expected_names) - set(names))
            unexpected = sorted(set(names) - set(expected_names))
            if missing or unexpected:
                raise LicenseBundleError(
                    "License bundle entry set mismatch: "
                    f"missing={missing}, unexpected={unexpected}"
                )
            if names != expected_names:
                raise LicenseBundleError("License bundle entries are not in canonical order")

            for entry in entries:
                _verify_entry_metadata(entry)
                try:
                    actual = archive.read(entry)
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    raise LicenseBundleError(
                        f"License bundle entry cannot be read: {entry.filename}"
                    ) from exc
                if actual != expected[entry.filename]:
                    raise LicenseBundleError(
                        "License bundle contains stale or tampered content: "
                        f"{entry.filename}"
                    )
    except zipfile.BadZipFile as exc:
        raise LicenseBundleError(f"Invalid license ZIP bundle: {bundle_path}") from exc


def _validate_entry_name(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name.startswith("LICENSES/")
        or "\\" in name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise LicenseBundleError(f"Invalid license bundle entry name: {name}")


def _verify_entry_metadata(entry: zipfile.ZipInfo) -> None:
    _validate_entry_name(entry.filename)
    if entry.is_dir():
        raise LicenseBundleError(
            f"License bundle must not contain directory entries: {entry.filename}"
        )
    if entry.file_size <= 0:
        raise LicenseBundleError(f"License bundle entry is empty: {entry.filename}")
    if entry.compress_type != zipfile.ZIP_STORED:
        raise LicenseBundleError(
            f"License bundle entry is not ZIP_STORED: {entry.filename}"
        )
    if entry.compress_size != entry.file_size:
        raise LicenseBundleError(
            f"ZIP_STORED size mismatch for license bundle entry: {entry.filename}"
        )
    if entry.date_time != ZIP_TIMESTAMP:
        raise LicenseBundleError(
            f"License bundle entry has a non-deterministic timestamp: {entry.filename}"
        )
    if entry.create_system != ZIP_CREATE_SYSTEM or entry.external_attr != ZIP_EXTERNAL_ATTR:
        raise LicenseBundleError(
            f"License bundle entry has a non-canonical file mode: {entry.filename}"
        )
    if entry.extra or entry.comment:
        raise LicenseBundleError(
            f"License bundle entry has non-canonical metadata: {entry.filename}"
        )
    if entry.flag_bits & 0x1:
        raise LicenseBundleError(
            f"License bundle entry must not be encrypted: {entry.filename}"
        )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("licenses_root", type=Path)
    parser.add_argument("platform_tools_notice", type=Path)
    parser.add_argument("bundle", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    try:
        verify_license_bundle(args.licenses_root, args.platform_tools_notice, args.bundle)
    except (LicenseBundleError, OSError) as exc:
        print(f"License bundle verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"Verified deterministic legal bundle: {args.bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
