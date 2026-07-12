from __future__ import annotations

import re
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from openadb.core.adb import ADBClient
from openadb.models.command_result import CommandResult

from .icon_extractor import IconExtractor
from .path_utils import ensure_dir, package_root, safe_filename, shell_quote
from .settings_manager import SettingsManager


@dataclass
class ACBridgeResult:
    available: bool
    labels: dict[str, str]
    metadata: dict[str, dict[str, str]]
    icons: dict[str, Path]
    message: str


@dataclass(slots=True)
class ACBridgeExportState:
    data_ready: bool
    icons_ready: bool
    error_ready: bool
    elapsed: float
    data_path: str = ""
    icons_path: str = ""
    error_path: str = ""
    raw_output: str = ""


class ACBridgeClient:
    """Read app labels and rendered icons produced by a device-side bridge app.

    OpenADB bundles its own independent helper APK. The helper uses Android's
    PackageManager on the device, so labels and rendered icons can be exported
    without pulling every APK to the PC.
    """

    PACKAGE = "com.communism420.acbridge"
    ACTIVITY = f"{PACKAGE}/.MainActivity"
    VERSION_CODE = 20004
    APK_FILENAME = "ACBridge-2.0.0.apk"
    REMOTE_DIR = "/sdcard/.adac"
    REMOTE_APP_DIR = f"/sdcard/Android/data/{PACKAGE}/files/openadb"
    REMOTE_SETTINGS = "/sdcard/.adac/settings"
    REMOTE_APP_SETTINGS = f"{REMOTE_APP_DIR}/settings"
    REMOTE_DATA = "/sdcard/.adac/.acbridge"
    REMOTE_METADATA = "/sdcard/.adac/metadata.tsv"
    REMOTE_APP_DATA = f"{REMOTE_APP_DIR}/.acbridge"
    REMOTE_APP_METADATA = f"{REMOTE_APP_DIR}/metadata.tsv"
    REMOTE_ICONS_ZIP = "/sdcard/.adac/icons.zip"
    REMOTE_APP_ICONS_ZIP = f"{REMOTE_APP_DIR}/icons.zip"
    REMOTE_ERROR = "/sdcard/.adac/error.txt"
    REMOTE_PROGRESS = "/sdcard/.adac/progress.txt"
    REMOTE_REQUEST = "/sdcard/.adac/packages.txt"
    REMOTE_APP_ERROR = f"{REMOTE_APP_DIR}/error.txt"
    REMOTE_APP_PROGRESS = f"{REMOTE_APP_DIR}/progress.txt"
    REMOTE_APP_REQUEST = f"{REMOTE_APP_DIR}/packages.txt"
    REMOTE_DELETE_RESULT = "/sdcard/.adac/delete_result.txt"
    REMOTE_APP_DELETE_RESULT = f"{REMOTE_APP_DIR}/delete_result.txt"
    LABEL_SEPARATOR = "\\+\\"

    def __init__(self, adb: ADBClient, settings: SettingsManager, icon_extractor: IconExtractor | None = None) -> None:
        self.adb = adb
        self.settings = settings
        self.icon_extractor = icon_extractor

    def delete_path(self, android_path: str, recursive: bool = True, use_root: bool = False, timeout: int = 90) -> CommandResult:
        installed, install_message = self.ensure_installed(require_current=True)
        if not installed:
            result = self.adb.run_shell("true", timeout=5)
            result.success = False
            result.exit_code = 1
            result.status = install_message
            result.stderr = install_message
            return result

        root_available = bool(use_root and self.adb.root_available())
        self._prepare_delete(use_root=root_available)
        start_result = self._start_delete(android_path, recursive=recursive, use_root=root_available)
        if not start_result.success:
            start_result.status = start_result.status or start_result.stderr or "ACBridge delete operation could not be started."
            return start_result

        wait_result = self._wait_for_delete(timeout=timeout)
        output = (wait_result.stdout or wait_result.stderr or "").strip()
        if output.startswith("OPENADB_DELETE_RESULT "):
            output = output.split(" ", 1)[1].strip()
        if output.startswith("OK\t"):
            wait_result.success = True
            wait_result.exit_code = 0
            wait_result.status = output[3:].strip() or f"Deleted through ACBridge: {android_path}"
        elif output.startswith("ERROR\t"):
            wait_result.success = False
            wait_result.exit_code = wait_result.exit_code if wait_result.exit_code not in (None, 0) else 1
            wait_result.status = output[6:].strip() or f"ACBridge could not delete: {android_path}"
            wait_result.stderr = wait_result.status
        elif wait_result.success:
            wait_result.status = output or f"ACBridge delete finished: {android_path}"
        else:
            wait_result.status = output or wait_result.status or f"ACBridge delete timed out for: {android_path}"
        if wait_result.success:
            verify = self.adb.run_shell(f"if [ -e {shell_quote(android_path)} ]; then echo exists; exit 1; fi", timeout=12)
            if not verify.success:
                wait_result.success = False
                wait_result.exit_code = 1
                wait_result.status = (
                    f"{wait_result.status} Android still reports this path after ACBridge delete attempt: {android_path}"
                )
                wait_result.stderr = wait_result.status
        if root_available and wait_result.status:
            wait_result.status += " Root mode: active."
        return wait_result

    def grant_storage_access(self, android_path: str = "", timeout: int = 600) -> CommandResult:
        installed, install_message = self.ensure_installed(require_current=True)
        if not installed:
            result = self.adb.run_shell("true", timeout=5)
            result.success = False
            result.exit_code = 1
            result.status = install_message
            result.stderr = install_message
            return result
        self._prepare_delete(use_root=False)
        start_result = self._start_storage_grant(android_path)
        if not start_result.success:
            start_result.status = start_result.status or start_result.stderr or "ACBridge storage permission request could not be started."
            return start_result
        wait_result = self._wait_for_delete(timeout=timeout)
        output = (wait_result.stdout or wait_result.stderr or "").strip()
        if output.startswith("OPENADB_DELETE_RESULT "):
            output = output.split(" ", 1)[1].strip()
        if output.startswith("OK\t"):
            wait_result.success = True
            wait_result.exit_code = 0
            wait_result.status = output[3:].strip() or "Android TV storage access was granted."
        elif output.startswith("ERROR\t"):
            wait_result.success = False
            wait_result.exit_code = wait_result.exit_code if wait_result.exit_code not in (None, 0) else 1
            wait_result.status = output[6:].strip() or "Android TV storage access was not granted."
            wait_result.stderr = wait_result.status
        elif wait_result.success:
            wait_result.status = output or "Android TV storage access request finished."
        else:
            wait_result.status = output or wait_result.status or "Android TV storage access request timed out."
        return wait_result

    def load_app_data(
        self,
        apps_by_package: dict[str, tuple[str, str]],
        device_serial: str = "",
        icon_size: int = 96,
        timeout: int = 90,
        need_labels: bool = True,
        need_icons: bool = True,
        need_metadata: bool = True,
        use_root: bool = False,
        progress_callback=None,
    ) -> ACBridgeResult:
        if not apps_by_package:
            return ACBridgeResult(False, {}, {}, {}, "ACBridge skipped: no packages.")
        if not need_labels and not need_icons and not need_metadata:
            return ACBridgeResult(False, {}, {}, {}, "ACBridge skipped: local cache is complete.")
        self._emit(progress_callback, "Checking ACBridge helper...")
        installed, install_message = self.ensure_installed()
        if not installed:
            return ACBridgeResult(False, {}, {}, {}, install_message)
        self._emit(progress_callback, install_message)
        root_available = bool(use_root and self.adb.root_available())
        if use_root and root_available:
            self._emit(progress_callback, "ACBridge root mode is available. Preparing bridge files through su/root.")
        elif use_root:
            self._emit(progress_callback, "ACBridge root mode was requested, but su/root was not granted.")

        started_at = time.monotonic()
        self._emit(progress_callback, "Preparing ACBridge export files on Android...")
        self._prepare_run(icon_size, need_icons=need_icons, package_names=apps_by_package.keys(), use_root=root_available)
        self._emit(progress_callback, "Starting ACBridge on the phone...")
        start_result = self._start_bridge(icon_size, need_icons=need_icons, use_root=root_available)
        if not start_result.success:
            return ACBridgeResult(
                True,
                {},
                {},
                {},
                start_result.status or start_result.stderr or "ACBridge could not be started.",
            )

        self._emit(progress_callback, "Waiting for ACBridge to export app labels and icons...")
        export_state = self._wait_for_export(timeout, need_icons=need_icons, package_count=len(apps_by_package), progress_callback=progress_callback)
        if not export_state.data_ready:
            error_text = ""
            if export_state.error_ready and export_state.error_path:
                error_text = self._download_remote_text(export_state.error_path, timeout=10)
            diagnostic = self._acbridge_diagnostic()
            message = "ACBridge did not export app data before timeout."
            if error_text:
                message += f" Device error: {error_text}"
            if diagnostic:
                message += f" Diagnostic: {diagnostic}"
            return ACBridgeResult(True, {}, {}, {}, message)

        local_dir = ensure_dir(self.settings.temp_folder / "acbridge")
        serial_key = safe_filename(device_serial or self.adb.serial or "device")
        local_data = local_dir / f"{serial_key}_acbridge.txt"
        local_icons_zip = local_dir / f"{serial_key}_icons.zip"

        data_path = export_state.data_path or self.REMOTE_APP_DATA
        icons_path = export_state.icons_path or self.REMOTE_APP_ICONS_ZIP
        data_ok, data_message = self._download_text_file_fast(data_path, local_data, timeout=45, use_root=root_available)
        if not data_ok:
            return ACBridgeResult(True, {}, {}, {}, data_message or "Unable to pull ACBridge app data.")

        labels = self._parse_labels(local_data, set(apps_by_package)) if need_labels else {}
        metadata = (
            self._download_and_parse_metadata(export_state, local_dir, serial_key, set(apps_by_package), use_root=root_available)
            if need_metadata
            else {}
        )
        icon_versions = dict(apps_by_package)
        for package_name, details in metadata.items():
            current_version_name, current_version_code = icon_versions.get(package_name, ("", ""))
            icon_versions[package_name] = (
                details.get("versionName", "") or current_version_name,
                details.get("versionCode", "") or current_version_code,
            )
        icons: dict[str, Path] = {}
        if need_icons and export_state.icons_ready:
            zip_timeout = max(180, min(420, 60 + len(apps_by_package)))
            zip_ok, _zip_message = self._download_binary_file_fast(icons_path, local_icons_zip, timeout=zip_timeout, use_root=root_available)
            if zip_ok and local_icons_zip.exists():
                icons = self._import_icons(local_icons_zip, icon_versions, source_key=f"acbridge_{serial_key}")

        duration = time.monotonic() - started_at
        icon_note = "" if icons or not need_icons else " Icon archive was not exported; fallback loader will continue."

        return ACBridgeResult(
            True,
            labels,
            metadata,
            icons,
            (
                f"{install_message} ACBridge fast path loaded {len(labels)} labels, {len(metadata)} metadata rows, "
                f"and {len(icons)} rendered icons "
                f"in {duration:.1f}s. Root mode: {'active' if root_available else 'not used'}.{icon_note}"
            ),
        )

    def _start_bridge(self, icon_size: int, need_icons: bool, use_root: bool) -> object:
        command = (
            f"am start -n {shell_quote(self.ACTIVITY)} "
            f"--ez showicons {'true' if need_icons else 'false'} "
            "--ez endexit true "
            f"--ei iconsize {max(48, min(192, int(icon_size)))} "
            "--ez appsizes true "
            "--ez legacy false "
            f"--ez rootmode {'true' if use_root else 'false'}"
        )
        if use_root:
            return self.adb.run_root_shell(command, timeout=20)
        return self.adb.run_shell(command, timeout=20)

    def _start_delete(self, android_path: str, recursive: bool, use_root: bool) -> CommandResult:
        command = (
            f"am start -n {shell_quote(self.ACTIVITY)} "
            "--es operation delete "
            f"--es path {shell_quote(android_path)} "
            f"--ez recursive {'true' if recursive else 'false'} "
            f"--ez rootmode {'true' if use_root else 'false'} "
            "--ez endexit true"
        )
        if use_root:
            return self.adb.run_root_shell(command, timeout=20)
        return self.adb.run_shell(command, timeout=20)

    def _start_storage_grant(self, android_path: str) -> CommandResult:
        command = (
            f"am start -n {shell_quote(self.ACTIVITY)} "
            "--es operation grantStorage "
            f"--es path {shell_quote(android_path)} "
            "--ez endexit true"
        )
        return self.adb.run_shell(command, timeout=20)

    def is_installed(self) -> bool:
        result = self.adb.run_shell(f"pm path {shell_quote(self.PACKAGE)}", timeout=10)
        return bool(result.stdout and "package:" in result.stdout)

    def ensure_installed(self, require_current: bool = False) -> tuple[bool, str]:
        installed_version = self.installed_version_code()
        if installed_version >= self.VERSION_CODE:
            return True, f"ACBridge is already installed (versionCode {installed_version})."

        apk = self.bundled_apk_path()
        if not apk.exists():
            return (
                False,
                f"ACBridge APK was not found at {apk}. Build it with tools/build_acbridge.py or place ACBridge.apk there.",
            )

        result = self.adb.install_apk_with_permissions(apk)
        if not result.success and self._looks_like_signature_mismatch(result.stdout + "\n" + result.stderr + "\n" + result.status):
            if installed_version > 0:
                if require_current:
                    return (
                        False,
                        (
                            f"This ACBridge operation requires bundled versionCode {self.VERSION_CODE}, but Android reports "
                            f"an installed helper with versionCode {installed_version} and a different signature. "
                            "Uninstall com.communism420.acbridge manually, then try again."
                        ),
                    )
                return (
                    True,
                    (
                        f"Using existing ACBridge versionCode {installed_version}. "
                        "Bundled ACBridge could not update it because Android reports a signature mismatch; "
                        "OpenADB did not delete the existing helper."
                    ),
                )
            return (
                False,
                (
                    "ACBridge is installed with a different signature. OpenADB will not delete it automatically; "
                    "uninstall com.communism420.acbridge manually if you want OpenADB to install the bundled helper."
                ),
            )
        if not result.success:
            return False, result.status or result.stderr or "Unable to install ACBridge helper APK."

        installed_version = self.installed_version_code()
        if installed_version >= self.VERSION_CODE or self.is_installed():
            return True, f"ACBridge installed from bundled APK (versionCode {installed_version or self.VERSION_CODE})."
        return False, "ACBridge install command finished, but Android does not report the helper package as installed."

    def bundled_apk_path(self) -> Path:
        return package_root() / "resources" / "acbridge" / self.APK_FILENAME

    def installed_version_code(self) -> int:
        result = self.adb.run_shell(f"dumpsys package {shell_quote(self.PACKAGE)}", timeout=15)
        output = result.stdout or ""
        match = re.search(r"versionCode=(\d+)", output)
        if not match:
            return 0
        try:
            return int(match.group(1))
        except ValueError:
            return 0

    def _prepare_run(self, icon_size: int, need_icons: bool, package_names, use_root: bool = False) -> None:
        settings_text = "\n".join(
            [
                f"showicons={'true' if need_icons else 'false'}",
                "endexit=true",
                "iconscache=true",
                "backup=false",
                "appsizes=true",
                f"rootmode={'true' if use_root else 'false'}",
                f"iconsize={max(48, min(192, int(icon_size)))}",
            ]
        ) + "\n"
        commands = [
            f"mkdir -p {shell_quote(self.REMOTE_DIR)} {shell_quote(self.REMOTE_APP_DIR)}",
            (
                f"printf %s {shell_quote(settings_text)} > {shell_quote(self.REMOTE_SETTINGS)}; "
                f"printf %s {shell_quote(settings_text)} > {shell_quote(self.REMOTE_APP_SETTINGS)}"
            ),
            (
                f"rm -f {shell_quote(self.REMOTE_DATA)} {shell_quote(self.REMOTE_METADATA)} "
                f"{shell_quote(self.REMOTE_ICONS_ZIP)} "
                f"{shell_quote(self.REMOTE_ERROR)} {shell_quote(self.REMOTE_PROGRESS)} "
                f"{shell_quote(self.REMOTE_APP_DATA)} {shell_quote(self.REMOTE_APP_METADATA)} "
                f"{shell_quote(self.REMOTE_APP_ICONS_ZIP)} "
                f"{shell_quote(self.REMOTE_APP_ERROR)} {shell_quote(self.REMOTE_APP_PROGRESS)}"
            ),
            f"pm grant {shell_quote(self.PACKAGE)} android.permission.WRITE_EXTERNAL_STORAGE >/dev/null 2>&1 || true",
            f"pm grant {shell_quote(self.PACKAGE)} android.permission.READ_EXTERNAL_STORAGE >/dev/null 2>&1 || true",
            f"appops set {shell_quote(self.PACKAGE)} android:legacy_storage allow >/dev/null 2>&1 || true",
            f"appops set {shell_quote(self.PACKAGE)} MANAGE_EXTERNAL_STORAGE allow >/dev/null 2>&1 || true",
            f"appops set --uid {shell_quote(self.PACKAGE)} MANAGE_EXTERNAL_STORAGE allow >/dev/null 2>&1 || true",
            f"chmod -R 0777 {shell_quote(self.REMOTE_DIR)} {shell_quote(self.REMOTE_APP_DIR)} >/dev/null 2>&1 || true",
        ]
        command = "; ".join(commands)
        if use_root:
            self.adb.run_root_shell(command, timeout=30)
        else:
            self.adb.run_shell(command, timeout=30)
        self._write_package_request(package_names, use_root=use_root)

    def _write_package_request(self, package_names, use_root: bool = False) -> None:
        local_dir = ensure_dir(self.settings.temp_folder / "acbridge")
        request_path = local_dir / "packages.txt"
        packages = sorted(str(package) for package in package_names if package)
        try:
            request_path.write_text("\n".join(packages) + "\n", encoding="utf-8")
        except OSError:
            return
        self.adb.push(request_path, self.REMOTE_REQUEST, timeout=30)
        self.adb.push(request_path, self.REMOTE_APP_REQUEST, timeout=30)
        if use_root:
            self.adb.run_root_shell(
                (
                    f"chmod 0666 {shell_quote(self.REMOTE_REQUEST)} {shell_quote(self.REMOTE_APP_REQUEST)} "
                    ">/dev/null 2>&1 || true"
                ),
                timeout=10,
            )

    def _prepare_delete(self, use_root: bool = False) -> None:
        commands = [
            f"mkdir -p {shell_quote(self.REMOTE_DIR)} {shell_quote(self.REMOTE_APP_DIR)}",
            f"rm -f {shell_quote(self.REMOTE_DELETE_RESULT)} {shell_quote(self.REMOTE_APP_DELETE_RESULT)}",
            f"pm grant {shell_quote(self.PACKAGE)} android.permission.WRITE_EXTERNAL_STORAGE >/dev/null 2>&1 || true",
            f"pm grant {shell_quote(self.PACKAGE)} android.permission.READ_EXTERNAL_STORAGE >/dev/null 2>&1 || true",
            f"appops set {shell_quote(self.PACKAGE)} android:legacy_storage allow >/dev/null 2>&1 || true",
            f"appops set {shell_quote(self.PACKAGE)} MANAGE_EXTERNAL_STORAGE allow >/dev/null 2>&1 || true",
            f"appops set --uid {shell_quote(self.PACKAGE)} MANAGE_EXTERNAL_STORAGE allow >/dev/null 2>&1 || true",
            f"chmod -R 0777 {shell_quote(self.REMOTE_DIR)} {shell_quote(self.REMOTE_APP_DIR)} >/dev/null 2>&1 || true",
        ]
        command = "; ".join(commands)
        if use_root:
            self.adb.run_root_shell(command, timeout=30)
        else:
            self.adb.run_shell(command, timeout=30)

    def _wait_for_export(self, timeout: int, need_icons: bool, package_count: int, progress_callback=None) -> ACBridgeExportState:
        started = time.monotonic()
        timeout = max(5, int(timeout))
        script = (
            f"data1={shell_quote(self.REMOTE_DATA)}; data2={shell_quote(self.REMOTE_APP_DATA)}; "
            f"icons1={shell_quote(self.REMOTE_ICONS_ZIP)}; icons2={shell_quote(self.REMOTE_APP_ICONS_ZIP)}; "
            f"error1={shell_quote(self.REMOTE_ERROR)}; error2={shell_quote(self.REMOTE_APP_ERROR)}; "
            f"progress1={shell_quote(self.REMOTE_PROGRESS)}; progress2={shell_quote(self.REMOTE_APP_PROGRESS)}; "
            f"need_icons={'1' if need_icons else '0'}; "
            f"timeout={timeout}; i=0; data_seen=0; icons_seen=0; error_seen=0; "
            "while [ \"$i\" -lt \"$timeout\" ]; do "
            "data_path=''; icons_path=''; error_path=''; "
            "[ -s \"$data1\" ] && data_path=\"$data1\"; "
            "[ -z \"$data_path\" ] && [ -s \"$data2\" ] && data_path=\"$data2\"; "
            "[ -s \"$icons1\" ] && icons_path=\"$icons1\"; "
            "[ -z \"$icons_path\" ] && [ -s \"$icons2\" ] && icons_path=\"$icons2\"; "
            "[ -s \"$error1\" ] && error_path=\"$error1\"; "
            "[ -z \"$error_path\" ] && [ -s \"$error2\" ] && error_path=\"$error2\"; "
            "[ -n \"$data_path\" ] && data_seen=1 || data_seen=0; "
            "[ -n \"$icons_path\" ] && icons_seen=1 || icons_seen=0; "
            "[ -n \"$error_path\" ] && error_seen=1 || error_seen=0; "
            "progress=''; [ -s \"$progress1\" ] && progress=$(cat \"$progress1\" 2>/dev/null | tr '\\n' ' '); "
            "[ -z \"$progress\" ] && [ -s \"$progress2\" ] && progress=$(cat \"$progress2\" 2>/dev/null | tr '\\n' ' '); "
            "echo OPENADB_PROGRESS data=$data_seen icons_ready=$icons_seen error=$error_seen $progress; "
            "if [ \"$error_seen\" = 1 ]; then "
            "echo OPENADB_EXPORT data=$data_seen icons=$icons_seen error=$error_seen data_path=$data_path icons_path=$icons_path error_path=$error_path; exit 1; "
            "fi; "
            "if [ \"$data_seen\" = 1 ]; then "
            "if [ \"$need_icons\" != 1 ] || [ \"$icons_seen\" = 1 ]; then "
            "echo OPENADB_EXPORT data=$data_seen icons=$icons_seen error=$error_seen data_path=$data_path icons_path=$icons_path error_path=$error_path; exit 0; "
            "fi; "
            "fi; "
            "i=$((i + 1)); sleep 1; "
            "done; "
            "echo OPENADB_EXPORT data=$data_seen icons=$icons_seen error=$error_seen data_path=$data_path icons_path=$icons_path error_path=$error_path; exit 1"
        )
        last_progress = ""

        def on_output(channel: str, text: str) -> None:
            nonlocal last_progress
            for line in (text or "").splitlines():
                if not line.startswith("OPENADB_PROGRESS"):
                    continue
                fields = self._key_value_fields(line)
                if not {"labels", "icons", "total", "stage"}.issubset(fields):
                    continue
                labels = fields.get("labels", "0")
                icons = fields.get("icons", "0")
                total = fields.get("total", str(package_count))
                stage = fields.get("stage", "waiting")
                message = f"ACBRIDGE_PROGRESS labels={labels} icons={icons} total={total} stage={stage}"
                if message != last_progress:
                    last_progress = message
                    self._emit(progress_callback, message)

        result = self.adb.run_raw_streaming(["shell", script], timeout=timeout + 8, output_callback=on_output)
        output = result.stdout or result.stderr or ""
        export_line = self._last_prefixed_line(output, "OPENADB_EXPORT")
        export_fields = self._key_value_fields(export_line)
        data_ready = export_fields.get("data") == "1"
        icons_ready = export_fields.get("icons") == "1"
        error_ready = export_fields.get("error") == "1"
        data_path = export_fields.get("data_path", "")
        icons_path = export_fields.get("icons_path", "")
        error_path = export_fields.get("error_path", "")
        return ACBridgeExportState(data_ready, icons_ready, error_ready, time.monotonic() - started, data_path, icons_path, error_path, output)

    def _wait_for_delete(self, timeout: int) -> CommandResult:
        timeout = max(10, int(timeout))
        script = (
            f"result1={shell_quote(self.REMOTE_DELETE_RESULT)}; result2={shell_quote(self.REMOTE_APP_DELETE_RESULT)}; "
            f"timeout={timeout}; i=0; "
            "while [ \"$i\" -lt \"$timeout\" ]; do "
            "result_path=''; "
            "[ -s \"$result1\" ] && result_path=\"$result1\"; "
            "[ -z \"$result_path\" ] && [ -s \"$result2\" ] && result_path=\"$result2\"; "
            "if [ -n \"$result_path\" ]; then "
            "printf 'OPENADB_DELETE_RESULT '; cat \"$result_path\" 2>/dev/null; exit 0; "
            "fi; "
            "i=$((i + 1)); sleep 1; "
            "done; "
            "echo 'ERROR\tACBridge delete result was not produced before timeout.'; exit 1"
        )
        return self.adb.run_shell(script, timeout=timeout + 8)

    def _last_prefixed_line(self, output: str, prefix: str) -> str:
        for line in reversed((output or "").splitlines()):
            if line.startswith(prefix):
                return line
        return ""

    def _key_value_fields(self, output: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)", output or ""):
            fields[match.group(1)] = match.group(2)
        return fields

    def _download_and_parse_metadata(
        self,
        export_state: ACBridgeExportState,
        local_dir: Path,
        serial_key: str,
        wanted: set[str],
        use_root: bool = False,
    ) -> dict[str, dict[str, str]]:
        local_metadata = local_dir / f"{serial_key}_metadata.tsv"
        candidates = (
            [self.REMOTE_METADATA, self.REMOTE_APP_METADATA]
            if export_state.data_path == self.REMOTE_DATA
            else [self.REMOTE_APP_METADATA, self.REMOTE_METADATA]
        )
        for remote_path in candidates:
            ok, _message = self._download_text_file_fast(remote_path, local_metadata, timeout=30, use_root=use_root)
            if ok and local_metadata.exists():
                parsed = self._parse_metadata(local_metadata, wanted)
                if parsed:
                    return parsed
        return {}

    def _download_text_file_fast(self, remote_path: str, local_path: Path, timeout: int, use_root: bool = False) -> tuple[bool, str]:
        result, data = self.adb.read_remote_file(remote_path, timeout=timeout, use_root=use_root)
        if result.success and data:
            try:
                local_path.write_bytes(data)
                return True, ""
            except OSError as exc:
                return False, str(exc)

        fallback = self.adb.pull(remote_path, local_path, timeout=timeout)
        if fallback.success and local_path.exists():
            return True, ""
        return False, fallback.status or fallback.stderr or result.status or "Unable to download ACBridge text export."

    def _download_binary_file_fast(self, remote_path: str, local_path: Path, timeout: int, use_root: bool = False) -> tuple[bool, str]:
        if use_root:
            result = self.adb.pull_file_streaming_to_file(remote_path, local_path, timeout=timeout, use_root=True)
        else:
            result = self.adb.pull(remote_path, local_path, timeout=timeout)
        if result.success and local_path.exists():
            return True, ""
        fallback, data = self.adb.read_remote_file(remote_path, timeout=timeout, use_root=use_root)
        if fallback.success and data:
            try:
                local_path.write_bytes(data)
                return True, ""
            except OSError as exc:
                return False, str(exc)
        return False, result.status or result.stderr or fallback.status or "Unable to download ACBridge binary export."

    def _download_remote_text(self, remote_path: str, timeout: int) -> str:
        result, data = self.adb.read_remote_file(remote_path, timeout=timeout)
        if result.success and data:
            return data.decode("utf-8", "replace").strip()
        return ""

    def _acbridge_diagnostic(self) -> str:
        result = self.adb.run_shell(
            (
                f"echo package:; dumpsys package {shell_quote(self.PACKAGE)} | grep -m 1 versionCode; "
                f"echo files:; ls -l {shell_quote(self.REMOTE_APP_DIR)} {shell_quote(self.REMOTE_DIR)} 2>/dev/null; "
                f"echo progress:; cat {shell_quote(self.REMOTE_PROGRESS)} {shell_quote(self.REMOTE_APP_PROGRESS)} 2>/dev/null; "
                f"echo crashes:; logcat -d -t 80 2>/dev/null | grep -i {shell_quote(self.PACKAGE)} | tail -n 20"
            ),
            timeout=20,
        )
        return " ".join((result.stdout or result.stderr or "").split())[:700]

    def _emit(self, progress_callback, message: str) -> None:
        if progress_callback:
            progress_callback.emit(message)

    def _parse_labels(self, path: Path, wanted: set[str]) -> dict[str, str]:
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError:
            return {}
        labels: dict[str, str] = {}
        for chunk in text.split("|"):
            if self.LABEL_SEPARATOR not in chunk:
                continue
            package_name, label = chunk.split(self.LABEL_SEPARATOR, 1)
            package_name = package_name.strip()
            label = " ".join(label.strip().split())
            if package_name in wanted and label and label != package_name:
                labels[package_name] = label
        return labels

    def _parse_metadata(self, path: Path, wanted: set[str]) -> dict[str, dict[str, str]]:
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError:
            return {}
        metadata: dict[str, dict[str, str]] = {}
        for raw_line in text.splitlines():
            parts = raw_line.split("\t")
            if len(parts) < 3:
                continue
            package_name = parts[0].strip()
            if package_name not in wanted:
                continue
            metadata[package_name] = {
                "versionName": parts[1].strip(),
                "versionCode": parts[2].strip(),
            }
            if len(parts) >= 4:
                metadata[package_name]["sizeBytes"] = parts[3].strip()
        return metadata

    def _import_icons(
        self,
        icons_zip: Path,
        apps_by_package: dict[str, tuple[str, str]],
        source_key: str = "",
    ) -> dict[str, Path]:
        icons: dict[str, Path] = {}
        if self.icon_extractor is None:
            return icons
        try:
            with zipfile.ZipFile(icons_zip) as archive:
                tasks: list[tuple[str, str, str, bytes]] = []
                for info in archive.infolist():
                    base_name = Path(info.filename).name
                    if not base_name.lower().endswith(".png"):
                        continue
                    package_name = base_name[:-4]
                    if package_name not in apps_by_package or not self._looks_like_package(package_name):
                        continue
                    version_name, version_code = apps_by_package[package_name]
                    tasks.append((package_name, version_name, version_code, archive.read(info)))
        except (OSError, zipfile.BadZipFile):
            return icons

        return self.icon_extractor.import_pre_rendered_icon_batch(
            [(package_name, data, version_name, version_code, source_key) for package_name, version_name, version_code, data in tasks]
        )

    def _looks_like_package(self, value: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", value))

    def _looks_like_signature_mismatch(self, text: str) -> bool:
        lowered = (text or "").lower()
        return "update_incompatible" in lowered or "signatures do not match" in lowered or "inconsistent certificates" in lowered
