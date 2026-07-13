from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from openadb.core.app_asset_loader import AppAssetLoader, AppLabelFormatter
from openadb.core.app_metadata_loader import AppMetadataLoader, metadata_worker_count
from openadb.core.device_context import StaleDeviceContext
from openadb.models.app_info import AppInfo


class MetadataADB:
    def __init__(self, details: dict[str, dict[str, str]] | None = None) -> None:
        self.details = details or {}
        self.calls = 0

    def get_package_details_many(
        self,
        package_names: list[str],
        *,
        max_workers: int,
        progress_callback=None,
        cancel_event=None,
    ) -> dict[str, dict[str, str]]:
        self.calls += 1
        for done, package_name in enumerate(package_names, start=1):
            if cancel_event is not None and cancel_event.is_set():
                break
            if progress_callback is not None:
                progress_callback(
                    done,
                    len(package_names),
                    package_name,
                    self.details.get(package_name, {}),
                )
        return dict(self.details)


class FakeMetadataExtractor:
    def __init__(self, labels: dict[str, str] | None = None) -> None:
        self.labels = labels or {}
        self.saved: dict[str, str] = {}

    def cached_label(self, app: AppInfo) -> str:
        return self.labels.get(app.package_name, "")

    def set_cached_label(self, app: AppInfo, label: str) -> None:
        self.saved[app.package_name] = label

    def extract_label(self, _path: Path) -> str:
        return ""


class FakeIconExtractor:
    def __init__(self, cache_path: Path, cached: bool) -> None:
        self._cache_path = cache_path
        self._cached = cached

    def cache_path(self, *args, **kwargs) -> Path:
        return self._cache_path

    def cached_icon_path(self, *args, **kwargs) -> Path | None:
        return self._cache_path if self._cached else None

    def extract_from_apk(self, *args, **kwargs) -> Path | None:
        return None


class AssetADB:
    def __init__(self) -> None:
        self.root_calls = 0
        self.detail_calls = 0
        self.size_calls = 0
        self.path_calls = 0
        self.pull_calls = 0

    def root_available(self, cancel_event=None) -> bool:
        self.root_calls += 1
        return False

    def get_package_details_many(self, *args, **kwargs):
        self.detail_calls += 1
        return {}

    def get_package_sizes_bulk(self, *args, **kwargs):
        self.size_calls += 1
        return {}

    def get_package_paths_bulk(self, *args, **kwargs):
        self.path_calls += 1
        return {}

    def pull_files_via_temp(self, *args, **kwargs):
        self.pull_calls += 1
        return []


class AppMetadataLoaderTests(unittest.TestCase):
    def test_loader_returns_isolated_updates_and_reports_progress(self) -> None:
        apps = [
            AppInfo(
                package_name="com.example.one",
                app_label="One",
                version_code="1",
                apk_paths=["/data/app/one/base.apk"],
            ),
            AppInfo(package_name="com.example.two", app_label="Two"),
        ]
        adb = MetadataADB(
            {
                "com.example.one": {
                    "versionName": "2.0",
                    "versionCode": "2",
                    "sizeBytes": "2048",
                },
                "com.example.two": {"versionName": "3.0", "versionCode": "3"},
            }
        )
        progress: list[str] = []
        items: list[AppInfo] = []

        result = AppMetadataLoader(adb, configured_parallelism=4).load(
            apps,
            cancel_event=threading.Event(),
            progress_callback=progress.append,
            item_callback=items.append,
        )

        self.assertEqual(adb.calls, 1)
        self.assertEqual([app.version_name for app in result], ["2.0", "3.0"])
        self.assertEqual(result[0].size, "2.0 KB")
        self.assertTrue(all(app.metadata_checked for app in result))
        self.assertEqual(len(items), 2)
        self.assertIn("2 workers", progress[-1])
        self.assertEqual(apps[0].version_code, "1")
        self.assertIsNot(result[0].apk_paths, apps[0].apk_paths)

    def test_cancelled_load_never_starts_device_work(self) -> None:
        event = threading.Event()
        event.set()
        adb = MetadataADB()

        result = AppMetadataLoader(adb).load(
            [AppInfo(package_name="com.example.cancelled")],
            cancel_event=event,
        )

        self.assertEqual(result, [])
        self.assertEqual(adb.calls, 0)

    def test_empty_metadata_preserves_cached_values_and_remains_retryable(self) -> None:
        original = AppInfo(
            package_name="com.example.cached",
            app_label="Cached label",
            version_name="1.2.3",
            version_code="123",
            apk_paths=["/data/app/cached/base.apk"],
            size="8.0 MB",
        )

        result = AppMetadataLoader(MetadataADB()).load([original])

        self.assertEqual(result[0].app_label, "Cached label")
        self.assertEqual(result[0].version_name, "1.2.3")
        self.assertEqual(result[0].version_code, "123")
        self.assertEqual(result[0].apk_paths, ["/data/app/cached/base.apk"])
        self.assertEqual(result[0].size, "8.0 MB")
        self.assertFalse(result[0].metadata_checked)
        self.assertIsNot(result[0], original)
        self.assertIsNot(result[0].apk_paths, original.apk_paths)

    def test_partial_metadata_merges_fields_without_erasing_cached_version(self) -> None:
        original = AppInfo(
            package_name="com.example.partial",
            app_label="Cached label",
            version_name="4.5.6",
            version_code="456",
            size="Unknown",
        )
        adb = MetadataADB(
            {
                original.package_name: {
                    "appLabel": "Fresh label",
                    "versionCode": "457",
                    "sizeBytes": "1024",
                }
            }
        )

        result = AppMetadataLoader(adb).load([original])

        self.assertEqual(result[0].app_label, "Fresh label")
        self.assertEqual(result[0].version_name, "4.5.6")
        self.assertEqual(result[0].version_code, "457")
        self.assertEqual(result[0].size, "1.0 KB")
        self.assertFalse(result[0].metadata_checked)

    def test_worker_count_is_bounded_and_tolerates_old_settings(self) -> None:
        self.assertEqual(metadata_worker_count(0, 20), 1)
        self.assertEqual(metadata_worker_count(3, "invalid"), 3)
        self.assertEqual(metadata_worker_count(100, 99), 8)


class AppAssetLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.settings = SimpleNamespace(temp_folder=self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _loader(
        self,
        adb: AssetADB,
        metadata: FakeMetadataExtractor,
        icons: FakeIconExtractor,
    ) -> AppAssetLoader:
        return AppAssetLoader(
            adb,
            self.settings,
            metadata,  # type: ignore[arg-type]
            icons,  # type: ignore[arg-type]
            device_serial="captured-device",
            temp_path=self.root,
        )

    def test_complete_local_cache_skips_acbridge_and_adb(self) -> None:
        icon = self.root / "cached.png"
        icon.write_bytes(b"cached-icon")
        adb = AssetADB()
        metadata = FakeMetadataExtractor({"com.example.cached": "Cached App"})
        loader = self._loader(adb, metadata, FakeIconExtractor(icon, cached=True))
        original = AppInfo(
            package_name="com.example.cached",
            app_label="",
            size="1.0 MB",
            metadata_checked=True,
        )

        with patch("openadb.core.app_asset_loader.ACBridgeClient") as bridge:
            result = loader.load([original], [original], cancel_event=threading.Event())

        bridge.assert_not_called()
        self.assertEqual(adb.root_calls, 0)
        self.assertEqual(result[0].app_label, "Cached App")
        self.assertEqual(result[0].icon_path, str(icon))
        self.assertTrue(result[0].assets_checked)
        self.assertEqual(original.app_label, "")

    def test_acbridge_result_is_returned_without_widget_dependencies(self) -> None:
        icon = self.root / "bridge.png"
        icon.write_bytes(b"bridge-icon")
        adb = AssetADB()
        metadata = FakeMetadataExtractor()
        loader = self._loader(adb, metadata, FakeIconExtractor(self.root / "missing.png", cached=False))
        original = AppInfo(package_name="com.example.photos", size="Unknown")
        bridge_result = SimpleNamespace(
            labels={original.package_name: "Photos"},
            icons={original.package_name: icon},
            metadata={
                original.package_name: {
                    "versionName": "5.1",
                    "versionCode": "51",
                    "sizeBytes": "4096",
                }
            },
            message="ACBridge complete",
        )
        bridge_client = MagicMock()
        bridge_client.load_app_data.return_value = bridge_result
        progress: list[str] = []
        items: list[AppInfo] = []

        with patch(
            "openadb.core.app_asset_loader.ACBridgeClient",
            return_value=bridge_client,
        ):
            result = loader.load(
                [original],
                [original],
                [original],
                cancel_event=threading.Event(),
                progress_callback=progress.append,
                item_callback=items.append,
            )

        self.assertEqual(adb.root_calls, 1)
        self.assertEqual(adb.detail_calls, 0)
        self.assertEqual(adb.size_calls, 0)
        self.assertEqual(adb.path_calls, 0)
        self.assertEqual(result[0].app_label, "Photos")
        self.assertEqual(result[0].version_name, "5.1")
        self.assertEqual(result[0].size, "4.0 KB")
        self.assertEqual(result[0].icon_path, str(icon))
        self.assertTrue(result[0].metadata_checked)
        self.assertEqual(items, result)
        self.assertEqual(metadata.saved[original.package_name], "Photos")
        self.assertTrue(any("ACBridge complete" in message for message in progress))

    def test_partial_acbridge_metadata_preserves_cache_and_remains_retryable(self) -> None:
        icon = self.root / "bridge-partial.png"
        icon.write_bytes(b"bridge-icon")
        adb = AssetADB()
        loader = self._loader(
            adb,
            FakeMetadataExtractor(),
            FakeIconExtractor(self.root / "missing-partial.png", cached=False),
        )
        original = AppInfo(
            package_name="com.example.partialbridge",
            version_name="7.0",
            size="2.0 MB",
        )
        bridge_client = MagicMock()
        bridge_client.load_app_data.return_value = SimpleNamespace(
            labels={original.package_name: "Partial Bridge"},
            icons={original.package_name: icon},
            metadata={original.package_name: {"versionCode": "71"}},
            message="ACBridge partial",
        )

        with patch(
            "openadb.core.app_asset_loader.ACBridgeClient",
            return_value=bridge_client,
        ):
            result = loader.load(
                [original],
                [original],
                [original],
                cancel_event=threading.Event(),
            )

        self.assertEqual(result[0].version_name, "7.0")
        self.assertEqual(result[0].version_code, "71")
        self.assertFalse(result[0].metadata_checked)

    def test_acbridge_failures_continue_through_adb_fallback(self) -> None:
        for failure_point in ("constructor", "load"):
            with self.subTest(failure_point=failure_point):
                adb = AssetADB()
                metadata = FakeMetadataExtractor()
                loader = self._loader(
                    adb,
                    metadata,
                    FakeIconExtractor(self.root / f"missing-{failure_point}.png", cached=False),
                )
                original = AppInfo(
                    package_name=f"com.example.{failure_point}",
                    size="1.0 MB",
                    metadata_checked=True,
                )
                progress: list[str] = []
                bridge_client = MagicMock()
                bridge_client.load_app_data.side_effect = RuntimeError("bridge load failed")
                bridge_factory = (
                    MagicMock(side_effect=RuntimeError("bridge constructor failed"))
                    if failure_point == "constructor"
                    else MagicMock(return_value=bridge_client)
                )

                with patch(
                    "openadb.core.app_asset_loader.ACBridgeClient",
                    bridge_factory,
                ):
                    result = loader.load(
                        [original],
                        [original],
                        cancel_event=threading.Event(),
                        progress_callback=progress.append,
                    )

                self.assertEqual(len(result), 1)
                self.assertTrue(result[0].app_label)
                self.assertTrue(result[0].assets_checked)
                self.assertEqual(adb.path_calls, 1)
                self.assertTrue(
                    any(
                        "ACBridge failed" in message
                        and "fallback" in message.casefold()
                        for message in progress
                    )
                )

    def test_stale_acbridge_context_does_not_start_adb_fallback(self) -> None:
        adb = AssetADB()
        loader = self._loader(
            adb,
            FakeMetadataExtractor(),
            FakeIconExtractor(self.root / "missing-stale.png", cached=False),
        )
        original = AppInfo(
            package_name="com.example.stale",
            size="1.0 MB",
            metadata_checked=True,
        )

        with (
            patch(
                "openadb.core.app_asset_loader.ACBridgeClient",
                side_effect=StaleDeviceContext("target changed"),
            ),
            self.assertRaises(StaleDeviceContext),
        ):
            loader.load(
                [original],
                [original],
                cancel_event=threading.Event(),
            )

        self.assertEqual(adb.path_calls, 0)
        self.assertEqual(adb.pull_calls, 0)

    def test_cancelled_asset_load_does_not_create_bridge_or_touch_adb(self) -> None:
        event = threading.Event()
        event.set()
        adb = AssetADB()
        loader = self._loader(
            adb,
            FakeMetadataExtractor(),
            FakeIconExtractor(self.root / "missing.png", cached=False),
        )

        with patch("openadb.core.app_asset_loader.ACBridgeClient") as bridge:
            result = loader.load(
                [AppInfo(package_name="com.example.cancelled")],
                [AppInfo(package_name="com.example.cancelled")],
                cancel_event=event,
            )

        self.assertEqual(result, [])
        bridge.assert_not_called()
        self.assertEqual(adb.root_calls, 0)

    def test_label_formatter_keeps_human_label_and_replaces_package_label(self) -> None:
        formatter = AppLabelFormatter()

        self.assertEqual(formatter.normalize("Open Camera", "net.sourceforge.opencamera"), "Open Camera")
        self.assertNotEqual(
            formatter.normalize("com.example.filemanager", "com.example.filemanager"),
            "com.example.filemanager",
        )


if __name__ == "__main__":
    unittest.main()
