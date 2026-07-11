from __future__ import annotations

from openadb.models.command_spec import CommandSpec


COMMAND_CATEGORIES = (
    "Common",
    "Device information",
    "Applications",
    "Files",
    "Reboot and recovery",
    "Advanced ADB",
    "Fastboot",
)


def command_specs() -> tuple[CommandSpec, ...]:
    """Return the complete built-in command catalog in display order."""
    return (
        _spec(
            "find_tools", "Find Platform Tools", "Search for adb and fastboot installations.",
            "OpenADB: find Platform Tools", "Common", "callback", basic=True,
        ),
        _spec(
            "adb_devices", "Show connected ADB devices", "List ADB transports with model and state details.",
            "adb devices -l", "Common", "adb", ("devices", "-l"), "ADB", use_serial=False,
        ),
        _spec(
            "fastboot_devices", "Show connected fastboot devices", "List devices visible to fastboot.",
            "fastboot devices", "Common", "fastboot", ("devices",), "fastboot", use_serial=False,
        ),
        _spec(
            "adb_version", "Show ADB version", "Display the selected adb executable version.",
            "adb version", "Common", "adb", ("version",), "ADB", use_serial=False,
        ),
        _spec(
            "fastboot_version", "Show fastboot version", "Display the selected fastboot executable version.",
            "fastboot --version", "Common", "fastboot", ("--version",), "fastboot", use_serial=False,
        ),
        _spec(
            "battery", "Show battery information", "Read charge, temperature, health, and power-source data.",
            "adb shell dumpsys battery", "Device information", "adb_shell", ("dumpsys", "battery"), "ADB", ("ADB",),
        ),
        _spec(
            "display_size", "Show display resolution", "Read the physical and overridden display size.",
            "adb shell wm size", "Device information", "adb_shell", ("wm", "size"), "ADB", ("ADB",),
        ),
        _spec(
            "display_density", "Show display density", "Read the physical and overridden display density.",
            "adb shell wm density", "Device information", "adb_shell", ("wm", "density"), "ADB", ("ADB",),
        ),
        _spec(
            "display_info", "Show complete display information", "Read display resolution and density together.",
            "adb shell \"wm size; wm density\"", "Device information", "adb_shell",
            ("wm", "size;", "wm", "density"), "ADB", ("ADB",),
        ),
        _spec(
            "getprop", "Show Android properties", "Read Android build, hardware, and runtime properties.",
            "adb shell getprop", "Device information", "adb_shell", ("getprop",), "ADB", ("ADB",), timeout=60,
        ),
        _spec(
            "global_settings", "Show global Android settings", "List global values from Android's settings provider.",
            "adb shell settings list global", "Device information", "adb_shell",
            ("settings", "list", "global"), "ADB", ("ADB",), basic=False,
        ),
        _spec(
            "list_packages", "List installed packages", "List package names reported by Android's package manager.",
            "adb shell pm list packages", "Applications", "adb_shell",
            ("pm", "list", "packages"), "ADB", ("ADB",),
        ),
        _spec(
            "install_apk", "Install an APK", "Choose an APK and install it on the active device.",
            "adb install <APK file>", "Applications", "adb", ("install",), "ADB", ("ADB",),
            file_requirement="append_file", file_filter="APK files (*.apk)",
        ),
        _spec(
            "uninstall_package", "Uninstall an application", "Remove a package and its application data.",
            "adb uninstall <package name>", "Applications", "adb", ("uninstall",), "ADB", ("ADB",),
            input_prompt="Package name:",
        ),
        _spec(
            "push", "Copy files to Android", "Choose a PC file or folder and an Android destination.",
            "adb push <PC source> <Android destination>", "Files", "adb", required_tool="ADB",
            required_modes=("ADB", "Recovery"), file_requirement="push_pair", timeout=None,
        ),
        _spec(
            "pull", "Copy files from Android", "Choose an Android source and a PC destination folder.",
            "adb pull <Android source> <PC destination>", "Files", "adb", required_tool="ADB",
            required_modes=("ADB", "Recovery"), file_requirement="pull_pair", timeout=None,
        ),
        _spec(
            "bugreport", "Save an Android bug report", "Write a full Android diagnostic report to a PC folder.",
            "adb bugreport <PC folder>", "Files", "adb", ("bugreport",), "ADB", ("ADB",),
            file_requirement="append_folder", timeout=600, basic=False,
        ),
        _spec(
            "reboot_system", "Reboot to Android", "Restart the active ADB device into the normal system.",
            "adb reboot", "Reboot and recovery", "adb", ("reboot",), "ADB", ("ADB", "Recovery"),
        ),
        _spec(
            "reboot_recovery", "Reboot to recovery", "Restart the active ADB device into recovery mode.",
            "adb reboot recovery", "Reboot and recovery", "adb", ("reboot", "recovery"), "ADB", ("ADB",),
        ),
        _spec(
            "reboot_bootloader", "Reboot to bootloader", "Restart the active ADB device into bootloader/fastboot mode.",
            "adb reboot bootloader", "Reboot and recovery", "adb", ("reboot", "bootloader"), "ADB", ("ADB", "Recovery"),
        ),
        _spec(
            "reboot_sideload", "Reboot to sideload mode", "Restart recovery into its ADB sideload mode.",
            "adb reboot sideload", "Reboot and recovery", "adb", ("reboot", "sideload"), "ADB", ("Recovery",), basic=False,
        ),
        _spec(
            "sideload_zip", "Sideload an update ZIP", "Choose an update ZIP and stream it to recovery.",
            "adb sideload <ZIP file>", "Reboot and recovery", "adb", ("sideload",), "ADB", ("Sideload",),
            file_requirement="append_file", file_filter="ZIP files (*.zip)", basic=False, timeout=None,
        ),
        _spec(
            "adb_start_server", "Start ADB server", "Start the local ADB background server.",
            "adb start-server", "Advanced ADB", "adb", ("start-server",), "ADB", use_serial=False, basic=False,
        ),
        _spec(
            "adb_kill_server", "Stop ADB server", "Stop the local ADB background server.",
            "adb kill-server", "Advanced ADB", "adb", ("kill-server",), "ADB", use_serial=False, basic=False,
        ),
        _spec(
            "logcat", "Capture a logcat snapshot", "Read the latest 300 Android log messages.",
            "adb shell logcat -d -t 300", "Advanced ADB", "adb_shell",
            ("logcat", "-d", "-t", "300"), "ADB", ("ADB",), basic=False, timeout=60,
        ),
        _spec(
            "shell", "Run an ADB shell command", "Enter one Android shell command to run without root.",
            "adb shell <custom command>", "Advanced ADB", "adb_shell_input", required_tool="ADB",
            required_modes=("ADB", "Recovery"), input_prompt="Shell command:", basic=False,
            risk_command="adb shell <custom command>",
        ),
        _spec(
            "root_check", "Check root access", "Check whether direct root or su access is available.",
            "adb shell id / su -c id", "Advanced ADB", "adb_root_check", required_tool="ADB",
            required_modes=("ADB",), basic=False, risk_command="adb shell id",
        ),
        _spec(
            "root_shell", "Run a root shell command", "Run one shell command through existing su/root access.",
            "adb shell su -c <custom command>", "Advanced ADB", "adb_root_shell_input", required_tool="ADB",
            required_modes=("ADB",), input_prompt="Root shell command:", requires_root=True, basic=False,
        ),
        _spec(
            "fastboot_reboot", "Reboot from fastboot", "Restart the active fastboot device into Android.",
            "fastboot reboot", "Fastboot", "fastboot", ("reboot",), "fastboot", ("Fastboot",),
        ),
        _spec(
            "fastboot_reboot_bootloader", "Reboot fastboot to bootloader", "Restart back into the bootloader.",
            "fastboot reboot-bootloader", "Fastboot", "fastboot", ("reboot-bootloader",), "fastboot", ("Fastboot",), basic=False,
        ),
        _spec(
            "fastboot_getvar", "Show all fastboot variables", "Read bootloader variables reported by fastboot.",
            "fastboot getvar all", "Fastboot", "fastboot", ("getvar", "all"), "fastboot", ("Fastboot",), timeout=60,
        ),
        _spec(
            "fastboot_flashing_unlock", "Unlock bootloader (flashing)", "Request bootloader unlock using the modern command.",
            "fastboot flashing unlock", "Fastboot", "fastboot", ("flashing", "unlock"), "fastboot", ("Fastboot",), basic=False,
        ),
        _spec(
            "fastboot_flashing_lock", "Lock bootloader (flashing)", "Request bootloader lock using the modern command.",
            "fastboot flashing lock", "Fastboot", "fastboot", ("flashing", "lock"), "fastboot", ("Fastboot",), basic=False,
        ),
        _spec(
            "fastboot_oem_unlock", "Unlock bootloader (OEM)", "Request bootloader unlock using the legacy OEM command.",
            "fastboot oem unlock", "Fastboot", "fastboot", ("oem", "unlock"), "fastboot", ("Fastboot",), basic=False,
        ),
        _spec(
            "fastboot_oem_lock", "Lock bootloader (OEM)", "Request bootloader lock using the legacy OEM command.",
            "fastboot oem lock", "Fastboot", "fastboot", ("oem", "lock"), "fastboot", ("Fastboot",), basic=False,
        ),
        _spec(
            "fastboot_boot", "Temporarily boot an image", "Boot an image without flashing it to a partition.",
            "fastboot boot <image file>", "Fastboot", "fastboot", ("boot",), "fastboot", ("Fastboot",),
            file_requirement="append_file", file_filter="Image files (*.img)", basic=False, timeout=300,
        ),
        _flash("flash_boot", "Flash boot image", "boot"),
        _flash("flash_init_boot", "Flash init_boot image", "init_boot"),
        _flash("flash_recovery", "Flash recovery image", "recovery"),
        _flash("flash_vbmeta", "Flash vbmeta image", "vbmeta"),
        _spec(
            "erase_userdata", "Erase userdata", "Erase the userdata partition and all user data.",
            "fastboot erase userdata", "Fastboot", "fastboot", ("erase", "userdata"), "fastboot", ("Fastboot",), basic=False,
        ),
        _spec(
            "erase_cache", "Erase cache", "Erase the fastboot cache partition when present.",
            "fastboot erase cache", "Fastboot", "fastboot", ("erase", "cache"), "fastboot", ("Fastboot",), basic=False,
        ),
        _spec(
            "format_userdata", "Format userdata", "Format userdata and remove all user data.",
            "fastboot format userdata", "Fastboot", "fastboot", ("format", "userdata"), "fastboot", ("Fastboot",), basic=False,
        ),
    )


def _flash(key: str, label: str, partition: str) -> CommandSpec:
    return _spec(
        key, label, f"Flash the selected image to the {partition} partition.",
        f"fastboot flash {partition} <image file>", "Fastboot", "fastboot", ("flash", partition),
        "fastboot", ("Fastboot",), file_requirement="append_file", file_filter="Image files (*.img)",
        basic=False, timeout=300,
    )


def _spec(
    key: str,
    label: str,
    description: str,
    actual_command: str,
    category: str,
    kind: str,
    args: tuple[str, ...] = (),
    required_tool: str = "None",
    required_modes: tuple[str, ...] = (),
    **kwargs,
) -> CommandSpec:
    return CommandSpec(
        key=key,
        label=label,
        description=description,
        actual_command=actual_command,
        category=category,
        kind=kind,
        args=args,
        required_tool=required_tool,
        required_modes=required_modes,
        **kwargs,
    )
