from __future__ import annotations

import os
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialogButtonBox

from openadb.core.device import DeviceManager
from openadb.models.device_info import DeviceInfo
from openadb.ui.device_status_bar import DeviceDetailsDialog, DeviceStatusBar
from openadb.ui.main_window import MainWindow
from openadb.ui.style import apply_theme
from openadb.ui.widgets.device_picker_dialog import DevicePickerDialog


class MemorySettings:
    def __init__(self, **values) -> None:
        self.data = {"auto_refresh_device": False, "refresh_interval_seconds": 8, **values}

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value, save: bool = True) -> None:
        self.data[key] = value


class FakeAdb:
    def __init__(self, devices: list[DeviceInfo] | None = None) -> None:
        self.devices = list(devices or [])
        self.serial = ""
        self.platform_tools = SimpleNamespace(active=SimpleNamespace(has_adb=True))

    def list_devices(self) -> list[DeviceInfo]:
        return list(self.devices)

    def get_device_info(self, serial: str) -> DeviceInfo:
        device = next(device for device in self.devices if device.serial == serial)
        return replace(device, model=device.model or f"Detailed {serial}")

    def set_serial(self, serial: str) -> None:
        self.serial = serial

    def track_devices(self, output_callback=None, cancel_event=None):
        return None


class FakeFastboot:
    def __init__(self, devices: list[DeviceInfo] | None = None) -> None:
        self.devices = list(devices or [])
        self.serial = ""

    def list_devices(self) -> list[DeviceInfo]:
        return list(self.devices)

    def set_serial(self, serial: str) -> None:
        self.serial = serial


class FakeDeviceManager:
    def __init__(self) -> None:
        self.adb = FakeAdb()
        self.active = DeviceInfo()
        self.devices: list[DeviceInfo] = []
        self.refresh_calls = 0
        self.reconnect_calls: list[tuple[str, int]] = []

    def refresh(self) -> DeviceInfo:
        self.refresh_calls += 1
        return self.active

    def reconnect_offline(self, serial: str, attempts: int, progress_callback=None) -> DeviceInfo:
        self.reconnect_calls.append((serial, attempts))
        return self.active


def device(serial: str, mode: str = "ADB", **values) -> DeviceInfo:
    defaults = {
        "model": f"Model {serial}",
        "manufacturer": "Example",
        "android_version": "16",
        "sdk_version": "36",
        "state": "device" if mode == "ADB" else mode.lower(),
        "form_factor": "Phone",
    }
    defaults.update(values)
    return DeviceInfo(serial=serial, mode=mode, **defaults)


class DeviceStatusBarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.manager = FakeDeviceManager()
        self.bar = DeviceStatusBar(self.manager, MemorySettings())
        self.bar.resize(700, 54)
        self.bar.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.bar.stop_device_monitor()
        self.bar.close()
        self.bar.deleteLater()
        self.app.processEvents()

    def test_all_required_modes_have_text_status_mode_and_short_state(self) -> None:
        cases = [
            (DeviceInfo(mode="Checking", state="checking"), "Checking", "Checking"),
            (DeviceInfo(mode="No device", state="none"), "No device", "Disconnected"),
            (device("unauthorized", "Unauthorized"), "Authorization required", "Unauthorized"),
            (device("offline", "Offline"), "Offline", "Offline"),
            (device("adb"), "Connected", "ADB"),
            (device("recovery", "Recovery"), "Connected", "Recovery"),
            (device("fastboot", "Fastboot"), "Connected", "Fastboot"),
        ]
        with patch("openadb.ui.device_status_bar.start_worker"):
            for current, status, mode in cases:
                with self.subTest(mode=current.mode):
                    self.manager.devices = [current] if current.serial else []
                    self.bar.set_device(current)
                    self.assertEqual(self.bar.summary.text(), status)
                    self.assertEqual(self.bar.mode_label.text(), mode)
                    self.assertTrue(self.bar.state_label.full_text())
                    self.assertIn(status, self.bar.dot.accessibleName())
                    if current.mode == "Offline":
                        self.bar._offline_reconnect_finished()

    def test_long_name_and_technical_values_are_elided_but_fully_available(self) -> None:
        long_model = "Very long Android model name " * 20
        long_serial = "SERIAL-" * 60
        current = device(
            long_serial,
            model=long_model,
            manufacturer="Long Manufacturer",
            android_version="16.0.0-build-with-a-long-name",
            sdk_version="36",
            product="product-code",
            transport_id="12345",
        )
        self.manager.devices = [current]
        self.bar.resize(620, 54)
        self.bar.set_device(current)
        self.app.processEvents()
        self.assertEqual(self.bar.device_name.full_text(), long_model)
        self.assertEqual(self.bar.device_name.toolTip(), long_model)
        self.assertNotEqual(self.bar.device_name.text(), long_model)
        self.assertIn(long_serial, self.bar.details_button.toolTip())

        dialog = DeviceDetailsDialog(current)
        try:
            self.assertEqual(dialog.fields["serial"].text(), long_serial)
            self.assertEqual(dialog.fields["serial"].toolTip(), long_serial)
            dialog.copy_details()
            self.assertIn(long_serial, QApplication.clipboard().text())
            self.assertIn("Long Manufacturer", dialog.detail_text())
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_device_selector_only_appears_when_useful(self) -> None:
        first = device("one", model="First phone")
        second = device("two", model="Second phone")
        self.manager.devices = [first]
        self.bar.set_device(first)
        self.assertTrue(self.bar.device_button.isHidden())

        self.manager.devices = [first, second]
        self.bar.set_device(first)
        self.assertFalse(self.bar.device_button.isHidden())
        self.assertEqual(self.bar.device_button.text(), "Devices (2)")
        self.assertIn("First phone", self.bar.device_button.toolTip())
        emitted: list[bool] = []
        self.bar.choose_device_requested.connect(lambda: emitted.append(True))
        self.bar.device_button.click()
        self.assertEqual(emitted, [True])

        self.manager.devices = [second]
        self.bar.set_device(DeviceInfo(mode="No device", state="selection_required"))
        self.assertFalse(self.bar.device_button.isHidden())
        self.assertEqual(self.bar.device_button.text(), "Choose device")
        self.assertEqual(self.bar.summary.text(), "Selection required")

    def test_refresh_monitor_and_offline_reconnect_keep_duplicate_guards(self) -> None:
        with patch("openadb.ui.device_status_bar.start_worker") as start_worker:
            self.bar.refresh()
            self.bar.refresh()
            self.assertEqual(start_worker.call_count, 1)
            self.bar._refresh_finished()

            self.bar.start_device_monitor()
            self.bar.start_device_monitor()
            self.assertEqual(start_worker.call_count, 2)
            cancel_event = self.bar._device_monitor_cancel_event
            self.assertIsNotNone(cancel_event)
            self.bar.stop_device_monitor()
            self.assertTrue(cancel_event.is_set())

        offline = device("offline", "Offline")
        self.manager.devices = [offline]
        with patch("openadb.ui.device_status_bar.start_worker") as start_worker:
            self.bar.set_device(offline)
            self.bar.set_device(offline)
            self.assertEqual(start_worker.call_count, 1)
            self.assertEqual(self.bar.state_label.full_text(), "Trying to reconnect")
            self.bar._set_reconnect_progress("Device offline. Reconnect attempt 2/4...")
            self.assertIn("2/4", self.bar.state_label.full_text())
            self.bar._offline_reconnect_complete(offline)
            self.bar._offline_reconnect_finished()
            self.bar.set_device(offline)
            self.assertEqual(start_worker.call_count, 1)

    def test_qr_pairing_suspends_transient_offline_reconnect(self) -> None:
        offline = device("adb-transient._adb-tls-connect._tcp", "Offline")
        self.manager.devices = [offline]
        with patch("openadb.ui.device_status_bar.start_worker") as start_worker:
            self.bar.set_offline_reconnect_suspended(True)
            self.bar.set_device(offline)
            self.assertEqual(start_worker.call_count, 0)
            self.assertFalse(self.bar._offline_reconnect_running)

            self.bar.set_offline_reconnect_suspended(False)
            self.bar.set_device(offline)
            self.assertEqual(start_worker.call_count, 0)

            self.bar.set_device(DeviceInfo(mode="No device", state="none"))
            self.bar.set_device(offline)
            self.assertEqual(start_worker.call_count, 1)
            self.bar._offline_reconnect_finished()

    def test_narrow_bar_renders_in_all_themes(self) -> None:
        first = device("one", model="Long phone name " * 12)
        second = device("two")
        self.manager.devices = [first, second]
        self.bar.set_device(first)
        self.bar.resize(620, 54)
        for theme in ("System", "Light", "Dark"):
            with self.subTest(theme=theme):
                apply_theme(self.app, theme)
                self.app.processEvents()
                self.assertEqual(self.bar.width(), 620)
                self.assertFalse(self.bar.grab().isNull())
                self.assertLessEqual(self.bar.minimumSizeHint().width(), 700)


