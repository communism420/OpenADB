# Changelog

All notable OpenADB changes made since the start of the local audit and project
redesign are documented in this file.

The format is based on Keep a Changelog. The current development version is
3.0.3.

## [3.0.3] — Unreleased

### Fixed

- Fixed PC-to-Android P2P uploads to removable MicroSD/USB storage failing
  after startup with `.openadb-*.part: open failed: EACCES (Permission
  denied)` when Android's global All files access setting was enabled.
- ACBridge now resolves removable destinations through the matching persisted
  Storage Access Framework tree before considering any direct filesystem
  fallback. A global primary-storage permission can no longer silently bypass
  an existing MicroSD/USB SAF grant.
- Storage access is selected once before ACBridge publishes `READY` and the
  same pinned backend is used for the complete upload. This removes the former
  check/use mismatch and prevents the PC from sending buffered file data to a
  destination that only appeared writable.
- On TV firmware without a usable SAF picker, the compatibility fallback now
  requires the storage-access flow to record approval for that removable
  volume, Android's global storage-manager permission, and a successful
  create/delete write probe. The probe completes before the P2P data server is
  opened; if it fails, ACBridge clears the fallback approval and remains in
  the permission flow instead of starting a transfer that will fail with
  `EACCES`.
- Persisted SAF trees are now accepted only while Android still reports an
  active read/write grant. ACBridge pins the grant by volume and performs a real
  create/open/delete probe in the exact destination before publishing
  `READY`, including when the destination is the granted tree root.
- The storage picker now reports success only after Android confirms an exact,
  persistent read/write tree grant. Invalid picker results and failed direct
  write probes are rejected, and removable-storage probing runs off the Android
  UI thread.
- Privileged bridge commands now use a shell-permission-protected activity;
  the public launcher rejects command extras and destructive operations from
  public bridge settings.

### Version

- Updated OpenADB, ACBridge, Windows metadata, build workflows, screenshots,
  and active release documentation to version 3.0.3.
- ACBridge 3.0.3 uses `versionCode 30301` and is rebuilt from source as
  `ACBridge-3.0.3.apk`.

### Validation

- Added source-level regressions for SAF-first removable-storage routing with
  All files access enabled, pre-`READY` access pinning, active read/write-grant
  checks, per-volume fallback approval, and Unicode removable paths. Added a
  host regression that converts the exact removable `.part`/`EACCES` failure
  into one permission request and one bounded retry.
- A disposable Android 16 emulator with global All files access enabled
  transferred 2,097,455 bytes through an approved public removable SAF tree;
  SHA-256 matched and no `.openadb-*` residue remained. After clearing the SAF
  grant, ACBridge requested permission without opening the data connection or
  creating the remote file. The emulator also rejected a command sent to the
  public launcher and accepted the storage flow through the DUMP-protected
  command activity. Physical Android TV MicroSD validation remains pending
  because no physical ADB device was connected.

## [3.0.2] — Unreleased (superseded by 3.0.3)

### Fixed

- Replaced the private-file `run-as` P2P bootstrap with a request-scoped
  abstract Android control socket reached through a temporary local-only ADB
  forward. P2P startup no longer fails on otherwise supported OEM devices
  whose `/data` permissions cause Android's `run-as` safety check to reject
  the helper before any file bytes are sent.
- ACBridge now reports storage-permission state and the authenticated `READY`
  startup acknowledgement through the same bounded control channel. OpenADB
  waits for that acknowledgement before opening the LAN data connection.
- Service startup now uses the Android 6–7-compatible path on API 23–25 and a
  foreground service on Android 8 and later.
- The LocalSocket read timeout now applies only to the bounded bootstrap. The
  established control monitor remains blocking until request-scoped cleanup
  closes it, preventing normal SAF waits from being mistaken for disconnects
  on Android releases that surface idle LocalSocket timeouts as `IOException`.

