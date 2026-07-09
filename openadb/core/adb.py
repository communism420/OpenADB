from __future__ import annotations

import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Callable

from openadb.models.app_info import AppInfo
from openadb.models.command_result import CommandResult
from openadb.models.device_info import DeviceInfo
from openadb.models.file_item import FileItem
from openadb.models.storage_volume import StorageVolume

from .command_runner import CommandRunner
from .path_utils import ensure_dir, format_bytes, join_android_path, safe_filename, shell_quote
from .platform_tools import PlatformToolsManager

try:
    from zeroconf import ServiceBrowser, Zeroconf
except ImportError:  # pragma: no cover - optional fallback dependency
    ServiceBrowser = None
    Zeroconf = None


class ADBClient:
    def __init__(self, platform_tools: PlatformToolsManager, runner: CommandRunner) -> None:
        self.platform_tools = platform_tools
        self.runner = runner
        self.serial: str = ""

    def set_serial(self, serial: str) -> None:
        self.serial = serial or ""

    def _base(self, serial: str | None = None) -> list[str]:
        adb = self.platform_tools.adb_path
        command = [str(adb) if adb else "adb"]
        selected = serial if serial is not None else self.serial
        if selected:
            command.extend(["-s", selected])
        return command

    def run_raw(self, args: list[str], timeout: int | float | None = 120, use_serial: bool = True) -> CommandResult:
        command = self._base() if use_serial else self._base(serial="")
        command.extend(args)
        return self.runner.run(command, timeout=timeout)

    def reconnect_offline_device(self, serial: str = "") -> CommandResult:
        selected = (serial or self.serial or "").strip()
        if selected:
            command = self._base(serial=selected)
            command.append("reconnect")
            result = self.runner.run(command, timeout=20)
            if result.success:
                return result
        return self.run_raw(["reconnect", "offline"], timeout=20, use_serial=False)

    def track_devices(self, output_callback=None, cancel_event: threading.Event | None = None) -> CommandResult:
        return self.run_raw_streaming(
            ["track-devices"],
            timeout=None,
            use_serial=False,
            output_callback=output_callback,
            cancel_event=cancel_event,
        )

    def run_raw_binary_output(
        self,
        args: list[str],
        timeout: int | float | None = 120,
        use_serial: bool = True,
    ) -> tuple[CommandResult, bytes]:
        command = self._base() if use_serial else self._base(serial="")
        command.extend(args)
        return self.runner.run_binary_output(command, timeout=timeout)

    def run_raw_streaming(
        self,
        args: list[str],
        timeout: int | float | None = 120,
        use_serial: bool = True,
        output_callback=None,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        command = self._base() if use_serial else self._base(serial="")
        command.extend(args)
        return self.runner.run_streaming(command, timeout=timeout, output_callback=output_callback, cancel_event=cancel_event)

    def run_raw_with_input_stream(
        self,
        args: list[str],
        input_writer: Callable[[BinaryIO], None],
        timeout: int | float | None = None,
        use_serial: bool = True,
        output_callback=None,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        command = self._base() if use_serial else self._base(serial="")
        command.extend(args)
        return self.runner.run_with_input_stream(
            command,
            input_writer=input_writer,
            timeout=timeout,
            output_callback=output_callback,
            cancel_event=cancel_event,
        )

    def run_raw_binary_output_to_file(
        self,
        args: list[str],
        destination: str | Path,
        timeout: int | float | None = None,
        use_serial: bool = True,
        output_callback=None,
        progress_callback=None,
        cancel_event: threading.Event | None = None,
        buffer_size: int = 1024 * 1024,
    ) -> CommandResult:
        command = self._base() if use_serial else self._base(serial="")
        command.extend(args)
        return self.runner.run_binary_output_to_file(
            command,
            destination=destination,
            timeout=timeout,
            output_callback=output_callback,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            buffer_size=buffer_size,
        )

    def run_shell(self, shell_command: str, timeout: int | float | None = 120) -> CommandResult:
        return self.run_raw(["shell", shell_command], timeout=timeout)

    def tcpip(self, port: int = 5555) -> CommandResult:
        port = _normalize_tcp_port(port, "TCP/IP port")
        return self.run_raw(["tcpip", str(port)], timeout=30, use_serial=True)

    def connect_wireless(self, host: str, port: int = 5555) -> CommandResult:
        target = _wireless_target(host, port)
        return _normalize_adb_connect_result(self.run_raw(["connect", target], timeout=35, use_serial=False), target)

    def disconnect_wireless(self, host: str = "", port: int | None = None) -> CommandResult:
        args = ["disconnect"]
        host = str(host or "").strip()
        if host:
            args.append(_wireless_target(host, port))
        return self.run_raw(args, timeout=30, use_serial=False)

    def pair_wireless(self, host: str, port: int, pairing_code: str) -> CommandResult:
        target = _wireless_target(host, port)
        code = str(pairing_code or "").strip()
        if not code:
            raise ValueError("Wireless debugging pairing code is empty.")
        return _normalize_adb_pair_result(self.run_raw(["pair", target, code], timeout=45, use_serial=False), target)

    def pair_wireless_target(self, target: str, pairing_code: str) -> CommandResult:
        target = str(target or "").strip()
        code = str(pairing_code or "").strip()
        if not target:
            raise ValueError("Wireless debugging pairing target is empty.")
        if not code:
            raise ValueError("Wireless debugging pairing code is empty.")
        return _normalize_adb_pair_result(self.run_raw(["pair", target, code], timeout=45, use_serial=False), target)

    def connect_wireless_target(self, target: str, timeout: int | float = 35) -> CommandResult:
        target = str(target or "").strip()
        if not target:
            raise ValueError("Wireless debugging connection target is empty.")
        return _normalize_adb_connect_result(self.run_raw(["connect", target], timeout=timeout, use_serial=False), target)

    def mdns_services(self) -> list[dict[str, str]]:
        result = self.run_raw(["mdns", "services"], timeout=10, use_serial=False)
        if not result.success:
            return []
        return _parse_mdns_services(result.stdout)

    def discover_wireless_connect_services(self, wait_seconds: float = 2.0) -> list[dict[str, str]]:
        self.run_raw(["start-server"], timeout=10, use_serial=False)
        services = self._discover_wireless_mdns_services(wait_seconds=wait_seconds)
        return _wireless_connect_service_records(services)

    def pair_wireless_qr(
        self,
        service_name: str,
        password: str,
        timeout: int = 90,
        progress_callback=None,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        service_name = str(service_name or "").strip()
        password = str(password or "").strip()
        if not service_name:
            raise ValueError("QR pairing service name is empty.")
        if not password:
            raise ValueError("QR pairing password is empty.")

        started = datetime.now()
        deadline = time.monotonic() + max(10, int(timeout))
        pairing_target = ""
        before_wireless_serials = self._wireless_device_serials()
        self.run_raw(["start-server"], timeout=10, use_serial=False)
        self._emit_wireless_qr_progress(
            progress_callback,
            "Scan the QR code on the phone. Waiting for Android Wireless debugging pairing service...",
        )

        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return _synthetic_result(
                    self._base(serial="") + ["qr-pair", service_name],
                    started,
                    False,
                    "QR pairing cancelled",
                    error_type="cancelled",
                )

            services = self._discover_wireless_mdns_services(wait_seconds=0.8)
            pairing = _find_mdns_service(services, service_name, "_adb-tls-pairing._tcp")
            if not pairing:
                self._emit_wireless_qr_progress(
                    progress_callback,
                    "QR is visible. Waiting for the phone's pairing service. Keep the QR screen open on Android...",
                )
                time.sleep(0.25)
                continue

            pairing_target = pairing["target"]
            self._emit_wireless_qr_progress(
                progress_callback,
                f"Pairing service found at {pairing_target} through {pairing.get('source', 'mDNS')}. Running adb pair...",
            )
            pair_result = self.pair_wireless_target(pairing_target, password)
            if not pair_result.success:
                pair_result.status = (
                    pair_result.status
                    + "\n\nThe phone may stay on the QR pairing screen until you close it. "
                    "Check that Windows Firewall/router does not block local mDNS/TCP traffic."
                )
                return pair_result

            auto_device = self._wait_for_new_wireless_device(
                before_wireless_serials,
                deadline,
                progress_callback,
                cancel_event,
                seconds=4.0,
            )
            if auto_device:
                return _synthetic_result(
                    self._base(serial="") + ["qr-pair", service_name],
                    started,
                    True,
                    f"QR pairing succeeded. Wireless ADB device is already connected: {auto_device}",
                    stdout=f"connected device: {auto_device}",
                )

            connect_candidates = self._wait_for_wireless_connect_candidates(pairing_target, deadline, progress_callback, cancel_event)
            if not connect_candidates:
                auto_device = self._new_wireless_device_serial(before_wireless_serials)
                if auto_device:
                    return _synthetic_result(
                        self._base(serial="") + ["qr-pair", service_name],
                        started,
                        True,
                        f"QR pairing succeeded. Wireless ADB device is already connected: {auto_device}",
                        stdout=f"connected device: {auto_device}",
                    )
                pair_result.status = (
                    "QR pairing succeeded, but the wireless ADB connection service was not found. "
                    "Enter the connection port manually or press Connect if it is already filled."
                )
                return pair_result

            connect_result = self._connect_wireless_qr_target_until_ready(
                connect_candidates,
                pairing_target,
                before_wireless_serials,
                deadline,
                progress_callback,
                cancel_event,
            )
            if connect_result.success:
                connect_result.status = "QR pairing and wireless ADB connection succeeded"
            else:
                connect_result.status = (
                    (connect_result.status or "QR pairing succeeded, but wireless ADB connection failed.")
                    + "\n\n"
                    "Check Windows Firewall, router client isolation, and that the phone stays on the same Wi-Fi network."
                )
            return connect_result

        return _synthetic_result(
            self._base(serial="") + ["qr-pair", service_name],
            started,
            False,
            (
                "Timed out waiting for QR pairing. Make sure the phone is on the same network, "
                "Wireless debugging is enabled, and the QR code is scanned from Android settings."
            ),
            error_type="timeout",
        )

    def _wait_for_wireless_connect_candidates(
        self,
        pairing_target: str,
        deadline: float,
        progress_callback=None,
        cancel_event: threading.Event | None = None,
    ) -> list[str]:
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return []
            candidates = self._wireless_connect_candidates(pairing_target, wait_seconds=0.8)
            if candidates:
                self._emit_wireless_qr_progress(
                    progress_callback,
                    "Found wireless ADB connect service: " + ", ".join(candidates[:3]),
                )
                return candidates
            self._emit_wireless_qr_progress(progress_callback, "Pairing succeeded. Waiting for wireless connect service...")
            time.sleep(1.0)
        return []

    def _connect_wireless_qr_target_until_ready(
        self,
        candidates: list[str],
        pairing_target: str,
        before_wireless_serials: set[str],
        deadline: float,
        progress_callback=None,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        attempts = 0
        last_result: CommandResult | None = None
        target = candidates[0] if candidates else ""
        tried: list[str] = []
        max_attempts = 8
        attempt_timeout = 5
        while attempts < max_attempts and time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return _synthetic_result(
                    self._base(serial="") + ["connect", target],
                    datetime.now(),
                    False,
                    "QR wireless ADB connection cancelled",
                    error_type="cancelled",
                )
            auto_device = self._new_wireless_device_serial(before_wireless_serials)
            if auto_device:
                return _synthetic_result(
                    self._base(serial="") + ["connect", auto_device],
                    datetime.now(),
                    True,
                    f"Wireless ADB device is already connected: {auto_device}",
                    stdout=f"connected device: {auto_device}",
                )
            refreshed_candidates = self._wireless_connect_candidates(pairing_target, wait_seconds=0.5)
            for candidate in refreshed_candidates:
                if candidate not in candidates:
                    candidates.append(candidate)
            candidate_pool = [candidate for candidate in candidates if candidate]
            if not candidate_pool:
                break
            target = candidate_pool[attempts % len(candidate_pool)]
            if target not in tried:
                tried.append(target)
            attempts += 1
            self._emit_wireless_qr_progress(
                progress_callback,
                f"Connecting to {target} ({attempts}/{max_attempts})...",
            )
            last_result = self.connect_wireless_target(target, timeout=attempt_timeout)
            if last_result.success:
                return last_result
            auto_device = self._new_wireless_device_serial(before_wireless_serials)
            if auto_device:
                return _synthetic_result(
                    self._base(serial="") + ["connect", auto_device],
                    datetime.now(),
                    True,
                    f"adb connect reported an error, but the wireless device is connected: {auto_device}",
                    stdout=f"connected device: {auto_device}",
                )
            if attempts >= max_attempts:
                break
            remaining = deadline - time.monotonic()
            self._emit_wireless_qr_progress(
                progress_callback,
                f"Connection attempt to {target} failed. Re-checking wireless ADB service before retry...",
            )
            time.sleep(min(1.2, max(0.2, remaining)))
        if last_result is not None:
            last_result.status = (
                "Wireless ADB connection failed. QR pairing itself may have succeeded, "
                "but OpenADB could not connect to any discovered _adb-tls-connect service. "
                f"Tried: {', '.join(tried) or target}."
            )
            return last_result
        return _synthetic_result(
            self._base(serial="") + ["connect", target],
            datetime.now(),
            False,
            f"Wireless ADB connection failed before adb connect could run: {target}",
            error_type="connection_failed",
        )

    def _wireless_device_serials(self) -> set[str]:
        return {device.serial for device in self.list_devices() if _looks_like_wireless_serial(device.serial)}

    def _new_wireless_device_serial(self, before: set[str]) -> str:
        for serial in sorted(self._wireless_device_serials()):
            if serial not in before:
                return serial
        if len(before) == 1:
            serial = next(iter(before))
            if serial in self._wireless_device_serials():
                return serial
        return ""

    def _wait_for_new_wireless_device(
        self,
        before: set[str],
        deadline: float,
        progress_callback=None,
        cancel_event: threading.Event | None = None,
        seconds: float = 4.0,
    ) -> str:
        end = min(deadline, time.monotonic() + max(0.5, seconds))
        while time.monotonic() < end:
            if cancel_event is not None and cancel_event.is_set():
                return ""
            serial = self._new_wireless_device_serial(before)
            if serial:
                return serial
            self._emit_wireless_qr_progress(
                progress_callback,
                "Pairing completed. Checking whether adb already connected the wireless device...",
            )
            time.sleep(0.5)
        return ""

    def _find_current_wireless_connect_target(self, pairing_host: str) -> str:
        services = self._discover_wireless_mdns_services(wait_seconds=0.5)
        connect_services = [
            service for service in services if _normalize_mdns_service_type(service.get("type", "")) == "_adb-tls-connect._tcp"
        ]
        if not connect_services:
            return ""
        if pairing_host:
            for service in connect_services:
                if _mdns_target_host(service.get("target", "")) == pairing_host:
                    return service.get("target", "")
        return connect_services[0].get("target", "")

    def _wireless_connect_candidates(self, pairing_target: str, wait_seconds: float = 0.5) -> list[str]:
        services = self._discover_wireless_mdns_services(wait_seconds=wait_seconds)
        return _wireless_connect_candidates_from_services(services, pairing_target)

    def _discover_wireless_mdns_services(self, wait_seconds: float = 0.5) -> list[dict[str, str]]:
        services: list[dict[str, str]] = []
        services.extend(_browse_mdns_services_with_zeroconf(wait_seconds))
        result = self.run_raw(["mdns", "services"], timeout=3, use_serial=False)
        if result.success:
            services.extend(_parse_mdns_services(result.stdout))
        return _dedupe_mdns_services(services)

    @staticmethod
    def _emit_wireless_qr_progress(progress_callback, message: str) -> None:
        if progress_callback:
            progress_callback.emit(message)

    def device_ip_addresses(self) -> list[str]:
        commands = [
            ("wlan", "ip -f inet addr show wlan0"),
            ("route", "ip route get 8.8.8.8"),
            ("ifconfig", "ifconfig wlan0"),
        ]
        addresses: list[str] = []
        for parser, command in commands:
            result = self.run_shell(command, timeout=12)
            output = "\n".join(part for part in [result.stdout, result.stderr] if part)
            if parser == "route":
                for match in re.findall(r"\bsrc\s+((?:\d{1,3}\.){3}\d{1,3})\b", output):
                    _append_usable_ipv4(addresses, match)
                continue
            for line in output.splitlines():
                patterns = [
                    r"\binet\s+((?:\d{1,3}\.){3}\d{1,3})(?:/\d+)?",
                    r"\binet addr:((?:\d{1,3}\.){3}\d{1,3})\b",
                ]
                for pattern in patterns:
                    match = re.search(pattern, line)
                    if match:
                        _append_usable_ipv4(addresses, match.group(1))
                        break
        return addresses

    def root_shell_script(self, shell_command: str) -> str:
        return f"su -c {shell_quote(shell_command)}"

    def run_root_shell(self, shell_command: str, timeout: int | float | None = 120) -> CommandResult:
        return self.run_shell(self.root_shell_script(shell_command), timeout=timeout)

    def root_available(self) -> bool:
        direct = self.run_shell("id -u", timeout=8)
        if _stdout_has_root_uid(direct.stdout):
            return True
        via_su = self.run_root_shell("id -u", timeout=12)
        return _stdout_has_root_uid(via_su.stdout)

    def list_devices(self) -> list[DeviceInfo]:
        result = self.run_raw(["devices", "-l"], timeout=15, use_serial=False)
        devices: list[DeviceInfo] = []
        if not result.stdout:
            return devices
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.lower().startswith("list of devices"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial = parts[0]
            state = parts[1]
            fields = _parse_key_value_tokens(parts[2:])
            mode = {
                "device": "ADB",
                "recovery": "Recovery",
                "unauthorized": "Unauthorized",
                "offline": "Offline",
            }.get(state, state.title())
            devices.append(
                DeviceInfo(
                    serial=serial,
                    model=fields.get("model", "").replace("_", " "),
                    manufacturer=fields.get("manufacturer", ""),
                    mode=mode,
                    state=state,
                    transport_id=fields.get("transport_id", ""),
                    product=fields.get("product", ""),
                )
            )
        return devices

    def get_state(self, serial: str | None = None) -> str:
        previous = self.serial
        if serial is not None:
            self.serial = serial
        try:
            result = self.run_raw(["get-state"], timeout=10)
            return (result.stdout or result.stderr).strip()
        finally:
            if serial is not None:
                self.serial = previous

    def get_device_info(self, serial: str | None = None) -> DeviceInfo:
        previous = self.serial
        if serial is not None:
            self.serial = serial
        try:
            props = [
                "ro.product.model",
                "ro.product.manufacturer",
                "ro.build.version.release",
                "ro.build.version.sdk",
                "ro.build.characteristics",
            ]
            command = "; ".join(f"getprop {prop}" for prop in props)
            result = self.run_shell(command, timeout=15)
            values = [line.strip() for line in (result.stdout or "").splitlines()]
            while len(values) < len(props):
                values.append("")
            return DeviceInfo(
                serial=self.serial,
                model=values[0],
                manufacturer=values[1],
                android_version=values[2],
                sdk_version=values[3],
                mode="ADB",
                state="device",
                form_factor=_device_form_factor(values[4]),
            )
        finally:
            if serial is not None:
                self.serial = previous

    def list_packages(self, include_system: bool = True, load_details: bool = False) -> list[AppInfo]:
        all_result = self.run_shell("pm list packages -f --show-versioncode", timeout=60)
        package_rows = _parse_package_paths(all_result.stdout)
        if not package_rows:
            all_result = self.run_shell("pm list packages -f", timeout=60)
            package_rows = _parse_package_paths(all_result.stdout)
        if not package_rows:
            fallback_result = self.run_shell("pm list packages", timeout=60)
            package_rows = [("", package_name, "") for package_name in _parse_package_list(fallback_result.stdout)]
            if not package_rows and not (all_result.stdout or fallback_result.stdout):
                message = fallback_result.stderr or all_result.stderr or fallback_result.status or all_result.status
                raise RuntimeError(message or "Android returned an empty package list.")

        user_set = set(_parse_package_list(self.run_shell("pm list packages -3", timeout=30).stdout))
        system_set = set(_parse_package_list(self.run_shell("pm list packages -s", timeout=30).stdout))
        disabled_set = set(_parse_package_list(self.run_shell("pm list packages -d", timeout=30).stdout))

        apps: list[AppInfo] = []
        for apk_path, package_name, version_code in package_rows:
            if not package_name:
                continue
            app_type = "user" if package_name in user_set else "system"
            if package_name in system_set:
                app_type = "system"
            if not include_system and app_type == "system":
                continue
            app = AppInfo(
                package_name=package_name,
                app_label="",
                app_type=app_type,
                state="disabled" if package_name in disabled_set else "enabled",
                version_code=version_code,
                apk_paths=[apk_path] if apk_path else [],
            )
            if load_details:
                details = self.get_package_details(package_name)
                app.version_name = details.get("versionName", "")
                app.version_code = details.get("versionCode", "") or app.version_code
                paths = self.get_package_path(package_name)
                if paths:
                    app.apk_paths = paths
                app.size = self._apk_size_text(app.apk_paths)
            apps.append(app)
        apps.sort(key=lambda item: item.display_name.lower())
        return apps

    def get_package_path(self, package_name: str) -> list[str]:
        result = self.run_shell(f"pm path {shell_quote(package_name)}", timeout=20)
        paths: list[str] = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("package:"):
                paths.append(line[len("package:") :].strip())
        return paths

    def get_package_paths_bulk(self, package_names: list[str], chunk_size: int = 80) -> dict[str, list[str]]:
        paths_by_package: dict[str, list[str]] = {package: [] for package in package_names}
        for start in range(0, len(package_names), chunk_size):
            chunk = [package for package in package_names[start : start + chunk_size] if package]
            if not chunk:
                continue
            package_args = " ".join(shell_quote(package) for package in chunk)
            script = f"for p in {package_args}; do echo OPENADB_PACKAGE:$p; pm path \"$p\"; done"
            result = self.run_shell(script, timeout=90)
            current = ""
            for raw_line in (result.stdout or "").splitlines():
                line = raw_line.strip()
                if line.startswith("OPENADB_PACKAGE:"):
                    current = line.split(":", 1)[1]
                    paths_by_package.setdefault(current, [])
                    continue
                if current and line.startswith("package:"):
                    paths_by_package.setdefault(current, []).append(line[len("package:") :].strip())
        return paths_by_package

    def get_package_sizes_bulk(self, package_names: list[str], chunk_size: int = 60, use_root: bool = False) -> dict[str, int]:
        sizes_by_package: dict[str, int] = {}
        for start in range(0, len(package_names), chunk_size):
            chunk = [package for package in package_names[start : start + chunk_size] if package]
            if not chunk:
                continue
            package_args = " ".join(shell_quote(package) for package in chunk)
            script = (
                f"for p in {package_args}; do "
                "total=0; "
                "for apk in $(pm path \"$p\" 2>/dev/null | sed 's/^package://'); do "
                "s=$(stat -c %s \"$apk\" 2>/dev/null); "
                "[ -z \"$s\" ] && s=$(wc -c < \"$apk\" 2>/dev/null); "
                "case \"$s\" in ''|*[!0-9]*) s=0;; esac; "
                "total=$((total + s)); "
                "done; "
                "echo OPENADB_PACKAGE_SIZE:$p:$total; "
                "done"
            )
            result = self.run_root_shell(script, timeout=120) if use_root else self.run_shell(script, timeout=120)
            for raw_line in (result.stdout or "").splitlines():
                line = raw_line.strip()
                if not line.startswith("OPENADB_PACKAGE_SIZE:"):
                    continue
                payload = line.split(":", 1)[1]
                if ":" not in payload:
                    continue
                package_name, size_text = payload.rsplit(":", 1)
                try:
                    sizes_by_package[package_name] = max(0, int(size_text))
                except ValueError:
                    continue
        return sizes_by_package

    def get_package_details(self, package_name: str) -> dict[str, str]:
        result = self.run_shell(f"dumpsys package {shell_quote(package_name)}", timeout=8)
        output = result.stdout or ""
        version_name = _first_match(output, r"versionName=([^\s]+)")
        version_code = _first_match(output, r"versionCode=(\d+)")
        app_label = _clean_dumpsys_label(_first_match(output, r"nonLocalizedLabel=([^\n]+)"))
        return {"versionName": version_name, "versionCode": version_code, "appLabel": app_label}

    def get_package_details_many(
        self,
        package_names: list[str],
        max_workers: int = 4,
        progress_callback: Callable[[int, int, str, dict[str, str]], None] | None = None,
    ) -> dict[str, dict[str, str]]:
        packages = [package for package in package_names if package]
        total = len(packages)
        if not packages:
            return {}
        worker_count = max(1, min(int(max_workers or 1), total, 8))
        results: dict[str, dict[str, str]] = {}

        def load_one(package_name: str) -> tuple[str, dict[str, str]]:
            try:
                return package_name, self.get_package_details(package_name)
            except Exception:
                return package_name, {}

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(load_one, package_name) for package_name in packages]
            for done, future in enumerate(as_completed(futures), start=1):
                package_name, details = future.result()
                results[package_name] = details
                if progress_callback:
                    progress_callback(done, total, package_name, details)
        return results

    def backup_package(self, package_name: str, destination: Path, use_root: bool = False) -> CommandResult:
        paths = self.get_package_path(package_name)
        last_result: CommandResult | None = None
        for apk_path in paths:
            target = destination / Path(apk_path).name
            if use_root:
                last_result = self.pull_file_streaming_to_file(apk_path, target, timeout=180, use_root=True)
            else:
                last_result = self.pull(apk_path, target, timeout=180)
            if not last_result.success:
                return last_result
        if last_result is None:
            return self.run_shell(f"pm path {shell_quote(package_name)}", timeout=10)
        return last_result

    def uninstall_package(self, package_name: str, system_app: bool = False, use_root: bool = False) -> CommandResult:
        quoted = shell_quote(package_name)
        attempts: list[tuple[str, str, bool]] = []
        if system_app:
            if use_root:
                attempts.extend(
                    [
                        ("root pm uninstall --user 0", f"pm uninstall --user 0 {quoted}", True),
                        ("root cmd package uninstall --user 0", f"cmd package uninstall --user 0 {quoted}", True),
                        ("root pm uninstall -k --user 0", f"pm uninstall -k --user 0 {quoted}", True),
                    ]
                )
            attempts.extend(
                [
                    ("pm uninstall --user 0", f"pm uninstall --user 0 {quoted}", False),
                    ("cmd package uninstall --user 0", f"cmd package uninstall --user 0 {quoted}", False),
                ]
            )
        else:
            if use_root:
                attempts.append(("root pm uninstall", f"pm uninstall {quoted}", True))
            attempts.append(("pm uninstall", f"pm uninstall {quoted}", False))
        return self._run_package_uninstall_attempts(package_name, attempts)

    def _run_package_uninstall_attempts(self, package_name: str, attempts: list[tuple[str, str, bool]]) -> CommandResult:
        failures: list[str] = []
        last_result: CommandResult | None = None
        for label, command, as_root in attempts:
            result = self.run_root_shell(command, timeout=120) if as_root else self.run_shell(command, timeout=120)
            last_result = result
            output = _command_output_text(result)
            if result.success and not _package_manager_output_failed(output):
                result.status = f"{label}: {package_name} removed for the selected user."
                return result
            result.success = False
            result.error_type = result.error_type or "package_uninstall_failed"
            failures.append(f"{label}: {_short_command_output(result)}")
        if last_result is None:
            return self.run_shell("true", timeout=5)
        last_result.status = "Uninstall failed. Tried " + "; ".join(failures)
        return last_result

    def disable_package(self, package_name: str) -> CommandResult:
        return self.run_shell(f"pm disable-user --user 0 {shell_quote(package_name)}", timeout=60)

    def enable_package(self, package_name: str) -> CommandResult:
        return self.run_shell(f"pm enable {shell_quote(package_name)}", timeout=60)

    def restore_existing_package(self, package_name: str) -> CommandResult:
        return self.run_shell(f"cmd package install-existing {shell_quote(package_name)}", timeout=120)

    def install_apk(self, apk_path: str | Path) -> CommandResult:
        return self.run_raw(["install", str(apk_path)], timeout=300)

    def install_apk_with_permissions(self, apk_path: str | Path) -> CommandResult:
        return self.run_raw(["install", "-r", "-g", str(apk_path)], timeout=300)

    def install_multiple(self, apk_paths: list[str | Path]) -> CommandResult:
        return self.run_raw(["install-multiple", *[str(path) for path in apk_paths]], timeout=300)

    def list_files(self, android_path: str, use_root: bool = False) -> list[FileItem]:
        command = (
            f"p={shell_quote(android_path)}; "
            'listp="$p"; '
            '[ "$p" != "/" ] && [ -d "$p" ] && listp="${p%/}/"; '
            'ls -la "$listp"; '
            'for item in "$listp"/* "$listp"/.[!.]* "$listp"/..?*; do '
            '[ -e "$item" ] || [ -L "$item" ] || continue; '
            '[ -d "$item" ] && printf "OPENADB_DIR:%s\\n" "${item##*/}"; '
            "done"
        )
        result = self.run_root_shell(command, timeout=30) if use_root else self.run_shell(command, timeout=30)
        if not result.success and not result.stdout:
            raise RuntimeError(result.status or result.stderr or "Unable to list Android files")
        directory_names: set[str] = set()
        ls_lines: list[str] = []
        for line in (result.stdout or "").splitlines():
            if line.startswith("OPENADB_DIR:"):
                name = line.split(":", 1)[1].strip()
                if name:
                    directory_names.add(name)
            else:
                ls_lines.append(line)
        items: list[FileItem] = []
        for line in ls_lines:
            item = _parse_ls_line(line, android_path)
            if item and item.name not in {".", ".."}:
                if item.name in directory_names:
                    item.is_dir = True
                    item.item_type = "Folder"
                    item.size = None
                items.append(item)
        items.sort(key=lambda item: (not item.is_dir, item.name.lower()))
        return items

    def storage_info(self, android_path: str, use_root: bool = False) -> dict[str, int | str]:
        script = (
            f"p={shell_quote(android_path)}; "
            'line=$(df -k "$p" 2>/dev/null | tail -n 1); '
            'echo "$line"'
        )
        result = self.run_root_shell(script, timeout=15) if use_root else self.run_shell(script, timeout=15)
        line = (result.stdout or "").strip().splitlines()
        if not line:
            return {}
        return _parse_df_line(line[-1])

    def storage_volumes(self, use_root: bool = False) -> list[StorageVolume]:
        script = r'''
emit_openadb_volume() {
    label="$1"
    path="$2"
    kind="$3"
    state="$4"
    [ -n "$path" ] || return
    [ -d "$path" ] || return
    line=$(df -k "$path" 2>/dev/null | tail -n 1)
    printf 'OPENADB_VOLUME\t%s\t%s\t%s\t%s\t%s\n' "$label" "$path" "$kind" "$state" "$line"
}
emit_openadb_volume "Internal shared storage" "/sdcard" "internal" "mounted"
if [ ! -d /sdcard ] && [ -d /storage/emulated/0 ]; then
    emit_openadb_volume "Internal shared storage" "/storage/emulated/0" "internal" "mounted"
fi
if command -v sm >/dev/null 2>&1; then
    sm list-volumes all 2>/dev/null | while read type state uuid rest; do
        case "$type" in
            public:*)
                if [ -n "$uuid" ] && [ "$uuid" != "null" ]; then
                    emit_openadb_volume "MicroSD / USB storage $uuid" "/storage/$uuid" "external" "$state"
                fi
                ;;
        esac
    done
fi
for path in /storage/*; do
    [ -e "$path" ] || continue
    name=${path##*/}
    case "$name" in
        emulated|self|sdcard0|sdcard1) continue ;;
    esac
    emit_openadb_volume "External storage $name" "$path" "external" "mounted"
done
for path in /mnt/media_rw/*; do
    [ -e "$path" ] || continue
    name=${path##*/}
    emit_openadb_volume "Root MicroSD / USB $name" "$path" "root-external" "mounted"
done
'''
        result = self.run_root_shell(script, timeout=20) if use_root else self.run_shell(script, timeout=20)
        volumes = _parse_storage_volumes(result.stdout)
        if not volumes:
            volumes.append(StorageVolume(label="Internal shared storage", path="/sdcard/", kind="internal", state="mounted"))
        return volumes

    def mkdir(self, android_path: str, use_root: bool = False) -> CommandResult:
        command = f"mkdir -p {shell_quote(android_path)}"
        return self.run_root_shell(command, timeout=30) if use_root else self.run_shell(command, timeout=30)

    def delete(self, android_path: str, recursive: bool = False, use_root: bool = False) -> CommandResult:
        flag = "-rf" if recursive else "-f"
        if not use_root and _is_public_removable_storage_path(android_path):
            self._prepare_shell_removable_storage_access()
        command = f"rm {flag} {shell_quote(android_path)}"
        result = self.run_root_shell(command, timeout=120) if use_root else self.run_shell(command, timeout=120)
        if result.success:
            result.status = f"Deleted: {android_path}"
            return result
        if not use_root and _is_public_removable_storage_path(android_path):
            fallback = self._delete_public_storage_via_mediastore(android_path, recursive=recursive)
            if fallback.success:
                fallback.status = f"Deleted through Android MediaStore fallback: {android_path}"
                return fallback
            result.status = _android_delete_failure_message(android_path, result, fallback)
            return result
        result.status = _android_delete_failure_message(android_path, result, None)
        return result

    def _prepare_shell_removable_storage_access(self) -> None:
        """Best-effort grant for Android shell storage operations on removable media.

        Some Android TV firmwares gate `/storage/<UUID>` writes behind appops even
        for ADB shell over legacy TCP/IP debugging. These commands are harmless if
        unsupported and can make normal `rm` work before ACBridge fallbacks are
        needed.
        """
        script = """
for pkg in com.android.shell shell; do
  pm grant "$pkg" android.permission.READ_EXTERNAL_STORAGE >/dev/null 2>&1 || true
  pm grant "$pkg" android.permission.WRITE_EXTERNAL_STORAGE >/dev/null 2>&1 || true
  appops set "$pkg" android:legacy_storage allow >/dev/null 2>&1 || true
  appops set "$pkg" LEGACY_STORAGE allow >/dev/null 2>&1 || true
  appops set "$pkg" MANAGE_EXTERNAL_STORAGE allow >/dev/null 2>&1 || true
  cmd appops set "$pkg" android:legacy_storage allow >/dev/null 2>&1 || true
  cmd appops set "$pkg" LEGACY_STORAGE allow >/dev/null 2>&1 || true
  cmd appops set "$pkg" MANAGE_EXTERNAL_STORAGE allow >/dev/null 2>&1 || true
  cmd appops set --uid "$pkg" MANAGE_EXTERNAL_STORAGE allow >/dev/null 2>&1 || true
done
"""
        self.run_shell(script, timeout=12)

    def _delete_public_storage_via_mediastore(self, android_path: str, recursive: bool = False) -> CommandResult:
        """Try deleting public removable-storage files through Android's MediaProvider.

        Some Android TV firmwares expose MicroSD/USB files under /storage/<UUID>
        but reject direct rm from the shell user in legacy TCP/IP debugging mode.
        MediaStore deletion can still remove indexed public files without MTP.
        """
        path = (android_path or "").replace("\\", "/").rstrip("/") or "/"
        quoted_path = shell_quote(path)
        volumes = " ".join(_safe_shell_word(volume) for volume in _mediastore_volume_candidates_for_path(path))
        script = rf'''
p={quoted_path}
static_volumes="{volumes}"
sql_escape() {{
    printf "%s" "$1" | sed "s/'/''/g"
}}
append_volume() {{
    v="$1"
    [ -n "$v" ] || return 0
    case " $volumes " in
        *" $v "*) ;;
        *) volumes="$volumes $v" ;;
    esac
}}
discover_media_volumes() {{
    escaped=$(sql_escape "$p")
    query_where="_data='$escaped'"
    if [ "$recursive_delete" = 1 ]; then
        query_where="(_data='$escaped' OR _data LIKE '$escaped/%')"
    fi
    content query --uri content://media/external/file --projection volume_name --where "$query_where" 2>/dev/null |
        sed -n 's/.*volume_name=\([^, ]*\).*/\1/p'
}}
delete_media_rows() {{
    escaped=$(sql_escape "$p")
    where="_data='$escaped'"
    if [ "$recursive_delete" = 1 ]; then
        where="(_data='$escaped' OR _data LIKE '$escaped/%')"
    fi
    volumes=""
    for v in $static_volumes; do
        append_volume "$v"
    done
    for v in $(discover_media_volumes | sort -u); do
        append_volume "$v"
    done
    for v in $volumes; do
        content delete --uri "content://media/$v/file" --where "$where" >/dev/null 2>&1 || true
    done
}}
remove_empty_dirs() {{
    [ -d "$p" ] || return 0
    find "$p" -depth -type d -exec rmdir {{}} \; >/dev/null 2>&1 || true
}}
recursive_delete=0
{"recursive_delete=1" if recursive else "recursive_delete=0"}
rm -rf "$p" >/dev/null 2>&1 || true
if [ -e "$p" ]; then
    delete_media_rows
fi
if [ -d "$p" ]; then
    rm -rf "$p" >/dev/null 2>&1 || true
    remove_empty_dirs
elif [ -e "$p" ]; then
    rm -f "$p" >/dev/null 2>&1 || true
fi
if [ -e "$p" ]; then
    echo "Android still reports this path after delete attempt: $p" >&2
    exit 1
fi
exit 0
'''
        return self.run_shell(script, timeout=180 if recursive else 60)

    def rename(self, old_path: str, new_path: str, use_root: bool = False) -> CommandResult:
        command = f"mv {shell_quote(old_path)} {shell_quote(new_path)}"
        return self.run_root_shell(command, timeout=60) if use_root else self.run_shell(command, timeout=60)

    def stat(self, android_path: str, use_root: bool = False) -> CommandResult:
        command = f"stat {shell_quote(android_path)}"
        return self.run_root_shell(command, timeout=20) if use_root else self.run_shell(command, timeout=20)

    def push(
        self,
        source: str | Path,
        destination: str,
        timeout: int | float | None = 300,
        disable_compression: bool = False,
    ) -> CommandResult:
        args = ["push"]
        if disable_compression:
            args.append("-Z")
        args.extend([str(source), destination])
        return self.run_raw(args, timeout=timeout)

    def pull(
        self,
        source: str,
        destination: str | Path,
        timeout: int | float | None = 300,
        disable_compression: bool = False,
    ) -> CommandResult:
        args = ["pull"]
        if disable_compression:
            args.append("-Z")
        args.extend([source, str(destination)])
        return self.run_raw(args, timeout=timeout)

    def read_remote_file(self, source: str, timeout: int | float | None = 120, use_root: bool = False) -> tuple[CommandResult, bytes]:
        script = f"cat {shell_quote(source)}"
        if use_root:
            script = self.root_shell_script(script)
        return self.run_raw_binary_output(["exec-out", "sh", "-c", script], timeout=timeout)

    def push_streaming(
        self,
        source: str | Path,
        destination: str,
        timeout: int | float | None = 300,
        output_callback=None,
        cancel_event: threading.Event | None = None,
        disable_compression: bool = False,
    ) -> CommandResult:
        args = ["push"]
        if disable_compression:
            args.append("-Z")
        args.extend([str(source), destination])
        return self.run_raw_streaming(args, timeout=timeout, output_callback=output_callback, cancel_event=cancel_event)

    def pull_streaming(
        self,
        source: str,
        destination: str | Path,
        timeout: int | float | None = 300,
        output_callback=None,
        cancel_event: threading.Event | None = None,
        disable_compression: bool = False,
    ) -> CommandResult:
        args = ["pull"]
        if disable_compression:
            args.append("-Z")
        args.extend([source, str(destination)])
        return self.run_raw_streaming(args, timeout=timeout, output_callback=output_callback, cancel_event=cancel_event)

    def pull_file_streaming_to_file(
        self,
        source: str,
        destination: str | Path,
        timeout: int | float | None = None,
        output_callback=None,
        progress_callback=None,
        cancel_event: threading.Event | None = None,
        use_root: bool = False,
        buffer_size: int = 1024 * 1024,
    ) -> CommandResult:
        script = f"cat {shell_quote(source)}"
        if use_root:
            script = self.root_shell_script(script)
        return self.run_raw_binary_output_to_file(
            ["exec-out", "sh", "-c", script],
            destination=destination,
            timeout=timeout,
            output_callback=output_callback,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            buffer_size=buffer_size,
        )

    def detect_tar_command(self) -> str:
        script = (
            "if command -v tar >/dev/null 2>&1; then "
            "echo tar; "
            "elif command -v toybox >/dev/null 2>&1 && toybox tar --help >/dev/null 2>&1; then "
            "echo 'toybox tar'; "
            "else echo none; fi"
        )
        result = self.run_shell(script, timeout=10)
        value = (result.stdout or "").strip().splitlines()
        command = value[-1].strip() if value else ""
        return command if command in {"tar", "toybox tar"} else ""

    def push_tar_streaming(
        self,
        destination: str,
        tar_command: str,
        input_writer: Callable[[BinaryIO], None],
        timeout: int | float | None = None,
        output_callback=None,
        cancel_event: threading.Event | None = None,
        use_root: bool = False,
        target_name: str = "",
    ) -> CommandResult:
        if tar_command not in {"tar", "toybox tar"}:
            raise ValueError("Unsupported Android tar command")
        quoted_destination = shell_quote(destination)
        if use_root:
            quoted_target_name = shell_quote(target_name)
            script = (
                f"dest={quoted_destination}; target_name={quoted_target_name}; "
                'mkdir -p "$dest" || exit $?; '
                'owner=$(stat -c "%u:%g" "$dest" 2>/dev/null || true); '
                f'cd "$dest" && {tar_command} -xf -; rc=$?; '
                'if [ $rc -eq 0 ] && [ -n "$owner" ] && [ -n "$target_name" ]; then '
                'target="$dest/$target_name"; '
                'chown -R "$owner" "$target" 2>/dev/null || true; '
                'restorecon -R "$target" 2>/dev/null || true; '
                'fi; '
                'exit $rc'
            )
            script = self.root_shell_script(script)
        else:
            script = f"mkdir -p {quoted_destination} && cd {quoted_destination} && {tar_command} -xf -"
        return self.run_raw_with_input_stream(
            ["exec-in", "sh", "-c", script],
            input_writer=input_writer,
            timeout=timeout,
            output_callback=output_callback,
            cancel_event=cancel_event,
        )

    def pull_tar_streaming(
        self,
        source: str,
        tar_command: str,
        output_writer: Callable[[BinaryIO], None],
        timeout: int | float | None = None,
        output_callback=None,
        cancel_event: threading.Event | None = None,
        use_root: bool = False,
    ) -> CommandResult:
        if tar_command not in {"tar", "toybox tar"}:
            raise ValueError("Unsupported Android tar command")
        clean_source = (source or "/").rstrip("/") or "/"
        script = (
            f"src={shell_quote(clean_source)}; "
            'parent=${src%/*}; name=${src##*/}; '
            '[ -z "$parent" ] && parent=/; [ "$parent" = "$src" ] && parent=/; '
            '[ -z "$name" ] && name=.; '
            f"cd \"$parent\" && {tar_command} -cf - \"$name\""
        )
        if use_root:
            script = self.root_shell_script(script)
        command = self._base()
        command.extend(["exec-out", "sh", "-c", script])
        return self.runner.run_binary_output_with_writer(
            command,
            output_writer=output_writer,
            timeout=timeout,
            output_callback=output_callback,
            cancel_event=cancel_event,
        )

    def pull_files_via_temp(
        self,
        pairs: list[tuple[str, Path]],
        chunk_size: int = 24,
        timeout: int | float | None = 600,
        progress_callback: Callable[[int, int, str, str, bool], None] | None = None,
        parallel_chunks: int = 1,
        use_root: bool = False,
    ) -> dict[Path, bool]:
        """Pull many device files with fewer adb process launches.

        adb can pull a whole directory quickly, but cannot map many remote
        source files to many distinct local filenames in one command. This
        method copies readable APKs to a temporary Android directory with
        unique names, pulls that directory once per chunk, and falls back to
        individual pull for files Android refused to copy.
        """
        results = {local: False for _remote, local in pairs}
        if not pairs:
            return results
        total = len(pairs)
        completed = 0
        progress_lock = threading.Lock()
        results_lock = threading.Lock()

        grouped: list[list[tuple[str, Path]]] = []
        for index in range(0, len(pairs), chunk_size):
            grouped.append(pairs[index : index + chunk_size])

        def report(remote: str, local: Path, success: bool) -> None:
            nonlocal completed
            with progress_lock:
                completed += 1
                done = completed
            if progress_callback:
                progress_callback(done, total, remote, str(local), success)

        def set_result(local: Path, success: bool) -> None:
            with results_lock:
                results[local] = success

        def get_result(local: Path) -> bool:
            with results_lock:
                return results.get(local, False)

        def process_chunk(chunk_index: int, chunk: list[tuple[str, Path]]) -> None:
            if not chunk:
                return
            local_parent = ensure_dir(chunk[0][1].parent)
            remote_dir = f"/data/local/tmp/openadb_bulk_{int(time.time() * 1000)}_{chunk_index}_{threading.get_ident()}"
            setup_command = f"rm -rf {shell_quote(remote_dir)}; mkdir -p {shell_quote(remote_dir)}; chmod 0777 {shell_quote(remote_dir)}"
            setup = self.run_root_shell(setup_command, timeout=30) if use_root else self.run_shell(setup_command, timeout=30)
            if not setup.success:
                self._pull_individual(chunk, results, timeout, report, results_lock, use_root=use_root)
                return

            copy_parts: list[str] = []
            for item_index, (remote, local) in enumerate(chunk):
                remote_name = safe_filename(f"{item_index}_{local.name}")
                remote_target = f"{remote_dir}/{remote_name}"
                copy_parts.append(
                    f"cp {shell_quote(remote)} {shell_quote(remote_target)} >/dev/null 2>&1 "
                    f"&& chmod 0644 {shell_quote(remote_target)} || true"
                )
            copy_command = "; ".join(copy_parts)
            copy_result = self.run_root_shell(copy_command, timeout=120) if use_root else self.run_shell(copy_command, timeout=120)
            if not copy_result.success and not copy_result.stdout:
                self._pull_individual(chunk, results, timeout, report, results_lock, use_root=use_root)
                cleanup_command = f"rm -rf {shell_quote(remote_dir)}"
                if use_root:
                    self.run_root_shell(cleanup_command, timeout=30)
                else:
                    self.run_shell(cleanup_command, timeout=30)
                return

            pull_result = self.pull(f"{remote_dir}/.", local_parent, timeout=timeout)
            pulled_successfully: list[tuple[str, Path]] = []
            for item_index, (remote, local) in enumerate(chunk):
                remote_name = safe_filename(f"{item_index}_{local.name}")
                pulled_path = local_parent / remote_name
                if pulled_path.exists():
                    try:
                        if local.exists():
                            local.unlink()
                        pulled_path.rename(local)
                    except OSError:
                        pass
                success = local.exists()
                set_result(local, success)
                if success:
                    pulled_successfully.append((remote, local))
            for remote, local in pulled_successfully:
                report(remote, local, True)
            cleanup_command = f"rm -rf {shell_quote(remote_dir)}"
            if use_root:
                self.run_root_shell(cleanup_command, timeout=30)
            else:
                self.run_shell(cleanup_command, timeout=30)

            failed = [(remote, local) for remote, local in chunk if not get_result(local)]
            if failed:
                self._pull_individual(failed, results, timeout, report, results_lock, use_root=use_root)

        worker_count = max(1, min(int(parallel_chunks or 1), len(grouped), 3))
        if worker_count == 1:
            for chunk_index, chunk in enumerate(grouped):
                process_chunk(chunk_index, chunk)
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [executor.submit(process_chunk, chunk_index, chunk) for chunk_index, chunk in enumerate(grouped)]
                for future in as_completed(futures):
                    future.result()
        return results

    def _pull_individual(
        self,
        pairs: list[tuple[str, Path]],
        results: dict[Path, bool],
        timeout: int | float | None,
        progress_callback: Callable[[str, Path, bool], None] | None = None,
        results_lock: threading.Lock | None = None,
        use_root: bool = False,
    ) -> None:
        for remote, local in pairs:
            ensure_dir(local.parent)
            if use_root:
                result = self.pull_file_streaming_to_file(remote, local, timeout=timeout, use_root=True)
            else:
                result = self.pull(remote, local, timeout=timeout)
            success = result.success and local.exists()
            if results_lock:
                with results_lock:
                    results[local] = success
            else:
                results[local] = success
            if progress_callback:
                progress_callback(remote, local, success)

    def reboot(self, target: str = "") -> CommandResult:
        args = ["reboot"]
        if target:
            args.append(target)
        return self.run_raw(args, timeout=60)

    def _apk_size_text(self, paths: list[str], use_root: bool = False) -> str:
        total = 0
        found = False
        for apk_path in paths:
            command = f"stat -c %s {shell_quote(apk_path)}"
            result = self.run_root_shell(command, timeout=10) if use_root else self.run_shell(command, timeout=10)
            text = (result.stdout or "").strip().splitlines()
            if text and text[0].isdigit():
                total += int(text[0])
                found = True
        if not found:
            return "Unknown"
        return format_bytes(total)


def _parse_key_value_tokens(tokens: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for token in tokens:
        if ":" in token:
            key, value = token.split(":", 1)
            values[key] = value
    return values


def _normalize_tcp_port(port: int | str, label: str = "Port") -> int:
    try:
        parsed = int(str(port).strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be a number from 1 to 65535.") from exc
    if parsed < 1 or parsed > 65535:
        raise ValueError(f"{label} must be from 1 to 65535.")
    return parsed


def _wireless_target(host: str, port: int | str | None = None) -> str:
    host = str(host or "").strip()
    if not host:
        raise ValueError("Device IP address or hostname is empty.")
    if port is None:
        return host
    parsed_port = _normalize_tcp_port(port)
    if host.startswith("[") and "]" in host:
        if re.search(r"\]:\d+$", host):
            return host
        return f"{host}:{parsed_port}"
    if re.search(r"^[^:]+:\d+$", host):
        return host
    if ":" in host:
        return f"[{host}]:{parsed_port}"
    return f"{host}:{parsed_port}"


def _normalize_adb_connect_result(result: CommandResult, target: str) -> CommandResult:
    text = "\n".join(part for part in [result.stdout, result.stderr] if part)
    lowered = text.lower()
    failure_markers = [
        "cannot connect",
        "failed to connect",
        "unable to connect",
        "connection refused",
        "actively refused",
        "no route to host",
        "timed out",
        "timeout",
        "10060",
        "10061",
        "10065",
    ]
    success_markers = ["connected to", "already connected to"]
    if any(marker in lowered for marker in failure_markers):
        result.success = False
        result.status = f"Wireless ADB connection failed: {target}"
        result.error_type = "connection_failed"
        return result
    if result.exit_code == 0 and not any(marker in lowered for marker in success_markers):
        result.success = False
        result.status = (
            f"Wireless ADB connection result is unclear for {target}. "
            "ADB did not report a successful connection."
        )
        result.error_type = "connection_unknown"
    return result


def _normalize_adb_pair_result(result: CommandResult, target: str) -> CommandResult:
    text = "\n".join(part for part in [result.stdout, result.stderr] if part)
    lowered = text.lower()
    failure_markers = [
        "failed",
        "cannot",
        "unable",
        "invalid",
        "refused",
        "timed out",
        "timeout",
        "protocol fault",
        "10060",
        "10061",
        "10065",
    ]
    if result.exit_code == 0 and any(marker in lowered for marker in failure_markers):
        result.success = False
        result.status = f"Wireless ADB pairing failed: {target}"
        result.error_type = "pairing_failed"
    return result


def _looks_like_wireless_serial(serial: str) -> bool:
    text = str(serial or "").strip()
    if not text:
        return False
    if text.startswith("[") and "]:" in text:
        return True
    return bool(re.match(r"^[^:\s]+:\d{1,5}$", text))


def _is_public_removable_storage_path(path: str) -> bool:
    text = str(path or "").replace("\\", "/").strip()
    if not text.startswith("/storage/"):
        return False
    return not text.startswith(("/storage/emulated/", "/storage/self/"))


def _mediastore_volume_candidates_for_path(path: str) -> list[str]:
    text = str(path or "").replace("\\", "/").strip()
    candidates = ["external", "external_primary"]
    match = re.match(r"^/storage/([^/]+)(?:/|$)", text)
    if match:
        storage_id = match.group(1).strip()
        if storage_id and storage_id not in {"emulated", "self"}:
            candidates.extend(_storage_id_volume_variants(storage_id))
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        clean = str(candidate or "").strip()
        if clean and clean not in seen:
            ordered.append(clean)
            seen.add(clean)
    return ordered


def _safe_shell_word(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]", "_", str(value or ""))


def _storage_id_volume_variants(storage_id: str) -> list[str]:
    variants = [storage_id, storage_id.lower(), storage_id.upper()]
    compact = re.sub(r"[^0-9A-Fa-f]", "", storage_id)
    if len(compact) == 8:
        variants.extend([f"{compact[:4]}-{compact[4:]}".upper(), f"{compact[:4]}-{compact[4:]}".lower()])
    elif len(compact) == 16:
        variants.extend(
            [
                f"{compact[:4]}-{compact[4:8]}".upper(),
                f"{compact[:4]}-{compact[4:8]}".lower(),
                f"{compact[:4]}-{compact[-4:]}".upper(),
                f"{compact[:4]}-{compact[-4:]}".lower(),
            ]
        )
    return variants


def _android_delete_failure_message(
    android_path: str,
    result: CommandResult,
    fallback: CommandResult | None,
) -> str:
    details = "\n".join(
        part.strip()
        for part in [
            result.stderr,
            result.stdout,
            fallback.stderr if fallback else "",
            fallback.stdout if fallback else "",
        ]
        if part and part.strip()
    )
    lowered = details.lower()
    if "permission denied" in lowered or "operation not permitted" in lowered:
        reason = "Android refused deletion: permission denied."
    elif "read-only file system" in lowered or "read-only" in lowered:
        reason = "Android refused deletion because the storage is mounted read-only."
    elif fallback is not None and _is_public_removable_storage_path(android_path):
        reason = "Android TV refused deletion on this MicroSD/USB storage path."
    else:
        reason = f"Delete failed with exit code {result.exit_code}."

    suggestions: list[str] = []
    if _is_public_removable_storage_path(android_path):
        suggestions.append(
            "This is a removable/public storage path. Some Android TV firmwares block ADB shell deletion there in legacy IP mode."
        )
        suggestions.append("If the TV is rooted, enable Root boost in File Manager and try again.")
        suggestions.append("Otherwise delete it from the TV's own file manager, or move it to internal shared storage first.")
    if details:
        suggestions.append(f"Technical details: {details}")
    return f"{reason} {' '.join(suggestions)}".strip()


def _command_output_text(result: CommandResult) -> str:
    return "\n".join(part.strip() for part in [result.stdout, result.stderr, result.status] if part and part.strip())


def _package_manager_output_failed(output: str) -> bool:
    lowered = (output or "").lower()
    return "failure [" in lowered or "exception" in lowered or "not installed" in lowered or "failed" in lowered


def _short_command_output(result: CommandResult, limit: int = 260) -> str:
    text = _command_output_text(result).replace("\r", "\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    summary = " | ".join(lines) if lines else f"exit code {result.exit_code}"
    if len(summary) > limit:
        return summary[: limit - 3].rstrip() + "..."
    return summary


def _is_active_refusal_or_timeout(result: CommandResult) -> bool:
    text = "\n".join(part for part in [result.stdout, result.stderr, result.status] if part).lower()
    return any(marker in text for marker in ["10060", "10061", "actively refused", "connection refused", "timed out", "timeout"])


def _parse_mdns_services(text: str) -> list[dict[str, str]]:
    services: list[dict[str, str]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("list of discovered"):
            continue
        match = re.match(r"^(?P<name>\S+)\s+(?P<type>_adb[^\s]+)\s+(?P<target>\S+)\s*$", line)
        if not match:
            continue
        services.append(
            {
                "name": _normalize_mdns_service_name(match.group("name"), match.group("type")),
                "type": _normalize_mdns_service_type(match.group("type")),
                "target": match.group("target"),
                "source": "adb mdns",
            }
        )
    return services


def _find_mdns_service(services: list[dict[str, str]], name: str, service_type: str) -> dict[str, str] | None:
    service_type = _normalize_mdns_service_type(service_type)
    name = _normalize_mdns_service_name(name, service_type)
    candidates = [
        service
        for service in services
        if _normalize_mdns_service_type(service.get("type", "")) == service_type and service.get("target")
    ]
    for service in candidates:
        service_name = _normalize_mdns_service_name(service.get("name", ""), service_type)
        if service_name == name:
            return service
    for service in candidates:
        service_name = _normalize_mdns_service_name(service.get("name", ""), service_type)
        if service_type == "_adb-tls-pairing._tcp" and service_name.startswith("studio-") and name in service_name:
            return service
    studio_candidates = [
        service
        for service in candidates
        if _normalize_mdns_service_name(service.get("name", ""), service_type).startswith("studio-")
    ]
    if service_type == "_adb-tls-pairing._tcp" and len(studio_candidates) == 1:
        return studio_candidates[0]
    if service_type == "_adb-tls-pairing._tcp" and studio_candidates:
        unique_names = {
            _normalize_mdns_service_name(service.get("name", ""), service_type) for service in studio_candidates
        }
        if len(unique_names) == 1:
            return studio_candidates[0]
    return None


def _wireless_connect_candidates_from_services(services: list[dict[str, str]], pairing_target: str) -> list[str]:
    pairing_target = str(pairing_target or "").strip()
    pairing_host = _mdns_target_host(pairing_target)
    candidates: list[str] = []

    connect_services = [
        service
        for service in services
        if _normalize_mdns_service_type(service.get("type", "")) == "_adb-tls-connect._tcp"
        and service.get("target")
    ]
    same_host_services = [
        service for service in connect_services if pairing_host and _mdns_target_host(service.get("target", "")) == pairing_host
    ]
    if same_host_services:
        connect_services = same_host_services
    connect_services.sort(
        key=lambda service: (
            0 if service.get("source") == "zeroconf" else 1,
            0 if pairing_host and _mdns_target_host(service.get("target", "")) == pairing_host else 1,
            service.get("name", ""),
            service.get("target", ""),
        )
    )
    for service in connect_services:
        target = str(service.get("target", "")).strip()
        if _same_wireless_endpoint(target, pairing_target):
            continue
        service_name = _normalize_mdns_service_name(service.get("name", ""), service.get("type", ""))
        if service_name and service_name.startswith("adb-"):
            _append_unique(candidates, service_name)
        if target:
            _append_unique(candidates, target)
    return candidates


def _wireless_connect_service_records(services: list[dict[str, str]]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    connect_services = [
        service
        for service in services
        if _normalize_mdns_service_type(service.get("type", "")) == "_adb-tls-connect._tcp"
        and service.get("target")
    ]
    connect_services.sort(
        key=lambda service: (
            0 if service.get("source") == "zeroconf" else 1,
            service.get("name", ""),
            service.get("target", ""),
        )
    )
    for service in connect_services:
        target = str(service.get("target", "")).strip()
        service_name = _normalize_mdns_service_name(service.get("name", ""), service.get("type", ""))
        connect_target = service_name if service_name.startswith("adb-") else target
        key = (service_name, target)
        if key in seen:
            continue
        seen.add(key)
        records.append(
            {
                "name": service_name,
                "target": target,
                "connect_target": connect_target,
                "source": str(service.get("source", "mDNS")),
            }
        )
    return records


def _same_wireless_endpoint(left: str, right: str) -> bool:
    left_host, left_port = _wireless_endpoint_parts(left)
    right_host, right_port = _wireless_endpoint_parts(right)
    if left_host and right_host and left_port and right_port:
        return left_host.lower() == right_host.lower() and left_port == right_port
    left_text = str(left or "").strip().lower()
    right_text = str(right or "").strip().lower()
    return bool(left_text and right_text and left_text == right_text)


def _append_unique(items: list[str], value: str) -> None:
    value = str(value or "").strip()
    if value and value not in items:
        items.append(value)


def _mdns_target_host(target: str) -> str:
    target = str(target or "").strip()
    if target.startswith("[") and "]" in target:
        return target[1 : target.index("]")]
    if ":" in target:
        return target.rsplit(":", 1)[0]
    return target


def _wireless_endpoint_parts(target: str) -> tuple[str, int | None]:
    text = str(target or "").strip()
    if not text:
        return "", None
    if text.startswith("[") and "]:" in text:
        host, port_text = text[1:].split("]:", 1)
    elif re.match(r"^[^:\s]+:\d{1,5}$", text):
        host, port_text = text.rsplit(":", 1)
    else:
        return text, None
    try:
        port = int(port_text)
    except ValueError:
        port = None
    return host, port


def _browse_mdns_services_with_zeroconf(wait_seconds: float) -> list[dict[str, str]]:
    if ServiceBrowser is None or Zeroconf is None:
        return []

    service_types = ["_adb-tls-pairing._tcp.local.", "_adb-tls-connect._tcp.local."]
    found: list[dict[str, str]] = []
    lock = threading.Lock()

    class Listener:
        def add_service(self, zeroconf, service_type: str, name: str) -> None:
            _add_zeroconf_service(zeroconf, service_type, name, found, lock)

        def update_service(self, zeroconf, service_type: str, name: str) -> None:
            _add_zeroconf_service(zeroconf, service_type, name, found, lock)

        def remove_service(self, zeroconf, service_type: str, name: str) -> None:
            return

    zeroconf = Zeroconf()
    try:
        listener = Listener()
        browsers = [ServiceBrowser(zeroconf, service_type, listener) for service_type in service_types]
        time.sleep(max(0.1, min(wait_seconds, 2.0)))
        for browser in browsers:
            try:
                browser.cancel()
            except Exception:
                continue
    finally:
        zeroconf.close()
    return found


def _add_zeroconf_service(zeroconf, service_type: str, name: str, found: list[dict[str, str]], lock: threading.Lock) -> None:
    try:
        info = zeroconf.get_service_info(service_type, name, timeout=1000)
    except Exception:
        return
    if not info or not getattr(info, "port", 0):
        return
    address = _zeroconf_address(info)
    if not address:
        return
    service = {
        "name": _normalize_mdns_service_name(name, service_type),
        "type": _normalize_mdns_service_type(service_type),
        "target": _format_mdns_target(address, int(info.port)),
        "source": "zeroconf",
    }
    with lock:
        found.append(service)


def _zeroconf_address(info) -> str:
    try:
        addresses = list(info.parsed_addresses())
    except Exception:
        addresses = []
    for address in addresses:
        if _is_usable_ipv4(address):
            return address
    for address in addresses:
        if address and "%" not in address:
            return address
    raw_addresses = list(getattr(info, "addresses", []) or [])
    for raw in raw_addresses:
        try:
            address = socket.inet_ntoa(raw)
        except OSError:
            continue
        if _is_usable_ipv4(address):
            return address
    return ""


def _format_mdns_target(address: str, port: int) -> str:
    if ":" in address and not address.startswith("["):
        return f"[{address}]:{port}"
    return f"{address}:{port}"


def _normalize_mdns_service_type(service_type: str) -> str:
    text = str(service_type or "").strip().rstrip(".")
    if text.endswith(".local"):
        text = text[: -len(".local")]
    return text.rstrip(".")


def _normalize_mdns_service_name(name: str, service_type: str) -> str:
    text = str(name or "").strip().rstrip(".")
    normalized_type = _normalize_mdns_service_type(service_type)
    suffixes = [
        f".{normalized_type}.local",
        f".{normalized_type}",
        "._adb-tls-pairing._tcp.local",
        "._adb-tls-pairing._tcp",
        "._adb-tls-connect._tcp.local",
        "._adb-tls-connect._tcp",
    ]
    for suffix in suffixes:
        if text.endswith(suffix):
            return text[: -len(suffix)].rstrip(".")
    return text


def _dedupe_mdns_services(services: list[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for service in services:
        key = (
            _normalize_mdns_service_name(service.get("name", ""), service.get("type", "")),
            _normalize_mdns_service_type(service.get("type", "")),
            service.get("target", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(service)
    return result


def _synthetic_result(
    command: list[str],
    started: datetime,
    success: bool,
    status: str,
    stdout: str = "",
    stderr: str = "",
    error_type: str = "",
) -> CommandResult:
    finished = datetime.now()
    return CommandResult(
        command=command,
        exit_code=0 if success else None,
        stdout=stdout,
        stderr=stderr,
        duration=(finished - started).total_seconds(),
        started_at=started,
        finished_at=finished,
        success=success,
        status=status,
        error_type=error_type,
    )


def _is_usable_ipv4(address: str) -> bool:
    parts = address.split(".")
    if len(parts) != 4:
        return False
    try:
        values = [int(part) for part in parts]
    except ValueError:
        return False
    if any(value < 0 or value > 255 for value in values):
        return False
    if values[0] in {0, 127, 169, 224, 255}:
        return False
    if values == [255, 255, 255, 255]:
        return False
    return True


def _append_usable_ipv4(addresses: list[str], address: str) -> None:
    if _is_usable_ipv4(address) and address not in addresses:
        addresses.append(address)


def _stdout_has_root_uid(stdout: str) -> bool:
    for line in (stdout or "").splitlines():
        if line.strip() == "0":
            return True
    return False


def _parse_package_list(stdout: str) -> list[str]:
    packages: list[str] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("package:"):
            payload = line[len("package:") :].strip()
            if payload:
                packages.append(payload.split()[0])
    return packages


def _parse_package_paths(stdout: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("package:"):
            continue
        payload = line[len("package:") :]
        tokens = payload.split()
        main = tokens[0] if tokens else payload
        version_code = ""
        for token in tokens[1:]:
            if token.startswith("versionCode:"):
                version_code = token.split(":", 1)[1].strip()
                break
        if "=" in main:
            apk_path, package_name = main.rsplit("=", 1)
            rows.append((apk_path.strip(), package_name.strip(), version_code))
        else:
            package_name = main.strip()
            if package_name:
                rows.append(("", package_name, version_code))
    return rows


def _parse_df_line(line: str) -> dict[str, int | str]:
    clean = (line or "").strip()
    if not clean:
        return {}
    parts = clean.split()
    if len(parts) < 4:
        return {"raw": clean}

    if len(parts) >= 4 and parts[1].isdigit() and parts[2].isdigit() and parts[3].isdigit():
        filesystem = parts[0]
        total_kb = int(parts[1])
        used_kb = int(parts[2])
        free_kb = int(parts[3])
        mount = parts[-1] if len(parts) >= 6 else ""
    else:
        numbers = [int(part) for part in parts if part.isdigit()]
        if len(numbers) < 3:
            return {"raw": clean}
        filesystem = parts[0]
        total_kb, used_kb, free_kb = numbers[:3]
        mount = parts[-1] if parts else ""

    used_percent: int | None = None
    for part in parts:
        if part.endswith("%") and part[:-1].isdigit():
            used_percent = int(part[:-1])
            break

    result: dict[str, int | str] = {
        "raw": clean,
        "filesystem": filesystem,
        "total_bytes": total_kb * 1024,
        "used_bytes": used_kb * 1024,
        "free_bytes": free_kb * 1024,
        "mount": mount,
    }
    if used_percent is not None:
        result["used_percent"] = used_percent
    return result


def _parse_storage_volumes(stdout: str) -> list[StorageVolume]:
    volumes: list[StorageVolume] = []
    seen_paths: set[str] = set()
    for raw_line in (stdout or "").splitlines():
        if not raw_line.startswith("OPENADB_VOLUME\t"):
            continue
        parts = raw_line.split("\t", 5)
        if len(parts) < 5:
            continue
        _marker, label, path, kind, state, df_line = (parts + [""])[:6]
        normalized_path = _normalize_storage_volume_path(path)
        if not normalized_path:
            continue
        dedupe_key = normalized_path.rstrip("/") or "/"
        if dedupe_key in seen_paths:
            continue
        seen_paths.add(dedupe_key)
        storage = _parse_df_line(df_line)
        volumes.append(
            StorageVolume(
                label=label.strip() or normalized_path,
                path=normalized_path,
                kind=kind.strip(),
                state=state.strip(),
                filesystem=str(storage.get("filesystem", "")),
                total_bytes=storage.get("total_bytes") if isinstance(storage.get("total_bytes"), int) else None,
                used_bytes=storage.get("used_bytes") if isinstance(storage.get("used_bytes"), int) else None,
                free_bytes=storage.get("free_bytes") if isinstance(storage.get("free_bytes"), int) else None,
                used_percent=storage.get("used_percent") if isinstance(storage.get("used_percent"), int) else None,
            )
        )
    volumes.sort(key=lambda volume: (0 if volume.kind == "internal" else 1, volume.label.lower(), volume.path))
    return volumes


def _device_form_factor(characteristics: str) -> str:
    values = {value.strip().lower() for value in str(characteristics or "").split(",") if value.strip()}
    if "tv" in values:
        return "Android TV"
    if "watch" in values:
        return "Wear OS"
    if "automotive" in values:
        return "Android Automotive"
    if "tablet" in values:
        return "Tablet"
    return "Android"


def _normalize_storage_volume_path(path: str) -> str:
    normalized = (path or "").strip().replace("\\", "/")
    normalized = re.sub(r"/+", "/", normalized)
    if not normalized:
        return ""
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if normalized in {"/sdcard", "/storage/emulated/0"}:
        return normalized + "/"
    return normalized.rstrip("/") or "/"


def _first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


def _clean_dumpsys_label(value: str) -> str:
    value = (value or "").strip().strip("'\"")
    if not value or value.lower() in {"null", "none"}:
        return ""
    lowered = value.lower()
    if value.startswith("@") or lowered.startswith("0x"):
        return ""
    if any(token in lowered for token in ("<", ">", "type 0x", "0x", "resource id")):
        return ""
    if len(value) > 72:
        return ""
    return " ".join(value.split())


def _parse_ls_line(line: str, parent: str) -> FileItem | None:
    line = line.rstrip()
    if not line or line.startswith("total "):
        return None
    parts = line.split(maxsplit=7)
    if len(parts) < 6:
        return None
    permissions = parts[0]
    is_dir = permissions.startswith("d")
    size: int | None = None
    name = ""
    modified = ""
    try:
        size = int(parts[4])
        modified = " ".join(parts[5:7])
        name = parts[7] if len(parts) >= 8 else ""
    except (ValueError, IndexError):
        tail = line.split(maxsplit=5)
        if len(tail) >= 6:
            name = tail[5]
    if " -> " in name:
        name = name.split(" -> ", 1)[0]
    if not name:
        return None
    return FileItem(
        name=name,
        path=join_android_path(parent, name),
        is_dir=is_dir,
        size=size,
        modified=modified,
        item_type="Folder" if is_dir else "File",
        permissions=permissions,
    )
