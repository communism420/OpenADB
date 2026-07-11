from __future__ import annotations

import shlex
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
    level: str = "Safe"
    needs_confirmation: bool = False
    description: str = ""
    typed_confirmation: str = ""


def is_dangerous_package(package_name: str) -> bool:
    return package_name in DANGEROUS_PACKAGES


def analyze_command_risk(command: Sequence[str] | str) -> RiskInfo:
    lowered = _canonical_command_text(command)
    words = lowered.split()

    if words and words[0] == "fastboot":
        if "unlock" in words:
            return RiskInfo("Critical", True, "Unlocking the bootloader commonly erases all user data.", "CONFIRM")
        if "lock" in words and ("flashing" in words or "oem" in words):
            return RiskInfo(
                "Critical", True,
                "Locking a modified bootloader can erase data or make the device unbootable.", "CONFIRM",
            )
        if "flash" in words:
            return RiskInfo(
                "Critical", True,
                "Flashing the wrong image or partition can make the device unbootable.", "CONFIRM",
            )
        if "boot" in words:
            return RiskInfo(
                "Critical", True,
                "Booting an untrusted or incompatible image can compromise or crash the device.", "CONFIRM",
            )
        if any(action in words for action in {"erase", "format", "-w"}):
            return RiskInfo("May erase data", True, "This fastboot operation permanently removes data.", "ERASE")

    critical_patterns = [
        ("fastboot flashing unlock", "Unlocking the bootloader commonly erases all user data."),
        ("fastboot flashing lock", "Locking a modified bootloader can erase data or make the device unbootable."),
        ("fastboot oem unlock", "OEM unlock commonly erases all user data."),
        ("fastboot oem lock", "OEM lock can erase data or make a modified device unbootable."),
        ("fastboot flash", "Flashing the wrong image or partition can make the device unbootable."),
        ("fastboot boot", "Booting an untrusted or incompatible image can compromise or crash the device."),
        ("su -c", "This runs an arbitrary command with existing root privileges."),
    ]
    for pattern, description in critical_patterns:
        if pattern in lowered:
            return RiskInfo("Critical", True, description, "CONFIRM")

    erase_patterns = [
        ("fastboot erase", "Erasing a partition permanently removes its contents."),
        ("fastboot format", "Formatting a partition permanently removes its contents."),
        ("fastboot -w", "The fastboot wipe option permanently removes Android user data."),
        ("rm -rf /data", "Recursively deleting /data can permanently remove Android user data."),
        ("rm -rf /sdcard", "Recursively deleting shared storage permanently removes user files."),
        ("rm -rf /storage", "Recursively deleting shared storage permanently removes user files."),
        ("--wipe_data", "This command can permanently erase Android user data."),
        ("--wipe-data", "This command can permanently erase Android user data."),
    ]
    for pattern, description in erase_patterns:
        if pattern in lowered:
            return RiskInfo("May erase data", True, description, "ERASE")

    state_change_patterns = [
        ("adb reboot", "The device will stop its current session and reboot into another mode."),
        ("fastboot reboot", "The device will leave its current fastboot session and reboot."),
        ("adb sideload", "Sideloading streams an update package that can modify the Android system."),
        ("adb install", "Installing an APK changes applications on the active device."),
        ("adb uninstall", "Uninstalling an application can remove its data and functionality."),
        ("pm uninstall", "Removing an application can remove data and break Android functionality."),
        ("disable-user", "Disabling a system application can break Android functionality."),
        ("adb push", "Copying files changes storage on the active Android device."),
        (" shell reboot", "The device will stop its current session and reboot."),
        ("pm install", "Installing an application changes applications on the active device."),
        ("settings put", "Changing Android settings can affect system behaviour."),
        ("settings delete", "Deleting Android settings can affect system behaviour."),
        ("fastboot --set-active", "Changing the active boot slot changes the next boot configuration."),
        ("<custom command>", "A custom shell command can change device state or data."),
    ]
    for pattern, description in state_change_patterns:
        if pattern in lowered:
            return RiskInfo("Changes device state", True, description)

    if lowered.strip() == "":
        return RiskInfo("Safe", False, "Command is empty.")
    return RiskInfo("Safe", False, "")


def _canonical_command_text(command: Sequence[str] | str) -> str:
    if isinstance(command, str):
        try:
            raw_tokens = shlex.split(command, posix=False)
        except ValueError:
            raw_tokens = command.split()
    else:
        raw_tokens = [str(part) for part in command]
    tokens = [str(token).strip().strip('"').lower() for token in raw_tokens if str(token).strip()]
    tool_index = -1
    tool = ""
    for index, token in enumerate(tokens):
        executable = token.replace("\\", "/").rsplit("/", 1)[-1]
        if executable in {"adb", "adb.exe"}:
            tool_index, tool = index, "adb"
            break
        if executable in {"fastboot", "fastboot.exe"}:
            tool_index, tool = index, "fastboot"
            break
    if tool_index < 0:
        return " ".join(tokens)

    normalized = [tool]
    index = tool_index + 1
    if tool == "adb":
        value_options = {"-s", "-t", "-h", "-p", "-l", "--one-device"}
        flag_options = {"-d", "-e", "-a", "--exit-on-write-error"}
    else:
        value_options = {"-s", "--slot", "--fs-options"}
        flag_options = {
            "-u", "--skip-secondary", "--skip-reboot", "--disable-verity",
            "--disable-verification", "--unbuffered", "--verbose",
        }
    while index < len(tokens):
        token = tokens[index]
        if token in value_options and index + 1 < len(tokens):
            index += 2
            continue
        if token in flag_options or token.startswith("--slot="):
            index += 1
            continue
        normalized.extend(tokens[index:])
        break
    return " ".join(normalized)


def needs_confirmation(command: Sequence[str] | str) -> bool:
    return analyze_command_risk(command).needs_confirmation
