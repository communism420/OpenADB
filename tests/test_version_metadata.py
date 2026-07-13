from __future__ import annotations

import hashlib
import re
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from apkutils2 import APK
from PIL import Image

from openadb import __version__
from openadb.core.acbridge import ACBridgeClient
from openadb.version import (
    ACBRIDGE_APK_FILENAME,
    ACBRIDGE_BUILD,
    ACBRIDGE_PACKAGE,
    ACBRIDGE_SIGNER_SHA256,
    ACBRIDGE_VERSION_CODE,
    RELEASE_EXE_FILENAME,
    VERSION,
    VERSION_PARTS,
    android_version_code,
)


ROOT = Path(__file__).resolve().parents[1]
ANDROID_NS = "{http://schemas.android.com/apk/res/android}"
BRIDGE_ROOT = ROOT / "openadb" / "resources" / "acbridge"
EXPECTED_SCREENSHOTS = {
    "applications-contextual-actions-dark-v3.0.0.png",
    "applications-dark-v3.0.0.png",
    "commands-dark-v3.0.0.png",
    "dashboard-dark-v3.0.0.png",
    "dashboard-light-v3.0.0.png",
    "file-manager-dark-v3.0.0.png",
    "settings-dark-v3.0.0.png",
}


class VersionMetadataTests(unittest.TestCase):
    def test_openadb_public_version_is_consistent(self) -> None:
        self.assertEqual(VERSION, "3.0.0")
        self.assertEqual(__version__, VERSION)
        self.assertEqual(RELEASE_EXE_FILENAME, "OpenADB-3.0.0.exe")
        self.assertIn("Version: `3.0.0`", (ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIn("## [3.0.0]", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertIn("## [3.0.0]", (ROOT / "CHANGELOG_EN.md").read_text(encoding="utf-8"))
        self.assertIn("OpenADB 3.0.0", (ROOT / "GUI_AUDIT.md").read_text(encoding="utf-8"))
        self.assertIn("OpenADB 3.0.0", (ROOT / "GUI_REDESIGN_REPORT.md").read_text(encoding="utf-8"))

    def test_android_version_code_policy_is_documented_and_monotonic(self) -> None:
        self.assertEqual(VERSION_PARTS, (3, 0, 0))
        self.assertEqual(ACBRIDGE_BUILD, 2)
        self.assertEqual(android_version_code((2, 0, 0), 4), 20004)
        self.assertEqual(android_version_code((2, 0, 1), 1), 20101)
        self.assertEqual(android_version_code(VERSION_PARTS, ACBRIDGE_BUILD), 30002)
        self.assertEqual(ACBRIDGE_VERSION_CODE, 30002)
        self.assertGreater(ACBRIDGE_VERSION_CODE, 20101)
        self.assertRegex(ACBRIDGE_SIGNER_SHA256, r"^[0-9a-f]{64}$")

    def test_acbridge_source_metadata_matches_client(self) -> None:
        manifest = ET.parse(BRIDGE_ROOT / "AndroidManifest.xml").getroot()
        self.assertEqual(manifest.attrib["package"], ACBRIDGE_PACKAGE)
        self.assertEqual(manifest.attrib[f"{ANDROID_NS}versionName"], VERSION)
        self.assertEqual(manifest.attrib[f"{ANDROID_NS}versionCode"], str(ACBRIDGE_VERSION_CODE))
        self.assertEqual(ACBridgeClient.PACKAGE, ACBRIDGE_PACKAGE)
        self.assertEqual(ACBridgeClient.VERSION_NAME, VERSION)
        self.assertEqual(ACBridgeClient.VERSION_CODE, ACBRIDGE_VERSION_CODE)
        self.assertEqual(ACBridgeClient.APK_FILENAME, ACBRIDGE_APK_FILENAME)

        build_info = (
            BRIDGE_ROOT / "src" / "com" / "communism420" / "acbridge" / "BuildInfo.java"
        ).read_text(encoding="utf-8")
        self.assertRegex(build_info, rf'VERSION_NAME\s*=\s*"{re.escape(VERSION)}"')
        self.assertRegex(build_info, rf"VERSION_CODE\s*=\s*{ACBRIDGE_VERSION_CODE}L")

    def test_bundled_apks_are_real_current_signed_builds(self) -> None:
        versioned_apk = BRIDGE_ROOT / ACBRIDGE_APK_FILENAME
        compatible_apk = BRIDGE_ROOT / "ACBridge.apk"
        self.assertGreater(versioned_apk.stat().st_size, 0)
        self.assertGreater(compatible_apk.stat().st_size, 0)
        self.assertEqual(
            hashlib.sha256(versioned_apk.read_bytes()).digest(),
            hashlib.sha256(compatible_apk.read_bytes()).digest(),
        )

        for apk_path in (versioned_apk, compatible_apk):
            metadata = APK(str(apk_path)).get_manifest()
            self.assertEqual(metadata["@package"], ACBRIDGE_PACKAGE)
            self.assertEqual(metadata["@android:versionName"], VERSION)
            self.assertEqual(metadata["@android:versionCode"], str(ACBRIDGE_VERSION_CODE))
            with zipfile.ZipFile(apk_path) as archive:
                signed_entries = {
                    name.upper()
                    for name in archive.namelist()
                    if name.upper().startswith("META-INF/")
                }
            self.assertTrue(any(name.endswith(".SF") for name in signed_entries))
            self.assertTrue(any(name.endswith(".RSA") for name in signed_entries))

    def test_pyinstaller_and_windows_metadata_match_release_artifact(self) -> None:
        spec = (ROOT / "OpenADB.spec").read_text(encoding="utf-8")
        self.assertIn("RELEASE_EXE_FILENAME", spec)
        self.assertIn("ACBRIDGE_APK_FILENAME", spec)
        self.assertNotIn("ACBridge-2.0.1.apk", spec)

        windows_metadata = (ROOT / "tools" / "openadb_version_info.txt").read_text(encoding="utf-8")
        self.assertIn("filevers=(3, 0, 0, 0)", windows_metadata)
        self.assertIn("prodvers=(3, 0, 0, 0)", windows_metadata)
        self.assertIn("FileVersion', '3.0.0'", windows_metadata)
        self.assertIn(f"OriginalFilename', '{RELEASE_EXE_FILENAME}'", windows_metadata)
        self.assertIn("ProductVersion', '3.0.0'", windows_metadata)

    def test_release_screenshot_names_match_version(self) -> None:
        screenshots = ROOT / "docs" / "screenshots"
        self.assertTrue(EXPECTED_SCREENSHOTS.issubset({path.name for path in screenshots.glob("*.png")}))
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        generator = (ROOT / "tools" / "capture_readme_screenshots.py").read_text(encoding="utf-8")
        for filename in EXPECTED_SCREENSHOTS:
            screenshot = screenshots / filename
            self.assertIn(f"docs/screenshots/{filename}", readme)
            self.assertIn(filename, generator)
            self.assertLess(screenshot.stat().st_size, 1_000_000)
            with Image.open(screenshot) as image:
                image.load()
                self.assertEqual(image.size, (1280, 820))
                self.assertEqual(image.mode, "RGB")
                self.assertFalse(image.getexif())
                self.assertLessEqual(set(image.info), {"dpi"})


if __name__ == "__main__":
    unittest.main()