### Security and lifecycle

- The bootstrap secret and generated session metadata remain in memory and are
  not placed in process arguments or request/status files on the Android
  filesystem. ACBridge accepts the forwarded control channel only from the
  Android shell or root peer identity.
- Cancellation and cleanup are request-scoped. Success, cancellation, timeout,
  and startup failure close the control sockets and remove only the matching
  temporary ADB forward, without overriding a more useful primary error.
- OpenADB also recovers the unique matching forward when Platform Tools loses
  or returns a malformed creation response, and sends a public-ID cancellation
  fallback so closing the tunnel cannot race ahead of Android consuming
  `CANCEL`.
- Control messages are size-bounded and deadline-aware; authenticated startup
  validation and the existing authenticated P2P data protocol remain in
  effect.

### Version

- Updated OpenADB, ACBridge, Windows metadata, build workflows, screenshots,
  and active release documentation to version 3.0.2.
- ACBridge 3.0.2 uses `versionCode 30201` under the documented version-code
  policy and was built as `ACBridge-3.0.2.apk`.

### Validation

- Added automated regression coverage for the ADB-forwarded control bootstrap,
  authenticated startup acknowledgement, cancellation, cleanup, redaction,
  control-frame limits, and Android-version-specific service startup.
- A disposable read-only API 36 Android emulator completed a two-session
  nested-folder upload: three files, six entries, 1,048,624 bytes, an empty
  directory, and every SHA-256 were verified. Emulator NAT required a
  test-only ADB forward for the data sockets, so this validates ACBridge,
  control, folder, and integrity behavior but not direct-LAN routing or speed.
- The unsigned one-file EXE passed a clean-profile title, bundled Platform
  Tools, normal-close, and crash-log smoke test. Thirty-eight isolated test
  modules passed cleanly; all 41 assertions in the remaining adaptive-window
  module passed, but its local PySide6 offscreen process then exited with a
  native Windows heap-corruption code. The packaged EXE did not reproduce that
  teardown failure. Windows CI run `29409867004` subsequently passed the full
  clean-process matrix on CPython 3.10–3.14, so the native exit remains a
  local-host-only observation rather than a reproduced release failure.
- No physical Android device, OEM Android 17 build, removable storage, or
  direct-LAN route was available, so physical hardware transfer success
  remains to be verified.

## [3.0.1] — 2026-07-13

### Fixed

- Fixed all PC-to-Android P2P uploads failing before the first file byte when
  Android parsed the streamed bootstrap redirection outside ACBridge's
  `run-as` shell. The complete nested command is now preserved as the single
  remote `adb shell` argument, so the one-time request is written inside the
  helper's private files directory.
- Made bootstrap directory preparation fail immediately if its private files
  directory cannot be created instead of allowing a later generic write
  failure.
- P2P bootstrap failures now prefer safely redacted Android `stderr` over the
  generic `Command failed with exit code 1` status, retaining the actionable
  cause without exposing the request ID or bootstrap secret.

### Version

- Updated OpenADB, ACBridge, Windows metadata, build workflows, screenshots,
  and active release documentation to version 3.0.1.
- ACBridge 3.0.1 uses `versionCode 30101` under the documented version-code
  policy and is rebuilt from source as `ACBridge-3.0.1.apk`.

### Validation

- Added regressions that require the nested `run-as ... sh -c` script to stay
  in one `adb shell` argument and that preserve useful, redacted Android error
  details when bootstrap creation fails.
- Passed all 39 isolated test modules and all 565 tests with
  `ResourceWarning` treated as an error, plus compileall, Ruff, workflow YAML,
  embedded PowerShell, immutable Action-reference, and diff validation.
- Rebuilt and independently verified both byte-identical 45,613-byte ACBridge
  APK aliases: package/version metadata, ZIP alignment, v1/v2/v3 signatures,
  and the established signer digest all pass.
