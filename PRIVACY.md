# OpenADB privacy policy

Effective date: 2026-08-28

This policy covers the OpenADB Windows desktop application and the bundled
OpenADB Bridge (`io.github.communism420.openadb.acbridge`) Android helper. It
describes the current `3.1.0` behavior. Material privacy changes must be
documented before a release containing them is published.

## Summary

OpenADB has no maintainer-operated online service, user account system,
advertising, analytics, telemetry, or automatic crash-report upload. OpenADB
does not sell personal information and does not send device data, files,
commands, logs, or usage statistics to the OpenADB maintainer.

**This program will not transfer any information to other networked systems unless specifically requested by the user or the person installing or operating it.**

Connecting a device, enabling automatic device monitoring, starting Wireless
ADB discovery, pairing or connecting over the network, choosing a file
transfer, or running an ADB or fastboot command is such a user-selected
operation. The destination is the selected Android device or a local Platform
Tools process—not an OpenADB collection service.

After a usable ADB connection is established, OpenADB automatically checks the
installed OpenADB Bridge version. It can install a missing bundled helper or
update an older one without a separate install click; an already-current or
newer helper is left unchanged. Android permission prompts and grants still
require the user's action.

## Information processed locally

Depending on the features used, OpenADB can process and store:

- Android device serials, model and manufacturer data, Android/SDK version,
  connection state, transport address, and user-defined device profile name;
- installed package names, application labels, versions, enabled state,
  classification data, APK paths, application icons, and package metadata;
- user-selected Windows and Android paths, file names, sizes, timestamps, and
  file contents transferred between the PC and Android device;
- APK backups, restore metadata, icons, command records, temporary files, and
  incomplete-transfer cleanup information;
- application settings, UI state, filters, command history, selected access
  mode, remembered Wireless ADB hosts and ports, and file-manager paths;
- commands executed through Platform Tools or the selected Root/Shizuku
  backend, together with their stdout, stderr, exit status, duration, and
  diagnostic errors.

Wireless ADB pairing codes and QR passwords are temporary credentials and are
not saved in OpenADB settings. Pairing and connection ports can be retained in
the selected device profile. OpenADB attempts to redact known pairing and P2P
secrets from its command and error logs, but logs can still contain device
serials, package names, file paths, command output, or other information
returned by the PC or Android device. Review logs before sharing them.

## Local storage and retention

By default, settings and application-created data are stored below:

```text
C:/Users/<user>/OpenADB/
```

Global settings and default global log, cache, temporary, and backup folders
can exist directly below that base directory before or without a device
profile being active. After a device profile is activated, device-specific
data is normally kept in separate `Phones/<device>/` or `TVs/<device>/`
folders. Older installations can migrate data from `%APPDATA%/OpenADB/` or a
former `OpenADB-data/` directory. A user can select custom backup, temporary,
and log folders, which remain at those locations until the user removes them.

`Settings > Maintenance` can reset UI state or clear settings and caches. APK
backups are preserved by default; deleting them requires a separate option and
an additional severe confirmation. Users can also close OpenADB and manually
delete its data directory and any configured external folders after reviewing
the files they want to retain.

## Device and network communication

OpenADB communicates with Android devices only for features selected or
enabled by the user:

- USB ADB and fastboot use Google Android Platform Tools between the PC and
  the selected device.
- Wireless ADB uses Platform Tools and local-network TCP connections. Modern
  Android Wireless debugging supplies its own pairing and transport security;
  legacy ADB-over-TCP can lack those protections and should be used only on a
  trusted private network.
- Wireless discovery uses local mDNS through Platform Tools and the `zeroconf`
  fallback. Discovery announcements and queries are visible to devices on the
  local network.
- PC-to-Android P2P transfers connect directly to OpenADB Bridge on the
  selected device. Sessions are authenticated and file integrity is verified,
  but file payloads are not encrypted. Use P2P only on a trusted private
  network. Android-to-PC transfers currently use Platform Tools.
- OpenADB can install or update the bundled OpenADB Bridge on a connected
  device and can ask Android to display Storage Access Framework, All files
  access, Root, or Shizuku permission controls. Android stores grants that the
  user approves until the user revokes them or uninstalls the helper.

OpenADB does not install Root or Shizuku, start the Shizuku service, unlock a
bootloader, bypass Android permission prompts, or transmit Root/Shizuku results
to the maintainer. The user remains responsible for the selected device,
network, command, files, and permissions.

## Third-party services and software

Runtime device operations use locally installed or bundled Android Platform
Tools and, when selected, the separately installed Shizuku service. Their
behavior and the Android/Windows operating systems are governed by their own
projects and platform policies.

OpenADB release files are distributed through GitHub. Downloading a release or
voluntarily opening an issue is governed by the
[GitHub Privacy Statement](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement).
Do not post device serials, pairing credentials, private logs, personal file
paths, or other sensitive material in a public issue.

If SignPath Foundation accepts the project, SignPath will be used during the
maintainer's release process for Authenticode code signing. This does not add
runtime telemetry to OpenADB. Windows signature validation can independently
contact certificate, timestamp, or revocation infrastructure according to the
operating system and certificate-provider policies. SignPath's handling of
application and signing-account data is described in the
[SignPath privacy policy](https://signpath.io/privacy-policy).

## Deleting OpenADB and device-side data

To remove the portable Windows application, close OpenADB and delete its
executable. To remove its local data, use the Maintenance controls or manually
delete `C:/Users/<user>/OpenADB/` and any custom folders after preserving any
backups you need.

To remove the current Android helper and its retained grants, uninstall
`io.github.communism420.openadb.acbridge` from Android and revoke any remaining
Storage Access Framework, All files access, Root, or Shizuku permission
associated with it. Devices that previously used the retired
`com.communism420.acbridge` development package must remove that package
separately. Android, Root-manager, and Shizuku interfaces vary by device.

## User disclosures and support

OpenADB does not automatically receive support data. If a user voluntarily
shares logs, screenshots, backups, or other diagnostic information through
GitHub or another channel, that disclosure is initiated and controlled by the
user. Remove sensitive information first.

Privacy questions and reports can be opened through
[GitHub Issues](https://github.com/communism420/OpenADB/issues). Do not include
secrets or private device data in a public report.

## Policy changes

Future changes to data collection, external communication, or retention must
be documented here and in the relevant release notes before release. The
effective date above will be updated when this policy materially changes.
