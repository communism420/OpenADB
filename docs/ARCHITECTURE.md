# OpenADB architecture

This document describes the current runtime boundaries and concurrency rules of
OpenADB. It is intentionally version-independent and records only durable
runtime contracts.

## Runtime composition

`openadb/main.py` is the Python entry point used by the source launcher and the
PyInstaller build. It delegates startup to `openadb/app.py`, which creates the
Qt application and the shared runtime services:

- `SettingsManager` for global settings and per-device profiles;
- `PlatformToolsManager`, `CommandRunner`, `ADBClient`, and `FastbootClient` for
  host-side tools and process ownership;
- `DeviceManager` for discovery, active-device identity, and operation
  invalidation;
- backup, icon, and other core services injected into `MainWindow`.

`MainWindow` is the composition root for the GUI. It owns the status bar,
navigation stack, global access-mode selector, and the Dashboard, Applications,
Backups, File Manager, Commands, Logs, and Settings pages.

The repository is split into three main layers:

- `openadb/models/` contains data models shared by the UI and core services;
- `openadb/core/` contains Platform Tools facades, device and operation state,
  settings, privilege handling, ACBridge integration, and page-independent
  controllers and coordinators;
- `openadb/ui/` contains Qt views, presentation workflows, dialogs, and worker
  signal wiring.

Pages remain responsible for presentation and user confirmation. Multi-step or
stateful device work belongs in core controllers/coordinators so it can operate
against captured inputs without reading live widgets.

## Device identity and stale results

`DeviceManager` owns the active `DeviceInfo` and a monotonically increasing
generation. Device-bound work starts by capturing a frozen `DeviceContext`,
which contains the serial, mode, transport ID, profile identity and paths, and
generation.

The generation advances when the active device identity or its active profile
changes. A refresh that preserves the same serial, mode, transport ID, and
profile remains in the same generation. When the generation advances,
`DeviceManager` cancels registered operations whose contexts became stale.

`ADBClient.for_context()` and `FastbootClient.for_context()` return facades
permanently bound to the captured serial and context. A background operation
must not re-read the mutable root client serial after it starts. Global
discovery and server operations, such as `adb devices`, `fastboot devices`, or
ADB server management, deliberately run without a device selector.

Code that applies an asynchronous result must verify all relevant identities:

1. its operation token is still registered and not cancelled;
2. its `DeviceContext` is still current;
3. any narrower view identity, such as an Android directory, device profile,
   or wireless connection attempt, still matches the request.

A stale result may be recorded in a redacted technical log, but must not update
the current page, write through current profile paths, select another device,
or report success for the replacement context. Wireless pairing and connection
use a separate immutable `WirelessConnectionAttempt` because a successful
connection can legitimately create a new transport.

## Operation ownership and background work

`OperationRegistry` is the process-wide ownership and cancellation registry. An
`OperationToken` carries an owner key, optional device context, cancellation
event, one or more conflict groups, and (for access-aware work) a privilege
lease. Conflict groups serialize operations that share a device-side resource,
while unrelated groups may run concurrently.

The registry does not schedule work. Qt tasks run through `Worker` on the global
`QThreadPool`; `start_worker()` keeps Python references alive and guarantees
registry cleanup through normal and fallback finalizers. `CommandRunner`
separately owns every spawned subprocess and supports cancellation, streaming,
context-bound logging, and shutdown termination.

The main page-independent workflow boundaries are:

- `AppsController`, `AppOperationCoordinator`, and
  `BackupOperationCoordinator` for captured application/profile work;
- `FileListingController` and `FileManagerActionCoordinator` for immutable
  listing and file-action requests;
- `TransferPlan` and `FileTransferController` for captured transfer direction,
  paths, transport, privilege choice, and P2P parallelism;
- page workflows in `openadb/ui/` for Qt-specific orchestration and result
  presentation.