- Regenerated and validated the seven versioned README screenshots. The
  contextual Applications capture now uses the complete offscreen render path
  and every persistent navigation row is checked.
- The failure was confirmed from a sanitized local device log, but no ADB
  transport remained connected for a real post-fix phone transfer. No
  hardware-transfer success is claimed by this changelog.

## [3.0.0] — 2026-07-13

### Version

- OpenADB, ACBridge, the bundled helper APK, PyInstaller artifact name, and
  Windows version metadata now use version 3.0.0.
- ACBridge uses the established versionCode scheme
  `major * 10000 + minor * 1000 + patch * 100 + build`; the security-hardened
  ACBridge 3.0.0 helper is build 2 and therefore uses `versionCode 30002`.
- The 3.0.0 helper APK is rebuilt from source and is not a renamed older APK.

### Architecture and operation lifecycle

- Long-running device work captures a frozen `DeviceContext` containing the
  target transport, profile identity and monotonic generation. Bound ADB and
  fastboot clients keep that target even if the active device changes.
- A central `OperationRegistry` owns conflicts, cancellation, generation
  invalidation and shutdown cleanup. Late results are rejected before they can
  update a different device, profile, path or cache.
- Applications, backups and File Manager workflows are separated into focused
  controllers, coordinators, immutable transfer models and transport
  strategies while retaining the existing settings, profiles and backup
  formats.

### File Manager and P2P

- ADB remains the default upload transport for new device profiles. The first
  unacknowledged P2P selection for each profile shows a warning that
  authentication and file-integrity checks do not encrypt file data. Accepting
  it suppresses repeats for the current run; cancelling keeps or restores ADB,
  and the optional `Do not show this warning again` acknowledgement is stored
  only in that profile.
- While P2P is selected, File Manager shows the compact
  `Authenticated, not encrypted` status and directs users to use only a trusted
  private network, never public, shared, guest, or otherwise untrusted Wi-Fi.
- P2P parallelism now defaults to `Auto (recommended)`. Its deterministic
  planner selects 1–4 streams from captured file statistics and does not probe,
  benchmark, or guess device or network speed. Per-profile manual 1–8 stream
  overrides remain available.

### Security

- ACBridge 3.0.0 build 2 (`versionCode 30002`) hardens the ADB-streamed P2P
  bootstrap, authenticated `READY` metadata, entry-metadata control frames,
  the canonical request transcript, per-file payload integrity, and the
  terminal success response with exact entry/file/byte counts. Cancellation
  and cleanup are request-scoped, network deadlines use monotonic time, and
  forged, truncated, or inconsistent success responses are rejected.
- Pairing, P2P, URL, and other authentication secrets are redacted from command
  previews, histories, logs, callbacks, worker output, errors, and object
  representations; ADB pairing secrets are passed through standard input
  instead of process arguments.
- Stage 5 validation used automated unit, offscreen, mock, socketpair, static,
  and local APK build/package/alignment/signature checks only; it does not
  claim new real-device or real-network verification.

### Interface and Windows integration

- Dashboard actions now follow the active transport state, including guarded
  Offline reconnect and direct Fastboot routing, without starting duplicate
  refresh work.
- Applications keeps its contextual actions usable at compact Windows window
  widths, preserves hidden selections, and exposes the relevant selection and
  filter state without expanding the page into a second toolbar.
- The System theme now follows Windows Light/Dark changes while OpenADB is
  running. Settings writes are atomic and retain a last-known-good backup;
  malformed or interrupted settings are preserved for diagnosis and recovered
  without silently mixing device profiles.

### Build and release

- Runtime, build, and development dependencies are now pinned and documented.
  Windows CI validates CPython 3.10 through 3.14 with compileall, Ruff, the
  complete tracked test suite in isolated processes, version/APK/spec checks,
  offscreen GUI smoke tests, privacy guardrails, and failure-only test logs.
