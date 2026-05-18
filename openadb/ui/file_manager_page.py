from __future__ import annotations

import os
import re
import shutil
import threading
import time
from pathlib import Path, PurePosixPath

from PySide6.QtCore import QThreadPool, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QInputDialog, QMessageBox, QSplitter, QVBoxLayout, QWidget

from openadb.core.adb import ADBClient
from openadb.core.device import DeviceManager
from openadb.core.path_utils import (
    is_probably_writable_android_path,
    join_android_path,
    parent_android_path,
    safe_filename,
    shell_quote,
)
from openadb.ui.widgets.file_panel import FilePanel
from openadb.ui.widgets.progress_dialog import TransferProgressDialog
from openadb.ui.widgets.windows_file_panel import WindowsFilePanel
from openadb.ui.workers import Worker, start_worker


PERCENT_PATTERN = re.compile(r"(\d{1,3})\s*%")


class FileManagerPage(QWidget):
    def __init__(self, adb: ADBClient, device_manager: DeviceManager, parent=None) -> None:
        super().__init__(parent)
        self.adb = adb
        self.device_manager = device_manager
        self.pool = QThreadPool.globalInstance()
        self.android_path = "/sdcard/"
        self.windows_path = str(Path.home())
        self._android_loading = False
        self._android_refresh_pending = False
        self._transfer_dialogs: list[TransferProgressDialog] = []

        layout = QVBoxLayout(self)
        splitter = QSplitter()
        self.android_panel = FilePanel("Android", "android")
        self.windows_panel = self._create_windows_panel()
        splitter.addWidget(self.android_panel)
        splitter.addWidget(self.windows_panel)
        splitter.setSizes([600, 600])
        layout.addWidget(splitter, 1)

        self.android_panel.navigate_requested.connect(self.navigate_android)
        self.android_panel.up_requested.connect(lambda: self.navigate_android(parent_android_path(self.android_path)))
        self.android_panel.refresh_requested.connect(self.refresh_android)
        self.android_panel.new_folder_requested.connect(lambda: self.new_folder("android"))
        self.android_panel.delete_requested.connect(lambda: self.delete_selected("android"))
        self.android_panel.rename_requested.connect(lambda: self.rename_selected("android"))
        self.android_panel.transfer_requested.connect(self.pull_selected)
        self.android_panel.copy_path_requested.connect(lambda: self.copy_path("android"))
        self.android_panel.properties_requested.connect(lambda: self.properties("android"))
        self.android_panel.dropped.connect(self.push_paths)

        self.windows_panel.navigate_requested.connect(self.navigate_windows)
        self.windows_panel.up_requested.connect(lambda: self.navigate_windows(str(Path(self.windows_path).parent)))
        self.windows_panel.refresh_requested.connect(self.refresh_windows)
        self.windows_panel.new_folder_requested.connect(lambda: self.new_folder("windows"))
        self.windows_panel.delete_requested.connect(lambda: self.delete_selected("windows"))
        self.windows_panel.rename_requested.connect(lambda: self.rename_selected("windows"))
        self.windows_panel.transfer_requested.connect(self.push_selected)
        self.windows_panel.copy_path_requested.connect(lambda: self.copy_path("windows"))
        self.windows_panel.properties_requested.connect(lambda: self.properties("windows"))
        self.windows_panel.open_external_requested.connect(self.open_explorer)
        self.windows_panel.dropped.connect(self.pull_paths)
        if hasattr(self.windows_panel, "path_changed"):
            self.windows_panel.path_changed.connect(self._windows_path_changed)

        self.windows_panel.set_path(self.windows_path)
        self.android_panel.set_path(self.android_path)

    def _create_windows_panel(self) -> QWidget:
        return WindowsFilePanel(self.windows_path)

    def refresh_all(self) -> None:
        self.refresh_windows()
        self.refresh_android()

    def refresh_android(self) -> None:
        if self._android_loading:
            self._android_refresh_pending = True
            return
        if self.device_manager.active.mode not in {"ADB", "Recovery"}:
            self.android_panel.set_path(self.android_path)
            self.android_panel.set_items([])
            return
        path = self.android_path
        self._android_loading = True
        self.android_panel.set_path(self.android_path)
        worker = Worker(lambda: (path, self.adb.list_files(path)))
        worker.signals.result.connect(self._android_items_loaded)
        worker.signals.error.connect(lambda message, _trace: QMessageBox.warning(self, "Android files", message))
        worker.signals.finished.connect(self._android_refresh_finished)
        start_worker(self, self.pool, worker)

    def _android_refresh_finished(self) -> None:
        self._android_loading = False
        if self._android_refresh_pending:
            self._android_refresh_pending = False
            self.refresh_android()

    def _android_items_loaded(self, result: tuple[str, list]) -> None:
        path, items = result
        if path == self.android_path:
            self.android_panel.set_items(items)

    def navigate_android(self, path: str) -> None:
        self.android_path = path.strip() or "/sdcard/"
        self.refresh_android()

    def refresh_windows(self) -> None:
        if hasattr(self.windows_panel, "refresh"):
            self.windows_panel.refresh()
        else:
            self.windows_panel.set_path(self.windows_path)

    def navigate_windows(self, path: str) -> None:
        if not path:
            return
        target = Path(path)
        if target.exists() and target.is_dir():
            self.windows_path = str(target)
            self.refresh_windows()

    def _windows_path_changed(self, path: str) -> None:
        if path:
            self.windows_path = path

    def new_folder(self, kind: str) -> None:
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if not ok or not name.strip():
            return
        if kind == "android":
            target = join_android_path(self.android_path, name.strip())
            if not self._warn_android_write(target):
                return
            worker = Worker(lambda: self.adb.mkdir(target))
            worker.signals.result.connect(lambda result: self._command_done("New folder", result.status, self.refresh_android))
            start_worker(self, self.pool, worker)
        else:
            try:
                (Path(self.windows_path) / safe_filename(name)).mkdir(parents=True, exist_ok=False)
                self.refresh_windows()
            except OSError as exc:
                QMessageBox.warning(self, "New folder", str(exc))

    def delete_selected(self, kind: str) -> None:
        panel = self.android_panel if kind == "android" else self.windows_panel
        paths = panel.selected_paths()
        if not paths:
            return
        answer = QMessageBox.warning(self, "Delete", "Delete selected item(s)?", QMessageBox.Ok | QMessageBox.Cancel)
        if answer != QMessageBox.Ok:
            return
        if kind == "android":
            if any(not self._warn_android_write(path) for path in paths):
                return

            def run() -> list[str]:
                messages: list[str] = []
                for path in paths:
                    result = self.adb.delete(path, recursive=True)
                    messages.append(f"{path}: {result.status}")
                return messages

            worker = Worker(run)
            worker.signals.result.connect(lambda messages: self._messages_done("Delete", messages, self.refresh_android))
            start_worker(self, self.pool, worker)
        else:
            def run_delete() -> list[str]:
                messages: list[str] = []
                for path in paths:
                    try:
                        p = Path(path)
                        if p.is_dir():
                            shutil.rmtree(p)
                        else:
                            p.unlink()
                        messages.append(f"{path}: deleted")
                    except OSError as exc:
                        messages.append(f"{path}: {exc}")
                return messages

            worker = Worker(run_delete)
            worker.signals.result.connect(lambda messages: self._messages_done("Delete", messages, self.refresh_windows))
            start_worker(self, self.pool, worker)

    def rename_selected(self, kind: str) -> None:
        panel = self.android_panel if kind == "android" else self.windows_panel
        path = panel.selected_path()
        if not path:
            return
        new_name, ok = QInputDialog.getText(self, "Rename", "New name:", text=Path(path).name if kind == "windows" else path.rstrip("/").split("/")[-1])
        if not ok or not new_name.strip():
            return
        if kind == "android":
            target = join_android_path(parent_android_path(path), new_name.strip())
            if not self._warn_android_write(path):
                return
            worker = Worker(lambda: self.adb.rename(path, target))
            worker.signals.result.connect(lambda result: self._command_done("Rename", result.status, self.refresh_android))
            start_worker(self, self.pool, worker)
        else:
            try:
                Path(path).rename(Path(path).with_name(new_name.strip()))
                self.refresh_windows()
            except OSError as exc:
                QMessageBox.warning(self, "Rename", str(exc))

    def pull_selected(self) -> None:
        self.pull_paths(self.android_panel.selected_paths())

    def pull_paths(self, android_paths: list[str]) -> None:
        if not android_paths:
            return
        destination = Path(self.windows_path)
        cancel_event = threading.Event()
        dialog = self._create_transfer_dialog("ADB Pull")
        dialog.cancel_requested.connect(lambda: self._cancel_transfer(dialog, cancel_event))

        def run(item_callback=None) -> dict:
            return self._run_pull_transfer(android_paths, destination, cancel_event, item_callback)

        worker = Worker(run)
        worker.signals.item.connect(dialog.apply_update)
        worker.signals.result.connect(lambda result: self._transfer_done(dialog, result, self.refresh_windows))
        worker.signals.error.connect(lambda message, _trace: self._transfer_failed(dialog, "Pull to PC", message))
        start_worker(self, self.pool, worker)
        dialog.show()

    def push_selected(self) -> None:
        self.push_paths(self.windows_panel.selected_paths())

    def push_paths(self, local_paths: list[str]) -> None:
        if not local_paths:
            return
        if not self._warn_android_write(self.android_path):
            return
        cancel_event = threading.Event()
        dialog = self._create_transfer_dialog("ADB Push")
        dialog.cancel_requested.connect(lambda: self._cancel_transfer(dialog, cancel_event))

        def run(item_callback=None) -> dict:
            return self._run_push_transfer(local_paths, self.android_path, cancel_event, item_callback)

        worker = Worker(run)
        worker.signals.item.connect(dialog.apply_update)
        worker.signals.result.connect(lambda result: self._transfer_done(dialog, result, self.refresh_android))
        worker.signals.error.connect(lambda message, _trace: self._transfer_failed(dialog, "Push to device", message))
        start_worker(self, self.pool, worker)
        dialog.show()

    def copy_path(self, kind: str) -> None:
        panel = self.android_panel if kind == "android" else self.windows_panel
        path = panel.selected_path() or panel.current_path
        QGuiApplication.clipboard().setText(path)

    def properties(self, kind: str) -> None:
        panel = self.android_panel if kind == "android" else self.windows_panel
        path = panel.selected_path() or panel.current_path
        if kind == "android":
            worker = Worker(lambda: self.adb.stat(path))
            worker.signals.result.connect(lambda result: QMessageBox.information(self, "Properties", result.stdout or result.stderr or result.status))
            start_worker(self, self.pool, worker)
        else:
            try:
                stat = Path(path).stat()
                text = f"Path: {path}\nSize: {stat.st_size} bytes\nModified: {stat.st_mtime}"
                QMessageBox.information(self, "Properties", text)
            except OSError as exc:
                QMessageBox.warning(self, "Properties", str(exc))

    def open_explorer(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.windows_path))

    def _warn_android_write(self, path: str) -> bool:
        if is_probably_writable_android_path(path):
            return True
        answer = QMessageBox.warning(
            self,
            "Android path warning",
            "This path may be read-only or protected without root. Continue?",
            QMessageBox.Ok | QMessageBox.Cancel,
        )
        return answer == QMessageBox.Ok

    def _command_done(self, title: str, message: str, refresh) -> None:
        QMessageBox.information(self, title, message)
        refresh()

    def _messages_done(self, title: str, messages: list[str], refresh) -> None:
        QMessageBox.information(self, title, "\n".join(messages[:80]))
        refresh()

    def _create_transfer_dialog(self, title: str) -> TransferProgressDialog:
        dialog = TransferProgressDialog(title, self)
        self._transfer_dialogs.append(dialog)
        dialog.finished.connect(lambda _code, dlg=dialog: self._forget_transfer_dialog(dlg))
        return dialog

    def _forget_transfer_dialog(self, dialog: TransferProgressDialog) -> None:
        if dialog in self._transfer_dialogs:
            self._transfer_dialogs.remove(dialog)

    def _cancel_transfer(self, dialog: TransferProgressDialog, cancel_event: threading.Event) -> None:
        cancel_event.set()
        dialog.apply_update({"type": "cancelled"})

    def _transfer_done(self, dialog: TransferProgressDialog, result: dict, refresh) -> None:
        dialog.apply_update(
            {
                "type": "done",
                "success": result.get("success", False),
                "message": result.get("summary", "Transfer finished."),
            }
        )
        refresh()

    def _transfer_failed(self, dialog: TransferProgressDialog, title: str, message: str) -> None:
        dialog.apply_update({"type": "done", "success": False, "message": f"{title} failed: {message}"})

    def _run_pull_transfer(
        self,
        android_paths: list[str],
        destination: Path,
        cancel_event: threading.Event,
        item_callback,
    ) -> dict:
        entries = []
        for path in android_paths:
            size, count = self._android_transfer_stats(path)
            entries.append({"source": path, "destination": destination, "size": size, "count": count})
        return self._run_transfer_entries("Android -> Windows", entries, cancel_event, item_callback, is_pull=True)

    def _run_push_transfer(
        self,
        local_paths: list[str],
        android_destination: str,
        cancel_event: threading.Event,
        item_callback,
    ) -> dict:
        entries = []
        for path in local_paths:
            source = Path(path)
            size, count = self._local_transfer_stats(source)
            entries.append({"source": source, "destination": android_destination, "size": size, "count": count})
        return self._run_transfer_entries("Windows -> Android", entries, cancel_event, item_callback, is_pull=False)

    def _run_transfer_entries(
        self,
        direction: str,
        entries: list[dict],
        cancel_event: threading.Event,
        item_callback,
        is_pull: bool,
    ) -> dict:
        started = time.monotonic()
        total_bytes = sum(entry["size"] for entry in entries if isinstance(entry["size"], int) and entry["size"] > 0)
        total_files = sum(entry["count"] for entry in entries if isinstance(entry["count"], int) and entry["count"] > 0) or len(entries)
        done_bytes = 0
        done_files = 0
        messages: list[str] = []
        self._emit_transfer(
            item_callback,
            {
                "type": "plan",
                "title": "ADB transfer started",
                "direction": direction,
                "total_files": total_files,
                "total_bytes": total_bytes,
                "source": "\n".join(str(entry["source"]) for entry in entries),
                "destination": str(entries[0]["destination"]) if entries else "",
                "message": f"Prepared {len(entries)} selected item(s), estimated files: {total_files}, estimated bytes: {self._format_bytes(total_bytes)}.",
            },
        )

        success = True
        for index, entry in enumerate(entries, start=1):
            if cancel_event.is_set():
                success = False
                messages.append("Transfer cancelled by user.")
                break
            source = entry["source"]
            destination = entry["destination"]
            entry_size = entry["size"] if isinstance(entry["size"], int) and entry["size"] > 0 else 0
            entry_count = entry["count"] if isinstance(entry["count"], int) and entry["count"] > 0 else 1
            command = self._transfer_command_text(source, destination, is_pull)
            self._emit_transfer(
                item_callback,
                {
                    "type": "file_start",
                    "current_file": f"{index}/{len(entries)}: {source}",
                    "command": command,
                    "message": f"Starting: {command}",
                },
            )

            last_percent = 0

            def on_output(channel: str, text: str) -> None:
                nonlocal last_percent
                percent = self._extract_percent(text)
                if percent is not None:
                    last_percent = percent
                current_bytes = done_bytes + int(entry_size * last_percent / 100) if entry_size else done_bytes
                self._emit_transfer(
                    item_callback,
                    {
                        "type": "progress",
                        "done_bytes": current_bytes,
                        "total_bytes": total_bytes,
                        "speed": self._speed_text(current_bytes, started),
                        "output": f"[{channel}] {text.strip()}",
                    },
                )

            transfer_state = self._run_entry_command_with_progress(
                source=source,
                destination=destination,
                is_pull=is_pull,
                timeout=900,
                cancel_event=cancel_event,
                output_callback=on_output,
                item_callback=item_callback,
                entry_size=entry_size,
                done_bytes=done_bytes,
                total_bytes=total_bytes,
                total_files=total_files,
                done_files=done_files,
                started=started,
            )
            result = transfer_state.get("result")
            observed_bytes = int(transfer_state.get("observed_bytes") or 0)
            observed_files = int(transfer_state.get("observed_files") or 0)
            if result is None:
                success = False
                done_bytes += observed_bytes
                done_files += observed_files
                message = f"{source} -> {destination}: transfer process did not return a result"
                messages.append(message)
                self._emit_transfer(
                    item_callback,
                    {
                        "type": "file_done",
                        "done_files": done_files,
                        "total_files": max(total_files, done_files),
                        "done_bytes": done_bytes,
                        "total_bytes": max(total_bytes, done_bytes),
                        "speed": self._speed_text(done_bytes, started),
                        "message": message,
                    },
                )
                continue
            if result.success:
                done_bytes += max(entry_size, observed_bytes)
                done_files += max(entry_count, observed_files)
            else:
                success = False
                done_bytes += observed_bytes
                done_files += observed_files
            message = f"{source} -> {destination}: {result.status}"
            messages.append(message)
            self._emit_transfer(
                item_callback,
                {
                    "type": "file_done",
                    "done_files": done_files,
                    "total_files": max(total_files, done_files),
                    "done_bytes": done_bytes,
                    "total_bytes": max(total_bytes, done_bytes),
                    "speed": self._speed_text(done_bytes, started),
                    "message": message,
                },
            )
        elapsed = time.monotonic() - started
        reported_total_files = max(total_files, done_files)
        summary = (
            f"Transfer {'completed' if success else 'finished with errors'}: "
            f"{done_files}/{reported_total_files} files, {self._format_bytes(done_bytes)} in {elapsed:.1f}s."
        )
        if messages:
            summary += "\n" + "\n".join(messages[-10:])
        return {"success": success, "summary": summary, "messages": messages}

    def _emit_transfer(self, item_callback, update: dict) -> None:
        if item_callback:
            item_callback.emit(update)

    def _run_entry_command_with_progress(
        self,
        source,
        destination,
        is_pull: bool,
        timeout: int,
        cancel_event: threading.Event,
        output_callback,
        item_callback,
        entry_size: int,
        done_bytes: int,
        total_bytes: int,
        total_files: int,
        done_files: int,
        started: float,
    ) -> dict:
        result_holder = {}
        command_done = threading.Event()
        entry_started = time.monotonic()
        baseline = self._transfer_observation_baseline(source, destination, is_pull)
        latest_bytes = 0
        latest_files = 0

        def run_command() -> None:
            try:
                if is_pull:
                    result_holder["result"] = self.adb.pull_streaming(
                        str(source),
                        destination,
                        timeout=timeout,
                        output_callback=output_callback,
                        cancel_event=cancel_event,
                    )
                else:
                    result_holder["result"] = self.adb.push_streaming(
                        source,
                        str(destination),
                        timeout=timeout,
                        output_callback=output_callback,
                        cancel_event=cancel_event,
                    )
            finally:
                command_done.set()

        thread = threading.Thread(target=run_command, daemon=True)
        thread.start()

        while not command_done.wait(0.5):
            if cancel_event.is_set():
                break
            latest_bytes, latest_files = self._observed_transfer_stats(
                source,
                destination,
                is_pull,
                entry_size,
                entry_started,
                baseline,
            )
            current_bytes = done_bytes + max(0, latest_bytes)
            current_files = done_files + max(0, latest_files)
            self._emit_transfer(
                item_callback,
                {
                    "type": "heartbeat",
                    "done_bytes": current_bytes,
                    "total_bytes": max(total_bytes, current_bytes),
                    "done_files": current_files,
                    "total_files": max(total_files, current_files),
                    "speed": self._speed_text(current_bytes, started),
                    "activity": "ADB transfer is running",
                },
            )

        thread.join(timeout=3)
        latest_bytes, latest_files = self._observed_transfer_stats(
            source,
            destination,
            is_pull,
            entry_size,
            entry_started,
            baseline,
        )
        return {
            "result": result_holder.get("result"),
            "observed_bytes": max(0, latest_bytes),
            "observed_files": max(0, latest_files),
        }

    def _transfer_observation_baseline(self, source, destination, is_pull: bool) -> tuple[int, int]:
        if not is_pull:
            return (0, 0)
        target = self._local_pull_target(str(source), Path(destination))
        return self._local_transfer_stats(target) if target.exists() else (0, 0)

    def _observed_transfer_stats(
        self,
        source,
        destination,
        is_pull: bool,
        entry_size: int,
        entry_started: float,
        baseline: tuple[int, int],
    ) -> tuple[int, int]:
        if is_pull:
            target = self._local_pull_target(str(source), Path(destination))
            if not target.exists():
                return (0, 0)
            size, count = self._local_transfer_stats(target)
            return (max(0, size - baseline[0]), max(0, count - baseline[1]))
        if entry_size <= 0:
            return (0, 0)
        elapsed = max(0.0, time.monotonic() - entry_started)
        # adb may not print progress on every platform-tools build. Keep the UI
        # visibly alive without claiming completion before adb exits.
        synthetic_ratio = min(0.92, elapsed / max(20.0, entry_size / (8 * 1024 * 1024)))
        return (int(entry_size * synthetic_ratio), 0)

    def _local_pull_target(self, source: str, destination: Path) -> Path:
        source_name = PurePosixPath(source.rstrip("/")).name or Path(source).name
        if destination.exists() and destination.is_dir():
            return destination / source_name
        return destination

    def _transfer_command_text(self, source, destination, is_pull: bool) -> str:
        args = ["pull", str(source), str(destination)] if is_pull else ["push", str(source), str(destination)]
        return self.adb.runner.command_text([*self.adb._base(), *args])

    def _extract_percent(self, text: str) -> int | None:
        match = PERCENT_PATTERN.search(text)
        if not match:
            return None
        value = max(0, min(100, int(match.group(1))))
        return value

    def _local_transfer_stats(self, path: Path) -> tuple[int, int]:
        try:
            if path.is_file():
                return path.stat().st_size, 1
            total = 0
            count = 0
            for child in path.rglob("*"):
                try:
                    if child.is_file():
                        total += child.stat().st_size
                        count += 1
                except OSError:
                    continue
            return total, count
        except OSError:
            return 0, 0

    def _android_transfer_stats(self, path: str) -> tuple[int, int]:
        quoted = shell_quote(path)
        kind = (self.adb.run_shell(f"if [ -d {quoted} ]; then echo dir; else echo file; fi", timeout=15).stdout or "").strip()
        if kind == "dir":
            count_result = self.adb.run_shell(f"find {quoted} -type f 2>/dev/null | wc -l", timeout=60)
            size_result = self.adb.run_shell(f"du -s -k {quoted} 2>/dev/null", timeout=60)
            count = self._first_int(count_result.stdout) or 1
            size_kb = self._first_int(size_result.stdout) or 0
            return size_kb * 1024, count
        size_result = self.adb.run_shell(f"stat -c %s {quoted} 2>/dev/null", timeout=15)
        return self._first_int(size_result.stdout) or 0, 1

    def _first_int(self, text: str) -> int | None:
        match = re.search(r"\d+", text or "")
        return int(match.group(0)) if match else None

    def _speed_text(self, bytes_done: int, started: float) -> str:
        elapsed = max(0.1, time.monotonic() - started)
        return f"{self._format_bytes(bytes_done / elapsed)}/s"

    def _format_bytes(self, size: int | float | None) -> str:
        if size is None:
            return "Unknown"
        value = float(size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return str(size)
