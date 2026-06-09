from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import BinaryIO, Callable

from openadb.models.app_info import AppInfo
from openadb.models.command_result import CommandResult
from openadb.models.device_info import DeviceInfo
from openadb.models.file_item import FileItem

from .command_runner import CommandRunner
from .path_utils import ensure_dir, format_bytes, join_android_path, safe_filename, shell_quote
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
        )

    def run_shell(self, shell_command: str, timeout: int | float | None = 120) -> CommandResult:
        return self.run_raw(["shell", shell_command], timeout=timeout)

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

    def mkdir(self, android_path: str, use_root: bool = False) -> CommandResult:
        command = f"mkdir -p {shell_quote(android_path)}"
        return self.run_root_shell(command, timeout=30) if use_root else self.run_shell(command, timeout=30)

    def delete(self, android_path: str, recursive: bool = False, use_root: bool = False) -> CommandResult:
        flag = "-rf" if recursive else "-f"
        command = f"rm {flag} {shell_quote(android_path)}"
        return self.run_root_shell(command, timeout=120) if use_root else self.run_shell(command, timeout=120)

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