- A pinned Windows workflow builds and inspects the one-file executable,
  bundles checksum-verified Android Platform Tools and ACBridge 3.0.0, and
  smoke-tests a clean temporary profile without device-changing commands.
- Authenticode signing is optional but fail-closed: partial secret setup,
  signing failure, or verification failure cannot produce a stable-named
  artifact. Builds without a certificate retain the `-unsigned` suffix.
- The release workflow requires successful CI for the exact tag commit,
  verifies source/build metadata and SHA-256 again, publishes signed builds as
  stable, and limits automatic unsigned output to a clearly labelled draft
  preview. The full operator and rollback procedure is documented in
  `docs/RELEASE_PROCESS.md`.
- Added a manual-only, approved-environment device-lab workflow and a
  fail-closed smoke tool whose default command set is strictly read-only.
  Sanitized JSON/JUnit reports exclude serials, IP addresses, usernames, home
  paths, filenames, secrets, and raw tool output.
- Added a 77-scenario Windows, Android transport, Applications, File Manager,
  and Commands validation matrix. Unavailable physical hardware, Windows 10,
  alternate-DPI/multi-monitor coverage, removable storage, controlled network
  faults, and signed-build checks remain explicitly unclaimed.

### Validation and release evidence

- Added deterministic release benchmarks for 1,200/3,000 applications, a
  5,000-entry File Manager tree, thousands of immutable transfer plans, Auto
  streams, stale-result checks, and operation-registry overhead. The report
  contains sanitized environment/timing data only and rejects non-finite or
  malformed results.
- Regenerated seven 3.0.0 README screenshots from isolated safe demo profiles:
  Dashboard Light/Dark, Applications with and without contextual actions, File
  Manager Auto P2P, Commands, and Settings. Automated checks require RGB
  1280×820 PNGs, empty EXIF, allowlisted metadata, current names, and README
  references.
- Hardened the repository privacy gate to scan text and binary content in
  UTF-8/UTF-16, Windows/POSIX home paths, private IPv4/IPv6 values, generated
  caches, signing containers, and PNG metadata. A pre-existing generated
  Androguard cache was removed from the active tree and ignored; its historical
  Git blob remains documented for a separately coordinated history rewrite.
- Built and smoke-tested the local unsigned preview
  `OpenADB-3.0.0-unsigned.exe` (90,452,041 bytes), SHA-256
  `B48BCB48F868581384D68EFAA2DC373317C347E90967AA7F11B393F4B8C01A5B`.
  Authenticode status is truthfully `NotSigned`; no signed stable release is
  claimed.
- Physical Windows 10, real Android/network/storage/device-lab scenarios,
  alternate DPI/multi-monitor coverage, and successful Authenticode signing
  remain external release blockers. Full evidence is in
  `OPENADB_3_RELEASE_REPORT.md`.

## [2.0.1] — 2026-07-12

### Added

- File Manager now offers 1–8 parallel streams when `P2P via ACBridge` is
  selected and stores the value separately for each device profile.
- Files are balanced between P2P sessions by size; directories are created
  before parallel writes begin to avoid Android SAF races.
- ACBridge supports multiple concurrent one-time P2P sessions and keeps its
  foreground service alive until every session has finished.
- A real three-stream transfer of three files totaling 268,201,658 bytes was
  verified on a Pixel 8 Pro; every local and Android SHA-256 hash matched.

### Fixed

- Fixed the first QR connection after settings removal: a transient mDNS
  transport in the `offline` state is no longer treated as a successful
  connection.
- QR pairing now succeeds only after a device in the `device` state appears in
  `adb devices`.
- After a successful `adb pair`, OpenADB continues waiting for the mDNS connect
  service and performs a real `adb connect` instead of triggering four useless
  Device Status Bar offline reconnect attempts.
- If pairing completes but no ready Wireless ADB connection appears, OpenADB
  reports an error instead of showing a false `QR pairing succeeded` dialog.
