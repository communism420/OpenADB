from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence


@dataclass(slots=True)
class CommandResult:
    command: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    duration: float
    started_at: datetime
    finished_at: datetime
    success: bool
    status: str = ""
    error_type: str = ""

    @property
    def command_text(self) -> str:
        return format_command(self.command)

    def to_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "command_text": self.command_text,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration": self.duration,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": self.finished_at.isoformat(timespec="seconds"),
            "success": self.success,
            "status": self.status,
            "error_type": self.error_type,
        }


def format_command(command: Sequence[str]) -> str:
    parts: list[str] = []
    for part in command:
        text = str(part)
        if not text:
            parts.append('""')
        elif any(ch.isspace() for ch in text) or any(ch in text for ch in '"&|<>^'):
            parts.append('"' + text.replace('"', '\\"') + '"')
        else:
            parts.append(text)
    return " ".join(parts)
