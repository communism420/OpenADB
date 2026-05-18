from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from openadb.models.app_info import AppInfo
from openadb.models.command_result import CommandResult
from openadb.models.device_info import DeviceInfo
from openadb.models.file_item import FileItem

from .command_runner import CommandRunner
from .path_utils import ensure_dir, join_android_path, safe_filename, shell_quote
from .platform_tools import PlatformToolsManager


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

    def run_shell(self, shell_command: str, timeout: int | float | None = 120) -> CommandResult:
        return self.run_raw(["shell", shell_command], timeout=timeout)

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

    def get_package_details(self, package_name: str) -> dict[str, str]:
        result = self.run_shell(f"dumpsys package {shell_quote(package_name)}", timeout=8)
        output = result.stdout or ""
        version_name = _first_match(output, r"versionName=([^\s]+)")
        version_code = _first_match(output, r"versionCode=(\d+)")
        app_label = _clean_dumpsys_label(_first_match(output, r"nonLocalizedLabel=([^\n]+)"))
        return {"versionName": version_name, "versionCode": version_code, "appLabel": app_label}

    def backup_package(self, package_name: str, destination: Path) -> CommandResult:
        paths = self.get_package_path(package_name)
        last_result: CommandResult | None = None
        for apk_path in paths:
            target = destination / Path(apk_path).name
            last_result = self.pull(apk_path, target, timeout=180)
            if not last_result.success:
                return last_result
        if last_result is None:
            return self.run_shell(f"pm path {shell_quote(package_name)}", timeout=10)
        return last_result

    def uninstall_package(self, package_name: str, system_app: bool = False) -> CommandResult:
        if system_app:
            return self.run_shell(f"pm uninstall --user 0 {shell_quote(package_name)}", timeout=120)
        return self.run_shell(f"pm uninstall {shell_quote(package_name)}", timeout=120)

    def disable_package(self, package_name: str) -> CommandResult:
        return self.run_shell(f"pm disable-user --user 0 {shell_quote(package_name)}", timeout=60)

    def enable_package(self, package_name: str) -> CommandResult:
        return self.run_shell(f"pm enable {shell_quote(package_name)}", timeout=60)

    def restore_existing_package(self, package_name: str) -> CommandResult:
        return self.run_shell(f"cmd package install-existing {shell_quote(package_name)}", timeout=120)

    def install_apk(self, apk_path: str | Path) -> CommandResult:
        return self.run_raw(["install", str(apk_path)], timeout=300)

    def install_multiple(self, apk_paths: list[str | Path]) -> CommandResult:
        return self.run_raw(["install-multiple", *[str(path) for path in apk_paths]], timeout=300)

    def list_files(self, android_path: str) -> list[FileItem]:
        result = self.run_shell(f"ls -la {shell_quote(android_path)}", timeout=30)
        if not result.success and not result.stdout:
            raise RuntimeError(result.status or result.stderr or "Unable to list Android files")
        items: list[FileItem] = []
        for line in (result.stdout or "").splitlines():
            item = _parse_ls_line(line, android_path)
            if item and item.name not in {".", ".."}:
                items.append(item)
        items.sort(key=lambda item: (not item.is_dir, item.name.lower()))
        return items

    def mkdir(self, android_path: str) -> CommandResult:
        return self.run_shell(f"mkdir -p {shell_quote(android_path)}", timeout=30)

    def delete(self, android_path: str, recursive: bool = False) -> CommandResult:
        flag = "-rf" if recursive else "-f"
        return self.run_shell(f"rm {flag} {shell_quote(android_path)}", timeout=120)

    def rename(self, old_path: str, new_path: str) -> CommandResult:
        return self.run_shell(f"mv {shell_quote(old_path)} {shell_quote(new_path)}", timeout=60)

    def stat(self, android_path: str) -> CommandResult:
        return self.run_shell(f"stat {shell_quote(android_path)}", timeout=20)

    def push(self, source: str | Path, destination: str, timeout: int | float | None = 300) -> CommandResult:
        return self.run_raw(["push", str(source), destination], timeout=timeout)

    def pull(self, source: str, destination: str | Path, timeout: int | float | None = 300) -> CommandResult:
        return self.run_raw(["pull", source, str(destination)], timeout=timeout)

    def push_streaming(
        self,
        source: str | Path,
        destination: str,
        timeout: int | float | None = 300,
        output_callback=None,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        return self.run_raw_streaming(["push", str(source), destination], timeout=timeout, output_callback=output_callback, cancel_event=cancel_event)

    def pull_streaming(
        self,
        source: str,
        destination: str | Path,
        timeout: int | float | None = 300,
        output_callback=None,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        return self.run_raw_streaming(["pull", source, str(destination)], timeout=timeout, output_callback=output_callback, cancel_event=cancel_event)

    def pull_files_via_temp(
        self,
        pairs: list[tuple[str, Path]],
        chunk_size: int = 24,
        timeout: int | float | None = 600,
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

        grouped: list[list[tuple[str, Path]]] = []
        for index in range(0, len(pairs), chunk_size):
            grouped.append(pairs[index : index + chunk_size])

        for chunk_index, chunk in enumerate(grouped):
            if not chunk:
                continue
            local_parent = ensure_dir(chunk[0][1].parent)
            remote_dir = f"/data/local/tmp/openadb_bulk_{int(time.time() * 1000)}_{chunk_index}"
            setup = self.run_shell(f"rm -rf {shell_quote(remote_dir)}; mkdir -p {shell_quote(remote_dir)}", timeout=30)
            if not setup.success:
                self._pull_individual(chunk, results, timeout)
                continue

            copy_parts: list[str] = []
            for item_index, (remote, local) in enumerate(chunk):
                remote_name = safe_filename(f"{item_index}_{local.name}")
                remote_target = f"{remote_dir}/{remote_name}"
                copy_parts.append(f"cp {shell_quote(remote)} {shell_quote(remote_target)} >/dev/null 2>&1 || true")
            copy_result = self.run_shell("; ".join(copy_parts), timeout=120)
            if not copy_result.success and not copy_result.stdout:
                self._pull_individual(chunk, results, timeout)
                self.run_shell(f"rm -rf {shell_quote(remote_dir)}", timeout=30)
                continue

            pull_result = self.pull(f"{remote_dir}/.", local_parent, timeout=timeout)
            for item_index, (_remote, local) in enumerate(chunk):
                remote_name = safe_filename(f"{item_index}_{local.name}")
                pulled_path = local_parent / remote_name
                if pulled_path.exists():
                    try:
                        if local.exists():
                            local.unlink()
                        pulled_path.rename(local)
                    except OSError:
                        pass
                results[local] = local.exists()
            self.run_shell(f"rm -rf {shell_quote(remote_dir)}", timeout=30)

            failed = [(remote, local) for remote, local in chunk if not results.get(local, False)]
            if failed:
                self._pull_individual(failed, results, timeout)
        return results

    def _pull_individual(
        self,
        pairs: list[tuple[str, Path]],
        results: dict[Path, bool],
        timeout: int | float | None,
    ) -> None:
        for remote, local in pairs:
            ensure_dir(local.parent)
            result = self.pull(remote, local, timeout=timeout)
            results[local] = result.success and local.exists()

    def reboot(self, target: str = "") -> CommandResult:
        args = ["reboot"]
        if target:
            args.append(target)
        return self.run_raw(args, timeout=60)

    def _apk_size_text(self, paths: list[str]) -> str:
        total = 0
        found = False
        for apk_path in paths:
            result = self.run_shell(f"stat -c %s {shell_quote(apk_path)}", timeout=10)
            text = (result.stdout or "").strip().splitlines()
            if text and text[0].isdigit():
                total += int(text[0])
                found = True
        if not found:
            return "Unknown"
        from .path_utils import format_bytes

        return format_bytes(total)


def _parse_key_value_tokens(tokens: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for token in tokens:
        if ":" in token:
            key, value = token.split(":", 1)
            values[key] = value
    return values


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
