from __future__ import annotations

# ruff: noqa: E402 -- the repository root must be added before importing release metadata.
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
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
SHIZUKU_DIR = BRIDGE_DIR / "third_party" / "shizuku-13.1.5"
SHIZUKU_AARS = {
    "api-13.1.5.aar": "4def9bde498ef8626614c2fc5db9af4749c86f16f6c33e3f5658d35e70bab59b",
    "provider-13.1.5.aar": "b0f18cd9812464ec171c53cac93a819fe411718a3965c311f01eb4de265381b3",
    "aidl-13.1.5.aar": "33fe7191cdd69fcb66d649264f3b0c47acb2f3d6343afc05b98dbbff6f221963",
    "shared-13.1.5.aar": "4659642c9339be0a26e9c65bb8648f7ad6d8f4a465f557993ccbc78802381635",
}
DESUGAR_DIR = BRIDGE_DIR / "third_party" / "desugar_jdk_libs-2.1.5"
DESUGAR_ARTIFACTS = {
    "desugar_jdk_libs-2.1.5.jar": "d8044befae095781b9a80bf1faa92edc30382d75d437476784c1bf991598a976",
    "desugar_jdk_libs_configuration-2.1.5.jar": "7bc9051b3a1ec19806311dcb6aa9b9ba7ef9c22caa6f4810da55bde285fb7770",
}
APK_LEGAL_FILES = {
    "assets/legal/LICENSE.txt": ROOT / "LICENSE",
    "assets/legal/THIRD_PARTY_NOTICES.md": ROOT / "THIRD_PARTY_NOTICES.md",
    "assets/legal/THIRD_PARTY_SOURCES.md": ROOT / "THIRD_PARTY_SOURCES.md",
    "assets/legal/Shizuku-API-MIT.txt": SHIZUKU_DIR / "LICENSE-Shizuku-API.txt",
    "assets/legal/desugar_jdk_libs-GPL-2.0-with-Classpath-exception.txt": (
        DESUGAR_DIR / "LICENSE-desugar_jdk_libs.txt"
    ),
    "assets/legal/desugar_jdk_libs_configuration-BSD-3-Clause.txt": (
        DESUGAR_DIR / "LICENSE-configuration.txt"
    ),
}


