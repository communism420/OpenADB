from __future__ import annotations

from pathlib import Path

from openadb.models.command_result import CommandResult

from .adb import ADBClient


class FileTransferManager:
    def __init__(self, adb: ADBClient) -> None:
        self.adb = adb

    def push(self, source: str | Path, android_destination: str) -> CommandResult:
        return self.adb.push(source, android_destination, timeout=600)

    def pull(self, android_source: str, pc_destination: str | Path) -> CommandResult:
        return self.adb.pull(android_source, pc_destination, timeout=600)