- Device Status Bar suspends automatic offline reconnect for the duration of a
  QR session and does not retry a transient transport already handled there.

### Version

- OpenADB, ACBridge, the bundled APK, and Windows build metadata were updated
  to 2.0.1.
- ACBridge versionCode was updated to 20101.

### Build

- Built the self-contained `exe_release/OpenADB-2.0.1.exe` (89,272,808 bytes).
- SHA-256: `33324F8015F411B97EF72AE6D27E384D3621882A1CF4FD98BC0F374E450C6220`.

## [2.0.0] — 2026-07-12

### Summary

OpenADB underwent a complete audit and a sequential GUI redesign without
removing existing functionality. The main window, Dashboard, Applications,
File Manager, Settings, Commands, dialogs, and error states now follow one
adaptive design system. Direct P2P file transfers through ACBridge were added,
QR Wireless Debugging was fixed, Material Design 3 icons were introduced, and
a self-contained Windows executable was prepared.

### Added

- A local technical audit of the GUI, risks, threads, settings, profiles,
  application shutdown, and performance.
- The `GUI_AUDIT.md`, `GUI_REDESIGN_PROGRESS.md`, and
  `GUI_REDESIGN_REPORT.md` documents.
- Adaptive side navigation with a compact mode and restored window state.
- Complete textual connection states: ADB, Recovery, Fastboot, Unauthorized,
  Offline, and No device.
- An active-device picker and a detailed device information dialog.
- Independent combinable Applications filters:
  `All/User/System`, `Any/Enabled/Disabled`, and UAD categories.
- Application search by both display name and package name.
- Displayed, total, and selected application counters.
- Per-device-profile persistence of sorting, filters, and hidden-row selection.
- A compact Applications bulk-action bar with an additional actions menu.
- A three-pane File Manager with a resizable splitter, global Windows path, and
  per-profile Android path.
- Android storage selection for internal storage, MicroSD, and USB.
- Root-access detection and an explicit root-assisted transfer mode with a safe
  fallback to regular ADB.
- File Manager keyboard actions: `F5`, `F2`, `Delete`, `Enter`, and
  `Backspace`.
- Bidirectional drag and drop between Android and Windows.
- A redesigned Settings page with separate Platform Tools, Appearance,
  Device monitoring, Applications and backups, Root and advanced features,
  Storage paths, and Maintenance sections.
- Separate Platform Tools discovery, folder selection, and verification
  operations.
- A safe UI-only reset and a full settings/cache reset that preserves APK
  backups.
- A catalog of 43 ADB/fastboot commands with Basic/Advanced modes, search,
  categories, availability checks, and exact-command preview before execution.
- A centralized command risk matrix, typed confirmation for critical
  operations, and rejection of custom commands outside `adb` and `fastboot`.
- Inline Commands output with status, exit code, duration, stdout, stderr,
  Copy, Clear, Cancel, and Open Logs actions.
- Consistent actionable empty states, semantic button roles, focus states,
  tooltips, and accessible names for icon-only controls.
- New local interface screenshots and expanded README documentation.

### Dashboard and Wireless ADB

- Connection status, active device, mode, and recommended next action became
  the primary visual block of the Dashboard.
- Technical information moved into a collapsible section whose state is
  preserved.
- Quick actions were reduced to Refresh, Reboot, and More actions.
- Reboot variants moved into a menu: System, Recovery, Bootloader, and
  Sideload.
- Wireless ADB was split into three independent scenarios:
  Modern Wireless Debugging, Legacy TCP/IP, and Android TV.
- QR pairing, pairing-code flow, mDNS discovery, Android TV discovery, and
  Legacy TCP/IP were added without showing irrelevant fields simultaneously.
- Pairing codes and QR passwords are never saved in settings.

### P2P and ACBridge

- Added the `P2P via ACBridge` transport for PC → Android uploads in File
  Manager.
