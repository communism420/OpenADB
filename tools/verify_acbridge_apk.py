from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.build_acbridge import (  # noqa: E402
    find_executable,
    find_sdk,
    selected_sdk_dir,
    verify_apk,
    verify_public_certificate,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify an ACBridge APK against source metadata and the pinned release signer."
    )
    parser.add_argument("apk", type=Path, help="Signed ACBridge APK to verify")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    verify_public_certificate()
    sdk = find_sdk()
    build_tools = selected_sdk_dir(
        sdk / "build-tools",
        version_environment="ANDROID_BUILD_TOOLS_VERSION",
    )
    aapt = build_tools / "aapt.exe"
    zipalign = build_tools / "zipalign.exe"
    apksigner_jar = build_tools / "lib" / "apksigner.jar"
    java = find_executable("java.exe", "java")
    missing = [
        str(path)
        for path in (aapt, zipalign, apksigner_jar)
        if not path.is_file()
    ]
    if not java:
        missing.append("java")
    if missing:
        raise SystemExit("Missing Android/Java verification tools:\n" + "\n".join(missing))
    apk = args.apk.expanduser().resolve()
    verify_apk(apk, aapt, zipalign, java, apksigner_jar)
    print(f"Verified ACBridge release APK: {apk}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
