"""Platform Tools transfer strategy used by the File Manager.

The strategy deliberately contains no Qt dependencies.  ``FileManagerPage``
inherits it only to preserve the project's established private compatibility
seams while the transfer worker itself is coordinated in core.
"""

from __future__ import annotations

from bisect import bisect_right
import os
import re
import tarfile
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from openadb.core.adb import ADBClient
from openadb.core.path_utils import (
    is_probably_writable_android_path,
    join_android_path,
    parent_android_path,
    shell_quote,
)


PERCENT_PATTERN = re.compile(r"(\d{1,3})\s*%")
ADB_LSTAT_FAILED_PATTERN = re.compile(r"cannot lstat '([^']+)'", re.IGNORECASE)
FAST_TAR_MIN_FILES = 256
FAST_TAR_MAX_AVERAGE_FILE_SIZE = 2 * 1024 * 1024
FAST_TAR_MAX_LARGE_FILE_RATIO = 0.05
FAST_TAR_LARGE_FILE_SIZE = 16 * 1024 * 1024
FAST_TAR_COPY_BUFFER_SIZE = 4 * 1024 * 1024
WIRELESS_FAST_TAR_COPY_BUFFER_SIZE = 8 * 1024 * 1024
FAST_TAR_PULL_MIN_FILES = 8
ADB_PUSH_LARGE_AVERAGE_FILE_SIZE = 16 * 1024 * 1024
ADB_PUSH_LARGE_TOTAL_SIZE = 8 * 1024 * 1024 * 1024
ADB_PUSH_LARGE_OBSERVATION_INTERVAL = 4.0
ADB_PUSH_DEFAULT_OBSERVATION_INTERVAL = 2.0
ADB_PUSH_FIRST_OBSERVATION_DELAY = 0.8
ADB_PUSH_PROGRESS_INTERPOLATION_CAP = 0.985
ADB_TRANSFER_DISABLE_COMPRESSION_SIZE = 256 * 1024 * 1024
ADB_TRANSFER_DISABLE_COMPRESSION_AVERAGE = 8 * 1024 * 1024
SINGLE_FILE_STREAM_BUFFER_SIZE = 4 * 1024 * 1024
WIRELESS_SINGLE_FILE_STREAM_BUFFER_SIZE = 8 * 1024 * 1024
SINGLE_FILE_STREAM_PROGRESS_INTERVAL = 0.2


class _ProgressFile:
    def __init__(self, fileobj: BinaryIO, on_read, cancel_event: threading.Event) -> None:
        self._fileobj = fileobj
        self._on_read = on_read
        self._cancel_event = cancel_event

    def read(self, size: int = -1) -> bytes:
        if self._cancel_event.is_set():
            raise OSError("Transfer cancelled by user")
        data = self._fileobj.read(size)
        if data:
            self._on_read(len(data))
        return data


