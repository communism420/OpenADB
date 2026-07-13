from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Callable, Iterable

from openadb.models.command_result import CommandResult, format_command

from .device_context import DeviceContext
from .path_utils import ensure_dir


LogCallback = Callable[[CommandResult], None]
OutputCallback = Callable[[str, str], None]
InputWriter = Callable[[BinaryIO], None]
BinaryProgressCallback = Callable[[int], None]
BinaryOutputWriter = Callable[[BinaryIO], None]


class CommandRunner:
    def __init__(self, logs_folder: Path) -> None:
        self.logs_folder = ensure_dir(logs_folder)
        self.log_file = self.logs_folder / "openadb.log"
        self.jsonl_file = self.logs_folder / "openadb.commands.jsonl"
        self._listeners: list[LogCallback] = []
        self._lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._active_processes: set[subprocess.Popen] = set()
        self._shutting_down = False

    def _start_process(self, command: list[str], **kwargs) -> subprocess.Popen | None:
        """Atomically reject or register a process against the shutdown gate."""

        with self._process_lock:
            if self._shutting_down:
                return None
            process = subprocess.Popen(command, **kwargs)
            self._active_processes.add(process)
            return process

    def _is_shutting_down(self) -> bool:
        with self._process_lock:
            return self._shutting_down

    def _unregister_process(self, process: subprocess.Popen | None) -> None:
        if process is None:
            return
        with self._process_lock:
            self._active_processes.discard(process)

    def active_process_count(self) -> int:
        with self._process_lock:
            return sum(process.poll() is None for process in self._active_processes)

    def shutdown(self) -> None:
        """Terminate subprocesses still owned by background operations."""
        with self._process_lock:
            self._shutting_down = True
            processes = tuple(self._active_processes)
        for process in processes:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass

    def add_listener(self, callback: LogCallback) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: LogCallback) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def set_logs_folder(self, logs_folder: Path) -> None:
        self.logs_folder = ensure_dir(logs_folder)
        self.log_file = self.logs_folder / "openadb.log"
        self.jsonl_file = self.logs_folder / "openadb.commands.jsonl"

    def for_context(self, context: DeviceContext) -> BoundCommandRunner:
        return BoundCommandRunner(self, context)

    def run(
        self,
        command: Iterable[str],
        timeout: int | float | None = 120,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        device_context: DeviceContext | None = None,
    ) -> CommandResult:
        command_list = [str(part) for part in command]
        started = datetime.now()
        if self._is_shutting_down():
            return self._shutdown_before_start(command_list, started, device_context)
        stdout = ""
        stderr = ""
        exit_code: int | None = None
        error_type = ""
        status = "Command completed"
        process: subprocess.Popen[str] | None = None
        try:
            process = self._start_process(
                command_list,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if process is None:
                return self._shutdown_before_start(command_list, started, device_context)
            stdout, stderr = process.communicate(timeout=timeout)
            stdout = stdout or ""
            stderr = stderr or ""
            exit_code = process.returncode
            if exit_code == 0 and not error_type:
                status = "Success"
            else:
                status, error_type = self._classify_error(stderr or stdout, exit_code)
        except FileNotFoundError as exc:
            stderr = str(exc)
            exit_code = None
            status = "Executable not found"
            error_type = "not_found"
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                process.kill()
                stopped_stdout, stopped_stderr = process.communicate()
                stdout = stopped_stdout or ""
                stderr = stopped_stderr or ""
            else:
                stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
                stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            exit_code = None
            status = f"Timed out after {timeout} seconds"
            error_type = "timeout"
        except OSError as exc:
            stderr = str(exc)
            exit_code = None
            status = "Operating system error"
            error_type = "os_error"
        finally:
            self._unregister_process(process)

        finished = datetime.now()
        duration = (finished - started).total_seconds()
        result = CommandResult(
            command=command_list,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration=duration,
            started_at=started,
            finished_at=finished,
            success=exit_code == 0 and not error_type,
            status=status,
            error_type=error_type,
        )
        self._finalize_result(result, device_context)
        return result

    def run_binary_output(
        self,
        command: Iterable[str],
        timeout: int | float | None = 120,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
        device_context: DeviceContext | None = None,
    ) -> tuple[CommandResult, bytes]:
        command_list = [str(part) for part in command]
        started = datetime.now()
        if self._is_shutting_down():
            return self._shutdown_before_start(command_list, started, device_context), b""
        if cancel_event is not None and cancel_event.is_set():
            return self._cancelled_before_start(command_list, started, device_context), b""
        stdout_bytes = b""
        stderr = ""
        exit_code: int | None = None
        error_type = ""
        status = "Command completed"
        process: subprocess.Popen[bytes] | None = None
        try:
            if cancel_event is not None and cancel_event.is_set():
                return self._cancelled_before_start(command_list, started, device_context), b""
            process = self._start_process(
                command_list,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if process is None:
                return self._shutdown_before_start(command_list, started, device_context), b""
            if cancel_event is None:
                raw_stdout, raw_stderr = process.communicate(timeout=timeout)
            else:
                deadline = time.monotonic() + timeout if timeout is not None else None
                while True:
                    if cancel_event.is_set():
                        process.kill()
                        raw_stdout, raw_stderr = process.communicate()
                        status = "Cancelled by user"
                        error_type = "cancelled"
                        break
                    wait_timeout = 0.1
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            process.kill()
                            raw_stdout, raw_stderr = process.communicate()
                            status = f"Timed out after {timeout} seconds"
                            error_type = "timeout"
                            break
                        wait_timeout = min(wait_timeout, remaining)
                    try:
                        raw_stdout, raw_stderr = process.communicate(timeout=wait_timeout)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            stdout_bytes = raw_stdout or b""
            stderr = (raw_stderr or b"").decode("utf-8", "replace")
            if error_type in {"cancelled", "timeout"}:
                exit_code = None
            else:
                exit_code = process.returncode
                if exit_code == 0:
                    status = "Success"
                else:
                    status, error_type = self._classify_error(stderr, exit_code)
        except FileNotFoundError as exc:
            stderr = str(exc)
            exit_code = None
            status = "Executable not found"
            error_type = "not_found"
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                process.kill()
                raw_stdout, raw_stderr = process.communicate()
                stdout_bytes = raw_stdout or b""
                stderr = (raw_stderr or b"").decode("utf-8", "replace")
            else:
                raw_stdout = exc.stdout or b""
                stdout_bytes = raw_stdout if isinstance(raw_stdout, bytes) else str(raw_stdout).encode("utf-8", "replace")
                stderr = (exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            exit_code = None
            status = f"Timed out after {timeout} seconds"
            error_type = "timeout"
        except OSError as exc:
            stderr = str(exc)
            exit_code = None
            status = "Operating system error"
            error_type = "os_error"
        finally:
            self._unregister_process(process)

        finished = datetime.now()
        duration = (finished - started).total_seconds()
        result = CommandResult(
            command=command_list,
            exit_code=exit_code,
            stdout=f"[binary stdout: {len(stdout_bytes)} bytes]" if stdout_bytes else "",
            stderr=stderr,
            duration=duration,
            started_at=started,
            finished_at=finished,
            success=exit_code == 0 and not error_type,
            status=status,
            error_type=error_type,
        )
        self._finalize_result(result, device_context)
        return result, stdout_bytes

    def run_streaming(
        self,
        command: Iterable[str],
        timeout: int | float | None = 120,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        output_callback: OutputCallback | None = None,
        cancel_event: threading.Event | None = None,
        device_context: DeviceContext | None = None,
    ) -> CommandResult:
        command_list = [str(part) for part in command]
        started = datetime.now()
        if self._is_shutting_down():
            return self._shutdown_before_start(command_list, started, device_context)
        if cancel_event is not None and cancel_event.is_set():
            return self._cancelled_before_start(command_list, started, device_context)
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code: int | None = None
        error_type = ""
        status = "Command completed"
        process: subprocess.Popen[str] | None = None

        def emit(channel: str, text: str) -> None:
            if not text:
                return
            if channel == "stdout":
                stdout_parts.append(text)
            else:
                stderr_parts.append(text)
            if output_callback:
                output_callback(channel, text)

        def reader(channel: str, stream) -> None:
            buffer: list[str] = []
            while True:
                try:
                    ch = stream.read(1)
                except ValueError:
                    break
                if ch == "":
                    break
                if ch in "\r\n":
                    text = "".join(buffer).strip()
                    buffer.clear()
                    if text:
                        emit(channel, text + "\n")
                else:
                    buffer.append(ch)
            text = "".join(buffer).strip()
            if text:
                emit(channel, text + "\n")

        try:
            if cancel_event is not None and cancel_event.is_set():
                return self._cancelled_before_start(command_list, started, device_context)
            process = self._start_process(
                command_list,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=0,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if process is None:
                return self._shutdown_before_start(command_list, started, device_context)
            threads = [
                threading.Thread(target=reader, args=("stdout", process.stdout), daemon=True),
                threading.Thread(target=reader, args=("stderr", process.stderr), daemon=True),
            ]
            for thread in threads:
                thread.start()

            deadline = time.monotonic() + timeout if timeout else None
            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    status = "Cancelled by user"
                    error_type = "cancelled"
                    process.kill()
                    break
                if deadline is not None and time.monotonic() > deadline:
                    status = f"Timed out after {timeout} seconds"
                    error_type = "timeout"
                    process.kill()
                    break
                time.sleep(0.05)
            exit_code = process.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=1)
            if exit_code == 0 and not error_type:
                status = "Success"
            elif error_type not in {"cancelled", "timeout"}:
                status, error_type = self._classify_error("".join(stderr_parts) or "".join(stdout_parts), exit_code)
        except FileNotFoundError as exc:
            emit("stderr", str(exc) + "\n")
            exit_code = None
            status = "Executable not found"
            error_type = "not_found"
        except subprocess.TimeoutExpired:
            if process:
                process.kill()
            exit_code = None
            status = "Timed out while stopping process"
            error_type = "timeout"
        except OSError as exc:
            emit("stderr", str(exc) + "\n")
            exit_code = None
            status = "Operating system error"
            error_type = "os_error"
        finally:
            self._unregister_process(process)

        finished = datetime.now()
        duration = (finished - started).total_seconds()
        result = CommandResult(
            command=command_list,
            exit_code=exit_code,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
            duration=duration,
            started_at=started,
            finished_at=finished,
            success=exit_code == 0 and not error_type,
            status=status,
            error_type=error_type,
        )
        self._finalize_result(result, device_context)
        return result

    def run_with_input_stream(
        self,
        command: Iterable[str],
        input_writer: InputWriter,
        timeout: int | float | None = None,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        output_callback: OutputCallback | None = None,
        cancel_event: threading.Event | None = None,
        device_context: DeviceContext | None = None,
    ) -> CommandResult:
        command_list = [str(part) for part in command]
        started = datetime.now()
        if self._is_shutting_down():
            return self._shutdown_before_start(command_list, started, device_context)
        if cancel_event is not None and cancel_event.is_set():
            return self._cancelled_before_start(command_list, started, device_context)
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code: int | None = None
        error_type = ""
        status = "Command completed"
        process: subprocess.Popen[bytes] | None = None
        writer_error: Exception | None = None

        def emit(channel: str, data: bytes | str) -> None:
            if not data:
                return
            text = data if isinstance(data, str) else data.decode("utf-8", "replace")
            if channel == "stdout":
                stdout_parts.append(text)
            else:
                stderr_parts.append(text)
            if output_callback:
                output_callback(channel, text)

        def reader(channel: str, stream) -> None:
            while True:
                try:
                    chunk = stream.readline()
                except ValueError:
                    break
                if not chunk:
                    break
                emit(channel, chunk)

        def writer() -> None:
            nonlocal writer_error
            try:
                if process is None or process.stdin is None:
                    raise OSError("Process stdin is not available")
                input_writer(process.stdin)
            except BrokenPipeError as exc:
                writer_error = exc
            except Exception as exc:
                writer_error = exc
                if cancel_event is None or not cancel_event.is_set():
                    emit("stderr", f"{exc}\n")
            finally:
                try:
                    if process is not None and process.stdin is not None:
                        process.stdin.close()
                except OSError:
                    pass

        try:
            if cancel_event is not None and cancel_event.is_set():
                return self._cancelled_before_start(command_list, started, device_context)
            process = self._start_process(
                command_list,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if process is None:
                return self._shutdown_before_start(command_list, started, device_context)
            threads = [
                threading.Thread(target=reader, args=("stdout", process.stdout), daemon=True),
                threading.Thread(target=reader, args=("stderr", process.stderr), daemon=True),
            ]
            writer_thread = threading.Thread(target=writer, daemon=True)
            for thread in threads:
                thread.start()
            writer_thread.start()

            deadline = time.monotonic() + timeout if timeout else None
            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    status = "Cancelled by user"
                    error_type = "cancelled"
                    process.kill()
                    break
                if writer_error is not None and not isinstance(writer_error, BrokenPipeError):
                    status = "Input stream failed"
                    error_type = "input_error"
                    process.kill()
                    break
                if deadline is not None and time.monotonic() > deadline:
                    status = f"Timed out after {timeout} seconds"
                    error_type = "timeout"
                    process.kill()
                    break
                time.sleep(0.05)

            exit_code = process.wait(timeout=5)
            writer_thread.join(timeout=2)
            for thread in threads:
                thread.join(timeout=1)

            if exit_code == 0 and writer_error is None and not error_type:
                status = "Success"
            elif error_type not in {"cancelled", "timeout", "input_error"}:
                status, error_type = self._classify_error("".join(stderr_parts) or "".join(stdout_parts), exit_code)
        except FileNotFoundError as exc:
            emit("stderr", str(exc) + "\n")
            exit_code = None
            status = "Executable not found"
            error_type = "not_found"
        except subprocess.TimeoutExpired:
            if process:
                process.kill()
            exit_code = None
            status = "Timed out while stopping process"
            error_type = "timeout"
        except OSError as exc:
            emit("stderr", str(exc) + "\n")
            exit_code = None
            status = "Operating system error"
            error_type = "os_error"
        finally:
            self._unregister_process(process)

        finished = datetime.now()
        duration = (finished - started).total_seconds()
        result = CommandResult(
            command=command_list,
            exit_code=exit_code,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
            duration=duration,
            started_at=started,
            finished_at=finished,
            success=exit_code == 0 and writer_error is None and not error_type,
            status=status,
            error_type=error_type,
        )
        self._finalize_result(result, device_context)
        return result

    def run_binary_output_to_file(
        self,
        command: Iterable[str],
        destination: str | Path,
        timeout: int | float | None = None,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        output_callback: OutputCallback | None = None,
        progress_callback: BinaryProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        buffer_size: int = 1024 * 1024,
        device_context: DeviceContext | None = None,
    ) -> CommandResult:
        command_list = [str(part) for part in command]
        destination_path = Path(destination)
        started = datetime.now()
        if self._is_shutting_down():
            return self._shutdown_before_start(command_list, started, device_context)
        if cancel_event is not None and cancel_event.is_set():
            return self._cancelled_before_start(command_list, started, device_context)
        stdout_bytes = 0
        stderr_parts: list[str] = []
        exit_code: int | None = None
        error_type = ""
        status = "Command completed"
        process: subprocess.Popen[bytes] | None = None
        writer_error: Exception | None = None

        def emit(channel: str, data: bytes | str) -> None:
            if not data:
                return
            text = data if isinstance(data, str) else data.decode("utf-8", "replace")
            if channel == "stderr":
                stderr_parts.append(text)
            if output_callback:
                output_callback(channel, text)

        def stderr_reader(stream) -> None:
            buffer: list[bytes] = []
            while True:
                try:
                    ch = stream.read(1)
                except ValueError:
                    break
                if not ch:
                    break
                if ch in b"\r\n":
                    data = b"".join(buffer).strip()
                    buffer.clear()
                    if data:
                        emit("stderr", data + b"\n")
                else:
                    buffer.append(ch)
            data = b"".join(buffer).strip()
            if data:
                emit("stderr", data + b"\n")

        def stdout_writer(stream) -> None:
            nonlocal stdout_bytes, writer_error
            try:
                ensure_dir(destination_path.parent)
                with destination_path.open("wb") as fileobj:
                    while True:
                        if cancel_event is not None and cancel_event.is_set():
                            raise OSError("Transfer cancelled by user")
                        chunk = stream.read(max(64 * 1024, int(buffer_size or 1024 * 1024)))
                        if not chunk:
                            break
                        fileobj.write(chunk)
                        stdout_bytes += len(chunk)
                        if progress_callback:
                            progress_callback(stdout_bytes)
            except Exception as exc:
                writer_error = exc
                if cancel_event is None or not cancel_event.is_set():
                    emit("stderr", f"{exc}\n")

        try:
            if cancel_event is not None and cancel_event.is_set():
                return self._cancelled_before_start(command_list, started, device_context)
            process = self._start_process(
                command_list,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if process is None:
                return self._shutdown_before_start(command_list, started, device_context)
            threads = [
                threading.Thread(target=stdout_writer, args=(process.stdout,), daemon=True),
                threading.Thread(target=stderr_reader, args=(process.stderr,), daemon=True),
            ]
            for thread in threads:
                thread.start()

            deadline = time.monotonic() + timeout if timeout else None
            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    status = "Cancelled by user"
                    error_type = "cancelled"
                    process.kill()
                    break
                if writer_error is not None:
                    status = "Output stream failed"
                    error_type = "output_error"
                    process.kill()
                    break
                if deadline is not None and time.monotonic() > deadline:
                    status = f"Timed out after {timeout} seconds"
                    error_type = "timeout"
                    process.kill()
                    break
                time.sleep(0.05)

            exit_code = process.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=2)

            if exit_code == 0 and writer_error is None and not error_type:
                status = "Success"
            elif error_type not in {"cancelled", "timeout", "output_error"}:
                status, error_type = self._classify_error("".join(stderr_parts), exit_code)
        except FileNotFoundError as exc:
            emit("stderr", str(exc) + "\n")
            exit_code = None
            status = "Executable not found"
            error_type = "not_found"
        except subprocess.TimeoutExpired:
            if process:
                process.kill()
            exit_code = None
            status = "Timed out while stopping process"
            error_type = "timeout"
        except OSError as exc:
            emit("stderr", str(exc) + "\n")
            exit_code = None
            status = "Operating system error"
            error_type = "os_error"
        finally:
            self._unregister_process(process)

        finished = datetime.now()
        duration = (finished - started).total_seconds()
        result = CommandResult(
            command=command_list,
            exit_code=exit_code,
            stdout=f"[binary stdout written: {stdout_bytes} bytes]" if stdout_bytes else "",
            stderr="".join(stderr_parts),
            duration=duration,
            started_at=started,
            finished_at=finished,
            success=exit_code == 0 and writer_error is None and not error_type,
            status=status,
            error_type=error_type,
        )
        self._finalize_result(result, device_context)
        return result

    def run_binary_output_with_writer(
        self,
        command: Iterable[str],
        output_writer: BinaryOutputWriter,
        timeout: int | float | None = None,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        output_callback: OutputCallback | None = None,
        cancel_event: threading.Event | None = None,
        device_context: DeviceContext | None = None,
    ) -> CommandResult:
        command_list = [str(part) for part in command]
        started = datetime.now()
        if self._is_shutting_down():
            return self._shutdown_before_start(command_list, started, device_context)
        if cancel_event is not None and cancel_event.is_set():
            return self._cancelled_before_start(command_list, started, device_context)
        stderr_parts: list[str] = []
        exit_code: int | None = None
        error_type = ""
        status = "Command completed"
        process: subprocess.Popen[bytes] | None = None
        writer_error: Exception | None = None

        def emit(channel: str, data: bytes | str) -> None:
            if not data:
                return
            text = data if isinstance(data, str) else data.decode("utf-8", "replace")
            if channel == "stderr":
                stderr_parts.append(text)
            if output_callback:
                output_callback(channel, text)

        def stderr_reader(stream) -> None:
            buffer: list[bytes] = []
            while True:
                try:
                    ch = stream.read(1)
                except ValueError:
                    break
                if not ch:
                    break
                if ch in b"\r\n":
                    data = b"".join(buffer).strip()
                    buffer.clear()
                    if data:
                        emit("stderr", data + b"\n")
                else:
                    buffer.append(ch)
            data = b"".join(buffer).strip()
            if data:
                emit("stderr", data + b"\n")

        def stdout_writer(stream) -> None:
            nonlocal writer_error
            try:
                output_writer(stream)
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
            except Exception as exc:
                writer_error = exc
                if cancel_event is None or not cancel_event.is_set():
                    emit("stderr", f"{exc}\n")

        try:
            if cancel_event is not None and cancel_event.is_set():
                return self._cancelled_before_start(command_list, started, device_context)
            process = self._start_process(
                command_list,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if process is None:
                return self._shutdown_before_start(command_list, started, device_context)
            threads = [
                threading.Thread(target=stdout_writer, args=(process.stdout,), daemon=True),
                threading.Thread(target=stderr_reader, args=(process.stderr,), daemon=True),
            ]
            for thread in threads:
                thread.start()

            deadline = time.monotonic() + timeout if timeout else None
            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    status = "Cancelled by user"
                    error_type = "cancelled"
                    process.kill()
                    break
                if writer_error is not None:
                    status = "Output stream failed"
                    error_type = "output_error"
                    process.kill()
                    break
                if deadline is not None and time.monotonic() > deadline:
                    status = f"Timed out after {timeout} seconds"
                    error_type = "timeout"
                    process.kill()
                    break
                time.sleep(0.05)

            exit_code = process.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=2)

            if exit_code == 0 and writer_error is None and not error_type:
                status = "Success"
            elif error_type not in {"cancelled", "timeout", "output_error"}:
                status, error_type = self._classify_error("".join(stderr_parts), exit_code)
        except FileNotFoundError as exc:
            emit("stderr", str(exc) + "\n")
            exit_code = None
            status = "Executable not found"
            error_type = "not_found"
        except subprocess.TimeoutExpired:
            if process:
                process.kill()
            exit_code = None
            status = "Timed out while stopping process"
            error_type = "timeout"
        except OSError as exc:
            emit("stderr", str(exc) + "\n")
            exit_code = None
            status = "Operating system error"
            error_type = "os_error"
        finally:
            self._unregister_process(process)

        finished = datetime.now()
        duration = (finished - started).total_seconds()
        result = CommandResult(
            command=command_list,
            exit_code=exit_code,
            stdout=(
                "[binary stdout streamed]"
                if exit_code == 0 and writer_error is None and not error_type
                else ""
            ),
            stderr="".join(stderr_parts),
            duration=duration,
            started_at=started,
            finished_at=finished,
            success=exit_code == 0 and writer_error is None and not error_type,
            status=status,
            error_type=error_type,
        )
        self._finalize_result(result, device_context)
        return result

    def _classify_error(self, text: str, exit_code: int | None) -> tuple[str, str]:
        lowered = text.lower()
        if "no devices/emulators found" in lowered or "device not found" in lowered:
            return "No Android device detected", "no_device"
        if "unauthorized" in lowered:
            return "ADB unauthorized. Confirm RSA fingerprint on your phone.", "unauthorized"
        if "offline" in lowered:
            return "Device is offline", "offline"
        if "more than one device" in lowered:
            return "Multiple devices detected. Choose an active device.", "multiple_devices"
        if "permission denied" in lowered:
            return "Permission denied by Android", "permission_denied"
        if "not found" in lowered and ("adb" in lowered or "fastboot" in lowered):
            return "Platform Tools executable not found", "not_found"
        return f"Command failed with exit code {exit_code}", "command_failed"

    def _finalize_result(
        self,
        result: CommandResult,
        device_context: DeviceContext | None,
    ) -> None:
        if device_context is not None:
            result.device_serial = device_context.serial
            result.device_generation = device_context.generation
            result.logs_folder = str(device_context.logs_path)
        requested_logs = device_context.logs_path if device_context is not None else self.logs_folder
        try:
            self._write_log(result, requested_logs)
        except OSError as primary_error:
            result.log_warning = f"Command completed, but its log could not be written: {primary_error}"
        self._notify(result)

    def _cancelled_before_start(
        self,
        command: list[str],
        started: datetime,
        device_context: DeviceContext | None,
    ) -> CommandResult:
        """Return a normal logged result without ever creating a subprocess."""

        finished = datetime.now()
        result = CommandResult(
            command=command,
            exit_code=None,
            stdout="",
            stderr="",
            duration=(finished - started).total_seconds(),
            started_at=started,
            finished_at=finished,
            success=False,
            status="Cancelled before execution",
            error_type="cancelled",
        )
        self._finalize_result(result, device_context)
        return result

    def _shutdown_before_start(
        self,
        command: list[str],
        started: datetime,
        device_context: DeviceContext | None,
    ) -> CommandResult:
        """Return a normal logged result after the subprocess gate has closed."""

        finished = datetime.now()
        result = CommandResult(
            command=command,
            exit_code=None,
            stdout="",
            stderr="",
            duration=(finished - started).total_seconds(),
            started_at=started,
            finished_at=finished,
            success=False,
            status="Command runner is shutting down",
            error_type="shutdown",
        )
        self._finalize_result(result, device_context)
        return result

    def _write_log(self, result: CommandResult, logs_folder: Path | None = None) -> None:
        target_folder = ensure_dir(logs_folder or self.logs_folder)
        log_file = target_folder / "openadb.log"
        jsonl_file = target_folder / "openadb.commands.jsonl"
        with self._lock:
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(f"[{result.started_at.isoformat(timespec='seconds')}] $ {result.command_text}\n")
                if result.stdout:
                    fh.write(result.stdout.rstrip() + "\n")
                if result.stderr:
                    fh.write("[stderr]\n" + result.stderr.rstrip() + "\n")
                fh.write(f"[exit={result.exit_code} duration={result.duration:.2f}s status={result.status}]\n\n")
            with jsonl_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")

    def _notify(self, result: CommandResult) -> None:
        for callback in list(self._listeners):
            try:
                callback(result)
            except Exception:
                continue

    @staticmethod
    def command_text(command: Iterable[str]) -> str:
        return format_command(list(command))


class BoundCommandRunner:
    """Command runner view that preserves the captured device log destination."""

    def __init__(self, source: CommandRunner, context: DeviceContext) -> None:
        self._source = source
        self.device_context = context

    def __getattr__(self, name: str):
        return getattr(self._source, name)

    def run(self, *args, **kwargs):
        kwargs["device_context"] = self.device_context
        return self._source.run(*args, **kwargs)

    def run_binary_output(self, *args, **kwargs):
        kwargs["device_context"] = self.device_context
        return self._source.run_binary_output(*args, **kwargs)

    def run_streaming(self, *args, **kwargs):
        kwargs["device_context"] = self.device_context
        return self._source.run_streaming(*args, **kwargs)

    def run_with_input_stream(self, *args, **kwargs):
        kwargs["device_context"] = self.device_context
        return self._source.run_with_input_stream(*args, **kwargs)

    def run_binary_output_to_file(self, *args, **kwargs):
        kwargs["device_context"] = self.device_context
        return self._source.run_binary_output_to_file(*args, **kwargs)

    def run_binary_output_with_writer(self, *args, **kwargs):
        kwargs["device_context"] = self.device_context
        return self._source.run_binary_output_with_writer(*args, **kwargs)
