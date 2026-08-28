from __future__ import annotations

# ruff: noqa: E402 -- the repository root must be added before importing release metadata.
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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
PUBLIC_CERTIFICATE = BRIDGE_DIR / "acbridge-release-cert.der"
COMPATIBLE_APK = BRIDGE_DIR / "ACBridge.apk"
SIGNING_KEYSTORE_ENV = "ACBRIDGE_RELEASE_KEYSTORE"
SIGNING_STORE_PASSWORD_ENV = "ACBRIDGE_RELEASE_STORE_PASSWORD"
SIGNING_KEY_PASSWORD_ENV = "ACBRIDGE_RELEASE_KEY_PASSWORD"
SIGNING_ALIAS_ENV = "ACBRIDGE_RELEASE_KEY_ALIAS"
ANDROID_NS = "{http://schemas.android.com/apk/res/android}"
FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
WINDOWS_REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)
WINDOWS_TRANSIENT_REPLACE_ERRORS = frozenset({5, 32})
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build ACBridge without a key or sign it with the permanent release identity."
    )
    parser.add_argument(
        "--signing-mode",
        choices=("unsigned", "release"),
        default="unsigned",
        help=(
            "Unsigned builds are the safe default and never replace bundled APKs. "
            "Release mode requires the external release keystore environment."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output path. Release mode without this option publishes both bundled APK aliases.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    verify_source_metadata()
    verify_public_certificate()
    sdk = find_sdk()
    build_tools = selected_sdk_dir(
        sdk / "build-tools",
        version_environment="ANDROID_BUILD_TOOLS_VERSION",
    )
    platform = selected_sdk_dir(
        sdk / "platforms",
        version_environment="ANDROID_PLATFORM_VERSION",
        name_prefix="android-",
    )
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
    if missing or not java or not javac or (args.signing_mode == "release" and not keytool):
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
    normalized = BUILD_DIR / "acbridge-normalized.apk"
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
    dex_files = sorted(dex_dir.glob("classes*.dex"), key=dex_sort_key)
    dex_files.extend(sorted(desugar_dex_dir.glob("classes*.dex"), key=dex_sort_key))
    append_dex_files(unsigned_with_dex, dex_files)
    normalize_apk_archive(unsigned_with_dex, normalized)

    run([zipalign, "-f", "4", normalized, aligned])
    if args.signing_mode == "unsigned":
        output = (args.output or (BUILD_DIR / f"{Path(ACBRIDGE_APK_FILENAME).stem}-unsigned.apk")).resolve()
        if output in {APK_OUT.resolve(), COMPATIBLE_APK.resolve()} or _is_within(output, BRIDGE_DIR):
            raise SystemExit(
                "Unsigned ACBridge output must stay outside openadb/resources/acbridge and cannot "
                "replace an official bundled APK."
            )
        verify_unsigned_apk(aligned, aapt, zipalign)
        atomic_publish(aligned, output)
        verify_unsigned_apk(output, aapt, zipalign)
        print(
            f"Built and verified unsigned {output} "
            f"(package={ACBRIDGE_PACKAGE}, versionName={VERSION}, "
            f"versionCode={ACBRIDGE_VERSION_CODE}, bytes={output.stat().st_size})"
        )
        return 0

    signing = release_signing_config(keytool)
    run(
        [
            java,
            "-Duser.timezone=UTC",
            "-jar",
            apksigner_jar,
            "sign",
            "--v1-signing-enabled",
            "true",
            "--v2-signing-enabled",
            "true",
            "--v3-signing-enabled",
            "true",
            "--v4-signing-enabled",
            "false",
            "--min-sdk-version",
            "23",
            "--ks",
            signing["keystore"],
            "--ks-key-alias",
            signing["alias"],
            "--ks-pass",
            f"env:{SIGNING_STORE_PASSWORD_ENV}",
            "--key-pass",
            f"env:{SIGNING_KEY_PASSWORD_ENV}",
            "--out",
            signed,
            aligned,
        ]
    )
    verify_apk(signed, aapt, zipalign, java, apksigner_jar)
    if args.output is not None:
        output = args.output.resolve()
        atomic_publish(signed, output)
        verify_apk(output, aapt, zipalign, java, apksigner_jar)
        print(
            f"Built and verified release-signed {output} "
            f"(package={ACBRIDGE_PACKAGE}, versionName={VERSION}, "
            f"versionCode={ACBRIDGE_VERSION_CODE}, bytes={output.stat().st_size})"
        )
        return 0

    atomic_publish_aliases(signed, (APK_OUT, COMPATIBLE_APK))
    verify_apk(APK_OUT, aapt, zipalign, java, apksigner_jar)
    verify_apk(COMPATIBLE_APK, aapt, zipalign, java, apksigner_jar)
    if APK_OUT.read_bytes() != COMPATIBLE_APK.read_bytes():
        raise SystemExit("ACBridge.apk does not contain the same build as the versioned APK")
    print(
        f"Built and verified {APK_OUT} "
        f"(package={ACBRIDGE_PACKAGE}, versionName={VERSION}, versionCode={ACBRIDGE_VERSION_CODE}, "
        f"bytes={APK_OUT.stat().st_size})"
    )
    return 0


def verify_public_certificate() -> None:
    if not PUBLIC_CERTIFICATE.is_file() or PUBLIC_CERTIFICATE.stat().st_size <= 0:
        raise SystemExit(f"Missing ACBridge public release certificate: {PUBLIC_CERTIFICATE}")
    actual = hashlib.sha256(PUBLIC_CERTIFICATE.read_bytes()).hexdigest()
    if actual != ACBRIDGE_SIGNER_SHA256:
        raise SystemExit(
            "ACBridge public certificate digest mismatch: "
            f"expected {ACBRIDGE_SIGNER_SHA256}, got {actual}"
        )


def release_signing_config(keytool: str | None) -> dict[str, str | Path]:
    if not keytool:
        raise SystemExit("keytool is required for an ACBridge release build.")
    required = {
        name: str(os.environ.get(name, "")).strip()
        for name in (
            SIGNING_KEYSTORE_ENV,
            SIGNING_STORE_PASSWORD_ENV,
            SIGNING_KEY_PASSWORD_ENV,
            SIGNING_ALIAS_ENV,
        )
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit(
            "ACBridge release signing requires the external signing environment: "
            + ", ".join(missing)
        )
    keystore = Path(required[SIGNING_KEYSTORE_ENV]).expanduser().resolve()
    if not keystore.is_file():
        raise SystemExit(f"ACBridge release keystore does not exist: {keystore}")
    if _is_within(keystore, ROOT):
        raise SystemExit("ACBridge release keystore must be stored outside the repository.")
    alias = required[SIGNING_ALIAS_ENV]
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", alias):
        raise SystemExit("ACBridge release key alias contains unsupported characters.")
    with tempfile.TemporaryDirectory(prefix="openadb-acbridge-cert-") as temp_dir:
        exported = Path(temp_dir) / "signer.der"
        run(
            [
                keytool,
                "-exportcert",
                "-storetype",
                "PKCS12",
                "-keystore",
                keystore,
                "-storepass:env",
                SIGNING_STORE_PASSWORD_ENV,
                "-alias",
                alias,
                "-file",
                exported,
            ]
        )
        if not exported.is_file() or exported.read_bytes() != PUBLIC_CERTIFICATE.read_bytes():
            actual = (
                hashlib.sha256(exported.read_bytes()).hexdigest()
                if exported.is_file()
                else "unreadable"
            )
            raise SystemExit(
                "ACBridge release keystore does not contain the pinned release certificate: "
                f"expected {ACBRIDGE_SIGNER_SHA256}, got {actual}"
            )
    return {"keystore": keystore, "alias": alias}


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


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


def append_dex_files(apk_path: Path, dex_files: list[Path]) -> None:
    if not dex_files:
        raise SystemExit("D8 did not produce any dex files")
    with zipfile.ZipFile(apk_path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, dex_file in enumerate(dex_files, start=1):
            dex_name = "classes.dex" if index == 1 else f"classes{index}.dex"
            dex_entry = zipfile.ZipInfo(dex_name, date_time=FIXED_ZIP_TIMESTAMP)
            dex_entry.compress_type = zipfile.ZIP_DEFLATED
            dex_entry.create_system = 3
            dex_entry.external_attr = 0o100644 << 16
            archive.writestr(dex_entry, dex_file.read_bytes())


def normalize_apk_archive(source: Path, destination: Path) -> None:
    """Write one platform-independent, byte-stable APK ZIP before alignment."""

    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        with zipfile.ZipFile(source, "r") as input_archive:
            members = input_archive.infolist()
            names = [member.filename for member in members]
            if len(names) != len(set(names)):
                raise SystemExit(f"APK contains duplicate ZIP members: {source}")
            payloads = {
                member.filename: input_archive.read(member.filename)
                for member in members
            }
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            for name in sorted(payloads):
                entry = zipfile.ZipInfo(name, date_time=FIXED_ZIP_TIMESTAMP)
                entry.compress_type = zipfile.ZIP_STORED
                entry.create_system = 3
                entry.create_version = 20
                entry.extract_version = 20
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, payloads[name])
        os.replace(temporary, destination)
    except zipfile.BadZipFile as exc:
        raise SystemExit(f"Invalid APK ZIP archive: {source}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def verify_unsigned_apk(apk_path: Path, aapt: Path, zipalign: Path) -> None:
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


def verify_apk(apk_path: Path, aapt: Path, zipalign: Path, java: str, apksigner_jar: Path) -> None:
    verify_unsigned_apk(apk_path, aapt, zipalign)
    with tempfile.TemporaryDirectory(prefix="openadb-acbridge-signature-") as temp_dir:
        verification_apk = Path(temp_dir) / apk_path.name
        shutil.copy2(apk_path, verification_apk)
        signature = run_capture(
            [
                java,
                "-Duser.timezone=UTC",
                "-jar",
                apksigner_jar,
                "verify",
                "--verbose",
                "--print-certs",
                verification_apk,
            ]
        )
    for scheme in ("v1", "v2", "v3"):
        if f"Verified using {scheme} scheme" not in signature or not re.search(
            rf"Verified using {scheme} scheme[^:]*:\s*true", signature
        ):
            raise SystemExit(f"ACBridge APK is not verified with the required {scheme} signature scheme")
    signer_count = re.search(r"Number of signers:\s*(\d+)", signature)
    if not signer_count or signer_count.group(1) != "1":
        actual_count = signer_count.group(1) if signer_count else "unreadable"
        raise SystemExit(f"ACBridge APK must have exactly one signer; got {actual_count}")
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
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        shutil.copy2(source, temporary)
        if source.read_bytes() != temporary.read_bytes():
            raise SystemExit(f"Failed to verify staged APK copy for {destination}")
        _replace_with_windows_retry(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_publish_aliases(source: Path, destinations: tuple[Path, ...]) -> None:
    """Publish matching official aliases and restore all of them on failure."""

    if not destinations or len(set(destinations)) != len(destinations):
        raise SystemExit("ACBridge release aliases must be unique and non-empty.")
    source_payload = source.read_bytes()
    records: list[tuple[Path, Path, Path, bool]] = []
    published: list[tuple[Path, Path, bool]] = []
    try:
        for destination in destinations:
            destination.parent.mkdir(parents=True, exist_ok=True)
            stage = destination.with_name(f".{destination.name}.openadb-stage")
            backup = destination.with_name(f".{destination.name}.openadb-backup")
            stage.unlink(missing_ok=True)
            backup.unlink(missing_ok=True)
            shutil.copy2(source, stage)
            if stage.read_bytes() != source_payload:
                raise SystemExit(f"Failed to verify staged APK alias for {destination}")
            existed = destination.is_file()
            if existed:
                shutil.copy2(destination, backup)
            records.append((destination, stage, backup, existed))

        for destination, stage, backup, existed in records:
            _replace_with_windows_retry(stage, destination)
            published.append((destination, backup, existed))
        if any(destination.read_bytes() != source_payload for destination in destinations):
            raise SystemExit("Published ACBridge aliases are not byte-identical.")
    except BaseException as exc:
        rollback_errors: list[str] = []
        for destination, backup, existed in reversed(published):
            try:
                if existed and backup.is_file():
                    _replace_with_windows_retry(backup, destination)
                elif not existed:
                    destination.unlink(missing_ok=True)
            except OSError as rollback_error:
                rollback_errors.append(f"{destination}: {rollback_error}")
        if rollback_errors:
            raise SystemExit(
                "ACBridge alias publication failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from exc
        raise
    finally:
        for _destination, stage, backup, _existed in records:
            stage.unlink(missing_ok=True)
            backup.unlink(missing_ok=True)


def _replace_with_windows_retry(source: Path, destination: Path) -> None:
    """Retry only transient Windows locks around an otherwise atomic replace."""

    for attempt in range(len(WINDOWS_REPLACE_RETRY_DELAYS) + 1):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if (
                getattr(exc, "winerror", None) not in WINDOWS_TRANSIENT_REPLACE_ERRORS
                or attempt == len(WINDOWS_REPLACE_RETRY_DELAYS)
            ):
                raise
            time.sleep(WINDOWS_REPLACE_RETRY_DELAYS[attempt])


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
    return max(
        dirs,
        key=lambda path: (
            tuple(int(part) for part in re.findall(r"\d+", path.name)),
            path.name,
        ),
    )


def selected_sdk_dir(
    parent: Path,
    *,
    version_environment: str,
    name_prefix: str = "",
) -> Path:
    configured_version = str(os.environ.get(version_environment, "")).strip()
    if not configured_version:
        return latest_dir(parent)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", configured_version):
        raise SystemExit(f"{version_environment} contains an invalid SDK version.")
    selected = parent / f"{name_prefix}{configured_version}"
    if not selected.is_dir():
        raise SystemExit(
            f"The configured SDK component does not exist for {version_environment}: {selected}"
        )
    return selected


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
