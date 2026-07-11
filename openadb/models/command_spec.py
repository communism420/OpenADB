from __future__ import annotations

from dataclasses import dataclass

from openadb.core.safety import RiskInfo, analyze_command_risk


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """Declarative metadata for one built-in Commands-page operation."""

    key: str
    label: str
    description: str
    actual_command: str
    category: str
    kind: str
    args: tuple[str, ...] = ()
    required_tool: str = "None"
    required_modes: tuple[str, ...] = ()
    file_requirement: str = ""
    file_filter: str = "All files (*.*)"
    input_prompt: str = ""
    requires_root: bool = False
    basic: bool = True
    use_serial: bool = True
    timeout: int | float | None = 120
    risk_command: str = ""

    @property
    def risk(self) -> RiskInfo:
        return analyze_command_risk(self.risk_command or self.actual_command)

    @property
    def risk_level(self) -> str:
        return self.risk.level

    @property
    def requires_file(self) -> bool:
        return bool(self.file_requirement)

    @property
    def requires_input(self) -> bool:
        return bool(self.input_prompt) or self.file_requirement in {"push_pair", "pull_pair"}

    @property
    def search_text(self) -> str:
        modes = " ".join(self.required_modes)
        return " ".join(
            [self.label, self.description, self.actual_command, self.category, self.required_tool, modes]
        ).casefold()
