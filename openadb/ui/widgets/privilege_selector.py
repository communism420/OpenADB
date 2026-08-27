from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt, Signal

from openadb.core.privilege import PrivilegeBackend
from openadb.ui.widgets.no_wheel_widgets import NoWheelComboBox


class PrivilegeModeSelector(NoWheelComboBox):
    """Consistent three-way access choice for an active or future device."""

    backend_changed = Signal(str)

    _OPTIONS = (
        (
            PrivilegeBackend.STANDARD,
            "Standard ADB (no requested Root/Shizuku)",
            "Standard",
            (
                "Use normal ADB only. OpenADB does not request Root or Shizuku elevation; "
                "dedicated device and transfer control planes retain their existing transport."
            ),
        ),
        (
            PrivilegeBackend.ROOT,
            "Root / su (when available)",
            "Root",
            "Use existing su/root access for supported operations when the device grants it.",
        ),
        (
            PrivilegeBackend.SHIZUKU,
            "Shizuku (when available)",
            "Shizuku",
            "Use the official Shizuku API for supported operations after Android grants access.",
        ),
    )

    def __init__(self, parent=None, *, compact: bool = False) -> None:
        super().__init__(parent)
        self._compact = bool(compact)
        self._profile_available = True
        self._runtime_status = ""
        self.setObjectName(
            "privilegeModeSelectorCompact"
            if self._compact
            else "privilegeModeSelector"
        )
        self.setAccessibleName("Privilege mode")
        self.setAccessibleDescription(
            "Choose Standard ADB without requested elevation, existing root access, or Shizuku "
            "for the active or next connected device profile."
        )
        self.setPlaceholderText(
            "Choose…" if self._compact else "Choose for the next device…"
        )
        for backend, full_label, compact_label, description in self._OPTIONS:
            self.addItem(
                compact_label if self._compact else full_label,
                backend.value,
            )
            index = self.count() - 1
            self.setItemData(index, description, Qt.ToolTipRole)
            self.setItemData(index, full_label, Qt.AccessibleTextRole)
        self.setCurrentIndex(0)
        self.currentIndexChanged.connect(self._selection_changed)
        self._update_help_text()

    def backend(self) -> PrivilegeBackend:
        return PrivilegeBackend.normalize(self.currentData())

    def has_backend(self) -> bool:
        """Return whether one of the three access modes is visibly selected."""

        return self.currentIndex() >= 0

    def set_backend(self, value) -> None:
        """Synchronize the visible choice without reporting a user change."""

        backend = PrivilegeBackend.normalize(value)
        changed = backend is not self.backend()
        index = self.findData(backend.value)
        if index < 0:
            index = self.findData(PrivilegeBackend.STANDARD.value)
        blocker = QSignalBlocker(self)
        self.setCurrentIndex(max(0, index))
        del blocker
        if changed:
            self._runtime_status = ""
        self._update_help_text()

    def set_pending_backend(self, value) -> None:
        """Show an explicit queued mode, or an honest empty offline state."""

        if str(getattr(value, "value", value) or "").strip():
            self.set_backend(value)
            return
        changed = self.currentIndex() >= 0
        blocker = QSignalBlocker(self)
        self.setCurrentIndex(-1)
        del blocker
        if changed:
            self._runtime_status = ""
        self._update_help_text()

    def set_runtime_status(self, text: str) -> None:
        """Expose the full live status to mouse and assistive-technology users."""

        self._runtime_status = str(text or "").strip()
        self._update_help_text()

    def set_profile_available(self, available: bool) -> None:
        """Describe whether the choice targets the active or next device."""

        self._profile_available = bool(available)
        self._update_help_text()

    def _selection_changed(self, _index: int) -> None:
        self._runtime_status = ""
        self._update_help_text()
        self.backend_changed.emit(self.backend().value)

    def _update_help_text(self) -> None:
        if not self.has_backend():
            help_text = (
                "No access-mode override is queued. Choose Standard ADB, Root, or "
                "Shizuku to apply it once to the next active device profile."
            )
            if self._runtime_status:
                help_text = f"{help_text}\n\nCurrent status: {self._runtime_status}"
            self.setToolTip(help_text)
            self.setAccessibleDescription(help_text)
            return
        backend = self.backend()
        description = next(
            option[3]
            for option in self._OPTIONS
            if option[0] is backend
        )
        profile_note = (
            "This choice is saved separately for the active device profile."
            if self._profile_available
            else (
                "No device profile is active. This choice is saved and applied once "
                "to the next active device profile."
            )
        )
        parts = [description, profile_note]
        if self._runtime_status:
            parts.append(f"Current status: {self._runtime_status}")
        help_text = "\n\n".join(parts)
        self.setToolTip(help_text)
        self.setAccessibleDescription(help_text)