- Platform Tools is used as the control plane for installing/updating ACBridge,
  passing the one-time request, and starting the foreground service.
- File data travels directly over the local network without MTP or root.
- Added a one-time authenticated protocol with HMAC-SHA256, per-file SHA-256
  verification, and a limited session lifetime.
- The session key is never placed in ADB command arguments.
- ACBridge does not open its TCP listener or accept file bytes before Android
  storage access has been granted.
- Added Android Storage Access Framework support for MicroSD and USB storage.
- Added an Android All Files Access fallback for Android TV firmware without a
  working folder picker.
- Added phone internal-storage writes by normalizing `/sdcard/` and
  `/storage/self/primary/` to `/storage/emulated/0/`.
- Partially received files are written to temporary `.part` or SAF documents
  and removed after cancellation or failure.
- After successful verification, ACBridge commits the temporary file. Providers
  without rename support use a non-atomic copy fallback when replacing an
  existing destination.
- ACBridge was updated to `versionName 2.0.0`, `versionCode 20004`.
- The primary bundled APK was renamed to `ACBridge-2.0.0.apk`; the compatible
  `ACBridge.apk` contains the same build.
- The ACBridge status channel was moved into private app storage and is read
  through `run-as`, because Android 17 prevents ADB shell from reading
  app-owned status files in scoped external storage.
- P2P transfers were verified with real files on Android TV removable storage
  and Pixel 8 Pro internal storage.
- For the 89,659,374-byte verification file, local and remote SHA-256 hashes
  matched:
  `828600483ed36058c4368f49c2d7288c10cf4fc2536d3b6a7d262e3b1895b481`.

### Fixed

- Eliminated a monitor/worker race during rapid application shutdown.
- Prevented repeated workers and repeated expensive refreshes during local
  filtering, sorting, and page navigation.
- Fixed window restoration after monitor disconnection and geometry outside
  the available screens.
- Fixed local UI state leaking between device profiles.
- Fixed clipping of long paths and serial numbers and reduced unnecessary
  fixed sizes in narrow windows.
- Fixed Applications selection persistence during filtering, searching, and
  sorting.
- Fixed duplicate-transfer, duplicate-command, and duplicate-refresh
  scenarios.
- Fixed safe worker/subprocess cancellation during application shutdown.
- Fixed the `SAF_PERMISSION_REQUIRED` P2P failure: ACBridge now opens the
  Android access request and waits for user confirmation.
- Fixed the P2P timeout after storage access was granted on Android 17.
- Fixed P2P rejection of the internal `/sdcard/` path.
- Fixed duplicate devices after QR pairing: mDNS serials such as
  `adb-…_adb-tls-connect._tcp` are recognized as an already connected Wireless
  Debugging transport, so an additional `adb connect IP:port` is not executed.
- Fixed Disconnect after QR pairing: the active mDNS target is disconnected
  without appending the stale port from the form.
- Repeated Connect operations with an mDNS serial no longer append an IP port.

### Material Design 3

- Replaced all stock Windows/Fusion UI icons with Material Symbols Rounded.
- Applied Material icons to navigation, collapsible cards, File Manager,
  file/folder rows, empty states, the fallback application icon, and standard
  Info/Warning/Critical/Question dialogs.
- Added a vector QtSvg renderer for Light, Dark, System, disabled, active, and
  selected states.
- Added high-DPI rendering: 24 dp correctly produces 48 physical pixels at DPR
  2.0.
- The OpenADB logo and real Android application icons remain unchanged.
- Added a notice for the official Google Material Symbols set under
  Apache-2.0.

### Windows EXE

- Added the reproducible `OpenADB.spec` PyInstaller onefile configuration.
- The executable is named `OpenADB-2.0.0.exe`.
- PE metadata contains File Version and Product Version 2.0.0.
- PySide6/QtSvg, Pillow, apkutils2, qrcode, and zeroconf are included in the
  executable.
