from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


DANGEROUS_PACKAGES = {
    "com.android.systemui",
    "com.android.settings",
    "com.google.android.packageinstaller",
    "com.android.permissioncontroller",
    "com.google.android.gms",
    "com.google.android.gsf",
    "com.android.shell",
    "com.android.providers.downloads",
    "com.android.providers.settings",
    "com.android.providers.media",
    "com.android.launcher",
    "com.google.android.apps.nexuslauncher",
    "com.android.inputmethod.latin",
}


@dataclass(slots=True)
class RiskInfo:
    level: str = "normal"
    needs_confirmation: bool = False
    description: str = ""


def is_dangerous_package(package_name: str) -> bool:
    return package_name in DANGEROUS_PACKAGES


def analyze_command_risk(command: Sequence[str] | str) -> RiskInfo:
    if isinstance(command, str):
        lowered = command.lower()
        tokens = lowered.split()
    else:
        tokens = [str(part).lower() for part in command]
        lowered = " ".join(tokens)

    dangerous_patterns = [
        ("fastboot flashing unlock", "Unlocking the bootloader can wipe all user data."),
        ("fastboot flashing lock", "Locking the bootloader can wipe data or brick modified devices."),
        ("fastboot oem unlock", "OEM unlock can wipe all user data."),
        ("fastboot oem lock", "OEM lock can wipe data or brick modified devices."),
        ("fastboot erase userdata", "This erases all user data."),
        ("fastboot format userdata", "This formats user data."),
        ("fastboot flash", "Flashing images can make the device unbootable if the file is wrong."),
        ("adb sideload", "Sideloading updates modifies the device system."),
        ("pm uninstall --user 0", "Removing a system app for user 0 can break Android features."),
    ]
    for pattern, description in dangerous_patterns:
        if pattern in lowered:
            return RiskInfo("danger", True, description)

    if "pm uninstall" in lowered or ("adb" in tokens and "uninstall" in tokens):
        return RiskInfo("warning", True, "Uninstalling apps can remove data and functionality.")
    if "disable-user" in lowered:
        return RiskInfo("warning", True, "Disabling system apps can break Android features.")
    if lowered.strip() == "":
        return RiskInfo("warning", False, "Command is empty.")
    return RiskInfo("normal", False, "")


def needs_confirmation(command: Sequence[str] | str) -> bool:
    return analyze_command_risk(command).needs_confirmation
