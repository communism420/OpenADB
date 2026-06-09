# OpenADB

![OpenADB logo](logo.png)

Version: `1.0.0`

OpenADB is a Windows desktop GUI for Android Platform Tools. It uses ADB and fastboot directly, without MTP and without root requirements, to inspect devices, manage apps, back up APKs before uninstalling, restore backups, transfer files, run common commands, and keep useful logs.

## Independence and Attribution

OpenADB is an independent project. It is not affiliated with, endorsed by, sponsored by, or connected to ADB AppControl, its author, or its brand.

The author of OpenADB does not claim ownership of any ADB AppControl code, branding, name, logo, design, or other intellectual property. Any mention of ADB AppControl is only descriptive, for compatibility context or user understanding.

OpenADB uses its own package name for its optional Android bridge helper:

```text
com.communism420.acbridge
```

The bundled ACBridge APK is an independent helper built from the source in `openadb/resources/acbridge/`. Do not use ADB AppControl branding, package identity, code, or assets as OpenADB branding.

## Acknowledgements

OpenADB was built with respect for the people and projects whose tools, code, data, or ideas helped shape it:

- Google, the Android Open Source Project, and the [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools) maintainers, for ADB and fastboot.
- CyberCat and [ADB AppControl](https://adbappcontrol.com/), for product ideas around practical ADB app management, real app labels/icons through a helper bridge, and a clear user workflow for non-root Android app control. OpenADB remains an independent project and does not claim ownership of, or bundle, ADB AppControl code, branding, package identity, logo, or assets.
- T0biasCZe and [AdbFileManager](https://github.com/T0biasCZe/AdbFileManager), for the open ADB-based file-manager reference used while shaping OpenADB's two-panel File Manager, transfer workflow, and native Windows Explorer-style PC side.
- Universal-Debloater-Alliance and [Universal Android Debloater Next Generation](https://github.com/Universal-Debloater-Alliance/universal-android-debloater-next-generation), for the open Universal Debloat List data used to classify installed packages in the Apps page.
- The [PySide6 / Qt for Python](https://doc.qt.io/qtforpython-6/) maintainers, for the desktop UI framework.
- The [Pillow](https://python-pillow.org/) maintainers, for image handling used in icon and cache workflows.
- The [apkutils2](https://pypi.org/project/apkutils2/) maintainers, for APK metadata parsing used as a fallback when bridge-based app labels/icons are unavailable.

No endorsement by these projects is implied.

## Requirements

- Windows 10 or Windows 11.
- Python 3.10 or newer.
- Android Platform Tools from Google.
- USB debugging enabled on the Android device for ADB features.
- PySide6 and Pillow Python packages.
- `apkutils2` for reading real application labels from APK metadata.

## Install Python Dependencies

```powershell
python -m pip install -r requirements.txt
```

## Install Android Platform Tools

Download Platform Tools from Google:

https://developer.android.com/tools/releases/platform-tools

Extract the archive. The extracted folder must contain `adb.exe` and `fastboot.exe`.

## How OpenADB Finds Platform Tools

OpenADB searches in this order:

1. Saved path from settings.
2. `platform-tools/` next to the program.
3. The program folder.
4. System `PATH`.
5. User `PATH`.
6. Typical folders:
   - `C:/platform-tools/`
   - `C:/Android/platform-tools/`
   - `C:/Program Files/Android/platform-tools/`
   - `C:/Users/<user>/AppData/Local/Android/Sdk/platform-tools/`
   - `C:/Users/<user>/platform-tools/`

If several valid folders are found, OpenADB shows a picker with path, ADB version, fastboot version, and source. You can change the active folder in `Settings`.

To add Platform Tools to `PATH`, add the folder containing `adb.exe` to your Windows user environment variable `Path`, then restart OpenADB.

## Run

Recommended on Windows:

```powershell
OpenADB.bat
```

Or from a terminal:

```powershell
python -m openadb.main
```

or:

```powershell
python openadb/main.py
```

## USB Debugging

On the phone:

1. Open Android Settings.
2. Enable Developer options.
3. Enable USB debugging.
4. Connect USB.
5. Confirm the RSA fingerprint prompt.

If OpenADB shows `ADB unauthorized`, unlock the phone and confirm the RSA prompt. If the prompt does not appear, reconnect USB, revoke USB debugging authorizations on the phone, or run `adb kill-server` and `adb start-server`.

## Dashboard

Dashboard shows device state, serial, model, Android version, SDK version, Platform Tools status, ADB version, fastboot version, and the active platform-tools path. It also has quick actions for refresh, reboot, ADB devices, fastboot devices, logs, and settings.

## Apps

Apps lists installed packages with checkbox, icon or fallback icon, label/package name, type, state, version, APK paths, and size when Android allows it.

For faster real labels and rendered application icons, OpenADB automatically installs and starts its own helper APK, `com.communism420.acbridge`, from `openadb/resources/acbridge/ACBridge.apk`. The helper exports app labels and PNG icons through ADB-readable files, then OpenADB caches them locally. If the helper cannot be installed or started, OpenADB falls back to APK metadata parsing and clearly reports that fallback in the Apps status line.

ACBridge v6 exports only the packages OpenADB asks for, reports live label/icon progress, exports versionName/versionCode and APK size through Android PackageManager, stores pre-rendered PNG icons without extra ZIP recompression, and OpenADB imports those PNGs directly into the icon cache. Like ADB AppControl's bridge workflow, OpenADB exchanges the generated app list, metadata table, and icon archive through the public `/sdcard/.adac` cache folder so ADB can pull compact cache files instead of hundreds of APK files.

OpenADB does not automatically delete an installed ACBridge package. If Android reports a signature mismatch while updating ACBridge, OpenADB keeps the existing helper and explains the issue. To move from an older manually built/debug-signed ACBridge to the bundled helper, uninstall `com.communism420.acbridge` manually and refresh Apps again.

OpenADB also loads per-package version metadata in parallel with a bounded worker pool. The default limit is `apps_metadata_parallelism: 6` in `settings.json`; raising it too high can make ADB slower or less stable on some devices.

OpenADB includes a local snapshot of the Universal Android Debloater Next Generation Universal Debloat List:

https://github.com/Universal-Debloater-Alliance/universal-android-debloater-next-generation/blob/main/resources/assets/uad_lists.json

The database is GPL-3.0 data from the Universal-Debloater-Alliance project. OpenADB uses it only to classify installed package names in the Apps table as `Recommended`, `Advanced`, `Expert`, `Unsafe`, or `Not listed`. `Unsafe` means the package is known to UAD but should not be removed casually.

Supported actions:

- Refresh apps.
- Search and filter user/system/enabled/disabled apps.
- Select all visible and unselect all.
- Back up selected apps.
- Uninstall selected apps.
- Disable or enable selected apps.
- Run `cmd package install-existing`.
- Export package list to CSV.

Before uninstalling, OpenADB creates an APK backup by default. If backup fails, uninstall is skipped for that app. Split APK packages are backed up by saving every APK path returned by `pm path`. Restore uses `adb install` for one APK and `adb install-multiple` for split APK backups.

System apps are removed only for Android user 0 with:

```text
pm uninstall --user 0 package.name
```

They can often be restored with:

```text
cmd package install-existing package.name
```

Critical packages such as System UI, Settings, Google Play services, package installer, permission controller, media/settings providers, launcher, shell, and keyboard are highlighted and require extra confirmation.

## Backups

Backups are stored as:

```text
<OpenADB data>/backups/package.name/date_time/
```

Each backup contains APK files, `metadata.json`, optional `icon.png`, and `command_log.txt`.

The Backups tab can refresh backups, restore selected backup, delete backup, open the backup folder, show metadata, and install APK files from backup.

## File Manager

The File Manager has two panels:

- Left: Android filesystem through ADB only.
- Right: Windows filesystem.

Android listing uses `adb shell`; transfer uses only:

```text
adb pull
adb push
```

MTP is not used. Drag and drop is implemented between the panels. Android protected paths show a warning because non-root ADB usually cannot write to system partitions.

## Commands

The Commands tab provides buttons for common ADB, fastboot, and preset commands, plus manual command input with command history.

Commands that need files open a Windows file picker. Commands that need a package name open an input dialog. Dangerous operations require confirmation, including bootloader unlock/lock, fastboot flash, fastboot erase/format, ADB sideload, and uninstall operations.

## Logs

OpenADB logs:

- Time.
- Full command.
- stdout.
- stderr.
- exit code.
- duration.
- human-readable status.

The Logs tab can clear the visible log, save it, copy it, and open the logs folder. Technical details are kept in log files.

## Settings

Settings are stored in JSON under the Windows user profile:

```text
C:/Users/<user>/OpenADB/
```

If older data exists in `%APPDATA%/OpenADB/` or the former portable `OpenADB-data/` folder, OpenADB migrates it into `C:/Users/<user>/OpenADB/` on startup.

When a phone is detected, OpenADB switches to a per-device profile under:

```text
C:/Users/<user>/OpenADB/devices/<device-serial>/
```

That profile contains its own `settings.json`, `backups/`, `temp/`, `logs/`, `app-cache/`, `icon-cache/`, APK metadata cache, ACBridge temporary files, and app backup folders. This keeps settings, app data, icons, logs, temporary files, and backups separated between different phones.

Settings include:

- platform-tools folder and versions.
- backups, temp, and logs folders.
- theme: System, Light, Dark.
- auto-refresh device status and interval.
- show system apps.
- show warnings.
- require backup before uninstall.
- clear icon cache.
- clear temporary files.

## Safety Notes

Fastboot unlock/lock can wipe data. Fastboot flash/erase/format can make a device unbootable if used incorrectly. Removing or disabling system apps can break Android features. OpenADB asks for confirmation for risky actions, but you are responsible for understanding the command before running it.