- The UAD database, logo, Material notice, and ACBridge 2.0.0 APK are included.
- A minimal official Android Platform Tools runtime is bundled: `adb.exe`,
  `fastboot.exe`, `AdbWinApi.dll`, `AdbWinUsbApi.dll`,
  `libwinpthread-1.dll`, and `NOTICE.txt`.
- An explicitly selected Platform Tools path remains preferred; when no saved
  installation is available, the frozen build automatically selects the
  bundled runtime.
- The resulting file is located at `exe_release/OpenADB-2.0.0.exe`.
- Build size: 89,269,476 bytes.
- Build SHA-256:
  `da256e450a329cde3429f5d5aa1015267f7d14065bca524a44d8e24d803caa7e`.
- The EXE is not Authenticode-signed because no signing certificate was
  provided.

### Reliability and Security

- Dangerous bootloader unlock/lock, flash, erase, format, sideload, and user
  data removal operations are never executed automatically.
- The exact command and its consequences are shown before critical actions.
- The P2P listener accepts only one authenticated session and terminates after
  transfer, timeout, or cancellation.
- The P2P data channel is authenticated but not encrypted and should be used
  only on a trusted local network.
- Settings are written atomically and separated between Phone and TV profiles.
- An installed ACBridge package with a different signature is never removed
  automatically.

### Validation

- The automated test suite grew from an absent/minimal state to 117 passing
  unittest tests.
- Light, Dark, and System themes were validated.
- Window sizes from 720–760 px through maximized were validated.
- DPI values of 100%, 125%, 150%, and 200% were validated, together with a
  dedicated DPR 2.0 SVG icon test.
- Applications lists containing 600 and 1,200 mock rows were validated.
- No device, Unauthorized, Offline, ADB, Recovery, and Fastboot states were
  validated.
- Startup without Platform Tools, startup with discovered/saved Platform Tools,
  and the frozen build with its bundled runtime were validated.
- A PyInstaller smoke test with a clean temporary profile opened the
  `OpenADB 2.0.0` window, selected `_MEI…/platform-tools`, detected bundled ADB
  and fastboot, and exited without creating a crash log.
- `compileall`, `git diff --check`, local source launches, offscreen rendering,
  and manual screenshot review were completed.

### Known Limitations

- Physical Windows 10 validation was not available; compatibility is preserved
  through Qt/Python APIs and the absence of Windows 11-only requirements.
- Live System theme changes while the application is already running were not
  physically validated.
- Android → PC P2P is not implemented yet and continues to use Platform Tools.
- P2P can be blocked by local-network client isolation or firewall rules.
- Mutable device serial handling and a complete generation token for every
  long-running asynchronous operation remain architectural limitations.
- The EXE has no Authenticode signature.

### Redesign Part History

| Part | Result | Commit |
|---:|---|---|
| 0 | Local GUI audit and work plan | `514f73e` |
| 1 | Dashboard and Wireless connection flow | `8faf4a6` |
| 2 | Combinable Applications filters | `2f7212e` |
| 3 | Simplified Applications bulk actions | `14fe2c3` |
| 4 | Adaptive main window and navigation | `c2a8dc3` |
| 5 | Compact Device Status Bar | `4bbba85` |
| 6 | File Manager improvements | `05141c1` |
| 7 | Settings redesign | `c677ce9` |
| 8 | Commands redesign | `ed3d302` |
| 9 | Unified design system | `03dc87b` |
| 10 | README and updated screenshots | `d2e359d` |
| 11 | Final regressions and report | `aa46fcc` |
| — | ACBridge P2P file transfers | `e685fdd` |
| — | Material icons and QR Wireless fixes | `cc45fdd` |

## [1.1.0] — Audit Baseline

Version 1.1.0 was used as the local starting point for Part 0. This changelog
documents the changes completed after that baseline during the current
development cycle.
