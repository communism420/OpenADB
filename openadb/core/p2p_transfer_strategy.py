"""ACBridge P2P upload strategy for the File Manager.

All socket/session details stay in core through :mod:`acbridge_p2p`; the UI
only supplies captured paths, a cancellation event, and a progress sink.
"""

from __future__ import annotations

import threading
from pathlib import Path

from openadb.core.acbridge import ACBridgeClient
from openadb.core.acbridge_p2p import ACBridgeP2PClient, P2PTransferError
from openadb.core.adb import ADBClient
from openadb.core.transfer_plan import FIXED_PARALLELISM


class P2PTransferStrategy:
    """Authenticated ACBridge upload flow including the SAF permission retry."""

    def _is_public_removable_android_path(self, path: str) -> bool:
        text = str(path or "").replace("\\", "/").strip()
        return text.startswith("/storage/") and not text.startswith(
            ("/storage/emulated/", "/storage/self/")
        )

    def _bridge_needs_storage_grant(self, result) -> bool:
        text = "\n".join(
            str(part or "")
            for part in [
                result,
                getattr(result, "status", ""),
                getattr(result, "stderr", ""),
                getattr(result, "stdout", ""),
            ]
        ).lower()
        return "saf_permission_required" in text or "grant microsd/usb access" in text

    def _run_p2p_push_transfer(
        self,
        adb: ADBClient,
        local_paths: list[str],
        android_destination: str,
        cancel_event: threading.Event,
        item_callback,
        parallelism: int | None = 1,
        temp_path: Path | None = None,
        parallelism_mode: str = FIXED_PARALLELISM,
    ) -> dict:
        bridge = ACBridgeClient(adb, self.settings, temp_folder=temp_path)
        client = ACBridgeP2PClient(bridge, temp_folder=temp_path)
        try:
            result = client.upload(
                local_paths,
                android_destination,
                cancel_event=cancel_event,
                progress_callback=lambda update: self._emit_transfer(
                    item_callback, update
                ),
                parallelism=parallelism,
                parallelism_mode=parallelism_mode,
            )
        except P2PTransferError as exc:
            if (
                not cancel_event.is_set()
                and self._bridge_needs_storage_grant(exc)
                and self._is_public_removable_android_path(android_destination)
            ):
                grant_result = bridge.grant_storage_access(
                    android_destination,
                    timeout=600,
                    cancel_event=cancel_event,
                )
                if grant_result.success and not cancel_event.is_set():
                    try:
                        result = client.upload(
                            local_paths,
                            android_destination,
                            cancel_event=cancel_event,
                            progress_callback=lambda update: self._emit_transfer(
                                item_callback, update
                            ),
                            parallelism=parallelism,
                            parallelism_mode=parallelism_mode,
                        )
                    except P2PTransferError as retry_exc:
                        exc = retry_exc
                    else:
                        return self._p2p_transfer_result(result, item_callback)
                else:
                    grant_message = (
                        grant_result.status
                        or grant_result.stderr
                        or "Storage access was not granted."
                    )
                    exc = P2PTransferError(
                        f"{exc}\nAndroid storage permission: {grant_message}"
                    )
            message = str(exc)
            self._emit_transfer(
                item_callback, {"type": "file_done", "message": message}
            )
            return {
                "success": False,
                "cancelled": cancel_event.is_set(),
                "messages": [message],
                "summary": message,
            }
        return self._p2p_transfer_result(result, item_callback)

    def _p2p_transfer_result(self, result, item_callback) -> dict:
        self._emit_transfer(
            item_callback,
            {
                "type": "file_done",
                "message": result.message,
                "done_files": result.files_sent,
                "total_files": result.files_sent,
                "done_bytes": result.bytes_sent,
                "total_bytes": result.bytes_sent,
            },
        )
        return {
            "success": result.success,
            "cancelled": False,
            "messages": [result.message],
            "summary": result.message,
            "done_bytes": result.bytes_sent,
            "done_files": result.files_sent,
        }
