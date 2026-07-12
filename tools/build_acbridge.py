from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_DIR = ROOT / "openadb" / "resources" / "acbridge"
BUILD_DIR = ROOT / "build" / "acbridge"
APK_OUT = BRIDGE_DIR / "ACBridge-2.0.0.apk"
KEYSTORE = BRIDGE_DIR / "openadb-debug.keystore"


def main() -> int:
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
            APK_OUT,
            aligned,
        ]
    )
    print(f"Built {APK_OUT}")
    return 0


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


if __name__ == "__main__":
    sys.exit(main())
