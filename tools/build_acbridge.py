from __future__ import annotations

# ruff: noqa: E402 -- the repository root must be added before importing release metadata.

import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openadb.version import (
    ACBRIDGE_APK_FILENAME,
    ACBRIDGE_PACKAGE,
    ACBRIDGE_SIGNER_SHA256,
    ACBRIDGE_VERSION_CODE,
    VERSION,
)


BRIDGE_DIR = ROOT / "openadb" / "resources" / "acbridge"
BUILD_DIR = ROOT / "build" / "acbridge"
APK_OUT = BRIDGE_DIR / ACBRIDGE_APK_FILENAME
KEYSTORE = BRIDGE_DIR / "openadb-debug.keystore"
ANDROID_NS = "{http://schemas.android.com/apk/res/android}"


def main() -> int:
    verify_source_metadata()
    sdk = find_sdk()
    build_tools = latest_dir(sdk / "build-tools")
    platform = latest_dir(sdk / "platforms")
    android_jar = platform / "android.jar"
    aapt = build_tools / "aapt.exe"
    d8_jar = build_tools / "lib" / "d8.jar"
    zipalign = build_tools / "zipalign.exe"
    apksigner_jar = build_tools / "lib" / "apksigner.jar"
    java = find_executable("java.exe", "java")
    javac = find_executable("javac.exe", "javac")
    keytool = find_executable("keytool.exe", "keytool")

    required = [android_jar, aapt, d8_jar, zipalign, apksigner_jar]
    missing = [str(path) for path in required if not path.exists()]
    if missing or not java or not javac or not keytool:
        raise SystemExit("Missing Android/Java build tools:\n" + "\n".join(missing + [str(x) for x in [java, javac, keytool] if not x]))

    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    classes_dir = BUILD_DIR / "classes"
    dex_dir = BUILD_DIR / "dex"
    classes_dir.mkdir(parents=True)
    dex_dir.mkdir(parents=True)

    java_files = [str(path) for path in (BRIDGE_DIR / "src").rglob("*.java")]
    run([javac, "-source", "1.8", "-target", "1.8", "-bootclasspath", android_jar, "-d", classes_dir, *java_files])
    class_files = [str(path) for path in classes_dir.rglob("*.class")]
    run([java, "-cp", d8_jar, "com.android.tools.r8.D8", "--lib", android_jar, "--min-api", "23", "--output", dex_dir, *class_files])

    unsigned = BUILD_DIR / "acbridge-unsigned.apk"
    unsigned_with_dex = BUILD_DIR / "acbridge-unsigned-dex.apk"
    aligned = BUILD_DIR / "acbridge-aligned.apk"
    signed = BUILD_DIR / "acbridge-signed.apk"
    aapt_command = [aapt, "package", "-f", "-M", BRIDGE_DIR / "AndroidManifest.xml", "-I", android_jar]
    res_dir = BRIDGE_DIR / "res"
    if res_dir.exists():
        aapt_command.extend(["-S", res_dir])
    aapt_command.extend(["-F", unsigned])
    run(aapt_command)
    shutil.copy2(unsigned, unsigned_with_dex)
    with zipfile.ZipFile(unsigned_with_dex, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(dex_dir / "classes.dex", "classes.dex")

    run([zipalign, "-f", "4", unsigned_with_dex, aligned])
    if not KEYSTORE.exists():
        run(
            [
                keytool,
                "-genkeypair",
                "-keystore",
                KEYSTORE,
                "-storepass",
                "android",
                "-keypass",
                "android",
                "-alias",
                "openadbdebug",
                "-dname",
                "CN=OpenADB Debug,O=OpenADB,C=US",
                "-keyalg",
                "RSA",
                "-keysize",
                "2048",
                "-validity",
                "10000",
            ]
        )
    run(
        [
            java,
            "-jar",
            apksigner_jar,
            "sign",
            "--v4-signing-enabled",
            "false",
            "--ks",
            KEYSTORE,
            "--ks-pass",
            "pass:android",
            "--key-pass",
            "pass:android",
            "--out",
            signed,
            aligned,
        ]
    )
    verify_apk(signed, aapt, zipalign, java, apksigner_jar)
    atomic_publish(signed, APK_OUT)
    compatible_apk = BRIDGE_DIR / "ACBridge.apk"
    atomic_publish(signed, compatible_apk)
    verify_apk(APK_OUT, aapt, zipalign, java, apksigner_jar)
    verify_apk(compatible_apk, aapt, zipalign, java, apksigner_jar)
    if APK_OUT.read_bytes() != compatible_apk.read_bytes():
        raise SystemExit("ACBridge.apk does not contain the same build as the versioned APK")
    print(
        f"Built and verified {APK_OUT} "
        f"(package={ACBRIDGE_PACKAGE}, versionName={VERSION}, versionCode={ACBRIDGE_VERSION_CODE}, "
        f"bytes={APK_OUT.stat().st_size})"
    )
    return 0


def verify_source_metadata() -> None:
    manifest = ET.parse(BRIDGE_DIR / "AndroidManifest.xml").getroot()
    actual = (
        manifest.attrib.get("package", ""),
        manifest.attrib.get(f"{ANDROID_NS}versionName", ""),
        manifest.attrib.get(f"{ANDROID_NS}versionCode", ""),
    )
    expected = (ACBRIDGE_PACKAGE, VERSION, str(ACBRIDGE_VERSION_CODE))
    if actual != expected:
        raise SystemExit(f"ACBridge source manifest metadata mismatch: expected {expected}, got {actual}")


def verify_apk(apk_path: Path, aapt: Path, zipalign: Path, java: str, apksigner_jar: Path) -> None:
    if not apk_path.is_file() or apk_path.stat().st_size <= 0:
        raise SystemExit(f"ACBridge APK is missing or empty: {apk_path}")
    # Legacy aapt builds cannot reliably reopen archives whose absolute path
    # contains non-ASCII Windows characters. Verification uses a byte-for-byte
    # temporary copy in the system temp folder while the shipped APK remains in
    # its original project location.
    with tempfile.TemporaryDirectory(prefix="openadb-acbridge-verify-") as temp_dir:
        verification_apk = Path(temp_dir) / apk_path.name
        shutil.copy2(apk_path, verification_apk)
        metadata = run_capture([aapt, "dump", "badging", verification_apk])
        run([zipalign, "-c", "-v", "4", verification_apk])
        signature = run_capture(
            [java, "-jar", apksigner_jar, "verify", "--verbose", "--print-certs", verification_apk]
        )
    package_match = re.search(
        r"package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'",
        metadata,
    )
    if not package_match:
        raise SystemExit(f"Unable to read package metadata from {apk_path}")
    actual = package_match.groups()
    expected = (ACBRIDGE_PACKAGE, str(ACBRIDGE_VERSION_CODE), VERSION)
    if actual != expected:
        raise SystemExit(f"ACBridge APK metadata mismatch: expected {expected}, got {actual}")
    for scheme in ("v1", "v2", "v3"):
        if f"Verified using {scheme} scheme" not in signature or not re.search(
            rf"Verified using {scheme} scheme[^:]*:\s*true", signature
        ):
            raise SystemExit(f"ACBridge APK is not verified with the required {scheme} signature scheme")
    signer_match = re.search(r"certificate SHA-256 digest:\s*([0-9a-f]+)", signature, re.IGNORECASE)
    if not signer_match or signer_match.group(1).lower() != ACBRIDGE_SIGNER_SHA256:
        actual_signer = signer_match.group(1).lower() if signer_match else "unreadable"
        raise SystemExit(
            f"ACBridge signer mismatch: expected {ACBRIDGE_SIGNER_SHA256}, got {actual_signer}"
        )


def atomic_publish(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        shutil.copy2(source, temporary)
        if source.read_bytes() != temporary.read_bytes():
            raise SystemExit(f"Failed to verify staged APK copy for {destination}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def find_sdk() -> Path:
    candidates = [
        os.environ.get("ANDROID_HOME"),
        os.environ.get("ANDROID_SDK_ROOT"),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk"),
    ]
    for raw in candidates:
        if raw:
            path = Path(raw)
            if (path / "build-tools").exists() and (path / "platforms").exists():
                return path
    raise SystemExit("Android SDK was not found.")


def latest_dir(parent: Path) -> Path:
    dirs = [path for path in parent.iterdir() if path.is_dir()]
    if not dirs:
        raise SystemExit(f"No directories in {parent}")
    return sorted(dirs, key=lambda path: path.name, reverse=True)[0]


def find_executable(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    roots = [
        Path("C:/Program Files/Java"),
        Path("C:/Program Files/Android/Android Studio/jbr/bin"),
    ]
    for root in roots:
        if not root.exists():
            continue
        for name in names:
            matches = list(root.rglob(name)) if root.is_dir() and root.name != "bin" else list(root.glob(name))
            if matches:
                return str(matches[0])
    return None


def run(command: list[object]) -> None:
    command_text = [str(part) for part in command]
    completed = subprocess.run(command_text, cwd=ROOT, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        raise SystemExit(f"Command failed: {' '.join(command_text)}")


def run_capture(command: list[object]) -> str:
    command_text = [str(part) for part in command]
    completed = subprocess.run(
        command_text,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout).strip()
        raise SystemExit(f"Command failed: {' '.join(command_text)}\n{details}")
    return completed.stdout


if __name__ == "__main__":
    sys.exit(main())
