# OpenADB 3.0.0 device-context migration plan

Baseline date: 13 July 2026
Baseline commit: `8c3792b` (`release: bump OpenADB and ACBridge to 3.0.0`)

## Baseline verification

The baseline is the local Windows source tree after the 3.0.0 version bump.
No destructive ADB or fastboot operation was executed.

| Check | Result |
|---|---|
| `python -m unittest discover -v` | 127 tests passed in 147.361 seconds |
| `python -m compileall -q openadb tests tools` | Passed |
| `ruff check openadb tests tools` | Passed with Ruff 0.15.12 |
| `pythonw.exe -m openadb.main` with a clean temporary profile | Responsive `OpenADB 3.0.0` window; graceful exit code 0 |
| Process/crash cleanup after source startup | 0 new adb processes, 0 new fastboot processes, 0 crash logs |

The local environment is Windows 11, Python 3.14.3, PySide6 6.11.1,
Pillow 12.2.0, apkutils2 1.0.0, qrcode 8.2, and zeroconf 0.149.16.
The supported release target remains Windows 10/11 and Python 3.10+; this
single-machine baseline does not replace the CI and device-lab matrices in
stages 7 and 8.

## Current dependencies and ownership

`openadb.app` constructs one mutable `ADBClient`, one mutable `FastbootClient`,
one `DeviceManager`, one `SettingsManager`, and shared service objects, then
injects them into `MainWindow`. `DeviceManager._set_active()` changes both
client serials. `MainWindow._activate_device_profile()` changes the active
settings directory and redirects backup, log, app-cache, APK-metadata, and
icon-cache roots. Page workers run in the global `QThreadPool`; pages own local
busy flags and cancellation events, but there is no shared operation registry
or generation check.

Runtime dependencies currently come from the unpinned `requirements.txt`:
PySide6, Pillow, apkutils2, qrcode with Pillow support, and zeroconf.
`CommandRunner` owns subprocess lifetime. ACBridge and P2P are implemented in
the repository without an additional Python network dependency. Dependency
reproducibility is addressed in stage 7.

## Largest modules before migration

| Lines | Module |
|---:|---|
| 2,885 | `openadb/ui/file_manager_page.py` |
| 2,146 | `openadb/core/adb.py` |
| 1,941 | `openadb/ui/apps_page.py` |
| 951 | `openadb/ui/main_window.py` |
| 844 | `openadb/core/command_runner.py` |
| 831 | `openadb/ui/dashboard_page.py` |
| 818 | `openadb/ui/commands_page.py` |
| 734 | `openadb/core/acbridge_p2p.py` |
| 676 | `openadb/core/acbridge.py` |
| 669 | `openadb/core/settings_manager.py` |
| 557 | `openadb/ui/widgets/app_list_widget.py` |
| 529 | `openadb/core/icon_extractor.py` |

The stage 3 and 4 refactors will split responsibilities, not mechanically
split files. `ADBClient` remains a transport facade; its device-bound view will
be immutable while global discovery methods remain available on the root
client.

## Mutable-state findings

- `ADBClient._base()` and `FastbootClient._base()` read `self.serial` for each
  subprocess. A multi-command worker can therefore start on one device and
  continue on another after a selection change.
- `ADBClient.get_state()` and `get_device_info()` temporarily overwrite the
  shared serial and restore it without a cross-thread lock.
- `DeviceManager.refresh()` and `_set_active()` mutate the active device and
  both transport clients. Refresh currently has no generation identity.
- Applications list, metadata, assets, ACBridge fallback, backup/uninstall,
  enable/disable, and install-existing workers use the shared ADB client.
  Several callbacks recompute the cache/profile target from the current
  device instead of the operation's original device.
- File listing, storage discovery, root checks, ADB transfers, tar/streaming
  fallback, and P2P bootstrap use the shared ADB client. Late completion can
  refresh the newly selected device's current page.
- Backup roots, command logs, app metadata, icon caches, and temporary folders
  are redirected when `SettingsManager.activate_device_profile()` runs. A
  worker that resolves these paths late can write old-device data into the new
  profile.
- Commands partially freeze a manually entered command into argv, but built-in
  ADB/fastboot calls and root-check completion still depend on current mutable
  state and have no stale-result guard.
- `start_worker()` safely retains a QRunnable and suppresses emits after its Qt
  signal owner is destroyed, but it does not register ownership, conflicts,
  cancellation reasons, device context, or generation.