Device-changing sequences revalidate their context between steps. In
particular, backup-before-uninstall must not continue to uninstall after a
device, profile, or access-mode transition.

## Access modes

OpenADB has one application-wide selector with three explicit backends:

- **Standard** verifies the context-bound ADB shell identity and permits shell
  work only as UID 2000; UID 0 or any unexpected identity fails closed until
  the user explicitly selects another backend;
- **Root** uses a verified root `adbd` directly or a verified `su` route, and
  records an explicit Standard fallback when Root is unavailable;
- **Shizuku** executes the Android shell plane through ACBridge's official
  Shizuku UserService after verifying UID 0 or UID 2000.

`PrivilegeManager` captures a `PrivilegeOperationLease` before access-aware work
starts. Switching modes resets the cached status, cancels the old lease, and
prevents prepared Standard, Root, or Shizuku facades from continuing under a
new selection. Shizuku fails closed when its selected backend cannot be
verified. Root may fall back to the captured Standard shell, but the prepared
facade exposes the requested and effective backends plus a user-facing fallback
message; a fallback is never reported as Root access.

Only Android shell execution is replaced by Root or Shizuku. Raw Platform Tools
operations and byte streams such as install, push, pull, reboot, and connection
management remain on the immutable direct ADB control/data plane. Features that
do not support a selected backend must report that boundary instead of guessing
or changing modes.

## ACBridge and P2P boundaries

ACBridge is the bundled Android helper used for package metadata/assets,
fixed-purpose Android actions, Storage Access Framework (SAF) access, Root and
Shizuku permission handshakes, and P2P uploads. On an eligible device
connection, OpenADB checks the installed helper and installs a missing version
or updates an older version without downgrading a newer one. Privileged flows
additionally require the installed base APK to match the bundled helper.

The Shizuku wire protocol and its trust boundaries are documented separately in
[`openadb/resources/acbridge/SHIZUKU_PROTOCOL.md`](../openadb/resources/acbridge/SHIZUKU_PROTOCOL.md).

ACBridge P2P currently supports PC-to-Android uploads only:

- ADB installs/verifies ACBridge and provides a private, request-scoped control
  tunnel for bootstrap and status metadata;
- file bytes travel directly over the local network to ACBridge and are written
  through the user's Android SAF grant;
- each one-time session is authenticated and file integrity is verified, but
  the payload is not encrypted, so P2P is intended only for trusted networks;
- Root and Shizuku do not replace SAF or become the P2P data path;
- Android-to-PC transfers continue to use Platform Tools.

One-time P2P secrets and authenticated locators must be redacted from command
history, logs, exceptions, and Qt signal payloads.

## Settings and device profiles

`SettingsManager` stores application-wide state under `~/OpenADB/settings.json`
and device profiles under `~/OpenADB/Phones/<profile>/` or
`~/OpenADB/TVs/<profile>/`. Profile activation captures profile-owned settings,
backup, temporary, log, application-cache, metadata, and icon paths for the
active device.

Filters, Android paths, transfer preferences, and the access backend are
profile-local. An access mode selected while no profile is active is stored as
a one-shot pending choice and applied when the next device profile is activated.
Existing settings retain compatibility defaults and legacy profile locations
are migrated transactionally.

Settings writes are atomic and maintain a last-known-good backup. Recovery and
reset operations preserve user data unless the user explicitly confirms the
separate destructive backup-removal option.

## Shutdown and lifecycle

`MainWindow.closeEvent()` is the coordinated shutdown boundary. It prevents new
workers, stops theme and device monitoring, persists window and File Manager
state, clears pending wireless/privilege work, cancels the operation registry,
cancels commands and transfers, closes Windows taskbar progress, and shuts down
`CommandRunner` so owned subprocesses are terminated.

The global Qt thread pool is cleared and given a bounded wait. Workers suppress
late signals after their Qt owners are destroyed and still run fallback cleanup,
so shutdown does not depend on a success callback reaching a deleted widget.