class DeviceManagerSelectionTests(unittest.TestCase):
    def test_multiple_devices_without_saved_choice_require_explicit_selection(self) -> None:
        first = device("one")
        second = device("two")
        settings = MemorySettings()
        adb = FakeAdb([first, second])
        fastboot = FakeFastboot()
        manager = DeviceManager(adb, fastboot, settings)
        active = manager.refresh()
        self.assertEqual(active.mode, "No device")
        self.assertEqual(active.state, "selection_required")
        self.assertEqual(active.serial, "")
        self.assertEqual(adb.serial, "")

        selected = manager.choose("two")
        self.assertEqual(selected.serial, "two")
        self.assertEqual(settings.get("active_device_serial"), "two")

    def test_disconnect_does_not_silently_switch_to_another_device(self) -> None:
        first = device("one")
        second = device("two")
        settings = MemorySettings(active_device_serial="one")
        adb = FakeAdb([first, second])
        fastboot = FakeFastboot()
        manager = DeviceManager(adb, fastboot, settings)
        self.assertEqual(manager.refresh().serial, "one")
        adb.devices = [second]
        active = manager.refresh()
        self.assertEqual(active.state, "selection_required")
        self.assertEqual(active.serial, "")
        self.assertEqual(adb.serial, "")
        self.assertEqual(fastboot.serial, "")

    def test_single_device_and_saved_device_restore_existing_behavior(self) -> None:
        first = device("one")
        second = device("two")
        settings = MemorySettings()
        manager = DeviceManager(FakeAdb([first]), FakeFastboot(), settings)
        self.assertEqual(manager.refresh().serial, "one")

        settings = MemorySettings(active_device_serial="two")
        manager = DeviceManager(FakeAdb([first, second]), FakeFastboot(), settings)
        self.assertEqual(manager.refresh().serial, "two")


class DevicePickerDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_current_device_is_selected_and_long_values_have_tooltips(self) -> None:
        first = device("SERIAL-" * 30, model="First")
        second = device("two", model="Second")
        dialog = DevicePickerDialog([first, second], active_serial="two")
        try:
            self.assertEqual(dialog.selected_serial(), "two")
            self.assertEqual(dialog.table.item(1, 0).text(), "Current")
            self.assertEqual(dialog.table.item(0, 3).toolTip(), first.serial)
            self.assertTrue(dialog.buttons.button(QDialogButtonBox.Ok).isEnabled())
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_no_implicit_row_is_selected_when_active_device_is_missing(self) -> None:
        first = device("one")
        second = device("two")
        dialog = DevicePickerDialog([first, second])
        try:
            self.assertEqual(dialog.selected_serial(), "")
            self.assertFalse(dialog.buttons.button(QDialogButtonBox.Ok).isEnabled())
            dialog.table.selectRow(0)
            self.app.processEvents()
            self.assertEqual(dialog.selected_serial(), "one")
            self.assertTrue(dialog.buttons.button(QDialogButtonBox.Ok).isEnabled())
        finally:
            dialog.close()
            dialog.deleteLater()


class ManualDeviceChoiceFlowTests(unittest.TestCase):
    def test_refresh_with_ambiguous_devices_does_not_open_a_modal_picker(self) -> None:
        current_page = object()
        target = SimpleNamespace(
            _activate_device_profile=MagicMock(return_value=False),
            dashboard=MagicMock(),
            apps_page=MagicMock(),
            file_manager_page=object(),
            stack=MagicMock(),
        )
        target.stack.currentWidget.return_value = current_page
        ambiguous = DeviceInfo(mode="No device", state="selection_required")
        with patch("openadb.ui.main_window.DevicePickerDialog") as dialog_class:
            MainWindow._on_device_refreshed(target, ambiguous)
        dialog_class.assert_not_called()
        target.dashboard.update_device.assert_called_once_with(ambiguous)
        target.apps_page.update_device_state.assert_called_once_with(ambiguous)

    def test_main_window_applies_only_the_explicitly_selected_device(self) -> None:
        first = device("one")
        second = device("two")
        manager = MagicMock()
        manager.devices = [first, second]
        manager.active = DeviceInfo(mode="No device", state="selection_required")
        manager.choose.return_value = second
        target = SimpleNamespace(
            device_manager=manager,
            device_bar=MagicMock(),
            _on_device_refreshed=MagicMock(),
        )
        dialog = MagicMock()
        dialog.exec.return_value = True
        dialog.selected_serial.return_value = "two"
        with patch("openadb.ui.main_window.DevicePickerDialog", return_value=dialog) as dialog_class:
            MainWindow.choose_active_device(target)
        dialog_class.assert_called_once_with([first, second], active_serial="", parent=target)
        manager.choose.assert_called_once_with("two")
        target.device_bar.set_device.assert_called_once_with(second)
        target._on_device_refreshed.assert_called_once_with(second)


if __name__ == "__main__":
    unittest.main()