## Operations that must be device-bound

The following operations will capture one immutable context before a worker or
multi-step workflow starts and will use bound ADB/fastboot clients and the
captured profile paths throughout:

- Applications list, package metadata, labels, icons, sizes, and cache writes;
- ACBridge install/export/fallback and device-specific status updates;
- app backup, backup-then-uninstall, uninstall, enable, disable, and
  install-existing;
- backup restore and APK installation to a selected target;
- Android file listing, requested path, storage volumes, and root checks;
- ADB push/pull, tar optimization, long-path streaming, P2P/SAF transfer, and
  all transfer progress/completion;
- built-in ADB and fastboot commands, root checks, and device-specific logs;
- Dashboard reboot/reconnect and legacy TCP/IP actions after a target exists.

Local backup scan, metadata read, opening/deleting a local backup folder, and
Windows-only File Manager operations will capture stable filesystem paths but
will not require an Android context.

## Operations that remain global

The root transport clients retain explicit context-free methods for:

- `adb devices`, `fastboot devices`, and device tracking;
- `adb start-server`, `adb kill-server`, ADB/fastboot version queries;
- Platform Tools discovery and verification;
- mDNS discovery before a transport is associated with a connection attempt;
- Wireless pair/connect/disconnect discovery steps, which bind to a dedicated
  connection-attempt token rather than the current active-device generation.

Global methods must construct argv with no inherited `-s` selector. Tests will
assert that this remains true after bound clients are added.

## Device context and generation

Stage 2 will add a frozen, slotted `DeviceContext` containing at least serial,
mode, transport ID, profile key/kind, and generation. `DeviceManager` will own
a monotonically increasing generation and expose `capture_context()`,
`is_context_current()`, `require_context()`, and `current_generation`.

Generation changes when any identity component that can invalidate an
operation changes:

- a different active serial is explicitly selected;
- the active device disconnects or selection becomes required;
- the same serial moves to an incompatible ADB/fastboot transport or mode;
- a different transport ID represents a replacement connection;
- a different device profile becomes active;
- a full settings/profile reset invalidates the captured profile.

Generation does not change for an ordinary refresh of the same serial, mode,
transport ID, and profile. Metadata such as model, free space, or Android
version can refresh without invalidating work.

`ADBClient.for_context()` and `FastbootClient.for_context()` will return small
immutable bound facades that always build argv from the captured serial. They
will not implement binding by temporarily modifying the root client's serial.

## Operation registry

A small core `OperationRegistry` will issue `OperationToken` objects containing
an operation ID, owner key (plain data, not a strong Qt-object reference),
optional `DeviceContext`, cancel event, and conflict group. It will:

- reject a second conflicting live operation;
- allow independent non-conflicting operations;
- record cancellation reason;
- invalidate/cancel device-bound tokens when generation changes;
- cancel all registered work during shutdown;
- unregister on success, error, or cancellation through a guaranteed final
  path;
- let UI callbacks test both token activity and context currency before
  applying a result.

The registry will coordinate existing workers and subprocess/socket/file
cancellation; it will not become a second task scheduler.

## Stale-result policy

Every asynchronous callback that mutates UI, cache, profile data, file
listing, backup state, status labels, command output, or storage volumes will
carry its token/context. A stale callback may write a redacted technical log,
but it must not update the new device, show success for it, switch selection,
or write through current profile paths. Repeated modal dialogs are avoided;
the page may expose one non-modal status explaining that an operation finished
for an inactive device.

Listing callbacks additionally compare the originally requested Android path,
so a late result for an older folder cannot replace a newer navigation result.
Backup-then-uninstall reuses the same context and rechecks it between steps;
uninstall never starts after context invalidation.

## Controllers and coordinators to extract

Stage 3 will introduce focused Applications components for selection/filter
state, metadata/assets loading, device-bound app operations, and backup
workflows. The page remains responsible for widgets and signal wiring; loaders
publish data/events rather than touching widgets.

Stage 4 will introduce immutable transfer planning, common progress accounting,
listing/state controllers, an operation coordinator, and separate ADB/P2P
strategies. Socket protocol details remain in core code. Windows-side actions
remain usable without an Android context.

Commands will use a small execution coordinator around the existing catalog,
safety analysis, bound client, and operation token. Wireless will use a frozen
`WirelessConnectionAttempt` with attempt ID, scenario, expected host/ports,
start generation, cancellation, and ready-transport identity.