def main() -> int:
    verify_source_metadata()
    sdk = find_sdk()
    build_tools = latest_dir(sdk / "build-tools")
    platform = latest_dir(sdk / "platforms")
    android_jar = platform / "android.jar"
    aapt = build_tools / "aapt.exe"
    aidl = build_tools / "aidl.exe"
    d8_jar = build_tools / "lib" / "d8.jar"
    zipalign = build_tools / "zipalign.exe"
    apksigner_jar = build_tools / "lib" / "apksigner.jar"
    java = find_executable("java.exe", "java")
    javac = find_executable("javac.exe", "javac")
    keytool = find_executable("keytool.exe", "keytool")

    required = [android_jar, aapt, aidl, d8_jar, zipalign, apksigner_jar]
    missing = [str(path) for path in required if not path.exists()]
    if missing or not java or not javac or not keytool:
        raise SystemExit("Missing Android/Java build tools:\n" + "\n".join(missing + [str(x) for x in [java, javac, keytool] if not x]))

    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    classes_dir = BUILD_DIR / "classes"
    dex_dir = BUILD_DIR / "dex"
    desugar_dex_dir = BUILD_DIR / "desugar_dex"
    generated_dir = BUILD_DIR / "generated"
    classes_dir.mkdir(parents=True)
    dex_dir.mkdir(parents=True)
    desugar_dex_dir.mkdir(parents=True)
    generated_dir.mkdir(parents=True)

    apk_assets = prepare_apk_legal_assets(BUILD_DIR / "apk_assets")
    dependency_jars = extract_verified_shizuku_jars(BUILD_DIR / "dependencies")
    desugar_library, desugar_configuration_jar, desugar_configuration = prepare_desugared_library(
        BUILD_DIR / "dependencies"
    )
    aidl_files = list((BRIDGE_DIR / "src").rglob("*.aidl"))
    for aidl_file in aidl_files:
        run(
            [
                aidl,
                "--lang=java",
                "--omit_invocation",
                "--min_sdk_version=23",
                "-I",
                BRIDGE_DIR / "src",
                "-o",
                generated_dir,
                aidl_file,
            ]
        )

    java_files = [str(path) for path in (BRIDGE_DIR / "src").rglob("*.java")]
    java_files.extend(str(path) for path in generated_dir.rglob("*.java"))
    classpath = os.pathsep.join(str(path) for path in dependency_jars)
    run(
        [
            javac,
            "-source",
            "1.8",
            "-target",
            "1.8",
            "-bootclasspath",
            android_jar,
            "-classpath",
            classpath,
            "-d",
            classes_dir,
            *java_files,
        ]
    )
    class_files = [str(path) for path in classes_dir.rglob("*.class")]
    run(
        [
            java,
            "-cp",
            d8_jar,
            "com.android.tools.r8.D8",
            "--lib",
            android_jar,
            "--classpath",
            desugar_library,
            "--classpath",
            desugar_configuration_jar,
            "--desugared-lib",
            desugar_configuration,
            "--min-api",
            "23",
            "--output",
            dex_dir,
            *class_files,
            *dependency_jars,
        ]
    )
    run(
        [
            java,
            "-cp",
            d8_jar,
            "com.android.tools.r8.L8",
            "--release",
            "--lib",
            android_jar,
            "--desugared-lib",
            desugar_configuration,
            "--min-api",
            "23",
            "--output",
            desugar_dex_dir,
            desugar_library,
            desugar_configuration_jar,
        ]
    )

    unsigned = BUILD_DIR / "acbridge-unsigned.apk"
    unsigned_with_dex = BUILD_DIR / "acbridge-unsigned-dex.apk"
    aligned = BUILD_DIR / "acbridge-aligned.apk"
    signed = BUILD_DIR / "acbridge-signed.apk"
    aapt_command = [aapt, "package", "-f", "-M", BRIDGE_DIR / "AndroidManifest.xml", "-I", android_jar]
    res_dir = BRIDGE_DIR / "res"
    if res_dir.exists():
        aapt_command.extend(["-S", res_dir])
    aapt_command.extend(["-A", apk_assets])
    aapt_command.extend(["-F", unsigned])
    run(aapt_command)
    shutil.copy2(unsigned, unsigned_with_dex)
    with zipfile.ZipFile(unsigned_with_dex, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        dex_files = sorted(dex_dir.glob("classes*.dex"), key=dex_sort_key)
        dex_files.extend(sorted(desugar_dex_dir.glob("classes*.dex"), key=dex_sort_key))
        if not dex_files:
            raise SystemExit("D8 did not produce any dex files")
        for index, dex_file in enumerate(dex_files, start=1):
            dex_name = "classes.dex" if index == 1 else f"classes{index}.dex"
            archive.write(dex_file, dex_name)

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


def prepare_apk_legal_assets(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    for archive_member, source in APK_LEGAL_FILES.items():
        if not source.is_file():
            raise SystemExit(f"Missing ACBridge legal file: {source}")
        expected_prefix = "assets/"
        if not archive_member.startswith(expected_prefix):
            raise SystemExit(f"Invalid ACBridge legal asset path: {archive_member}")
        relative_path = Path(archive_member.removeprefix(expected_prefix))
        if not relative_path.parts or relative_path.is_absolute() or ".." in relative_path.parts:
            raise SystemExit(f"Invalid ACBridge legal asset path: {archive_member}")
        payload = source.read_bytes()
        if not payload.strip():
            raise SystemExit(f"ACBridge legal file is empty: {source}")
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return destination


def extract_verified_shizuku_jars(destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    jars: list[Path] = []
    for filename, expected_sha256 in SHIZUKU_AARS.items():
        aar = SHIZUKU_DIR / filename
        if not aar.is_file():
            raise SystemExit(f"Missing pinned Shizuku dependency: {aar}")
        actual_sha256 = hashlib.sha256(aar.read_bytes()).hexdigest()
        if actual_sha256 != expected_sha256:
            raise SystemExit(
                f"Shizuku dependency hash mismatch for {filename}: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )
        jar = destination / f"{aar.stem}-classes.jar"
        try:
            with zipfile.ZipFile(aar, "r") as archive:
                members = [name for name in archive.namelist() if name == "classes.jar"]
                if members != ["classes.jar"]:
                    raise SystemExit(f"Unexpected classes.jar layout in {aar}")
                jar.write_bytes(archive.read("classes.jar"))
        except zipfile.BadZipFile as exc:
            raise SystemExit(f"Invalid Shizuku AAR: {aar}") from exc
        jars.append(jar)
    return jars


def prepare_desugared_library(destination: Path) -> tuple[Path, Path, Path]:
    destination.mkdir(parents=True, exist_ok=True)
    verified: dict[str, Path] = {}
    for filename, expected_sha256 in DESUGAR_ARTIFACTS.items():
        artifact = DESUGAR_DIR / filename
        if not artifact.is_file():
            raise SystemExit(f"Missing pinned core-library desugaring dependency: {artifact}")
        actual_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if actual_sha256 != expected_sha256:
            raise SystemExit(
                f"Core-library desugaring dependency hash mismatch for {filename}: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )
        verified[filename] = artifact

    library = verified["desugar_jdk_libs-2.1.5.jar"]
    configuration_jar = verified["desugar_jdk_libs_configuration-2.1.5.jar"]
    configuration = destination / "desugar.json"
    configuration_member = "META-INF/desugar/d8/desugar.json"
    try:
        with zipfile.ZipFile(configuration_jar, "r") as archive:
            members = [name for name in archive.namelist() if name == configuration_member]
            if members != [configuration_member]:
                raise SystemExit(
                    f"Unexpected desugar configuration layout in {configuration_jar}"
                )
            configuration.write_bytes(archive.read(configuration_member))
    except zipfile.BadZipFile as exc:
        raise SystemExit(f"Invalid desugar configuration JAR: {configuration_jar}") from exc
    return library, configuration_jar, configuration


def dex_sort_key(path: Path) -> tuple[int, str]:
    match = re.fullmatch(r"classes(\d*)\.dex", path.name)
    if not match:
        return (sys.maxsize, path.name)
    return (int(match.group(1) or "1"), path.name)


def verify_apk(apk_path: Path, aapt: Path, zipalign: Path, java: str, apksigner_jar: Path) -> None:
    if not apk_path.is_file() or apk_path.stat().st_size <= 0:
        raise SystemExit(f"ACBridge APK is missing or empty: {apk_path}")
    verify_apk_legal_files(apk_path)
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


def verify_apk_legal_files(apk_path: Path) -> None:
    try:
        with zipfile.ZipFile(apk_path, "r") as archive:
            archive_members = archive.namelist()
            for member, source in APK_LEGAL_FILES.items():
                if not source.is_file():
                    raise SystemExit(f"Missing ACBridge legal file: {source}")
                if archive_members.count(member) != 1:
                    raise SystemExit(
                        f"ACBridge APK must contain exactly one {member}: {apk_path}"
                    )
                expected = source.read_bytes()
                if not expected.strip():
                    raise SystemExit(f"ACBridge legal file is empty: {source}")
                if archive.read(member) != expected:
                    raise SystemExit(
                        f"ACBridge APK legal file does not match {source} byte-for-byte: {member}"
                    )
    except zipfile.BadZipFile as exc:
        raise SystemExit(f"Invalid ACBridge APK: {apk_path}") from exc


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
