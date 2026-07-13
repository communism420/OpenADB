from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openadb.core.platform_tools import PlatformToolsManager


class MemorySettings:
    def __init__(self) -> None:
        self.values = {"platform_tools_path": ""}

    def get(self, key: str, default=None):
        return self.values.get(key, default)

    def set(self, key: str, value) -> None:
        self.values[key] = value


class PlatformToolsPackagingTests(unittest.TestCase):
    def test_frozen_package_candidate_is_detected_and_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            extracted_root = Path(temp) / "_MEI-openadb"
            package_root = extracted_root / "openadb"
            tools = extracted_root / "platform-tools"
            tools.mkdir(parents=True)
            (tools / "adb.exe").write_bytes(b"test")
            (tools / "fastboot.exe").write_bytes(b"test")
            settings = MemorySettings()
            manager = PlatformToolsManager(settings)

            with (
                patch("openadb.core.platform_tools.app_root", return_value=Path(temp) / "exe-release"),
                patch("openadb.core.platform_tools.package_root", return_value=package_root),
                patch.object(manager, "_version", return_value=("bundled test", True)),
                patch("openadb.core.platform_tools.shutil.which", return_value=None),
                patch("openadb.core.platform_tools.normalized_env_paths", return_value=[]),
                patch("openadb.core.platform_tools.user_home", return_value=Path(temp) / "home"),
            ):
                detected = manager.detect(select=True)

        bundled = [item for item in detected if item.source == "Bundled with OpenADB"]
        self.assertEqual(len(bundled), 1)
        self.assertEqual(manager.active.source, "Bundled with OpenADB")
        self.assertEqual(
            manager.active.folder.resolve(strict=False),
            tools.resolve(strict=False),
        )


if __name__ == "__main__":
    unittest.main()
