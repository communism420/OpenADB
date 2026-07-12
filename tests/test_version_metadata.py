from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from openadb import __version__
from openadb.core.acbridge import ACBridgeClient


ROOT = Path(__file__).resolve().parents[1]
ANDROID_NS = "{http://schemas.android.com/apk/res/android}"


class VersionMetadataTests(unittest.TestCase):
    def test_openadb_public_version_is_consistent(self) -> None:
        self.assertEqual(__version__, "2.0.0")
        self.assertIn("Version: `2.0.0`", (ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIn("OpenADB 2.0.0", (ROOT / "GUI_AUDIT.md").read_text(encoding="utf-8"))
        self.assertIn("OpenADB 2.0.0", (ROOT / "GUI_REDESIGN_REPORT.md").read_text(encoding="utf-8"))

    def test_acbridge_version_name_and_code_match_client(self) -> None:
        manifest = ET.parse(ROOT / "openadb" / "resources" / "acbridge" / "AndroidManifest.xml").getroot()
        self.assertEqual(manifest.attrib[f"{ANDROID_NS}versionName"], "2.0.0")
        self.assertEqual(manifest.attrib[f"{ANDROID_NS}versionCode"], "20004")
        self.assertEqual(ACBridgeClient.VERSION_CODE, 20004)
        self.assertEqual(ACBridgeClient.APK_FILENAME, "ACBridge-2.0.0.apk")


if __name__ == "__main__":
    unittest.main()
