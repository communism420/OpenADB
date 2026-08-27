from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from shiboken6 import delete as delete_qt_object
from shiboken6 import isValid

from openadb.core.adb import ADBClient
from openadb.core.backup_manager import BackupManager
from openadb.core.command_runner import CommandRunner
from openadb.core.device import DeviceManager
from openadb.core.device_context import DeviceContextUnavailable
from openadb.core.fastboot import FastbootClient
from openadb.core.icon_extractor import IconExtractor
from openadb.core.platform_tools import PlatformToolsManager
from openadb.core.privilege import PrivilegeBackend, PrivilegeStatus
from openadb.core.settings_manager import ApkBackupCleanupResult, SettingsManager
from openadb.models.app_info import AppInfo
from openadb.models.backup_info import BackupInfo
from openadb.models.command_result import CommandResult
from openadb.models.device_info import DeviceInfo
from openadb.models.platform_tools_info import PlatformToolsInfo
from openadb.ui.main_window import MainWindow
from openadb.ui.style import apply_theme


class IsolatedSettings(SettingsManager):
    def __init__(self, config_dir: Path) -> None:
        self._test_config_dir = config_dir
        super().__init__()

    def _config_dir(self) -> Path:
        return self._test_config_dir

    def _legacy_config_dirs(self) -> list[Path]:
        return []


class AdaptiveMainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self.windows: list[MainWindow] = []
        self.single_shot_patch = patch("openadb.ui.main_window.QTimer.singleShot")
        self.native_panel_patch = patch(
            "openadb.ui.file_manager_page.NativeExplorerPanel",
            side_effect=RuntimeError("Use deterministic Qt fallback in tests"),
        )
        self.single_shot_patch.start()
        self.native_panel_patch.start()

    def tearDown(self) -> None:
        self._dispose_windows()
        self.native_panel_patch.stop()
        self.single_shot_patch.stop()
        self.temp_dir.cleanup()

    def _dispose_windows(self) -> None:
        for window in reversed(self.windows):
            window.close()
            if isValid(window):
                delete_qt_object(window)
        self.windows.clear()
        self.app.processEvents()

    def _settings(self) -> IsolatedSettings:
        settings = IsolatedSettings(self.config_dir)
        settings.set("auto_refresh_device", False)
        return settings

    def test_window_cleanup_synchronously_deletes_qt_windows(self) -> None:
        existing_top_levels = set(self.app.topLevelWidgets())
        window = self._window()

        self._dispose_windows()

        self.assertFalse(isValid(window))
        remaining_top_levels = [
            widget
            for widget in self.app.topLevelWidgets()
            if widget not in existing_top_levels and isValid(widget)
        ]
        self.assertEqual(remaining_top_levels, [])

    def _window(self, settings: IsolatedSettings | None = None) -> MainWindow:
        settings = settings or self._settings()
        platform_tools = PlatformToolsManager(settings)
        runner = CommandRunner(settings.logs_folder)
        adb = ADBClient(platform_tools, runner)
        fastboot = FastbootClient(platform_tools, runner)
        device_manager = DeviceManager(adb, fastboot, settings)
        window = MainWindow(
            settings=settings,
            platform_tools=platform_tools,
            runner=runner,
            adb=adb,
            fastboot=fastboot,
            device_manager=device_manager,
            backup_manager=BackupManager(settings),
            icon_extractor=IconExtractor(settings),
        )
        self.windows.append(window)
        return window

    @staticmethod
    def _activate_device(window: MainWindow, serial: str, transport_id: str = "1") -> DeviceInfo:
        device = DeviceInfo(
            serial=serial,
            model=f"Device {serial}",
            mode="ADB",
            state="device",
            transport_id=transport_id,
            form_factor="Phone",
        )
        window.device_manager.devices = [device]
        window.device_manager._set_active(device, reason="test device selected")
        window.settings.activate_device_profile(serial, device.model, device.form_factor)
        window.device_manager.notify_profile_changed(serial, "Phone")
        return device

    def test_navigation_icons_accessibility_and_collapsed_state_round_trip(self) -> None:
        settings = self._settings()
        window = self._window(settings)
        self.assertFalse(window.navigation_collapsed)
        expected_icons = {
            "Dashboard": "dashboard",
            "Apps": "apps",
            "Backups": "backup",
            "File Manager": "folder",
            "Commands": "terminal",
            "Logs": "description",
            "Settings": "settings",
        }
        for row, name in enumerate(window.pages):
            item = window.nav.item(row)
            self.assertFalse(item.icon().isNull())
            self.assertEqual(item.icon().name(), expected_icons[name])
            self.assertEqual(item.text(), name)
            self.assertEqual(item.toolTip(), name)
            self.assertEqual(item.data(Qt.AccessibleTextRole), name)

        window.toggle_navigation()
        self.assertTrue(window.navigation_collapsed)
        self.assertEqual(window.nav_toggle.icon().name(), "chevron_right")
        self.assertTrue(all(not window.nav.item(row).text() for row in range(window.nav.count())))
        self.assertEqual(window.nav_toggle.accessibleName(), "Expand navigation")
        self.assertTrue(settings.get_global("navigation_collapsed"))

        window._save_window_state()
        restored = self._window(IsolatedSettings(self.config_dir))
        self.assertTrue(restored.navigation_collapsed)
        restored.toggle_navigation()
        self.assertFalse(restored.navigation_collapsed)
        self.assertEqual(restored.nav_toggle.icon().name(), "chevron_left")
        self.assertEqual(restored.nav_toggle.accessibleName(), "Collapse navigation")

    def test_file_manager_transfer_signals_drive_and_close_taskbar_progress(self) -> None:
        taskbar = MagicMock()
        with patch(
            "openadb.ui.main_window.WindowsTaskbarProgress",
            return_value=taskbar,
        ) as taskbar_type:
            window = self._window()

        taskbar_type.assert_called_once()
        hwnd_provider = taskbar_type.call_args.args[0]
        self.assertTrue(callable(hwnd_provider))

        update = {"type": "progress", "done_bytes": 3, "total_bytes": 10}
        window.file_manager_page.transfer_started.emit("transfer-1")
        window.file_manager_page.transfer_progress_changed.emit(
            "transfer-1",
            update,
        )
        window.file_manager_page.transfer_finished.emit("transfer-1")

        taskbar.begin.assert_called_once_with("transfer-1")
        taskbar.apply_update.assert_called_once_with("transfer-1", update)
        taskbar.finish.assert_called_once_with("transfer-1")

        window.show()
        self.app.processEvents()
        window.close()
        self.app.processEvents()
        taskbar.close.assert_called_once_with()

    def test_privilege_selector_is_global_persistent_and_synchronized(self) -> None:
        settings = self._settings()
        window = self._window(settings)
        selector = window.privilege_mode_selector

        self.assertEqual(selector.backend().value, "standard")
        self.assertEqual(selector.accessibleName(), "Privilege mode")
        self.assertIs(window.statusBar(), selector.parentWidget().parentWidget())
        self._activate_device(window, "device-1")
        window._set_privilege_profile_available(True)

        selector.setCurrentIndex(selector.findData("root"))

        self.assertEqual(settings.get("privilege_backend"), "root")
        self.assertTrue(settings.get("root_mode_enabled"))
        self.assertEqual(window.settings_page.privilege_mode.backend().value, "root")
        self.assertIn("Root", window.privilege_runtime_status.full_text())

        operation_status = "A separate operation status " + "with-details-" * 80
        window.statusBar().showMessage(operation_status, 5000)
        window.show()
        self.app.processEvents()
        self.assertTrue(window.privilege_status_panel.isVisible())
        self.assertTrue(selector.isVisible())
        self.assertEqual(window.statusBar().toolTip(), operation_status)
        self.assertEqual(
            window.statusBar().accessibleDescription(),
            operation_status,
        )

    def test_commands_selector_delegates_one_shared_reset_to_main_window(self) -> None:
        settings = self._settings()
        window = self._window(settings)
        self._activate_device(window, "device-command-selector")
        window._set_privilege_profile_available(True)

        with (
            patch.object(window.privilege_manager, "reset") as reset,
            patch.object(window, "_schedule_privilege_recheck"),
        ):
            selector = window.commands_page.privilege_selector
            selector.setCurrentIndex(selector.findData("root"))

        reset.assert_called_once_with()
        self.assertEqual(window.privilege_mode_selector.backend().value, "root")
        self.assertEqual(window.settings_page.privilege_mode.backend().value, "root")

    def test_settings_selector_delegates_one_shared_reset_to_main_window(self) -> None:
        settings = self._settings()
        window = self._window(settings)
        self._activate_device(window, "device-settings-selector")
        window._set_privilege_profile_available(True)

        with (
            patch.object(window.privilege_manager, "reset") as reset,
            patch.object(window, "_schedule_privilege_recheck"),
        ):
            selector = window.settings_page.privilege_mode
            selector.setCurrentIndex(selector.findData("root"))

        reset.assert_called_once_with()
        self.assertEqual(window.privilege_mode_selector.backend().value, "root")
        self.assertEqual(window.commands_page.privilege_selector.backend().value, "root")

    def test_same_generation_shizuku_reentry_restores_ready_after_mode_switch(self) -> None:
        for intermediate_backend in ("standard", "root"):
            with self.subTest(intermediate_backend=intermediate_backend):
                settings = self._settings()
                window = self._window(settings)
                serial = f"device-shizuku-reentry-{intermediate_backend}"
                self._activate_device(window, serial)
                window._set_privilege_profile_available(True)
                selector = window.privilege_mode_selector

                with patch.object(window, "_schedule_privilege_recheck"):
                    selector.setCurrentIndex(selector.findData("shizuku"))

                context = window.device_manager.require_context(("ADB",))
                connection_key = (context.serial, context.generation)
                first_ready = PrivilegeStatus(
                    backend=PrivilegeBackend.SHIZUKU,
                    state="ready",
                    uid=2000,
                    level="shell",
                    message="Initial Shizuku shell is ready.",
                    device_serial=context.serial,
                    device_generation=context.generation,
                )
                window.privilege_manager._cache_if_current(first_ready)
                window._last_automatic_shizuku_key = connection_key
                window._pending_privilege_recheck = False
                window._privilege_barrier_waits_for_recheck = False
                window._set_privilege_feature_barrier_busy(False)
                window._set_automatic_shizuku_ui_busy(False)
                window._apply_privilege_status(first_ready)

                with patch.object(window, "_schedule_privilege_recheck"):
                    selector.setCurrentIndex(selector.findData(intermediate_backend))

                self.assertIsNone(window._last_automatic_shizuku_key)
                self.assertEqual(
                    window.device_manager.current_generation,
                    context.generation,
                )
                window._pending_privilege_recheck = False
                window._privilege_barrier_waits_for_recheck = False
                window._set_privilege_feature_barrier_busy(False)
                window._set_automatic_shizuku_ui_busy(False)

                with patch.object(window, "_schedule_privilege_recheck") as recheck:
                    selector.setCurrentIndex(selector.findData("shizuku"))

                recheck.assert_called_once_with()
                self.assertIsNone(window._last_automatic_shizuku_key)
                self.assertEqual(
                    window.device_manager.current_generation,
                    context.generation,
                )

                token = window.device_manager.operations.register(
                    "privilege-access",
                    device_context=context,
                )
                window._privilege_token = token
                window._privilege_operation_kind = "automatic-shizuku"
                window._privilege_operation_interactive = False
                window._privilege_operation_busy_message = (
                    "Requesting and verifying Shizuku access…"
                )
                window._automatic_shizuku_inflight_key = connection_key
                window._automatic_shizuku_attempts[connection_key] = 1
                window._set_automatic_shizuku_ui_busy(True)
                ready_again = PrivilegeStatus(
                    backend=PrivilegeBackend.SHIZUKU,
                    state="ready",
                    uid=2000,
                    level="shell",
                    message="Shizuku shell is ready again.",
                    device_serial=context.serial,
                    device_generation=context.generation,
                )
                window.privilege_manager._cache_if_current(ready_again)
                window._privilege_operation_result(token, ready_again)
                with patch.object(window, "_resume_feature_refresh_after_acbridge"):
                    window._privilege_operation_finished(token)
                window.device_manager.operations.finish(token)

                self.assertEqual(
                    window._last_automatic_shizuku_key,
                    connection_key,
                )
                self.assertFalse(window._automatic_shizuku_ui_busy)
                self.assertFalse(window._privilege_feature_barrier_busy)
                self.assertFalse(window._privilege_barrier_waits_for_recheck)
                self.assertFalse(window.settings_page._privilege_busy)
                self.assertFalse(window.commands_page._privilege_busy)
                self.assertTrue(window.apps_page.isEnabled())
                self.assertTrue(window.backups_page.isEnabled())
                self.assertTrue(window.file_manager_page.isEnabled())
                self.assertIn(
                    "Shizuku shell is ready again.",
                    window.privilege_runtime_status.full_text(),
                )

    def test_transition_waits_only_for_captured_outgoing_worker_ids(self) -> None:
        window = self._window()
        old = window.device_manager.operations.register("apps.list.old")
        old.privilege_lease = window.privilege_manager.capture_operation_lease()

        window._capture_privilege_transition_blockers()
        new = window.device_manager.operations.register("apps.list.new")
        new.privilege_lease = window.privilege_manager.capture_operation_lease()

        self.assertTrue(window._privilege_transition_blockers_are_draining())
        window.device_manager.operations.finish(old)
        self.assertFalse(window._privilege_transition_blockers_are_draining())
        self.assertTrue(window.device_manager.operations.contains(new))
        window.device_manager.operations.finish(new)

    def test_live_privilege_switch_cancels_only_mode_sensitive_operations(self) -> None:
        settings = self._settings()
        window = self._window(settings)
        self._activate_device(window, "device-live")
        window._set_privilege_profile_available(True)
        leased = window.device_manager.operations.register("apps.list")
        leased.privilege_lease = window.privilege_manager.capture_operation_lease()
        context = window.device_manager.require_context(("ADB",))
        independent = window.device_manager.operations.register(
            "file-manager.p2p",
            device_context=context,
            conflict_groups=(
                f"device-exclusive:{context.serial}",
                f"acbridge-maintenance:{context.serial}",
            ),
        )

        with (
            patch.object(window, "_schedule_privilege_recheck") as recheck,
            patch.object(
                window.file_manager_page,
                "invalidate_privilege_backend_view",
            ) as invalidate_file_manager,
        ):
            window.privilege_mode_selector.setCurrentIndex(
                window.privilege_mode_selector.findData("root")
            )

            recheck.assert_not_called()
            self.assertTrue(window.commands_page._privilege_busy)
            self.assertTrue(window.settings_page._privilege_busy)
            self.assertFalse(window.commands_page.custom_run_button.isEnabled())
            self.assertFalse(window.commands_page.check_privilege_button.isEnabled())
            self.assertFalse(window.settings_page.check_privilege_button.isEnabled())
            window.device_manager.operations.finish(leased)
            window._run_privilege_transition_drain_check()
            recheck.assert_not_called()
            self.assertTrue(window.device_manager.operations.contains(independent))
            self.assertTrue(window.commands_page._privilege_busy)
            self.assertTrue(window.settings_page._privilege_busy)
            self.assertFalse(window.apps_page.isEnabled())
            self.assertFalse(window.backups_page.isEnabled())
            self.assertFalse(window.file_manager_page.isEnabled())
            window.device_manager.operations.finish(independent)
            window._run_privilege_transition_drain_check()
            recheck.assert_called_once_with()

        self.assertTrue(leased.cancelled)
        self.assertEqual(leased.cancellation_reason, "selected access mode changed")
        self.assertFalse(independent.cancelled)
        self.assertEqual(
            window._pending_acbridge_feature_refresh,
            {"apps", "file-manager"},
        )
        self.assertTrue(window._privilege_barrier_waits_for_recheck)
        invalidate_file_manager.assert_called_once_with()

        window._set_privilege_feature_barrier_busy(False)
        self.assertFalse(window.commands_page._privilege_busy)
        self.assertFalse(window.settings_page._privilege_busy)

    def test_pending_backend_refresh_is_retained_until_each_page_opens(self) -> None:
        window = self._window()
        self._activate_device(window, "device-pending")
        window._set_privilege_profile_available(True)
        window._pending_acbridge_feature_refresh.update(
            {"apps", "file-manager"}
        )

        window._resume_feature_refresh_after_acbridge()

        self.assertEqual(
            window._pending_acbridge_feature_refresh,
            {"apps", "file-manager"},
        )
        apps_index = list(window.pages).index("Apps")
        window.stack.setCurrentWidget(window.apps_page)
        with patch.object(window, "_refresh_device_feature") as refresh:
            window._on_page_changed(apps_index)

        refresh.assert_called_once_with("apps")
        self.assertEqual(
            window._pending_acbridge_feature_refresh,
            {"file-manager"},
        )

    def test_late_pending_file_manager_callback_waits_for_page_return(self) -> None:
        window = self._window()
        self._activate_device(window, "device-late-file-manager")
        window._set_privilege_profile_available(True)
        window._pending_acbridge_feature_refresh.add("file-manager")
        window.stack.setCurrentWidget(window.file_manager_page)
        callbacks: list[object] = []

        with (
            patch.object(
                window,
                "_automatic_shizuku_workflow_pending",
                return_value=False,
            ),
            patch.object(
                MainWindow,
                "_acbridge_update_workflow_pending",
                return_value=False,
            ),
            patch.object(window, "_refresh_device_feature") as refresh,
            patch(
                "openadb.ui.main_window.QTimer.singleShot",
                side_effect=lambda _delay, callback: callbacks.append(callback),
            ),
        ):
            window._resume_feature_refresh_after_acbridge()

            self.assertEqual(len(callbacks), 1)
            refresh.assert_not_called()

            window.stack.setCurrentWidget(window.dashboard)
            callbacks.pop()()

            refresh.assert_not_called()
            self.assertIn(
                "file-manager",
                window._pending_acbridge_feature_refresh,
            )

            window.stack.setCurrentWidget(window.file_manager_page)
            file_manager_index = list(window.pages).index("File Manager")
            window._on_page_changed(file_manager_index)

            refresh.assert_called_once_with("file-manager")
            self.assertNotIn(
                "file-manager",
                window._pending_acbridge_feature_refresh,
            )

    def test_file_manager_navigation_preempts_only_apps_assets_and_refreshes_once_after_finish(
        self,
    ) -> None:
        settings = self._settings()
        window = self._window(settings)
        device = self._activate_device(window, "device-assets-handoff")
        window._set_privilege_profile_available(True)
        settings.set("privilege_backend", "shizuku")
        status = PrivilegeStatus(
            backend=PrivilegeBackend.SHIZUKU,
            state="ready",
            uid=2000,
            level="shell",
            message="Shizuku shell is ready.",
            device_serial=device.serial,
            device_generation=window.device_manager.current_generation,
        )
        window.privilege_manager._cache_if_current(status)
        window._apply_privilege_status(status)
        window._pending_privilege_recheck = False
        window._privilege_barrier_waits_for_recheck = False

        context = window.device_manager.require_context(("ADB",))
        assets_token = window.apps_page._register_operation(
            context,
            "assets",
            "apps-assets",
        )
        window.apps_page._assets_token = assets_token
        window.apps_page._assets_loading = True
        unrelated = window.device_manager.operations.register(
            "logs.local",
            device_context=context,
        )
        window.apps_page.apps = [AppInfo(package_name="com.example.cached")]
        file_manager_index = list(window.pages).index("File Manager")
        window.stack.setCurrentWidget(window.file_manager_page)
        callbacks: list[object] = []

        with (
            patch.object(
                window,
                "_automatic_shizuku_workflow_pending",
                return_value=False,
            ),
            patch.object(
                MainWindow,
                "_acbridge_update_workflow_pending",
                return_value=False,
            ),
            patch.object(window.file_manager_page, "refresh_all") as refresh_all,
            patch(
                "openadb.ui.main_window.QTimer.singleShot",
                side_effect=lambda _delay, callback: callbacks.append(callback),
            ),
        ):
            window._on_page_changed(file_manager_index)

            self.assertTrue(assets_token.cancelled)
            self.assertFalse(unrelated.cancelled)
            self.assertTrue(window.device_manager.operations.contains(assets_token))
            refresh_all.assert_not_called()

            # A retry may poll while the cancelled worker is still draining,
            # but it must not treat cancel() as ownership release.
            callbacks_before_finish = list(callbacks)
            callbacks.clear()
            for callback in callbacks_before_finish:
                callback()
            refresh_all.assert_not_called()

            window.apps_page._apk_assets_finished(assets_token)
            self.assertFalse(window.device_manager.operations.contains(assets_token))
            for _attempt in range(8):
                if refresh_all.call_count or not callbacks:
                    break
                callbacks.pop(0)()
            self.app.processEvents()

            refresh_all.assert_called_once_with()
            self.assertIn("apps", window._pending_acbridge_feature_refresh)
            self.assertNotIn(
                "file-manager",
                window._pending_acbridge_feature_refresh,
            )
            for callback in tuple(callbacks):
                callback()
            refresh_all.assert_called_once_with()

        window.device_manager.operations.finish(unrelated)

    def _assert_file_manager_assets_handoff_for_backend(
        self,
        backend: PrivilegeBackend,
    ) -> None:
        settings = self._settings()
        window = self._window(settings)
        device = self._activate_device(
            window,
            f"device-{backend.value}-assets-handoff",
        )
        window._set_privilege_profile_available(True)
        settings.set("privilege_backend", backend.value)
        status = PrivilegeStatus(
            backend=backend,
            state="ready",
            uid=0 if backend is PrivilegeBackend.ROOT else 2000,
            level="root" if backend is PrivilegeBackend.ROOT else "shell",
            message=f"{backend.value} access is ready.",
            device_serial=device.serial,
            device_generation=window.device_manager.current_generation,
        )
        window.privilege_manager._cache_if_current(status)
        window._apply_privilege_status(status)
        window._pending_privilege_recheck = False
        window._privilege_barrier_waits_for_recheck = False
        context = window.device_manager.require_context(("ADB",))

        # Bulk application work is not passive. A File Manager activation may
        # wait for it, but must never request its cancellation.
        bulk_token = window.apps_page._register_operation(
            context,
            "bulk",
            "apps-bulk",
        )
        self.assertFalse(
            MainWindow._yield_passive_apps_work_to_file_manager(window)
        )
        self.assertFalse(bulk_token.cancelled)
        window.device_manager.operations.finish(bulk_token)

        assets_token = window.apps_page._register_operation(
            context,
            "assets",
            "apps-assets",
        )
        window.apps_page._assets_token = assets_token
        window.apps_page._assets_loading = True
        window.apps_page.apps = [
            AppInfo(package_name=f"com.example.{backend.value}.cached")
        ]
        file_manager_index = list(window.pages).index("File Manager")
        window.stack.setCurrentWidget(window.file_manager_page)
        callbacks: list[object] = []

        with (
            patch.object(
                window,
                "_automatic_shizuku_workflow_pending",
                return_value=False,
            ),
            patch.object(
                MainWindow,
                "_acbridge_update_workflow_pending",
                return_value=False,
            ),
            patch.object(window.file_manager_page, "refresh_all") as refresh_all,
            patch(
                "openadb.ui.main_window.QTimer.singleShot",
                side_effect=lambda _delay, callback: callbacks.append(callback),
            ),
        ):
            window._on_page_changed(file_manager_index)

            self.assertTrue(assets_token.cancelled)
            self.assertFalse(bulk_token.cancelled)
            self.assertTrue(window.device_manager.operations.contains(assets_token))
            refresh_all.assert_not_called()
            self.assertNotIn(
                "Operation conflict",
                window.file_manager_page.status_label.text(),
            )

            callbacks_before_finish = tuple(callbacks)
            callbacks.clear()
            for callback in callbacks_before_finish:
                callback()
            refresh_all.assert_not_called()
            self.assertTrue(window.device_manager.operations.contains(assets_token))

            window.apps_page._apk_assets_finished(assets_token)
            self.assertFalse(window.device_manager.operations.contains(assets_token))
            for _attempt in range(12):
                if refresh_all.call_count or not callbacks:
                    break
                callbacks.pop(0)()
            self.app.processEvents()

            refresh_all.assert_called_once_with()
            self.assertNotIn(
                "Operation conflict",
                window.file_manager_page.status_label.text(),
            )
            for callback in tuple(callbacks):
                callback()
            refresh_all.assert_called_once_with()

        self.assertEqual(window.device_manager.operations.active_count, 0)

    def test_standard_file_manager_waits_for_assets_worker_release(self) -> None:
        self._assert_file_manager_assets_handoff_for_backend(
            PrivilegeBackend.STANDARD
        )

    def test_root_file_manager_waits_for_assets_worker_release(self) -> None:
        self._assert_file_manager_assets_handoff_for_backend(
            PrivilegeBackend.ROOT
        )

    def test_device_refresh_does_not_release_a_draining_backend_switch(self) -> None:
        window = self._window()
        self._activate_device(window, "device-refresh-race")
        window._set_privilege_profile_available(True)
        context = window.device_manager.require_context(("ADB",))
        token = window.device_manager.operations.register(
            "apps.list",
            device_context=context,
            conflict_groups=(f"acbridge-maintenance:{context.serial}",),
        )
        token.privilege_lease = window.privilege_manager.capture_operation_lease()

        with (
            patch.object(window, "_schedule_privilege_recheck"),
            patch.object(window, "_schedule_acbridge_update", return_value=False),
        ):
            window.privilege_mode_selector.setCurrentIndex(
                window.privilege_mode_selector.findData("root")
            )
            refreshed = DeviceInfo(
                serial="device-refresh-race",
                model="Reconnected device",
                mode="ADB",
                state="device",
                transport_id="2",
                form_factor="Phone",
            )
            window.device_manager._set_active(
                refreshed,
                reason="test reconnect during access transition",
            )
            window._on_device_refreshed(refreshed)

        self.assertTrue(token.cancelled)
        self.assertTrue(window._privilege_barrier_waits_for_recheck)
        self.assertTrue(window.commands_page._privilege_busy)
        self.assertTrue(window.settings_page._privilege_busy)
        self.assertFalse(window.apps_page.isEnabled())
        self.assertFalse(window.backups_page.isEnabled())
        self.assertFalse(window.file_manager_page.isEnabled())
        window.device_manager.operations.finish(token)

    def test_shizuku_switch_in_recovery_is_explicitly_unavailable_without_refresh(self) -> None:
        window = self._window()
        self._activate_device(window, "device-recovery")
        recovery = DeviceInfo(
            serial="device-recovery",
            model="Recovery device",
            mode="Recovery",
            state="device",
            transport_id="2",
        )
        window.device_manager._set_active(recovery, reason="test recovery mode")
        window._set_privilege_profile_available(True)
        window._pending_acbridge_feature_refresh.clear()

        with patch.object(window, "_schedule_privilege_recheck") as recheck:
            window.privilege_mode_selector.setCurrentIndex(
                window.privilege_mode_selector.findData("shizuku")
            )

        recheck.assert_not_called()
        self.assertEqual(window._pending_acbridge_feature_refresh, set())
        self.assertIn(
            "unavailable while the device is in Recovery mode",
            window.privilege_runtime_status.full_text(),
        )
        apps_index = list(window.pages).index("Apps")
        file_manager_index = list(window.pages).index("File Manager")
        with (
            patch.object(window.apps_page, "refresh_apps") as refresh_apps,
            patch.object(window.file_manager_page, "refresh_all") as refresh_files,
        ):
            window._on_page_changed(apps_index)
            window._on_page_changed(file_manager_index)

        refresh_apps.assert_not_called()
        refresh_files.assert_not_called()
        self.assertFalse(window.apps_page._device_available_for_apps())
        self.assertFalse(
            window.file_manager_page._android_shell_backend_available()
        )

    def test_unavailable_shizuku_retains_refresh_until_access_becomes_ready(self) -> None:
        window = self._window()
        self._activate_device(window, "device-permission")
        window._set_privilege_profile_available(True)
        with patch.object(window, "_schedule_privilege_recheck"):
            window.privilege_mode_selector.setCurrentIndex(
                window.privilege_mode_selector.findData("shizuku")
            )
        window._pending_privilege_recheck = False
        window._privilege_barrier_waits_for_recheck = False
        window._set_privilege_feature_barrier_busy(False)
        generation = window.device_manager.current_generation
        denied = PrivilegeStatus(
            backend=PrivilegeBackend.SHIZUKU,
            state="permission_denied",
            level="unavailable",
            message="Shizuku permission was denied.",
            device_serial="device-permission",
            device_generation=generation,
        )
        window.privilege_manager._cache_if_current(denied)
        window._apply_privilege_status(denied)
        apps_index = list(window.pages).index("Apps")
        with patch.object(window, "_refresh_device_feature") as refresh:
            window._on_page_changed(apps_index)
        refresh.assert_not_called()
        self.assertIn("apps", window._pending_acbridge_feature_refresh)

        ready = PrivilegeStatus(
            backend=PrivilegeBackend.SHIZUKU,
            state="ready",
            uid=2000,
            level="shell",
            message="Shizuku shell is ready.",
            device_serial="device-permission",
            device_generation=generation,
        )
        window.privilege_manager._cache_if_current(ready)
        window._apply_privilege_status(ready)
        window.stack.setCurrentWidget(window.apps_page)
        with patch.object(window, "_refresh_device_feature") as refresh:
            window._on_page_changed(apps_index)
        refresh.assert_called_once_with("apps")
        self.assertNotIn("apps", window._pending_acbridge_feature_refresh)

    def test_privilege_selectors_stay_available_offline_but_device_actions_do_not(self) -> None:
        window = self._window()

        self.assertTrue(window.privilege_mode_selector.isEnabled())
        self.assertTrue(window.settings_page.privilege_mode.isEnabled())
        self.assertTrue(window.commands_page.privilege_selector.isEnabled())
        self.assertFalse(window.settings_page.check_privilege_button.isEnabled())
        self.assertFalse(window.settings_page.request_shizuku_button.isEnabled())
        self.assertFalse(window.settings_page.open_shizuku_button.isEnabled())
        self.assertFalse(window.commands_page.check_privilege_button.isEnabled())
        self.assertFalse(window.commands_page.request_shizuku_button.isEnabled())
        self.assertFalse(window.commands_page.open_shizuku_button.isEnabled())
        self.assertIn("next active Android device profile", window.privilege_status_panel.toolTip())

    def test_offline_privilege_choices_synchronize_without_device_work(self) -> None:
        settings = self._settings()
        window = self._window(settings)
        selectors = (
            window.privilege_mode_selector,
            window.settings_page.privilege_mode,
            window.commands_page.privilege_selector,
        )
        choices = (
            (selectors[0], "root", True),
            (selectors[1], "shizuku", False),
            (selectors[2], "standard", False),
        )

        with (
            patch.object(window, "_schedule_privilege_recheck") as recheck,
            patch.object(window, "_start_privilege_operation") as start_operation,
            patch.object(window.device_manager, "require_context") as require_context,
            patch("openadb.ui.main_window.start_worker") as start_worker,
        ):
            for source, backend, root_enabled in choices:
                with self.subTest(backend=backend):
                    source.setCurrentIndex(source.findData(backend))

                    self.assertEqual(
                        [selector.backend().value for selector in selectors],
                        [backend, backend, backend],
                    )
                    self.assertEqual(
                        settings.get_global("privilege_backend"),
                        backend,
                    )
                    self.assertEqual(
                        settings.get_global("pending_privilege_backend"),
                        backend,
                    )
                    self.assertEqual(
                        settings.get_global("root_mode_enabled"),
                        root_enabled,
                    )

        recheck.assert_not_called()
        start_operation.assert_not_called()
        require_context.assert_not_called()
        start_worker.assert_not_called()

    def test_consumed_offline_choice_returns_to_empty_and_can_be_queued_again(self) -> None:
        settings = self._settings()
        window = self._window(settings)
        selector = window.privilege_mode_selector

        selector.setCurrentIndex(selector.findData("root"))
        self.assertEqual(settings.pending_privilege_backend(), "root")
        self._activate_device(window, "one-shot-device")
        window._settings_changed(profile_changed=True)
        self.assertEqual(settings.get("privilege_backend"), "root")
        self.assertEqual(settings.pending_privilege_backend(), "")

        window._set_privilege_profile_available(False)

        mirrored = (
            selector,
            window.settings_page.privilege_mode,
            window.commands_page.privilege_selector,
        )
        self.assertTrue(all(not item.has_backend() for item in mirrored))
        self.assertIn("choose an access mode", window.privilege_runtime_status.full_text())

        selector.setCurrentIndex(selector.findData("root"))

        self.assertEqual(settings.pending_privilege_backend(), "root")
        self.assertTrue(all(item.backend().value == "root" for item in mirrored))

    def test_privilege_selector_follows_each_device_profile(self) -> None:
        window = self._window()
        selector = window.privilege_mode_selector

        self._activate_device(window, "profile-a", "1")
        window._settings_changed(profile_changed=True)
        selector.setCurrentIndex(selector.findData("shizuku"))
        self.assertEqual(window.settings.get("privilege_backend"), "shizuku")

        self._activate_device(window, "profile-b", "2")
        window._settings_changed(profile_changed=True)
        self.assertEqual(selector.backend().value, "standard")
        selector.setCurrentIndex(selector.findData("root"))
        self.assertEqual(window.settings.get("privilege_backend"), "root")

        self._activate_device(window, "profile-a", "3")
        window._settings_changed(profile_changed=True)
        self.assertEqual(selector.backend().value, "shizuku")
        self.assertEqual(
            window.settings_page.privilege_mode.backend().value,
            "shizuku",
        )

    def test_window_geometry_round_trip_uses_global_settings(self) -> None:
        settings = self._settings()
        window = self._window(settings)
        window.show()
        window.setGeometry(30, 40, 740, 600)
        self.app.processEvents()
        window._save_window_state()

        settings.activate_device_profile("device-one", "Test device", "Phone")
        settings.set_global_values({"navigation_collapsed": True})
        self.assertTrue(settings.get_global("navigation_collapsed"))
        global_json = json.loads(settings.global_path.read_text(encoding="utf-8"))
        self.assertEqual(global_json["window_width"], 740)
        self.assertEqual(global_json["window_height"], 600)

        restored = self._window(IsolatedSettings(self.config_dir))
        restored.show()
        self.app.processEvents()
        self.assertEqual(restored.geometry(), QRect(30, 40, 740, 600))
        self.assertTrue(restored.navigation_collapsed)

    def test_disconnected_monitor_and_oversized_geometry_are_recovered(self) -> None:
        primary = QRect(0, 0, 1920, 1040)
        disconnected = QRect(4000, 300, 1100, 800)
        recovered = MainWindow._bounded_window_geometry(disconnected, [primary])
        self.assertTrue(primary.contains(recovered))
        self.assertEqual(recovered.size(), disconnected.size())

        second = QRect(1920, 0, 1920, 1040)
        valid_second_screen = QRect(2100, 100, 1000, 700)
        self.assertEqual(
            MainWindow._bounded_window_geometry(valid_second_screen, [primary, second]),
            valid_second_screen,
        )

        oversized = MainWindow._bounded_window_geometry(QRect(-500, -500, 5000, 3000), [primary])
        self.assertEqual(oversized, primary)

    def test_narrow_standard_and_maximized_layout_in_all_themes(self) -> None:
        window = self._window()
        window.show()
        window._set_navigation_collapsed(True, persist=False)
        for width, height in [(760, 520), (960, 640)]:
            window.showNormal()
            window.resize(width, height)
            self.app.processEvents()
            self.assertEqual(window.size().width(), width)
            self.assertGreaterEqual(window.stack.width(), width - 110)
            for row in range(window.nav.count()):
                window.nav.setCurrentRow(row)
                self.app.processEvents()
                self.assertEqual(window.stack.currentIndex(), row)

        for theme in ("System", "Light", "Dark"):
            apply_theme(self.app, theme)
            self.app.processEvents()
            self.assertFalse(window.grab().isNull())

        window.showMaximized()
        self.app.processEvents()
        self.assertTrue(window.isMaximized())
        window._save_window_state()
        self.assertTrue(window.settings.get_global("window_maximized"))
        restored = self._window(IsolatedSettings(self.config_dir))
        restored.show()
        self.app.processEvents()
        self.assertTrue(restored.isMaximized())

    def test_privilege_footer_uses_available_width_and_elides_safely(self) -> None:
        window = self._window()
        window.show()
        window._set_navigation_collapsed(True, persist=False)
        window.resize(1920, 720)
        realistic_status = "Shizuku: Shizuku root (UID 0) is ready."
        window._set_global_privilege_status_text(realistic_status)
        self.app.processEvents()

        label = window.privilege_runtime_status
        self.assertGreater(label.maximumWidth(), 240)
        self.assertGreater(label.width(), 240)
        self.assertEqual(label.text(), realistic_status)
        self.assertEqual(label.full_text(), realistic_status)
        self.assertEqual(label.toolTip(), realistic_status)
        self.assertIn(realistic_status, label.accessibleName())

        window.resize(760, 520)
        long_status = (
            "Shizuku access is ready for the active device, and this deliberately long "
            "diagnostic remains available without expanding the window beyond its requested width."
        )
        window._set_global_privilege_status_text(long_status)
        self.app.processEvents()

        self.assertEqual(window.width(), 760)
        self.assertNotEqual(label.text(), long_status)
        self.assertLessEqual(
            label.fontMetrics().horizontalAdvance(label.text()),
            label.contentsRect().width(),
        )
        self.assertEqual(label.full_text(), long_status)
        self.assertEqual(label.toolTip(), long_status)
        self.assertIn(long_status, label.accessibleName())

        status = PrivilegeStatus(
            backend=PrivilegeBackend.SHIZUKU,
            state="ready",
            uid=0,
            level="root",
            message="Shizuku root (UID 0) is ready.",
        )
        self.assertEqual(
            MainWindow._privilege_status_text(status, PrivilegeBackend.SHIZUKU),
            status.message,
        )

    def test_legacy_settings_receive_safe_ui_defaults(self) -> None:
        (self.config_dir / "settings.json").write_text(
            json.dumps({"theme": "Dark", "auto_refresh_device": False}),
            encoding="utf-8",
        )
        settings = IsolatedSettings(self.config_dir)
        self.assertEqual(settings.get_global("window_width"), 1280)
        self.assertEqual(settings.get_global("window_height"), 820)
        self.assertFalse(settings.get_global("window_maximized"))
        self.assertFalse(settings.get_global("navigation_collapsed"))

    def test_selected_platform_tools_verification_does_not_run_discovery(self) -> None:
        settings = self._settings()
        window = self._window(settings)
        selected = self.config_dir / "selected-platform-tools"
        selected.mkdir()
        info = window.platform_tools.choose_folder(selected)
        window._update_tools(info)
        verified = PlatformToolsInfo(folder=selected, source="Manual")

        with (
            patch.object(window.platform_tools, "inspect_folder", return_value=verified) as inspect,
            patch.object(window.platform_tools, "detect") as detect,
            patch("openadb.ui.main_window.start_worker") as start,
        ):
            window.verify_selected_platform_tools()
            worker = start.call_args.args[2]
            worker.run()

        inspect.assert_called_once_with(selected, "Manual")
        detect.assert_not_called()
        self.assertIn("Verification result", window.settings_page.last_verification.text())
        self.assertFalse(window._verifying_platform_tools)

    def test_background_platform_tools_detection_defers_selection_to_guarded_result(self) -> None:
        window = self._window()
        candidate = PlatformToolsInfo(
            folder=self.config_dir / "detected-platform-tools",
            adb_path=self.config_dir / "detected-platform-tools" / "adb.exe",
            source="PATH",
        )

        with (
            patch.object(window.platform_tools, "detect", return_value=[candidate]) as detect,
            patch("openadb.ui.main_window.start_worker") as start,
        ):
            window.detect_platform_tools(interactive=False)
            worker = start.call_args.args[2]
            token = start.call_args.kwargs["operation_token"]
            self.assertIsNone(token.device_context)
            self.assertTrue(window.device_manager.operations.contains(token))
            worker.run()

        detect.assert_called_once_with(select=False)
        self.assertIs(window.platform_tools.active, candidate)
        self.assertEqual(window.device_manager.operations.active_count, 0)
        self.assertFalse(window._detecting_platform_tools)

    def test_platform_tools_late_callbacks_are_ignored_and_registry_is_clean_on_close(self) -> None:
        for operation in ("detect", "verify"):
            with self.subTest(operation=operation):
                window = self._window()
                selected = PlatformToolsInfo(
                    folder=self.config_dir / f"selected-{operation}",
                    source="Manual",
                )
                candidate = PlatformToolsInfo(
                    folder=self.config_dir / f"late-{operation}",
                    adb_path=self.config_dir / f"late-{operation}" / "adb.exe",
                    source="PATH",
                )
                window.platform_tools.active = selected

                with patch("openadb.ui.main_window.start_worker", return_value=True) as start:
                    if operation == "detect":
                        window.detect_platform_tools(interactive=False)
                        token = window._platform_tools_detection_token
                    else:
                        window.verify_selected_platform_tools()
                        token = window._platform_tools_verification_token

                self.assertIsNotNone(token)
                assert token is not None
                self.assertIsNone(token.device_context)
                self.assertTrue(window.device_manager.operations.contains(token))
                self.assertIs(start.call_args.kwargs["operation_registry"], window.device_manager.operations)
                self.assertIs(start.call_args.kwargs["operation_token"], token)

                previous_verification = window.settings_page.last_verification.text()
                window.close()
                self.assertTrue(token.cancelled)
                self.assertEqual(token.cancellation_reason, "application shutdown")
                self.assertEqual(window.device_manager.operations.active_count, 0)

                with (
                    patch.object(window.platform_tools, "set_active") as set_active,
                    patch.object(window, "_update_tools") as update_tools,
                    patch.object(QMessageBox, "warning") as warning,
                ):
                    if operation == "detect":
                        window._platform_tools_detected([candidate], False, token)
                        window._platform_tools_detection_failed(token, "late detection failure")
                    else:
                        window._platform_tools_verified(candidate, token)
                        window._platform_tools_verification_failed(
                            "late verification failure",
                            "trace",
                            token,
                        )

                set_active.assert_not_called()
                update_tools.assert_not_called()
                warning.assert_not_called()
                self.assertIs(window.platform_tools.active, selected)
                self.assertEqual(
                    window.settings_page.last_verification.text(),
                    previous_verification,
                )

    def test_platform_tools_picker_rechecks_shutdown_across_nested_event_loop(self) -> None:
        for close_during_picker in (False, True):
            with self.subTest(close_during_picker=close_during_picker):
                window = self._window()
                previous = PlatformToolsInfo(
                    folder=self.config_dir / f"previous-{close_during_picker}",
                    source="Saved settings",
                )
                selected = PlatformToolsInfo(
                    folder=self.config_dir / f"selected-{close_during_picker}",
                    source="PATH",
                )
                candidates = [
                    selected,
                    PlatformToolsInfo(
                        folder=self.config_dir / f"second-{close_during_picker}",
                        source="Android SDK",
                    ),
                ]
                window.platform_tools.active = previous
                with patch("openadb.ui.main_window.start_worker", return_value=True):
                    window.detect_platform_tools(interactive=True)
                token = window._platform_tools_detection_token
                self.assertIsNotNone(token)
                assert token is not None

                def finish_inside_picker() -> int:
                    if close_during_picker:
                        window.close()
                    else:
                        # A real queued finished signal can run in dialog.exec().
                        window._platform_tools_detection_finished(token)
                    return 1

                with patch("openadb.ui.main_window.PlatformToolsPickerDialog") as picker:
                    picker.return_value.exec.side_effect = finish_inside_picker
                    picker.return_value.selected_info.return_value = selected
                    window._platform_tools_detected(candidates, True, token)

                expected = previous if close_during_picker else selected
                self.assertIs(window.platform_tools.active, expected)
                self.assertEqual(window.device_manager.operations.active_count, 0)

    def test_platform_tools_find_cancel_keeps_previous_selection(self) -> None:
        window = self._window()
        previous = PlatformToolsInfo(folder=self.config_dir / "previous", source="Saved settings")
        window.platform_tools.active = previous
        candidates = [
            PlatformToolsInfo(folder=self.config_dir / "candidate-one", source="PATH"),
            PlatformToolsInfo(folder=self.config_dir / "candidate-two", source="Android SDK"),
        ]

        with patch("openadb.ui.main_window.PlatformToolsPickerDialog") as picker:
            picker.return_value.exec.return_value = 0
            window._platform_tools_detected(candidates, interactive=True)

        self.assertIs(window.platform_tools.active, previous)
        self.assertIn("cancelled", window.settings_page.last_verification.text())

    def test_platform_tools_find_can_open_manual_choice_before_worker_finished(self) -> None:
        window = self._window()
        window._detecting_platform_tools = True
        with (
            patch.object(QMessageBox, "warning", return_value=QMessageBox.Yes),
            patch.object(window, "_choose_platform_tools_folder") as choose,
        ):
            window._platform_tools_detected([], interactive=True)
        choose.assert_called_once_with()

    def test_icon_cache_action_calls_cache_manager(self) -> None:
        window = self._window()
        with (
            patch.object(window.icon_extractor, "clear_cache") as clear,
            patch.object(QMessageBox, "information"),
        ):
            window._clear_icon_cache()
        clear.assert_called_once_with()

    def test_temporary_cleanup_is_bound_to_the_confirmed_path(self) -> None:
        settings = self._settings()
        window = self._window(settings)
        confirmed_path = str(settings.temp_folder)
        with (
            patch(
                "openadb.ui.main_window.exec_bounded_message_box",
                return_value=QMessageBox.Ok,
            ) as confirm,
            patch.object(QMessageBox, "information"),
            patch.object(settings, "clear_temporary_files", return_value=[]) as clear,
        ):
            window._clear_temporary_files()

        clear.assert_called_once_with(expected_path=confirmed_path)
        self.assertIn(confirmed_path, confirm.call_args.kwargs["detailed_text"])

    def test_backup_folder_change_invalidates_stale_rows_before_refresh(self) -> None:
        settings = self._settings()
        window = self._window(settings)
        old_backup = BackupInfo(
            path=settings.backups_folder / "com.example.old" / "one",
            package_name="com.example.old",
        )
        window.backups_page._backups_loaded([old_backup])
        self.assertEqual(window.backups_page.table.rowCount(), 1)

        new_root = self.config_dir / "replacement-backups"
        settings.set("backups_folder", str(new_root))
        with patch.object(window.backups_page, "refresh") as refresh:
            window._settings_changed()

        self.assertEqual(window.backup_manager.root, new_root)
        self.assertEqual(window.backups_page.backups, [])
        self.assertEqual(window.backups_page.table.rowCount(), 0)
        refresh.assert_called_once_with()

    def test_profile_activation_failure_invalidates_context_and_skips_auto_refresh(self) -> None:
        settings = self._settings()
        window = self._window(settings)
        window.stack.setCurrentWidget(window.apps_page)
        device = DeviceInfo(
            serial="unavailable-profile",
            model="Unavailable profile",
            mode="ADB",
            state="device",
            transport_id="9",
            form_factor="Phone",
        )
        window.device_manager._set_active(device, reason="test profile activation failure")

        with (
            patch.object(
                settings,
                "activate_device_profile",
                side_effect=OSError("profile drive unavailable"),
            ),
            patch.object(
                window.device_manager,
                "invalidate_profile",
                wraps=window.device_manager.invalidate_profile,
            ) as invalidate,
            patch.object(window.apps_page, "refresh_apps") as refresh_apps,
            patch.object(window.backups_page, "refresh") as refresh_backups,
            patch.object(QMessageBox, "warning") as warning,
        ):
            window._on_device_refreshed(device)

        invalidate.assert_called_once_with("device profile activation failed")
        refresh_apps.assert_not_called()
        refresh_backups.assert_not_called()
        warning.assert_called_once()
        self.assertIn("profile drive unavailable", warning.call_args.args[2])
        with self.assertRaises(DeviceContextUnavailable):
            window.device_manager.capture_context()

    def test_profile_sync_retries_after_post_activation_refresh_failure(self) -> None:
        settings = self._settings()
        window = self._window(settings)
        device = DeviceInfo(
            serial="retry-profile",
            model="Retry profile",
            mode="ADB",
            state="device",
            transport_id="10",
            form_factor="Phone",
        )
        window.device_manager._set_active(device, reason="test profile retry")

        with (
            patch.object(
                window,
                "_settings_changed",
                side_effect=OSError("dependent profile refresh failed"),
            ),
            patch.object(QMessageBox, "warning"),
        ):
            self.assertIsNone(window._activate_device_profile(device))

        self.assertEqual(settings.active_profile_serial, device.serial)
        with self.assertRaises(DeviceContextUnavailable):
            window.device_manager.capture_context()

        with patch.object(window.backups_page, "refresh"):
            self.assertTrue(window._activate_device_profile(device))

        context = window.device_manager.capture_context()
        self.assertEqual(context.serial, device.serial)

    def test_full_reset_cancel_preserves_settings_and_profile(self) -> None:
        settings = self._settings()
        settings.set("theme", "Dark")
        settings.activate_device_profile("keep-device", "Keep phone", "Phone")
        profile_path = settings.path
        window = self._window(settings)

        with patch(
            "openadb.ui.main_window.exec_bounded_message_box",
            return_value=QMessageBox.Cancel,
        ):
            window._reset_all_settings_and_caches()

        self.assertEqual(settings.get("theme"), "Dark")
        self.assertTrue(profile_path.exists())
        self.assertIn("cancelled", window.statusBar().currentMessage().lower())

    def test_full_reset_apk_backup_option_is_unchecked_and_non_persistent(self) -> None:
        window = self._window()
        option = window.settings_page.delete_apk_backups_on_full_reset

        self.assertFalse(option.isChecked())
        self.assertIn("permanently delete", option.text().lower())
        self.assertTrue(option.accessibleDescription())

        option.setChecked(True)
        with patch(
            "openadb.ui.main_window.exec_bounded_message_box",
            return_value=QMessageBox.Cancel,
        ):
            window._reset_all_settings_and_caches()

        self.assertFalse(option.isChecked())

    def test_full_reset_second_destructive_cancel_preserves_backups_and_settings(self) -> None:
        settings = self._settings()
        settings.set("theme", "Dark")
        snapshot = (
            settings.backups_folder
            / "com.example.keep"
            / "2026-08-25_12-00-00-keep"
        )
        snapshot.mkdir(parents=True)
        (snapshot / "base.apk").write_text("backup", encoding="utf-8")
        window = self._window(settings)
        window.settings_page.delete_apk_backups_on_full_reset.setChecked(True)

        with patch(
            "openadb.ui.main_window.exec_bounded_message_box",
            side_effect=(QMessageBox.Ok, QMessageBox.Cancel),
        ) as confirm:
            window._reset_all_settings_and_caches()

        self.assertTrue(snapshot.exists())
        self.assertEqual(settings.get("theme"), "Dark")
        self.assertFalse(
            window.settings_page.delete_apk_backups_on_full_reset.isChecked()
        )
        warning_text = confirm.call_args.args[2]
        warning_details = confirm.call_args.kwargs["detailed_text"]
        self.assertIn("IRREVERSIBLE DATA LOSS", warning_text)
        self.assertIn("Recycle Bin", warning_text)
        self.assertIn("some backups", warning_text)
        self.assertIn(str(settings.backups_folder), warning_details)

    def test_confirmed_full_reset_permanently_deletes_apk_backup_snapshots(self) -> None:
        settings = self._settings()
        settings.set("theme", "Dark")
        snapshot = (
            settings.backups_folder
            / "com.example.remove"
            / "2026-08-25_12-00-00-remove"
        )
        snapshot.mkdir(parents=True)
        for name in ("base.apk", "icon.png", "command_log.txt"):
            (snapshot / name).write_text(name, encoding="utf-8")
        (snapshot / "metadata.json").write_text(
            json.dumps(
                {
                    "package_name": "com.example.remove",
                    "app_label": "Remove",
                    "backup_date": "2026-08-25T12:00:00",
                    "backup_status": "success",
                    "device_serial": "test-device",
                    "apk_files": ["base.apk"],
                }
            ),
            encoding="utf-8",
        )
        window = self._window(settings)
        window.backups_page._loading = False
        window.backups_page._action_busy = False
        window.settings_page.delete_apk_backups_on_full_reset.setChecked(True)

        with (
            patch(
                "openadb.ui.main_window.exec_bounded_message_box",
                side_effect=(QMessageBox.Ok, QMessageBox.Yes),
            ),
            patch.object(QMessageBox, "information") as information,
            patch.object(window.backups_page, "refresh"),
        ):
            window._reset_all_settings_and_caches()

        self.assertFalse(snapshot.exists())
        self.assertEqual(settings.get("theme"), "System")
        self.assertFalse(
            window.settings_page.delete_apk_backups_on_full_reset.isChecked()
        )
        self.assertIn("permanently deleted", information.call_args.args[2])

    def test_backup_cleanup_option_is_blocked_while_backup_page_is_busy(self) -> None:
        settings = self._settings()
        snapshot = (
            settings.backups_folder
            / "com.example.keep"
            / "2026-08-25_12-00-00-keep"
        )
        snapshot.mkdir(parents=True)
        (snapshot / "base.apk").write_text("backup", encoding="utf-8")
        window = self._window(settings)
        window.settings_page.delete_apk_backups_on_full_reset.setChecked(True)
        window.backups_page._loading = True

        with (
            patch.object(QMessageBox, "information") as information,
            patch.object(QMessageBox, "warning") as warning,
        ):
            window._reset_all_settings_and_caches()

        self.assertTrue(snapshot.exists())
        warning.assert_not_called()
        self.assertIn("still running", information.call_args.args[2])
        self.assertFalse(
            window.settings_page.delete_apk_backups_on_full_reset.isChecked()
        )

    def test_partial_backup_cleanup_never_reports_a_successful_full_reset(self) -> None:
        settings = self._settings()
        settings.set("theme", "Dark")
        window = self._window(settings)
        window.backups_page._loading = False
        window.backups_page._action_busy = False
        window.settings_page.delete_apk_backups_on_full_reset.setChecked(True)
        partial = ApkBackupCleanupResult(
            backup_roots=settings.apk_backup_folders(),
            removed_snapshots=(settings.backups_folder / "removed-snapshot",),
            failures=("D:/locked-backups: access denied",),
        )

        with (
            patch(
                "openadb.ui.main_window.exec_bounded_message_box",
                side_effect=(QMessageBox.Ok, QMessageBox.Yes),
            ),
            patch("openadb.ui.main_window.show_error_dialog") as show_error,
            patch.object(QMessageBox, "information") as information,
            patch.object(settings, "clear_apk_backups", return_value=partial),
            patch.object(window.backups_page, "refresh"),
        ):
            window._reset_all_settings_and_caches()

        self.assertEqual(settings.get("theme"), "Dark")
        information.assert_not_called()
        show_error.assert_called_once()
        self.assertIn("not completed", show_error.call_args.args[1].lower())
        self.assertIn("access denied", show_error.call_args.args[2])
        self.assertIn("already", show_error.call_args.args[2])

    def test_full_reset_with_active_device_returns_access_controls_offline(self) -> None:
        settings = self._settings()
        window = self._window(settings)
        self._activate_device(window, "reset-device")
        window._settings_changed(profile_changed=True)
        self.assertTrue(window._privilege_profile_available)

        with (
            patch(
                "openadb.ui.main_window.exec_bounded_message_box",
                return_value=QMessageBox.Ok,
            ),
            patch.object(QMessageBox, "information"),
        ):
            window._reset_all_settings_and_caches()

        self.assertFalse(window._privilege_profile_available)
        self.assertFalse(window.privilege_mode_selector.has_backend())
        self.assertFalse(window.settings_page.check_privilege_button.isEnabled())
        self.assertFalse(window.commands_page.check_privilege_button.isEnabled())

        selector = window.privilege_mode_selector
        selector.setCurrentIndex(selector.findData("standard"))
        self.assertEqual(settings.pending_privilege_backend(), "standard")
        self.assertIn(
            "Next device: Standard ADB",
            window.privilege_runtime_status.full_text(),
        )

    def test_settings_recovery_warning_is_presented_once_with_preserved_path(
        self,
    ) -> None:
        settings_path = self.config_dir / "settings.json"
        settings_path.write_text("broken settings", encoding="utf-8")
        settings = IsolatedSettings(self.config_dir)
        window = self._window(settings)

        with patch.object(QMessageBox, "warning") as warning:
            window._show_pending_settings_recovery()
            window._show_pending_settings_recovery()

        warning.assert_called_once()
        self.assertEqual(warning.call_args.args[1], "Settings recovery")
        message = warning.call_args.args[2]
        self.assertIn("preserved at", message)
        self.assertIn("settings.corrupt-", message)
        self.assertIn("Device profiles, backups, and logs were not removed", message)

    def test_minimum_window_keeps_application_context_actions_unclipped(self) -> None:
        window = self._window()
        window._set_navigation_collapsed(True, persist=False)
        page = window.apps_page
        apps = [
            AppInfo(
                package_name="com.example.enabled",
                app_label="Enabled application",
                app_type="user",
                state="enabled",
            ),
            AppInfo(
                package_name="com.example.disabled",
                app_label="Disabled application",
                app_type="user",
                state="disabled",
            ),
        ]
        page.apps = apps
        page.table.set_apps_sorted(
            apps,
            "name",
            checked_packages={"com.example.enabled"},
        )
        page.apply_filter(save_state=False)
        window.open_page("Apps")
        window.resize(720, 600)
        window.show()

        for theme in ("Light", "Dark"):
            with self.subTest(theme=theme):
                apply_theme(self.app, theme)
                self.app.processEvents()

                self.assertEqual(window.width(), 720)
                self.assertLessEqual(page.width(), 620)
                self.assertEqual(page.refresh_button.text(), "Refresh")
                self.assertEqual(page.refresh_button.accessibleName(), "Refresh applications")
                self.assertEqual(page.page_actions_button.text(), "Page")
                self.assertEqual(page.select_all_check.text(), "Visible")
                self.assertEqual(
                    page.select_all_check.accessibleName(),
                    "Select visible applications",
                )
                self.assertTrue(page.active_filters_label.isHidden())
                self.assertGreater(page.total_label.width(), 0)
                self.assertEqual(page.total_label.toolTip(), "Showing 2 of 2 applications")
                self.assertEqual(page.selection_summary_label.full_text(), "1 selected")
                self.assertGreater(page.selection_summary_label.width(), 0)
                for control in (
                    page.refresh_button,
                    page.sort_button,
                    page.page_actions_button,
                    page.filters_button,
                    page.select_all_check,
                    page.backup_button,
                    page.enable_button,
                    page.disable_button,
                    page.uninstall_button,
                    page.more_button,
                ):
                    with self.subTest(control=control.text()):
                        self.assertGreaterEqual(
                            control.width(),
                            control.minimumSizeHint().width(),
                        )

    def test_runtime_settings_recovery_is_queued_to_the_ui_once(self) -> None:
        settings = self._settings()
        window = self._window(settings)
        settings.path.write_text("damaged after window startup", encoding="utf-8")

        with patch.object(QMessageBox, "warning") as warning:
            settings.set("show_warnings", False)
            self.app.processEvents()
            window._show_pending_settings_recovery()

        warning.assert_called_once()
        self.assertIn("settings.corrupt-", warning.call_args.args[2])

    def test_runtime_recovery_warnings_never_open_as_nested_modals(self) -> None:
        settings = self._settings()
        self._window(settings)
        depths: list[int] = []
        active_depth = 0

        def warning_side_effect(*_args) -> QMessageBox.StandardButton:
            nonlocal active_depth
            active_depth += 1
            depths.append(active_depth)
            try:
                if len(depths) == 1:
                    settings.path.write_text("second runtime damage", encoding="utf-8")
                    settings.set("show_warnings", False)
                    self.app.processEvents()
                return QMessageBox.Ok
            finally:
                active_depth -= 1

        settings.path.write_text("first runtime damage", encoding="utf-8")
        with patch.object(QMessageBox, "warning", side_effect=warning_side_effect) as warning:
            settings.set("theme", "Dark")
            for _index in range(4):
                self.app.processEvents()

        self.assertEqual(warning.call_count, 2)
        self.assertEqual(depths, [1, 1])

    def test_ui_reset_applies_to_current_pages_without_removing_operational_settings(self) -> None:
        settings = self._settings()
        settings.set("theme", "Dark")
        settings.set("apps_filter_type", "system")
        settings.set("platform_tools_path", "C:/keep/platform-tools")
        settings.set_global_values({"navigation_collapsed": True, "window_width": 760, "window_height": 520})
        window = self._window(settings)
        window._set_navigation_collapsed(True, persist=False)

        with (
            patch(
                "openadb.ui.main_window.exec_bounded_message_box",
                return_value=QMessageBox.Ok,
            ),
            patch.object(QMessageBox, "information"),
        ):
            window._reset_ui_settings()

        self.assertFalse(window.navigation_collapsed)
        self.assertEqual(window.settings_page.theme.currentText(), "System")
        self.assertEqual(window.apps_page._filter_values["type"], "all")
        self.assertEqual(settings.get("platform_tools_path"), "C:/keep/platform-tools")
        self.assertEqual(settings.get_global("window_width"), MainWindow.DEFAULT_WINDOW_SIZE.width())
        self.assertEqual(settings.get_global("window_height"), MainWindow.DEFAULT_WINDOW_SIZE.height())

    def test_identical_device_refresh_does_not_reload_open_file_manager_repeatedly(self) -> None:
        settings = self._settings()
        window = self._window(settings)
        window.stack.setCurrentWidget(window.file_manager_page)
        device = self._activate_device(window, "stable-device")
        with (
            patch.object(window, "_activate_device_profile", return_value=False),
            patch.object(window, "_schedule_acbridge_update", return_value=False),
            patch.object(window.file_manager_page, "refresh_all") as refresh_all,
        ):
            window._on_device_refreshed(device)

            settings.set("privilege_backend", "shizuku")
            status = PrivilegeStatus(
                backend=PrivilegeBackend.SHIZUKU,
                state="ready",
                uid=2000,
                level="shell",
                message="Shizuku shell (UID 2000, not root) is ready.",
                device_serial=device.serial,
                device_generation=window.device_manager.current_generation,
            )
            window.privilege_manager._cache_if_current(status)
            window._apply_privilege_status(status)

            window._on_device_refreshed(device)
            window._on_device_refreshed(device)

        refresh_all.assert_called_once_with()
        self.assertIs(window.privilege_manager.cached_status(), status)
        self.assertIn("UID 2000", window.privilege_runtime_status.full_text())
        self.assertIn("UID 2000", window.settings_page.privilege_status.text())
        self.assertIn("UID 2000", window.commands_page.privilege_status.text())
        self.assertIn(
            "UID 2000",
            window.file_manager_page.root_status_label.full_text(),
        )

    def test_close_cancels_operations_and_stops_owned_processes(self) -> None:
        window = self._window()
        with (
            patch.object(window.commands_page, "cancel_running_command") as cancel_command,
            patch.object(window.file_manager_page, "cancel_active_transfers") as cancel_transfers,
            patch.object(window.device_bar, "stop_device_monitor") as stop_monitor,
            patch.object(window.runner, "shutdown") as shutdown_runner,
        ):
            window.close()
        cancel_command.assert_called_once_with()
        cancel_transfers.assert_called_once_with()
        stop_monitor.assert_called_once_with()
        shutdown_runner.assert_called_once_with()
        self.assertTrue(window._closing)
        self.assertFalse(window.system_theme_controller.is_listening)
        for owner in (
            window,
            window.device_bar,
            window.apps_page,
            window.backups_page,
            window.file_manager_page,
            window.commands_page,
        ):
            self.assertTrue(owner._workers_shutting_down)

    def test_second_qr_pair_request_reuses_existing_dialog(self) -> None:
        window = self._window()
        dialog = MagicMock()
        window._wireless_qr_dialog = dialog
        with patch("openadb.ui.main_window.generate_wireless_qr_payload") as generate_payload:
            window.pair_wireless_adb_qr()
        generate_payload.assert_not_called()
        dialog.show.assert_called_once_with()
        dialog.raise_.assert_called_once_with()
        dialog.activateWindow.assert_called_once_with()

    def test_cancelled_qr_dialog_can_close_after_worker_finish_and_reopen(self) -> None:
        window = self._window()
        started = window._begin_wireless_attempt(
            action="qr",
            pairing_target="first-pairing-service",
        )
        self.assertIsNotNone(started)
        first_attempt, first_token = started
        first_dialog = MagicMock()
        window._wireless_qr_dialog = first_dialog

        first_token.cancel("user cancelled")
        window._wireless_qr_finished(first_attempt, first_token)
        window.device_manager.operations.finish(first_token)
        window._clear_wireless_qr_dialog(first_dialog)

        self.assertIsNone(window._wireless_qr_dialog)
        second_payload = MagicMock(
            service_name="second-pairing-service",
            password="fresh-password",
        )
        second_dialog = MagicMock()
        second_worker = MagicMock()
        with (
            patch(
                "openadb.ui.main_window.generate_wireless_qr_payload",
                return_value=second_payload,
            ) as generate_payload,
            patch(
                "openadb.ui.main_window.WirelessQrDialog",
                return_value=second_dialog,
            ),
            patch("openadb.ui.main_window.Worker", return_value=second_worker),
            patch("openadb.ui.main_window.start_worker", return_value=True),
        ):
            window.pair_wireless_adb_qr()

        generate_payload.assert_called_once_with()
        self.assertIs(window._wireless_qr_dialog, second_dialog)
        self.assertIsNotNone(window._wireless_attempt)
        self.assertIsNotNone(window._wireless_token)
        second_attempt = window._wireless_attempt
        second_token = window._wireless_token
        assert second_attempt is not None
        assert second_token is not None
        self.assertIsNot(second_token, first_token)
        self.assertFalse(second_token.cancelled)

        window._wireless_qr_finished(second_attempt, second_token)
        window.device_manager.operations.finish(second_token)
        window._clear_wireless_qr_dialog(second_dialog)

    def test_late_old_qr_dialog_close_does_not_clear_new_dialog(self) -> None:
        window = self._window()
        old_dialog = MagicMock()
        new_dialog = MagicMock()
        window._wireless_qr_dialog = new_dialog

        window._clear_wireless_qr_dialog(old_dialog)

        self.assertIs(window._wireless_qr_dialog, new_dialog)

    def test_qr_pairing_suspends_offline_reconnect_until_result(self) -> None:
        window = self._window()
        payload = MagicMock(service_name="service", password="password")
        dialog = MagicMock()
        dialog.status.text.return_value = "Connection was not ready"
        dialog.status.full_text.return_value = "Connection was not ready"
        worker = MagicMock()
        result = MagicMock(
            spec=CommandResult,
            success=False,
            status="Connection was not ready",
            stdout="",
            stderr="",
        )
        with (
            patch("openadb.ui.main_window.generate_wireless_qr_payload", return_value=payload),
            patch("openadb.ui.main_window.WirelessQrDialog", return_value=dialog),
            patch("openadb.ui.main_window.Worker", return_value=worker),
            patch("openadb.ui.main_window.start_worker"),
            patch.object(window.device_bar, "set_offline_reconnect_suspended") as suspend,
            patch.object(window.device_bar, "refresh") as refresh,
            patch.object(QMessageBox, "warning"),
        ):
            window.pair_wireless_adb_qr()
            suspend.assert_called_once_with(True)
            window._wireless_qr_result(dialog, result)

        self.assertEqual(suspend.call_args_list[-1].args, (False,))
        refresh.assert_called_once_with()

    def test_qr_result_extracts_mdns_wireless_serial(self) -> None:
        window = self._window()
        serial = "adb-3A131FDJG000SZ-example._adb-tls-connect._tcp"
        result = MagicMock(spec=CommandResult, stdout=f"connected device: {serial}", stderr="", status="")

        self.assertEqual(window._wireless_target_from_result(result), serial)

    def test_disconnect_prefers_active_mdns_serial_over_stale_form_target(self) -> None:
        window = self._window()
        serial = "adb-3A131FDJG000SZ-example._adb-tls-connect._tcp"
        window.device_manager.active = DeviceInfo(serial=serial, mode="ADB", state="device")
        with (
            patch.object(window.adb, "disconnect_wireless") as disconnect,
            patch.object(
                window,
                "_run_wireless_worker",
                side_effect=lambda fn, *_args, **_kwargs: fn(threading.Event()),
            ),
        ):
            window.disconnect_wireless_adb("192.0.2.59", 40765)

        disconnect.assert_called_once_with(serial, None, cancel_event=ANY)

    def test_disconnect_prefers_active_ip_transport_over_stale_form_target(self) -> None:
        window = self._window()
        serial = "192.0.2.59:40765"
        window.device_manager.active = DeviceInfo(serial=serial, mode="ADB", state="device")
        with (
            patch.object(window.adb, "disconnect_wireless") as disconnect,
            patch.object(
                window,
                "_run_wireless_worker",
                side_effect=lambda fn, *_args, **_kwargs: fn(threading.Event()),
            ),
        ):
            window.disconnect_wireless_adb("192.0.2.59", 5555)

        disconnect.assert_called_once_with(serial, None, cancel_event=ANY)

    def test_connect_uses_mdns_serial_without_appending_form_port(self) -> None:
        window = self._window()
        serial = "adb-3A131FDJG000SZ-example._adb-tls-connect._tcp"
        with (
            patch.object(window.adb, "connect_wireless_target") as connect_target,
            patch.object(window.adb, "connect_wireless") as connect_host,
            patch.object(
                window,
                "_run_wireless_worker",
                side_effect=lambda fn, *_args, **_kwargs: fn(threading.Event()),
            ),
        ):
            window.connect_wireless_adb(serial, 40765)

        connect_target.assert_called_once_with(serial, cancel_event=ANY)
        connect_host.assert_not_called()

    def test_dashboard_reboot_is_bound_and_stale_worker_never_executes(self) -> None:
        window = self._window()
        self._activate_device(window, "device-a")
        bound = MagicMock()
        bound.run_raw.return_value = MagicMock(spec=CommandResult, status="Success")
        captured = []

        def fake_start(_owner, _pool, worker, **kwargs):
            registry = kwargs.get("operation_registry")
            token = kwargs.get("operation_token")
            if registry is not None and token is not None:
                worker.add_finalizer(lambda: registry.finish(token))
            captured.append(worker)

        with (
            patch.object(window.adb, "for_context", return_value=bound) as for_context,
            patch("openadb.ui.main_window.start_worker", side_effect=fake_start),
            patch.object(QMessageBox, "information") as information,
        ):
            window.run_dashboard_command("adb_reboot")
            context = for_context.call_args.args[0]
            self.assertEqual(context.serial, "device-a")
            window.device_manager._set_active(
                DeviceInfo(serial="device-b", mode="ADB", state="device", transport_id="2"),
                reason="test device switch",
            )
            captured[0].run()
            self.app.processEvents()

        bound.run_raw.assert_not_called()
        information.assert_not_called()

    def test_dashboard_context_change_during_registration_prevents_worker_start(self) -> None:
        window = self._window()
        self._activate_device(window, "device-a")
        context = window.device_manager.require_context(("ADB",))

        with (
            patch.object(window.device_manager, "is_context_current", return_value=False),
            patch("openadb.ui.main_window.start_worker") as start_worker,
        ):
            window._start_dashboard_command(lambda _cancel_event: None, context=context)

        start_worker.assert_not_called()
        self.assertEqual(window.device_manager.operations.active_count, 0)

    def test_dashboard_global_devices_result_survives_generation_change(self) -> None:
        window = self._window()
        self._activate_device(window, "device-a")
        result = MagicMock(spec=CommandResult, status="Success")
        captured = []

        def fake_start(_owner, _pool, worker, **kwargs):
            registry = kwargs.get("operation_registry")
            token = kwargs.get("operation_token")
            if registry is not None and token is not None:
                worker.add_finalizer(lambda: registry.finish(token))
            captured.append(worker)

        with (
            patch.object(window.adb, "run_raw", return_value=result) as run_raw,
            patch("openadb.ui.main_window.start_worker", side_effect=fake_start),
            patch.object(QMessageBox, "information") as information,
        ):
            window.run_dashboard_command("adb_devices")
            window.device_manager._set_active(
                DeviceInfo(serial="device-b", mode="ADB", state="device", transport_id="2"),
                reason="test device switch",
            )
            captured[0].run()
            self.app.processEvents()

        run_raw.assert_called_once_with(
            ["devices", "-l"],
            use_serial=False,
            cancel_event=ANY,
        )
        information.assert_called_once_with(window, "Command", "Success")

    def test_wireless_attempt_survives_generation_and_rejects_offline_transport(self) -> None:
        window = self._window()
        started = window._begin_wireless_attempt(
            action="connect",
            expected_host="demo.local",
            connect_target="demo.local:37123",
            expected_connect_port=37123,
            expected_ready_serials=("demo.local:37123",),
        )
        self.assertIsNotNone(started)
        attempt, token = started

        window.device_manager.operations.cancel_stale(
            window.device_manager.current_generation + 1,
            "new wireless transport",
        )

        self.assertFalse(token.cancelled)
        self.assertTrue(window._wireless_attempt_is_current(attempt, token))
        self.assertFalse(
            window._attempt_accepts_transport(
                attempt,
                DeviceInfo(serial="demo.local:37123", mode="Offline", state="offline"),
            )
        )
        self.assertTrue(
            window._attempt_accepts_transport(
                attempt,
                DeviceInfo(serial="demo.local:37123", mode="ADB", state="device"),
            )
        )
        window._wireless_attempt_finished(attempt, token)
        window.device_manager.operations.finish(token)

    def test_cancelled_wireless_attempt_does_not_invoke_queued_command(self) -> None:
        window = self._window()
        command = MagicMock()
        captured = []

        def fake_start(_owner, _pool, worker, **kwargs):
            registry = kwargs.get("operation_registry")
            token = kwargs.get("operation_token")
            if registry is not None and token is not None:
                worker.add_finalizer(lambda: registry.finish(token))
            captured.append(worker)

        with patch("openadb.ui.main_window.start_worker", side_effect=fake_start):
            window._run_wireless_worker(command, "Wireless ADB connect")

        self.assertIsNotNone(window._wireless_token)
        window._wireless_token.cancel("test cancellation before worker execution")
        captured[0].run()
        self.app.processEvents()

        command.assert_not_called()
        self.assertIsNone(window._wireless_attempt)
        self.assertEqual(window.device_manager.operations.active_count, 0)

    def test_wireless_worker_forwards_its_token_event_into_running_command(self) -> None:
        window = self._window()
        captured = []
        received_events: list[threading.Event] = []

        def fake_start(_owner, _pool, worker, **kwargs):
            registry = kwargs.get("operation_registry")
            token = kwargs.get("operation_token")
            if registry is not None and token is not None:
                worker.add_finalizer(lambda: registry.finish(token))
            captured.append(worker)

        def command(cancel_event: threading.Event):
            received_events.append(cancel_event)
            cancel_event.set()
            return MagicMock(spec=CommandResult, success=False)

        with (
            patch("openadb.ui.main_window.start_worker", side_effect=fake_start),
            patch.object(QMessageBox, "warning") as warning,
        ):
            window._run_wireless_worker(command, "Wireless ADB connect")
            token = window._wireless_token
            self.assertIsNotNone(token)
            captured[0].run()
            self.app.processEvents()

        self.assertEqual(received_events, [token.cancel_event])
        warning.assert_not_called()
        self.assertIsNone(window._wireless_attempt)
        self.assertEqual(window.device_manager.operations.active_count, 0)

    def test_wireless_readiness_poll_forwards_attempt_cancel_event(self) -> None:
        window = self._window()
        started = window._begin_wireless_attempt(
            action="connect",
            connect_target="demo.local:37123",
            expected_ready_serials=("demo.local:37123",),
        )
        self.assertIsNotNone(started)
        attempt, token = started
        received_events: list[threading.Event] = []
        result = MagicMock(spec=CommandResult, success=True, error_type="", status="Success")

        def list_devices(*, cancel_event: threading.Event):
            received_events.append(cancel_event)
            cancel_event.set()
            return []

        with patch.object(window.adb, "list_devices", side_effect=list_devices):
            window._wait_for_expected_wireless_transport(attempt, token, result)

        self.assertEqual(received_events, [token.cancel_event])
        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "cancelled")
        window._wireless_attempt_finished(attempt, token)
        window.device_manager.operations.finish(token)

    def test_wireless_discovery_forwards_registry_cancel_event(self) -> None:
        window = self._window()
        captured = []
        received_events: list[threading.Event] = []

        def fake_start(_owner, _pool, worker, **kwargs):
            registry = kwargs.get("operation_registry")
            token = kwargs.get("operation_token")
            if registry is not None and token is not None:
                worker.add_finalizer(lambda: registry.finish(token))
            captured.append(worker)

        def discover(*, wait_seconds: float, cancel_event: threading.Event):
            self.assertEqual(wait_seconds, 2.5)
            received_events.append(cancel_event)
            return []

        with (
            patch.object(
                window.adb,
                "discover_wireless_connect_services",
                side_effect=discover,
            ),
            patch("openadb.ui.main_window.start_worker", side_effect=fake_start),
            patch.object(QMessageBox, "warning") as warning,
        ):
            window.scan_wireless_android_tv()
            token = window._wireless_discovery_token
            self.assertIsNotNone(token)
            token.cancel("test cancellation")
            captured[0].run()
            self.app.processEvents()

        self.assertEqual(received_events, [token.cancel_event])
        warning.assert_not_called()
        self.assertIsNone(window._wireless_discovery_token)
        self.assertEqual(window.device_manager.operations.active_count, 0)

    def test_discovered_short_mdns_name_accepts_full_ready_serial(self) -> None:
        window = self._window()
        started = window._begin_wireless_attempt(
            action="connect",
            connect_target="adb-demo",
            expected_ready_serials=("adb-demo", "192.0.2.20:37123"),
        )
        self.assertIsNotNone(started)
        attempt, token = started

        self.assertTrue(
            window._attempt_accepts_transport(
                attempt,
                DeviceInfo(
                    serial="adb-demo._adb-tls-connect._tcp",
                    mode="ADB",
                    state="device",
                ),
            )
        )
        window._wireless_attempt_finished(attempt, token)
        window.device_manager.operations.finish(token)

    def test_qr_finished_refreshes_before_releasing_offline_suspension(self) -> None:
        window = self._window()
        started = window._begin_wireless_attempt(action="qr", pairing_target="pairing")
        self.assertIsNotNone(started)
        attempt, token = started

        with patch.object(window.device_bar, "refresh_after_wireless_pairing") as refresh:
            window._wireless_qr_finished(attempt, token)

        refresh.assert_called_once_with()
        self.assertIsNone(window._wireless_attempt)
        window.device_manager.operations.finish(token)

    def test_late_qr_callbacks_cannot_update_new_attempt(self) -> None:
        window = self._window()
        first = window._begin_wireless_attempt(action="qr", pairing_target="first")
        self.assertIsNotNone(first)
        first_attempt, first_token = first
        first_dialog = MagicMock()
        window._wireless_attempt_finished(first_attempt, first_token)
        window.device_manager.operations.finish(first_token)

        second = window._begin_wireless_attempt(action="qr", pairing_target="second")
        self.assertIsNotNone(second)
        second_attempt, second_token = second
        window.dashboard.set_wireless_status("Second attempt")
        window._wireless_qr_progress(
            first_attempt,
            first_token,
            first_dialog,
            "stale first progress",
        )

        first_dialog.set_status.assert_not_called()
        self.assertEqual(window.dashboard.wireless_message.text(), "Second attempt")
        window._wireless_attempt_finished(second_attempt, second_token)
        window.device_manager.operations.finish(second_token)


if __name__ == "__main__":
    unittest.main()