class ADBTransferStrategy:
    """Reusable ADB/tar/stream transfer execution with normalized progress."""
    def _root_available_for_worker(
        self,
        adb: ADBClient,
        requested: bool,
        cancel_event=None,
    ) -> bool:
        return bool(
            requested
            and adb.root_available(cancel_event=cancel_event)
        )

    def _is_wireless_adb_transport(self, serial: str) -> bool:
        serial = str(serial or "").strip()
        if not serial:
            return False
        if serial.startswith("[") and "]:" in serial:
            return True
        if re.match(r"^[^:\\s]+:\\d{2,5}$", serial):
            return True
        return bool(re.match(r"^(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}$", serial))

    def _single_file_stream_buffer_size(self, wireless_mode: bool) -> int:
        return WIRELESS_SINGLE_FILE_STREAM_BUFFER_SIZE if wireless_mode else SINGLE_FILE_STREAM_BUFFER_SIZE

    def _tar_copy_buffer_size(self, wireless_mode: bool) -> int:
        return WIRELESS_FAST_TAR_COPY_BUFFER_SIZE if wireless_mode else FAST_TAR_COPY_BUFFER_SIZE

    def _run_pull_transfer(
        self,
        adb: ADBClient,
        android_paths: list[str],
        destination: Path,
        cancel_event: threading.Event,
        item_callback,
        use_root_requested: bool,
    ) -> dict:
        entries = []
        if cancel_event.is_set():
            return self._run_transfer_entries(
                adb,
                "Android → PC",
                entries,
                cancel_event,
                item_callback,
                is_pull=True,
                use_root_requested=False,
            )
        use_root = self._root_available_for_worker(
            adb,
            use_root_requested,
            cancel_event,
        )
        for path in android_paths:
            if cancel_event.is_set():
                break
            size, count, is_dir = self._android_transfer_stats_with_kind(
                adb,
                path,
                use_root=use_root,
                cancel_event=cancel_event,
            )
            if cancel_event.is_set():
                break
            entries.append({"source": path, "destination": destination, "size": size, "count": count, "is_dir": is_dir})
        return self._run_transfer_entries(
            adb,
            "Android → PC",
            entries,
            cancel_event,
            item_callback,
            is_pull=True,
            use_root_requested=use_root,
        )

    def _run_adb_push_transfer(
        self,
        adb: ADBClient,
        local_paths: list[str],
        android_destination: str,
        cancel_event: threading.Event,
        item_callback,
        use_root_requested: bool,
    ) -> dict:
        entries = []
        for path in local_paths:
            if cancel_event.is_set():
                break
            source = Path(path)
            size, count, file_markers = self._local_transfer_stats_with_markers(
                source,
                cancel_event=cancel_event,
            )
            entries.append(
                {
                    "source": source,
                    "destination": android_destination,
                    "size": size,
                    "count": count,
                    "file_markers": file_markers,
                }
            )
        return self._run_transfer_entries(
            adb,
            "PC → Android",
            entries,
            cancel_event,
            item_callback,
            is_pull=False,
            use_root_requested=use_root_requested,
        )

    def _run_transfer_entries(
        self,
        adb: ADBClient,
        direction: str,
        entries: list[dict],
        cancel_event: threading.Event,
        item_callback,
        is_pull: bool,
        use_root_requested: bool = False,
    ) -> dict:
        started = time.monotonic()
        total_bytes = sum(entry["size"] for entry in entries if isinstance(entry["size"], int) and entry["size"] > 0)
        total_files = sum(entry["count"] for entry in entries if isinstance(entry["count"], int) and entry["count"] > 0) or len(entries)
        done_bytes = 0
        done_files = 0
        messages: list[str] = []
        if cancel_event.is_set():
            return {
                "success": False,
                "cancelled": True,
                "summary": "Transfer cancelled before it started.",
                "messages": ["Transfer cancelled before it started."],
            }
        tar_command = adb.detect_tar_command(cancel_event=cancel_event)
        if cancel_event.is_set():
            return {
                "success": False,
                "cancelled": True,
                "summary": "Transfer cancelled while preparing transfer tools.",
                "messages": ["Transfer cancelled while preparing transfer tools."],
            }
        root_available = False
        root_message = ""
        wireless_mode = self._is_wireless_adb_transport(adb.serial)
        wireless_message = ""
        if wireless_mode:
            wireless_message = (
                "Wireless ADB fast mode is active. OpenADB will prefer one long streaming transfer "
                "over many per-file ADB operations."
            )
        if use_root_requested:
            root_available = adb.root_available(cancel_event=cancel_event)
            if root_available:
                root_message = "Root boost is active. OpenADB will use su/root streaming where it is safer or faster."
            else:
                root_message = "Root boost was requested, but root access was not granted. Using normal ADB transfer."
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
                "message": (
                    f"Prepared {len(entries)} selected item(s), estimated files: {total_files}, "
                    f"estimated bytes: {self._format_bytes(total_bytes)}."
                    + (f"\n{wireless_message}" if wireless_message else "")
                    + (f"\n{root_message}" if root_message else "")
                ),
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
            file_markers = entry.get("file_markers") if isinstance(entry.get("file_markers"), list) else []
            root_mode = root_available and use_root_requested
            fast_push = self._should_use_fast_tar_push(
                source,
                entry_size,
                entry_count,
                file_markers,
                tar_command,
                is_pull,
                root_mode,
                str(destination),
                wireless_mode,
            )
            fast_pull = self._should_use_fast_tar_pull(
                source,
                entry_size,
                entry_count,
                tar_command,
                is_pull,
                bool(entry.get("is_dir")),
                root_mode,
                wireless_mode,
            )
            stream_file = self._should_use_single_file_stream(
                source,
                is_pull,
                entry_count,
                bool(entry.get("is_dir")),
                wireless_mode,
            )
            transfer_source = source
            transfer_destination = destination
            if root_mode and is_pull and (fast_pull or stream_file):
                transfer_source = self._root_accel_android_path(str(source), preserve_root_name=True)
            elif root_mode and not is_pull and (fast_push or stream_file):
                transfer_destination = self._root_accel_android_path(str(destination))
            disable_adb_compression = self._should_disable_adb_compression(
                source,
                entry_size,
                entry_count,
                file_markers,
                fast_push=fast_push,
                fast_pull=fast_pull,
                stream_file=stream_file,
            )
            command = self._transfer_command_text(
                adb,
                source,
                destination,
                is_pull,
                fast_push=fast_push,
                fast_pull=fast_pull,
                tar_command=tar_command,
                stream_file=stream_file,
                root_mode=root_mode,
                transfer_source=transfer_source,
                transfer_destination=transfer_destination,
                disable_compression=disable_adb_compression,
            )
            start_message = f"Starting: {command}"
            if fast_pull:
                start_message = f"Starting {'root ' if root_mode else ''}fast TAR pull mode: {command}"
            elif fast_push:
                start_message = f"Starting {'root ' if root_mode else ''}fast TAR push mode: {command}"
            elif stream_file:
                start_message = f"Starting {'root ' if root_mode else ''}live single-file stream: {command}"
            elif disable_adb_compression:
                start_message = f"Starting: {command}\nUsing native ADB transfer with compression disabled for large/already-compressed files."
            elif is_pull and bool(entry.get("is_dir")) and not tar_command:
                start_message = f"Starting: {command}\nFast TAR pull mode is unavailable because Android tar was not found."
            elif not is_pull and Path(source).is_dir() and tar_command:
                start_message = f"Starting: {command}\nUsing standard adb push because this folder is better suited for native ADB transfer."
            elif not is_pull and Path(source).is_dir() and not tar_command:
                start_message = f"Starting: {command}\nFast TAR push mode is unavailable because Android tar was not found."
            self._emit_transfer(
                item_callback,
                {
                    "type": "file_start",
                    "current_file": self._current_transfer_file_label(source, 0, file_markers),
                    "command": command,
                    "message": start_message,
                },
            )

            last_percent = 0

            def on_output(channel: str, text: str) -> None:
                nonlocal last_percent
                percent = self._extract_percent(text)
                if percent is not None:
                    last_percent = max(last_percent, percent)
                current_entry_bytes = int(entry_size * last_percent / 100) if entry_size else 0
                current_entry_files = (
                    0
                    if is_pull
                    else self._estimate_observed_files(entry_count, entry_size, current_entry_bytes, file_markers)
                )
                current_bytes = done_bytes + current_entry_bytes
                current_files = done_files + current_entry_files
                current_file = (
                    ""
                    if is_pull
                    else self._current_transfer_file_label(source, current_entry_bytes, file_markers)
                )
                update = {
                    "type": "progress",
                    "done_bytes": current_bytes,
                    "total_bytes": total_bytes,
                    "done_files": current_files,
                    "total_files": total_files,
                    "speed": self._speed_text(current_bytes, started),
                    "output": f"[{channel}] {text.strip()}",
                }
                if current_file:
                    update["current_file"] = current_file
                self._emit_transfer(item_callback, update)

            transfer_state = self._run_entry_command_with_progress(
                adb=adb,
                source=source,
                destination=destination,
                is_pull=is_pull,
                transfer_source=transfer_source,
                transfer_destination=transfer_destination,
                root_mode=root_mode,
                timeout=None,
                cancel_event=cancel_event,
                output_callback=on_output,
                item_callback=item_callback,
                entry_size=entry_size,
                done_bytes=done_bytes,
                total_bytes=total_bytes,
                total_files=total_files,
                done_files=done_files,
                started=started,
                entry_count=entry_count,
                file_markers=file_markers,
                fast_push=fast_push,
                fast_pull=fast_pull,
                tar_command=tar_command,
                stream_file=stream_file,
                entry_is_dir=bool(entry.get("is_dir")),
                disable_compression=disable_adb_compression,
                wireless_mode=wireless_mode,
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
        adb: ADBClient,
        source,
        destination,
        is_pull: bool,
        transfer_source,
        transfer_destination,
        root_mode: bool,
        timeout: int | float | None,
        cancel_event: threading.Event,
        output_callback,
        item_callback,
        entry_size: int,
        done_bytes: int,
        total_bytes: int,
        total_files: int,
        done_files: int,
        started: float,
        entry_count: int,
        file_markers: list[tuple[int, str]],
        fast_push: bool = False,
        fast_pull: bool = False,
        tar_command: str = "",
        stream_file: bool = False,
        entry_is_dir: bool = False,
        disable_compression: bool = False,
        wireless_mode: bool = False,
    ) -> dict:
        if fast_pull:
            return self._run_fast_tar_pull_with_progress(
                adb=adb,
                source=str(transfer_source),
                destination=Path(destination),
                tar_command=tar_command,
                cancel_event=cancel_event,
                output_callback=output_callback,
                item_callback=item_callback,
                entry_size=entry_size,
                done_bytes=done_bytes,
                total_bytes=total_bytes,
                total_files=total_files,
                done_files=done_files,
                started=started,
                entry_count=entry_count,
                use_root=root_mode,
                wireless_mode=wireless_mode,
            )
        if fast_push:
            return self._run_fast_tar_push_with_progress(
                adb=adb,
                source=Path(source),
                destination=str(transfer_destination),
                tar_command=tar_command,
                cancel_event=cancel_event,
                output_callback=output_callback,
                item_callback=item_callback,
                entry_size=entry_size,
                done_bytes=done_bytes,
                total_bytes=total_bytes,
                total_files=total_files,
                done_files=done_files,
                started=started,
                entry_count=entry_count,
                use_root=root_mode,
                wireless_mode=wireless_mode,
            )
        if stream_file and is_pull and not entry_is_dir:
            return self._run_single_file_pull_with_progress(
                adb=adb,
                source=str(transfer_source),
                display_source=str(source),
                destination=Path(destination),
                cancel_event=cancel_event,
                output_callback=output_callback,
                item_callback=item_callback,
                entry_size=entry_size,
                done_bytes=done_bytes,
                total_bytes=total_bytes,
                total_files=total_files,
                done_files=done_files,
                started=started,
                use_root=root_mode,
                wireless_mode=wireless_mode,
            )
        if stream_file and not is_pull and isinstance(source, Path) and source.is_file():
            return self._run_single_file_push_with_progress(
                adb=adb,
                source=source,
                destination=str(transfer_destination),
                cancel_event=cancel_event,
                output_callback=output_callback,
                item_callback=item_callback,
                entry_size=entry_size,
                done_bytes=done_bytes,
                total_bytes=total_bytes,
                total_files=total_files,
                done_files=done_files,
                started=started,
                use_root=root_mode,
                wireless_mode=wireless_mode,
            )

        result_holder = {}
        command_done = threading.Event()
        entry_started_wall = time.time()
        entry_started_monotonic = time.monotonic()
        baseline = self._transfer_observation_baseline(
            adb,
            source,
            destination,
            is_pull,
            cancel_event=cancel_event,
        )
        latest_bytes = 0
        latest_files = 0
        latest_file = self._current_transfer_file_label(source, 0, file_markers)
        observed_speed = 0.0
        previous_observation_bytes = 0
        previous_observation_time = entry_started_monotonic
        observation_interval = (
            1.0
            if is_pull
            else self._push_observation_interval(entry_size, entry_count, file_markers)
        )
        next_observation = (
            0.0
            if is_pull
            else entry_started_monotonic + min(ADB_PUSH_FIRST_OBSERVATION_DELAY, observation_interval)
        )

        def run_command() -> None:
            try:
                if is_pull:
                    result_holder["result"] = adb.pull_streaming(
                        str(source),
                        destination,
                        timeout=timeout,
                        output_callback=output_callback,
                        cancel_event=cancel_event,
                        disable_compression=disable_compression,
                    )
                else:
                    result_holder["result"] = adb.push_streaming(
                        source,
                        str(destination),
                        timeout=timeout,
                        output_callback=output_callback,
                        cancel_event=cancel_event,
                        disable_compression=disable_compression,
                    )
            except BaseException as exc:  # Propagate command-thread failures through the owning Worker.
                result_holder["error"] = exc
            finally:
                command_done.set()

        thread = threading.Thread(target=run_command, daemon=True)
        thread.start()

        while not command_done.wait(0.5):
            if cancel_event.is_set():
                break
            now = time.monotonic()
            if now >= next_observation:
                latest_bytes, latest_files, latest_file = self._observed_transfer_stats(
                    adb,
                    source,
                    destination,
                    is_pull,
                    entry_size,
                    entry_started_wall,
                    baseline,
                    entry_count,
                    file_markers,
                    cancel_event=cancel_event,
                )
                if latest_bytes >= previous_observation_bytes:
                    delta_bytes = latest_bytes - previous_observation_bytes
                    delta_seconds = max(0.1, now - previous_observation_time)
                    if delta_bytes > 0:
                        observed_speed = delta_bytes / delta_seconds
                    previous_observation_bytes = latest_bytes
                    previous_observation_time = now
                next_observation = now + observation_interval
            current_entry_bytes = max(0, latest_bytes)
            current_entry_files = max(0, latest_files)
            current_file = latest_file
            if not is_pull and entry_size > current_entry_bytes and observed_speed > 0:
                estimated_bytes = int(latest_bytes + observed_speed * max(0.0, now - previous_observation_time))
                interpolation_cap = max(current_entry_bytes, int(entry_size * ADB_PUSH_PROGRESS_INTERPOLATION_CAP))
                estimated_bytes = min(max(current_entry_bytes, estimated_bytes), interpolation_cap)
                if estimated_bytes > current_entry_bytes:
                    current_entry_bytes = estimated_bytes
                    current_entry_files = max(
                        current_entry_files,
                        self._estimate_observed_files(entry_count, entry_size, estimated_bytes, file_markers),
                    )
                    current_file = self._current_transfer_file_label(source, estimated_bytes, file_markers)
            current_bytes = done_bytes + current_entry_bytes
            current_files = done_files + current_entry_files
            self._emit_transfer(
                item_callback,
                {
                    "type": "heartbeat",
                    "done_bytes": current_bytes,
                    "total_bytes": max(total_bytes, current_bytes),
                    "done_files": current_files,
                    "total_files": max(total_files, current_files),
                    "current_file": current_file,
                    "speed": self._speed_text(current_bytes, started),
                    "activity": "ADB transfer is running",
                },
            )

        thread.join(timeout=1 if cancel_event.is_set() else 8)
        if thread.is_alive():
            cancel_event.set()
            thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("The ADB transfer process did not stop after cancellation.")
        command_error = result_holder.get("error")
        if command_error is not None:
            raise command_error
        result = result_holder.get("result")
        if (
            not cancel_event.is_set()
            and not is_pull
            and result is not None
            and isinstance(source, Path)
            and source.is_dir()
        ):
            missing_files = self._standard_push_failed_local_paths(
                result,
                source,
                cancel_event=cancel_event,
            )
            if missing_files:
                fixed_files, failed_files = self._repair_standard_push_missing_files(
                    adb=adb,
                    missing_files=missing_files,
                    source_root=source,
                    destination=str(destination),
                    cancel_event=cancel_event,
                    output_callback=output_callback,
                    item_callback=item_callback,
                    entry_size=entry_size,
                    entry_count=entry_count,
                    done_bytes=done_bytes,
                    done_files=done_files,
                    total_bytes=total_bytes,
                    total_files=total_files,
                    started=started,
                    use_root=root_mode,
                )
                if failed_files:
                    result.success = False
                    result.status = (
                        f"Partial transfer: repaired {fixed_files}/{len(missing_files)} long-path file(s); "
                        f"{len(failed_files)} file(s) still failed."
                    )
                    failed_text = "\n".join(str(path) for path in failed_files[:10])
                    result.stderr = (result.stderr + "\n" if result.stderr else "") + failed_text
                elif fixed_files:
                    result.status = f"Success; repaired {fixed_files} long-path file(s) through OpenADB streaming fallback."
        if not cancel_event.is_set() and not is_pull and result is not None and result.success:
            return {
                "result": result,
                "observed_bytes": entry_size,
                "observed_files": entry_count,
            }
        if not cancel_event.is_set():
            latest_bytes, latest_files, latest_file = self._observed_transfer_stats(
                adb,
                source,
                destination,
                is_pull,
                entry_size,
                entry_started_wall,
                baseline,
                entry_count,
                file_markers,
                cancel_event=cancel_event,
            )
        return {
            "result": result_holder.get("result"),
            "observed_bytes": max(0, latest_bytes),
            "observed_files": max(0, latest_files),
        }

    def _standard_push_failed_local_paths(
        self,
        result,
        source_root: Path,
        cancel_event: threading.Event | None = None,
    ) -> list[Path]:
        if cancel_event is not None and cancel_event.is_set():
            return []
        text = "\n".join(part for part in [getattr(result, "stdout", ""), getattr(result, "stderr", "")] if part)
        if not text:
            return []
        candidates = []
        for match in ADB_LSTAT_FAILED_PATTERN.finditer(text):
            raw_path = match.group(1).strip()
            if raw_path:
                candidates.append(raw_path)
        if not candidates:
            return []
        known_files = {}
        try:
            for path in source_root.rglob("*"):
                if cancel_event is not None and cancel_event.is_set():
                    break
                if path.is_file():
                    known_files[os.path.normcase(str(path))] = path
        except OSError:
            known_files = {}
        failed: list[Path] = []
        seen: set[str] = set()
        for raw_path in candidates:
            if cancel_event is not None and cancel_event.is_set():
                break
            path = Path(raw_path)
            if not path.exists():
                path = known_files.get(os.path.normcase(raw_path), path)
            if not path.exists() or not path.is_file():
                continue
            key = os.path.normcase(str(path))
            if key in seen:
                continue
            seen.add(key)
            failed.append(path)
        return failed

    def _repair_standard_push_missing_files(
        self,
        adb: ADBClient,
        missing_files: list[Path],
        source_root: Path,
        destination: str,
        cancel_event: threading.Event,
        output_callback,
        item_callback,
        entry_size: int,
        entry_count: int,
        done_bytes: int,
        done_files: int,
        total_bytes: int,
        total_files: int,
        started: float,
        use_root: bool,
    ) -> tuple[int, list[Path]]:
        missing_sizes = []
        for path in missing_files:
            try:
                missing_sizes.append(path.stat().st_size)
            except OSError:
                missing_sizes.append(0)
        missing_total = sum(missing_sizes)
        base_bytes = max(0, entry_size - missing_total)
        base_files = max(0, entry_count - len(missing_files))
        repaired_bytes = 0
        repaired_files = 0
        failed_files: list[Path] = []

        for path, size in zip(missing_files, missing_sizes):
            if cancel_event.is_set():
                failed_files.append(path)
                continue
            try:
                relative = path.relative_to(source_root).as_posix()
            except ValueError:
                relative = path.name
            remote_target = join_android_path(join_android_path(destination, source_root.name), relative)
            target_use_root = bool(use_root and not is_probably_writable_android_path(remote_target))
            result, sent = self._stream_push_file_to_android_target(
                adb=adb,
                source=path,
                target=remote_target,
                cancel_event=cancel_event,
                output_callback=output_callback,
                item_callback=item_callback,
                base_done_bytes=done_bytes + base_bytes + repaired_bytes,
                base_done_files=done_files + base_files + repaired_files,
                total_bytes=total_bytes,
                total_files=total_files,
                started=started,
                use_root=target_use_root,
                activity="Long Windows path fallback push is running",
                expected_size=size,
            )
            repaired_bytes += sent if result.success else 0
            if result.success:
                repaired_files += 1
            else:
                failed_files.append(path)
        return repaired_files, failed_files

    def _stream_push_file_to_android_target(
        self,
        adb: ADBClient,
        source: Path,
        target: str,
        cancel_event: threading.Event,
        output_callback,
        item_callback,
        base_done_bytes: int,
        base_done_files: int,
        total_bytes: int,
        total_files: int,
        started: float,
        use_root: bool = False,
        wireless_mode: bool = False,
        activity: str = "ADB single-file push is running",
        expected_size: int | None = None,
    ) -> tuple[object, int]:
        temp_target = self._android_temp_sibling_path(target)
        sent_bytes = 0
        last_emit = 0.0
        buffer_size = self._single_file_stream_buffer_size(wireless_mode)
        if expected_size is None:
            expected_size = source.stat().st_size
        expected_size = max(0, int(expected_size))
        source_changed = ""

        def emit_progress(force: bool = False) -> None:
            nonlocal last_emit
            now = time.monotonic()
            if not force and now - last_emit < SINGLE_FILE_STREAM_PROGRESS_INTERVAL:
                return
            last_emit = now
            current_bytes = base_done_bytes + max(0, sent_bytes)
            # A streamed item counts as complete only after local/remote size
            # verification and atomic publish; the outer file_done event owns it.
            current_files = base_done_files
            self._emit_transfer(
                item_callback,
                {
                    "type": "heartbeat",
                    "done_bytes": current_bytes,
                    "total_bytes": max(total_bytes, current_bytes),
                    "done_files": current_files,
                    "total_files": max(total_files, current_files),
                    "current_file": str(source),
                    "speed": self._speed_text(current_bytes, started),
                    "activity": activity,
                },
            )

        def input_writer(stream: BinaryIO) -> None:
            nonlocal sent_bytes, source_changed
            with source.open("rb") as fileobj:
                opened = os.fstat(fileobj.fileno())
                initial_fingerprint = (
                    int(opened.st_dev),
                    int(opened.st_ino),
                    int(opened.st_size),
                    int(opened.st_mtime_ns),
                )
                if opened.st_size != expected_size:
                    source_changed = (
                        f"Source size changed before streaming: expected {expected_size} bytes, "
                        f"found {opened.st_size} bytes"
                    )
                    emit_progress(force=True)
                    return
                while sent_bytes < expected_size:
                    if cancel_event.is_set():
                        raise OSError("Transfer cancelled by user")
                    chunk = fileobj.read(min(buffer_size, expected_size - sent_bytes))
                    if not chunk:
                        source_changed = (
                            f"Source ended during streaming: expected {expected_size} bytes, "
                            f"read {sent_bytes} bytes"
                        )
                        break
                    stream.write(chunk)
                    sent_bytes += len(chunk)
                    emit_progress()
                if not source_changed and fileobj.read(1):
                    source_changed = (
                        f"Source grew during streaming: expected {expected_size} bytes"
                    )
                finished = os.fstat(fileobj.fileno())
                final_fingerprint = (
                    int(finished.st_dev),
                    int(finished.st_ino),
                    int(finished.st_size),
                    int(finished.st_mtime_ns),
                )
                if not source_changed and final_fingerprint != initial_fingerprint:
                    source_changed = "Source metadata changed during streaming"
            emit_progress(force=True)

        script = (
            f"target={shell_quote(target)}; tmp={shell_quote(temp_target)}; "
            'parent=${target%/*}; [ "$parent" = "$target" ] && parent=.; '
            'mkdir -p "$parent" && cat > "$tmp"'
        )
        if use_root:
            script = adb.root_shell_script(script)
        committed = False
        try:
            result = adb.run_raw_with_input_stream(
                ["exec-in", "sh", "-c", script],
                input_writer=input_writer,
                timeout=None,
                output_callback=output_callback,
                cancel_event=cancel_event,
            )
            if cancel_event.is_set():
                result.success = False
                result.status = "Transfer cancelled by user"
                result.error_type = "cancelled"
            elif result.success and (source_changed or sent_bytes != expected_size):
                detail = source_changed or (
                    f"Source size changed during streaming: expected {expected_size} bytes, "
                    f"read {sent_bytes} bytes"
                )
                result.success = False
                result.status = "Source file changed during transfer; temporary upload was discarded"
                result.error_type = "source_changed"
                result.stderr = (result.stderr + "\n" if result.stderr else "") + detail
            if result.success:
                finalize_script = (
                    f"target={shell_quote(target)}; tmp={shell_quote(temp_target)}; "
                    'parent=${target%/*}; [ "$parent" = "$target" ] && parent=.; '
                    'actual=$(stat -c %s "$tmp" 2>/dev/null) || { '
                    'echo "OpenADB: could not verify temporary file size" >&2; exit 74; }; '
                    f'[ "$actual" = "{expected_size}" ] || {{ '
                    'echo "OpenADB: temporary file size mismatch" >&2; exit 75; }; '
                    'if [ -d "$target" ]; then '
                    'echo "OpenADB: destination is a directory" >&2; exit 73; fi; '
                    'owner=$(stat -c "%u:%g" "$parent" 2>/dev/null || true); '
                    'mv -f "$tmp" "$target"; rc=$?; '
                    'if [ $rc -eq 0 ] && [ -n "$owner" ]; then '
                    'chown "$owner" "$target" 2>/dev/null || true; '
                    'restorecon "$target" 2>/dev/null || true; '
                    'fi; exit $rc'
                )
                finalize_result = (
                    adb.run_root_shell(
                        finalize_script,
                        timeout=30,
                        cancel_event=cancel_event,
                    )
                    if use_root
                    else adb.run_shell(
                        finalize_script,
                        timeout=30,
                        cancel_event=cancel_event,
                    )
                )
                # A successful mv means the temporary path no longer exists,
                # even if cancellation was observed immediately afterwards.
                committed = bool(finalize_result.success)
                if cancel_event.is_set():
                    result.success = False
                    result.status = "Transfer cancelled before the remote file was finalized"
                    result.error_type = "cancelled"
                elif not finalize_result.success:
                    result.success = False
                    result.status = f"Remote file finalize failed: {finalize_result.status}"
                    result.error_type = finalize_result.error_type or "remote_finalize_failed"
                    detail = finalize_result.stderr or finalize_result.stdout or finalize_result.status
                    result.stderr = (result.stderr + "\n" if result.stderr else "") + detail
            return result, sent_bytes
        finally:
            if not committed:
                cleanup_script = f"rm -f {shell_quote(temp_target)}"
                try:
                    if use_root:
                        adb.run_root_shell(cleanup_script, timeout=5)
                    else:
                        adb.run_shell(cleanup_script, timeout=5)
                except BaseException:
                    # Cleanup is best-effort and must never hide the original
                    # stream/finalize failure.
                    pass

    def _run_single_file_push_with_progress(
        self,
        adb: ADBClient,
        source: Path,
        destination: str,
        cancel_event: threading.Event,
        output_callback,
        item_callback,
        entry_size: int,
        done_bytes: int,
        total_bytes: int,
        total_files: int,
        done_files: int,
        started: float,
        use_root: bool = False,
        wireless_mode: bool = False,
    ) -> dict:
        target = self._android_push_target(source, destination)
        result, sent_bytes = self._stream_push_file_to_android_target(
            adb=adb,
            source=source,
            target=target,
            cancel_event=cancel_event,
            output_callback=output_callback,
            item_callback=item_callback,
            base_done_bytes=done_bytes,
            base_done_files=done_files,
            total_bytes=total_bytes,
            total_files=total_files,
            started=started,
            use_root=use_root,
            wireless_mode=wireless_mode,
            activity="Root single-file push is running" if use_root else "ADB single-file push is running",
            expected_size=entry_size,
        )
        observed_bytes = entry_size if result.success else sent_bytes
        observed_files = 1 if result.success else 0
        return {"result": result, "observed_bytes": observed_bytes, "observed_files": observed_files}

    def _android_temp_sibling_path(self, target: str) -> str:
        parent = parent_android_path(target)
        stamp = int(time.time() * 1000)
        suffix = abs(hash((target, stamp, threading.get_ident()))) & 0xFFFFFF
        return join_android_path(parent, f".openadb-part-{stamp}-{suffix:06x}")

    def _local_temp_sibling_path(self, target: Path) -> Path:
        stamp = int(time.time() * 1000)
        suffix = abs(hash((str(target), stamp, threading.get_ident()))) & 0xFFFFFF
        return target.with_name(f".openadb-part-{stamp}-{suffix:06x}")

    @staticmethod
    def _install_local_staged_file(staged: Path, target: Path) -> None:
        """Atomically publish a staged file without clobbering a new target."""

        try:
            os.link(staged, target)
        except FileExistsError:
            raise
        except OSError:
            if os.name != "nt":
                raise
            # FAT/exFAT do not support hard links. On Windows ``rename`` is
            # still create-without-replace and fails if a concurrent target
            # appeared after the caller's final existence check.
            os.rename(staged, target)
        else:
            try:
                staged.unlink()
            except OSError:
                # The target hard link is already committed. The transaction
                # cleanup below gets another chance to remove this staging name.
                pass

    def _run_single_file_pull_with_progress(
        self,
        adb: ADBClient,
        source: str,
        display_source: str,
        destination: Path,
        cancel_event: threading.Event,
        output_callback,
        item_callback,
        entry_size: int,
        done_bytes: int,
        total_bytes: int,
        total_files: int,
        done_files: int,
        started: float,
        use_root: bool = False,
        wireless_mode: bool = False,
    ) -> dict:
        target = self._local_pull_target(display_source, destination)
        temp_target = self._local_temp_sibling_path(target)
        received_bytes = 0
        last_emit = 0.0

        def emit_progress(force: bool = False) -> None:
            nonlocal last_emit
            now = time.monotonic()
            if not force and now - last_emit < SINGLE_FILE_STREAM_PROGRESS_INTERVAL:
                return
            last_emit = now
            current_bytes = done_bytes + max(0, received_bytes)
            # Receiving the expected byte count is not completion until the
            # staged file is size-checked and atomically published.
            current_files = done_files
            self._emit_transfer(
                item_callback,
                {
                    "type": "heartbeat",
                    "done_bytes": current_bytes,
                    "total_bytes": max(total_bytes, current_bytes),
                    "done_files": current_files,
                    "total_files": max(total_files, current_files),
                    "current_file": display_source,
                    "speed": self._speed_text(current_bytes, started),
                    "activity": "Root single-file pull is running" if use_root else "ADB single-file pull is running",
                },
            )

        def on_progress(total_written: int) -> None:
            nonlocal received_bytes
            received_bytes = max(received_bytes, int(total_written))
            emit_progress()

        committed = False
        try:
            result = adb.pull_file_streaming_to_file(
                source,
                temp_target,
                timeout=None,
                output_callback=output_callback,
                progress_callback=on_progress,
                cancel_event=cancel_event,
                use_root=use_root,
                buffer_size=self._single_file_stream_buffer_size(wireless_mode),
            )
            emit_progress(force=True)
            if cancel_event.is_set():
                result.success = False
                result.status = "Transfer cancelled by user"
                result.error_type = "cancelled"
            if result.success:
                try:
                    actual_size = temp_target.stat().st_size
                    received_bytes = max(received_bytes, actual_size)
                    if actual_size != entry_size:
                        raise OSError(
                            f"Downloaded file size changed: expected {entry_size} bytes, "
                            f"received {actual_size} bytes"
                        )
                    if target.exists() and target.is_dir():
                        raise OSError(f"Cannot overwrite directory: {target}")
                    os.replace(temp_target, target)
                    committed = True
                except OSError as exc:
                    result.success = False
                    if "Downloaded file size changed" in str(exc):
                        result.status = "Downloaded file changed during transfer; existing destination was preserved"
                        result.error_type = "source_changed"
                    else:
                        result.status = f"Local file rename failed: {exc}"
                        result.error_type = "local_rename_failed"
                    result.stderr = (result.stderr + "\n" if result.stderr else "") + str(exc)
            observed_bytes = entry_size if result.success else received_bytes
            observed_files = 1 if result.success else 0
            return {"result": result, "observed_bytes": observed_bytes, "observed_files": observed_files}
        finally:
            if not committed:
                try:
                    temp_target.unlink(missing_ok=True)
                except OSError:
                    pass

    def _run_fast_tar_pull_with_progress(
        self,
        adb: ADBClient,
        source: str,
        destination: Path,
        tar_command: str,
        cancel_event: threading.Event,
        output_callback,
        item_callback,
        entry_size: int,
        done_bytes: int,
        total_bytes: int,
        total_files: int,
        done_files: int,
        started: float,
        entry_count: int,
        use_root: bool = False,
        wireless_mode: bool = False,
    ) -> dict:
        received_bytes = 0
        received_files = 0
        current_file = source
        last_emit = 0.0
        destination_root = destination.resolve()
        pending_files: dict[Path, Path] = {}
        backup_files: dict[Path, Path] = {}
        reserved_backup_files: set[Path] = set()
        committed_targets: set[Path] = set()
        committed_fingerprints: dict[Path, tuple[int, int, int, int]] = {}
        created_directories: set[Path] = set()

        def remove_temp_file(temp_target: Path) -> None:
            try:
                temp_target.unlink(missing_ok=True)
            except OSError:
                pass

        def cleanup_pending_files() -> None:
            for temp_target in set(pending_files.values()):
                remove_temp_file(temp_target)

        def ensure_directory(directory: Path) -> None:
            missing: list[Path] = []
            candidate = directory
            while not (candidate.exists() or candidate.is_symlink()):
                missing.append(candidate)
                parent = candidate.parent
                if parent == candidate:
                    break
                candidate = parent
            if not candidate.is_dir():
                raise OSError(f"Cannot create directory over a non-directory: {candidate}")
            for candidate in reversed(missing):
                try:
                    candidate.mkdir()
                except FileExistsError:
                    if not candidate.is_dir():
                        raise OSError(
                            f"Cannot create directory over a non-directory: {candidate}"
                        ) from None
                    continue
                created_directories.add(candidate)

        def cleanup_created_directories() -> None:
            for directory in sorted(
                created_directories,
                key=lambda value: len(value.parts),
                reverse=True,
            ):
                try:
                    directory.rmdir()
                except OSError:
                    continue

        def reserve_backup_file(target: Path) -> Path:
            descriptor, backup_name = tempfile.mkstemp(
                prefix=".openadb-backup-",
                dir=target.parent,
            )
            os.close(descriptor)
            backup_target = Path(backup_name)
            reserved_backup_files.add(backup_target)
            return backup_target

        def file_fingerprint(target: Path) -> tuple[int, int, int, int] | None:
            try:
                metadata = target.stat(follow_symlinks=False)
            except OSError:
                return None
            return (
                int(metadata.st_dev),
                int(metadata.st_ino),
                int(metadata.st_size),
                int(metadata.st_mtime_ns),
            )

        def rollback_replacements() -> list[str]:
            errors: list[str] = []
            for target in reversed(list(pending_files)):
                backup_target = backup_files.get(target)
                try:
                    target_exists = target.exists() or target.is_symlink()
                    expected_fingerprint = committed_fingerprints.get(target)
                    target_is_ours = bool(
                        expected_fingerprint is not None
                        and target_exists
                        and file_fingerprint(target) == expected_fingerprint
                    )
                    if backup_target is not None:
                        if target_exists and not target_is_ours:
                            errors.append(
                                f"{target}: destination changed concurrently; "
                                f"original retained at {backup_target}"
                            )
                            continue
                        os.replace(backup_target, target)
                        backup_files.pop(target, None)
                        reserved_backup_files.discard(backup_target)
                    elif target in committed_targets and target_exists:
                        if not target_is_ours:
                            errors.append(
                                f"{target}: destination changed concurrently; "
                                "the newer file was retained"
                            )
                            continue
                        target.unlink(missing_ok=True)
                except OSError as exc:
                    errors.append(f"{target}: {exc}")
            return errors

        def cleanup_unprotected_backups() -> None:
            protected = set(backup_files.values())
            for backup_target in set(reserved_backup_files) - protected:
                remove_temp_file(backup_target)
                reserved_backup_files.discard(backup_target)

        def emit_progress(force: bool = False) -> None:
            nonlocal last_emit
            now = time.monotonic()
            if not force and now - last_emit < 0.25:
                return
            last_emit = now
            current_bytes = done_bytes + max(0, received_bytes)
            current_files = done_files + max(0, received_files)
            self._emit_transfer(
                item_callback,
                {
                    "type": "heartbeat",
                    "done_bytes": current_bytes,
                    "total_bytes": max(total_bytes, current_bytes),
                    "done_files": current_files,
                    "total_files": max(total_files, current_files),
                    "current_file": current_file,
                    "speed": self._speed_text(current_bytes, started),
                    "activity": "Root fast TAR pull is running" if use_root else "Fast TAR pull is running",
                },
            )

        def safe_target(member_name: str) -> Path | None:
            clean_name = str(PurePosixPath(member_name.replace("\\", "/"))).lstrip("/")
            parts = PurePosixPath(clean_name).parts
            if not parts or any(part in {"", ".", ".."} for part in parts):
                return None
            target = destination.joinpath(*parts).resolve()
            try:
                target.relative_to(destination_root)
            except ValueError:
                return None
            return target

        def output_writer(stream: BinaryIO) -> None:
            nonlocal received_bytes, received_files, current_file
            ensure_directory(destination)
            with tarfile.open(fileobj=stream, mode="r|*") as archive:
                for member in archive:
                    if cancel_event.is_set():
                        raise OSError("Transfer cancelled by user")
                    target = safe_target(member.name)
                    if target is None:
                        continue
                    current_file = str(target)
                    emit_progress(force=True)
                    if member.isdir():
                        ensure_directory(target)
                        continue
                    if not member.isfile():
                        continue
                    ensure_directory(target.parent)
                    source_file = archive.extractfile(member)
                    if source_file is None:
                        continue
                    descriptor, temp_name = tempfile.mkstemp(
                        prefix=".openadb-part-",
                        dir=target.parent,
                    )
                    os.close(descriptor)
                    temp_target = Path(temp_name)
                    try:
                        with source_file, temp_target.open("wb") as fileobj:
                            while True:
                                if cancel_event.is_set():
                                    raise OSError("Transfer cancelled by user")
                                chunk = source_file.read(self._tar_copy_buffer_size(wireless_mode))
                                if not chunk:
                                    break
                                fileobj.write(chunk)
                                received_bytes += len(chunk)
                                emit_progress()
                    except BaseException:
                        remove_temp_file(temp_target)
                        raise
                    if member.mtime:
                        try:
                            os.utime(temp_target, (member.mtime, member.mtime))
                        except OSError:
                            pass
                    previous_temp = pending_files.get(target)
                    if previous_temp is not None:
                        remove_temp_file(previous_temp)
                    pending_files[target] = temp_target
                    received_files += 1
                    emit_progress(force=True)

        try:
            result = adb.pull_tar_streaming(
                source=source,
                tar_command=tar_command,
                output_writer=output_writer,
                timeout=None,
                output_callback=output_callback,
                cancel_event=cancel_event,
                use_root=use_root,
            )
        except BaseException:
            cleanup_pending_files()
            cleanup_created_directories()
            raise
        if cancel_event.is_set():
            result.success = False
            result.status = "Transfer cancelled by user"
            result.error_type = "cancelled"
        if result.success:
            try:
                for target in pending_files:
                    target_exists = target.exists() or target.is_symlink()
                    if target_exists and target.is_dir():
                        raise OSError(f"Cannot overwrite directory: {target}")
                for target, temp_target in pending_files.items():
                    if cancel_event.is_set():
                        raise OSError("Transfer cancelled by user")
                    target_exists = target.exists() or target.is_symlink()
                    if target_exists and target.is_dir():
                        raise OSError(f"Cannot overwrite directory: {target}")
                    if target_exists:
                        backup_target = reserve_backup_file(target)
                        os.replace(target, backup_target)
                        backup_files[target] = backup_target
                    expected_fingerprint = file_fingerprint(temp_target)
                    if expected_fingerprint is None:
                        raise OSError(f"Could not verify staged file: {target}")
                    self._install_local_staged_file(temp_target, target)
                    committed_targets.add(target)
                    committed_fingerprints[target] = expected_fingerprint
                    if file_fingerprint(target) != expected_fingerprint:
                        raise OSError(
                            f"Destination changed while the file was being committed: {target}"
                        )
                if cancel_event.is_set():
                    raise OSError("Transfer cancelled by user")
            except BaseException as exc:
                rollback_errors = rollback_replacements()
                cleanup_unprotected_backups()
                if not isinstance(exc, OSError):
                    cleanup_pending_files()
                    cleanup_created_directories()
                    raise
                result.success = False
                if cancel_event.is_set():
                    result.status = "Transfer cancelled by user"
                    result.error_type = "cancelled"
                else:
                    result.status = f"Local TAR extraction finalize failed: {exc}"
                    result.error_type = "local_rename_failed"
                detail = str(exc)
                if rollback_errors:
                    detail += "\nRollback warning: " + "; ".join(rollback_errors)
                result.stderr = (result.stderr + "\n" if result.stderr else "") + detail
            else:
                backup_files.clear()
                cleanup_unprotected_backups()
                cleanup_pending_files()
        if not result.success:
            cleanup_pending_files()
            cleanup_unprotected_backups()
            cleanup_created_directories()
        observed_bytes = entry_size if result.success else received_bytes
        observed_files = entry_count if result.success else received_files
        return {"result": result, "observed_bytes": observed_bytes, "observed_files": observed_files}

    def _run_fast_tar_push_with_progress(
        self,
        adb: ADBClient,
        source: Path,
        destination: str,
        tar_command: str,
        cancel_event: threading.Event,
        output_callback,
        item_callback,
        entry_size: int,
        done_bytes: int,
        total_bytes: int,
        total_files: int,
        done_files: int,
        started: float,
        entry_count: int,
        use_root: bool = False,
        wireless_mode: bool = False,
    ) -> dict:
        directories, files = self._tar_stream_items(
            source,
            cancel_event=cancel_event,
        )
        sent_bytes = 0
        sent_files = 0
        current_file = files[0][1] if files else str(source)
        last_emit = 0.0

        def emit_progress(force: bool = False) -> None:
            nonlocal last_emit
            now = time.monotonic()
            if not force and now - last_emit < 0.25:
                return
            last_emit = now
            current_bytes = done_bytes + max(0, sent_bytes)
            current_files = done_files + max(0, sent_files)
            self._emit_transfer(
                item_callback,
                {
                    "type": "heartbeat",
                    "done_bytes": current_bytes,
                    "total_bytes": max(total_bytes, current_bytes),
                    "done_files": current_files,
                    "total_files": max(total_files, current_files),
                    "current_file": current_file,
                    "speed": self._speed_text(current_bytes, started),
                    "activity": "Root fast TAR push is running" if use_root else "Fast TAR push is running",
                },
            )

        def input_writer(stream: BinaryIO) -> None:
            nonlocal sent_bytes, sent_files, current_file
            with tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT, dereference=True) as archive:
                archive.copybufsize = self._tar_copy_buffer_size(wireless_mode)
                for directory, arcname in directories:
                    if cancel_event.is_set():
                        raise OSError("Transfer cancelled by user")
                    info = archive.gettarinfo(str(directory), arcname=arcname)
                    archive.addfile(info)
                for file_path, arcname, _file_size in files:
                    if cancel_event.is_set():
                        raise OSError("Transfer cancelled by user")
                    current_file = arcname
                    emit_progress(force=True)

                    def on_read(chunk_size: int) -> None:
                        nonlocal sent_bytes
                        sent_bytes += chunk_size
                        emit_progress()

                    info = archive.gettarinfo(str(file_path), arcname=arcname)
                    with file_path.open("rb") as fileobj:
                        archive.addfile(info, _ProgressFile(fileobj, on_read, cancel_event))
                    sent_files += 1
                    emit_progress(force=True)

        result = adb.push_tar_streaming(
            destination=destination,
            tar_command=tar_command,
            input_writer=input_writer,
            timeout=None,
            output_callback=output_callback,
            cancel_event=cancel_event,
            use_root=use_root,
            target_name=source.name,
        )
        observed_bytes = entry_size if result.success else sent_bytes
        observed_files = entry_count if result.success else sent_files
        return {"result": result, "observed_bytes": observed_bytes, "observed_files": observed_files}

    def _push_observation_interval(
        self,
        entry_size: int,
        entry_count: int,
        file_markers: list[tuple[int, str]],
    ) -> float:
        if entry_size <= 0 or entry_count <= 0:
            return ADB_PUSH_DEFAULT_OBSERVATION_INTERVAL
        average_size = entry_size / max(1, entry_count)
        if average_size >= ADB_PUSH_LARGE_AVERAGE_FILE_SIZE or entry_size >= ADB_PUSH_LARGE_TOTAL_SIZE:
            return ADB_PUSH_LARGE_OBSERVATION_INTERVAL
        if any(size >= ADB_PUSH_LARGE_AVERAGE_FILE_SIZE for size in self._file_sizes_from_markers(file_markers)):
            return ADB_PUSH_LARGE_OBSERVATION_INTERVAL
        return ADB_PUSH_DEFAULT_OBSERVATION_INTERVAL

    def _transfer_observation_baseline(
        self,
        adb: ADBClient,
        source,
        destination,
        is_pull: bool,
        cancel_event: threading.Event | None = None,
    ) -> tuple[int, int]:
        if cancel_event is not None and cancel_event.is_set():
            return (0, 0)
        if is_pull:
            target = self._local_pull_target(str(source), Path(destination))
            return (
                self._local_transfer_stats(target, cancel_event=cancel_event)
                if target.exists()
                else (0, 0)
            )
        return self._android_transfer_observation(
            adb,
            self._android_push_target(source, destination),
            cancel_event=cancel_event,
        )

    def _observed_transfer_stats(
        self,
        adb: ADBClient,
        source,
        destination,
        is_pull: bool,
        entry_size: int,
        entry_started_wall: float,
        baseline: tuple[int, int],
        entry_count: int,
        file_markers: list[tuple[int, str]],
        cancel_event: threading.Event | None = None,
    ) -> tuple[int, int, str]:
        if cancel_event is not None and cancel_event.is_set():
            return (0, 0, str(source))
        if is_pull:
            target = self._local_pull_target(str(source), Path(destination))
            if not target.exists():
                return (0, 0, str(source))
            size, count, current_file = self._local_transfer_observation(
                target,
                entry_started_wall,
                cancel_event=cancel_event,
            )
            return (max(0, size - baseline[0]), max(0, count - baseline[1]), current_file or str(source))
        target = self._android_push_target(source, destination)
        size, count = self._android_transfer_observation(
            adb,
            target,
            cancel_event=cancel_event,
        )
        observed_bytes = max(0, size - baseline[0])
        observed_files = max(0, count - baseline[1])
        if observed_files <= 0 and observed_bytes > 0:
            observed_files = self._estimate_observed_files(entry_count, entry_size, observed_bytes, file_markers)
        current_file = self._current_transfer_file_label(source, observed_bytes, file_markers)
        return (observed_bytes, observed_files, current_file)

    def _android_push_target(self, source, destination) -> str:
        name = Path(source).name
        destination_text = str(destination).replace("\\", "/").strip() or "/sdcard/"
        return join_android_path(destination_text, name)

    def _android_transfer_observation(
        self,
        adb: ADBClient,
        android_path: str,
        use_root: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> tuple[int, int]:
        if cancel_event is not None and cancel_event.is_set():
            return (0, 0)
        quoted_path = shell_quote(android_path)
        script = (
            f"p={quoted_path}; "
            'if [ -d "$p" ]; then '
            'size=$(du -s -k "$p" 2>/dev/null | sed -n "1s/[[:space:]].*$//p"); '
            'count=$(find "$p" -type f 2>/dev/null | wc -l); '
            'echo OPENADB_SIZE_KB:${size:-0}; '
            'echo OPENADB_FILES:${count:-0}; '
            'elif [ -e "$p" ]; then '
            'size=$(stat -c %s "$p" 2>/dev/null); '
            'echo OPENADB_SIZE_BYTES:${size:-0}; '
            'echo OPENADB_FILES:1; '
            'else '
            'echo OPENADB_SIZE_BYTES:0; '
            'echo OPENADB_FILES:0; '
            "fi"
        )
        result = (
            adb.run_root_shell(script, timeout=12, cancel_event=cancel_event)
            if use_root
            else adb.run_shell(script, timeout=12, cancel_event=cancel_event)
        )
        if not result.stdout:
            return (0, 0)
        size_bytes = 0
        file_count = 0
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("OPENADB_SIZE_BYTES:"):
                size_bytes = self._first_int(line.split(":", 1)[1]) or 0
            elif line.startswith("OPENADB_SIZE_KB:"):
                size_bytes = (self._first_int(line.split(":", 1)[1]) or 0) * 1024
            elif line.startswith("OPENADB_FILES:"):
                file_count = self._first_int(line.split(":", 1)[1]) or 0
        return (size_bytes, file_count)

    def _local_pull_target(self, source: str, destination: Path) -> Path:
        source_name = PurePosixPath(source.rstrip("/")).name or Path(source).name
        if destination.exists() and destination.is_dir():
            return destination / source_name
        return destination

    def _transfer_command_text(
        self,
        adb: ADBClient,
        source,
        destination,
        is_pull: bool,
        fast_push: bool = False,
        fast_pull: bool = False,
        tar_command: str = "",
        stream_file: bool = False,
        root_mode: bool = False,
        transfer_source=None,
        transfer_destination=None,
        disable_compression: bool = False,
    ) -> str:
        effective_source = str(transfer_source if transfer_source is not None else source)
        effective_destination = str(transfer_destination if transfer_destination is not None else destination)
        if fast_pull:
            clean_source = effective_source.rstrip("/") or "/"
            script = (
                f"src={shell_quote(clean_source)}; "
                'parent=${src%/*}; name=${src##*/}; '
                '[ -z "$parent" ] && parent=/; [ "$parent" = "$src" ] && parent=/; '
                '[ -z "$name" ] && name=.; '
                f"cd \"$parent\" && {tar_command} -cf - \"$name\""
            )
            if root_mode:
                script = adb.root_shell_script(script)
            args = ["exec-out", "sh", "-c", script]
        elif fast_push:
            quoted_destination = shell_quote(effective_destination)
            if root_mode:
                quoted_target_name = shell_quote(Path(source).name)
                script = (
                    f"dest={quoted_destination}; target_name={quoted_target_name}; "
                    'mkdir -p "$dest" || exit $?; '
                    'owner=$(stat -c "%u:%g" "$dest" 2>/dev/null || true); '
                    f'cd "$dest" && {tar_command} -xf -; rc=$?; '
                    'if [ $rc -eq 0 ] && [ -n "$owner" ] && [ -n "$target_name" ]; then '
                    'target="$dest/$target_name"; chown -R "$owner" "$target" 2>/dev/null || true; '
                    'restorecon -R "$target" 2>/dev/null || true; fi; exit $rc'
                )
                script = adb.root_shell_script(script)
            else:
                script = f"mkdir -p {quoted_destination} && cd {quoted_destination} && {tar_command} -xf -"
            args = ["exec-in", "sh", "-c", script]
        elif stream_file and is_pull:
            script = f"cat {shell_quote(effective_source)}"
            if root_mode:
                script = adb.root_shell_script(script)
            args = ["exec-out", "sh", "-c", script]
        elif stream_file:
            target = self._android_push_target(source, effective_destination)
            script = f"cat > {shell_quote(target)}"
            if root_mode:
                script = adb.root_shell_script(script)
            args = ["exec-in", "sh", "-c", script]
        else:
            if is_pull:
                args = ["pull"]
                if disable_compression:
                    args.append("-Z")
                args.extend([str(source), str(destination)])
            else:
                args = ["push"]
                if disable_compression:
                    args.append("-Z")
                args.extend([str(source), str(destination)])
        return adb.runner.command_text([*adb._base(), *args])

    def _should_use_single_file_stream(
        self,
        source,
        is_pull: bool,
        entry_count: int,
        entry_is_dir: bool,
        wireless_mode: bool = False,
    ) -> bool:
        if entry_count != 1 or entry_is_dir:
            return False
        if wireless_mode:
            return True
        if is_pull:
            return True
        return isinstance(source, Path) and source.is_file()

    def _should_use_fast_tar_push(
        self,
        source,
        entry_size: int,
        entry_count: int,
        file_markers: list[tuple[int, str]],
        tar_command: str,
        is_pull: bool,
        root_mode: bool = False,
        destination: str = "",
        wireless_mode: bool = False,
    ) -> bool:
        if is_pull or not tar_command or not isinstance(source, Path) or not source.is_dir():
            return False
        if wireless_mode:
            return entry_count > 0
        if root_mode and destination and not is_probably_writable_android_path(destination):
            return entry_count > 0
        if entry_count < FAST_TAR_MIN_FILES or entry_size <= 0:
            return False

        average_size = entry_size / max(1, entry_count)
        if average_size > FAST_TAR_MAX_AVERAGE_FILE_SIZE:
            return False

        file_sizes = self._file_sizes_from_markers(file_markers)
        if file_sizes:
            large_files = sum(1 for size in file_sizes if size >= FAST_TAR_LARGE_FILE_SIZE)
            if large_files / len(file_sizes) > FAST_TAR_MAX_LARGE_FILE_RATIO:
                return False
        return True

    def _should_disable_adb_compression(
        self,
        source,
        entry_size: int,
        entry_count: int,
        file_markers: list[tuple[int, str]],
        fast_push: bool = False,
        fast_pull: bool = False,
        stream_file: bool = False,
    ) -> bool:
        if fast_push or fast_pull or stream_file:
            return False
        if entry_size >= ADB_TRANSFER_DISABLE_COMPRESSION_SIZE:
            return True
        average_size = entry_size / max(1, entry_count)
        if average_size >= ADB_TRANSFER_DISABLE_COMPRESSION_AVERAGE:
            return True
        compressed_extensions = {
            ".7z",
            ".avi",
            ".flac",
            ".gz",
            ".jpg",
            ".jpeg",
            ".m4a",
            ".mkv",
            ".mov",
            ".mp3",
            ".mp4",
            ".ogg",
            ".png",
            ".rar",
            ".ts",
            ".webm",
            ".zip",
        }
        paths = [label for _size, label in file_markers[:32]]
        if isinstance(source, Path) and source.is_file():
            paths.append(str(source))
        return any(Path(path).suffix.lower() in compressed_extensions for path in paths)

    def _should_use_fast_tar_pull(
        self,
        source,
        entry_size: int,
        entry_count: int,
        tar_command: str,
        is_pull: bool,
        entry_is_dir: bool,
        root_mode: bool = False,
        wireless_mode: bool = False,
    ) -> bool:
        if not is_pull or not tar_command or not entry_is_dir:
            return False
        if wireless_mode:
            return bool(str(source).strip())
        if root_mode and not is_probably_writable_android_path(str(source)):
            return bool(str(source).strip())
        if entry_count < FAST_TAR_PULL_MIN_FILES or entry_size <= 0:
            return False
        return bool(str(source).strip())

    def _root_accel_android_path(self, path: str, preserve_root_name: bool = False) -> str:
        normalized = (path or "").replace("\\", "/").strip() or "/"
        while "//" in normalized:
            normalized = normalized.replace("//", "/")
        sdcard = "/sdcard"
        emulated = "/storage/emulated/0"
        direct = "/data/media/0"
        if normalized == sdcard:
            return normalized if preserve_root_name else direct
        if normalized.startswith(sdcard + "/"):
            return direct + normalized[len(sdcard) :]
        if normalized == emulated:
            return normalized if preserve_root_name else direct
        if normalized.startswith(emulated + "/"):
            return direct + normalized[len(emulated) :]
        return normalized

    def _file_sizes_from_markers(self, file_markers: list[tuple[int, str]]) -> list[int]:
        sizes: list[int] = []
        previous = 0
        for cumulative, _label in file_markers:
            size = max(0, int(cumulative) - previous)
            previous = int(cumulative)
            sizes.append(size)
        return sizes

    def _extract_percent(self, text: str) -> int | None:
        match = PERCENT_PATTERN.search(text)
        if not match:
            return None
        value = max(0, min(100, int(match.group(1))))
        return value

    def _local_transfer_stats(
        self,
        path: Path,
        cancel_event: threading.Event | None = None,
    ) -> tuple[int, int]:
        size, count, _markers = self._local_transfer_stats_with_markers(
            path,
            cancel_event=cancel_event,
        )
        return size, count

    def _local_transfer_stats_with_markers(
        self,
        path: Path,
        cancel_event: threading.Event | None = None,
    ) -> tuple[int, int, list[tuple[int, str]]]:
        try:
            if cancel_event is not None and cancel_event.is_set():
                return 0, 0, []
            if path.is_file():
                size = path.stat().st_size
                return size, 1, [(size, str(path))]
            total = 0
            count = 0
            markers: list[tuple[int, str]] = []
            for child in path.rglob("*"):
                if cancel_event is not None and cancel_event.is_set():
                    break
                try:
                    if child.is_file():
                        total += child.stat().st_size
                        count += 1
                        try:
                            label = str(Path(path.name) / child.relative_to(path))
                        except Exception:
                            label = str(child)
                        markers.append((total, label))
                except OSError:
                    continue
            return total, count, markers
        except OSError:
            return 0, 0, []

    def _tar_stream_items(
        self,
        source: Path,
        cancel_event: threading.Event | None = None,
    ) -> tuple[list[tuple[Path, str]], list[tuple[Path, str, int]]]:
        directories: list[tuple[Path, str]] = []
        files: list[tuple[Path, str, int]] = []
        try:
            if cancel_event is not None and cancel_event.is_set():
                return directories, files
            if source.is_file():
                return [], [(source, source.name, source.stat().st_size)]
            root_name = source.name
            directories.append((source, root_name))
            for child in source.rglob("*"):
                if cancel_event is not None and cancel_event.is_set():
                    break
                try:
                    arcname = str(Path(root_name) / child.relative_to(source)).replace("\\", "/")
                    if child.is_dir():
                        directories.append((child, arcname))
                    elif child.is_file():
                        files.append((child, arcname, child.stat().st_size))
                except OSError:
                    continue
        except OSError:
            return directories, files
        return directories, files

    def _estimate_observed_files(
        self,
        entry_count: int,
        entry_size: int,
        observed_bytes: int,
        file_markers: list[tuple[int, str]],
    ) -> int:
        if entry_count <= 0 or observed_bytes <= 0:
            return 0
        if entry_size > 0 and observed_bytes >= entry_size:
            return entry_count
        marker_estimate = bisect_right([marker[0] for marker in file_markers], observed_bytes) if file_markers else 0
        ratio_estimate = int(entry_count * observed_bytes / entry_size) if entry_size > 0 else 0
        return min(entry_count, max(1, marker_estimate, ratio_estimate))

    def _current_transfer_file_label(self, source, observed_bytes: int, file_markers: list[tuple[int, str]]) -> str:
        if not file_markers:
            return str(source)
        if observed_bytes <= 0:
            return file_markers[0][1]
        sizes = [marker[0] for marker in file_markers]
        index = bisect_right(sizes, observed_bytes)
        if index >= len(file_markers):
            index = len(file_markers) - 1
        return file_markers[index][1]

    def _local_transfer_observation(
        self,
        path: Path,
        started_wall: float,
        cancel_event: threading.Event | None = None,
    ) -> tuple[int, int, str]:
        try:
            if cancel_event is not None and cancel_event.is_set():
                return 0, 0, ""
            if path.is_file():
                return path.stat().st_size, 1, str(path)
            total = 0
            count = 0
            newest_file = ""
            newest_mtime = 0.0
            for child in path.rglob("*"):
                if cancel_event is not None and cancel_event.is_set():
                    break
                try:
                    if not child.is_file():
                        continue
                    stat = child.stat()
                    total += stat.st_size
                    count += 1
                    if stat.st_mtime >= started_wall - 2 and stat.st_mtime >= newest_mtime:
                        newest_mtime = stat.st_mtime
                        newest_file = str(child)
                except OSError:
                    continue
            return total, count, newest_file
        except OSError:
            return 0, 0, ""

    def _android_transfer_stats(
        self,
        adb: ADBClient,
        path: str,
        use_root: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> tuple[int, int]:
        size, count, _is_dir = self._android_transfer_stats_with_kind(
            adb,
            path,
            use_root=use_root,
            cancel_event=cancel_event,
        )
        return size, count

    def _android_transfer_stats_with_kind(
        self,
        adb: ADBClient,
        path: str,
        use_root: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> tuple[int, int, bool]:
        if cancel_event is not None and cancel_event.is_set():
            return 0, 0, False
        quoted = shell_quote(path)
        kind_command = f"if [ -d {quoted} ]; then echo dir; else echo file; fi"
        kind_result = (
            adb.run_root_shell(kind_command, timeout=15, cancel_event=cancel_event)
            if use_root
            else adb.run_shell(kind_command, timeout=15, cancel_event=cancel_event)
        )
        if cancel_event is not None and cancel_event.is_set():
            return 0, 0, False
        kind = (kind_result.stdout or "").strip()
        if kind == "dir":
            count_command = f"find {quoted} -type f 2>/dev/null | wc -l"
            size_command = f"du -s -k {quoted} 2>/dev/null"
            count_result = (
                adb.run_root_shell(count_command, timeout=60, cancel_event=cancel_event)
                if use_root
                else adb.run_shell(count_command, timeout=60, cancel_event=cancel_event)
            )
            if cancel_event is not None and cancel_event.is_set():
                return 0, 0, True
            size_result = (
                adb.run_root_shell(size_command, timeout=60, cancel_event=cancel_event)
                if use_root
                else adb.run_shell(size_command, timeout=60, cancel_event=cancel_event)
            )
            count = self._first_int(count_result.stdout) or 1
            size_kb = self._first_int(size_result.stdout) or 0
            return size_kb * 1024, count, True
        size_command = f"stat -c %s {quoted} 2>/dev/null"
        size_result = (
            adb.run_root_shell(size_command, timeout=15, cancel_event=cancel_event)
            if use_root
            else adb.run_shell(size_command, timeout=15, cancel_event=cancel_event)
        )
        return self._first_int(size_result.stdout) or 0, 1, False

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
