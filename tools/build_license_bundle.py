"""Build the deterministic legal bundle shipped with Windows releases."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Sequence

if __package__:
    from .verify_license_bundle import (
        LicenseBundleError,
        ZIP_CREATE_SYSTEM,
        ZIP_EXTERNAL_ATTR,
        ZIP_TIMESTAMP,
        expected_license_files,
        verify_license_bundle,
    )
else:
    from verify_license_bundle import (  # type: ignore[no-redef]
        LicenseBundleError,
        ZIP_CREATE_SYSTEM,
        ZIP_EXTERNAL_ATTR,
        ZIP_TIMESTAMP,
        expected_license_files,
        verify_license_bundle,
    )


def build_license_bundle(
    licenses_root: Path,
    platform_tools_notice: Path,
    output_path: Path,
) -> None:
    """Atomically write a canonical ZIP_STORED bundle and verify it."""

    expected = expected_license_files(licenses_root, platform_tools_notice)
    licenses_root = licenses_root.resolve()
    platform_tools_notice = platform_tools_notice.resolve()
    if output_path.is_symlink():
        raise LicenseBundleError("License bundle output must not be a symlink")
    output_path = output_path.resolve()
    if output_path.is_relative_to(licenses_root) or output_path == platform_tools_notice:
        raise LicenseBundleError("License bundle output must be outside its source files")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for name in sorted(expected):
                info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = ZIP_CREATE_SYSTEM
                info.external_attr = ZIP_EXTERNAL_ATTR
                archive.writestr(info, expected[name])
        verify_license_bundle(licenses_root, platform_tools_notice, temporary_path)
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("licenses_root", type=Path)
    parser.add_argument("platform_tools_notice", type=Path)
    parser.add_argument("output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    try:
        build_license_bundle(args.licenses_root, args.platform_tools_notice, args.output)
    except (LicenseBundleError, OSError) as exc:
        print(f"License bundle build failed: {exc}", file=sys.stderr)
        return 1
    print(f"Built deterministic legal bundle: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