## Migration sequence

1. Add context, bound transports, generation semantics, operation registry,
   and pure unit tests without removing the compatibility `serial` properties.
2. Wire `DeviceManager` identity transitions and profile activation to one
   generation lifecycle; keep ordinary refresh stable.
3. Move page operations to capture once at start, register once, and apply
   callbacks only while current.
4. Cover Applications/Backups, then File Manager/P2P, then Commands/status and
   Wireless attempt semantics. Global discovery remains unbound.
5. Extract controllers/coordinators only after their context and lifecycle
   boundaries are explicit.
6. Remove remaining long-operation reads of mutable active serial/profile
   state, retain compatibility only at short synchronous call sites, and run
   full shutdown/stale-result tests.

Old `settings.json`, device profiles, backups, caches, and the mutable client
properties remain readable throughout migration. No schema rewrite is needed
for `DeviceContext`, because contexts are runtime snapshots.

## Risk controls

- **Wireless ADB:** a successful pair can legitimately create a new transport,
  so active-device generation alone must not cancel the attempt. Success
  requires the expected transport in state `device`; transient `offline` is
  not success.
- **Multiple devices:** no worker may infer a target after it starts. Explicit
  selection and profile key are captured together; stale callbacks cannot
  auto-select a replacement device.
- **Shutdown:** registry cancellation reaches subprocesses, sockets, file
  streams, and workers; late Qt emits remain guarded; cleanup is verified with
  an empty registry and no owned adb/fastboot processes.
- **Stale results:** context checks occur before UI writes and before persistent
  cache/profile writes, not only at final completion.
- **Compatibility:** global commands remain callable without a context; local
  filesystem operations do not acquire an artificial device dependency.

## Architecture migration completion criteria

- Bound ADB/fastboot argv remains tied to the captured serial after a device
  switch, while global commands contain no selector.
- Generation changes only on real identity/profile invalidation and cancels or
  invalidates all registered device-bound operations.
- All listed Applications, Backups, File Manager, P2P, Commands, fastboot, and
  status workflows apply results only to the captured context/profile/path.
- Backup-then-uninstall cannot cross a generation boundary.
- Conflict groups, independent operations, success/error/cancel cleanup, and
  shutdown cancellation are covered by unit tests.
- Wireless attempt tests distinguish pairing, temporary offline discovery,
  ready connection, cancellation, and timeout.
- Page business logic is moved into focused controllers/coordinators without
  circular imports or loss of existing signals, filters, selections, safety
  checks, transfer fallbacks, or Windows-only behavior.
- The full test suite, compileall, Ruff, source startup, and shutdown checks
  remain green after every migration stage.

## Implementation outcome

The migration above was implemented in stages 2–6 without changing the user
settings, backup, or cache formats. `DeviceContext` is a frozen snapshot of the
serial, mode, transport, profile identity/paths, and monotonic generation.
`ADBClient.for_context()` and `FastbootClient.for_context()` now build commands
from that snapshot instead of temporarily changing a shared serial.

`OperationRegistry` owns conflict groups, cancellation events/reasons,
generation invalidation, shutdown cancellation, and guaranteed token cleanup.
Applications, backups, File Manager listings/transfers, Commands, Dashboard
actions, device status, and Wireless ADB callbacks validate their captured
context or connection-attempt token before applying UI or persistent results.
Global discovery and server operations remain deliberately context-free.

The planned responsibility split produced these focused components:

- Applications data/action workflows, filter and selection state,
  metadata/assets loaders, `AppsController`, `AppOperationCoordinator`, and
  `BackupOperationCoordinator`;
- File Manager state/listing/action controllers, immutable `TransferPlan`,
  shared progress/error models, `FileTransferController`, and separate ADB and
  P2P strategies;
- frozen Wireless connection attempts, command safety/bound execution, and
  the live `SystemThemeController` lifecycle.

Automated regressions cover bound-target stability, generation transitions,
stale UI/cache/profile rejection, switch-during-operation scenarios, conflict
handling, cancellation, late Qt signals, and empty-registry shutdown. This is
software evidence rather than a hardware claim: real multi-device, Android
transport, Windows 10, and device-lab execution remain recorded separately in
`docs/DEVICE_LAB_MATRIX.md`.
