# OpenADB 3.0.2 device-lab matrix

Last updated: 2026-07-15

This matrix separates automated evidence from physical-device evidence. A mock,
offscreen Qt test, or source inspection is never reported as a successful
hardware run.

## Current baseline

- The local host identifies itself as Windows 11 Pro, version `10.0.26200`.
- Thirty-eight of 39 isolated modules passed their clean-process gate on
  CPython 3.14.3 with `QT_QPA_PLATFORM=offscreen` (531 tests). All 41
  assertions in `test_main_window_adaptive` also passed, but that local
  PySide6 process exited afterward with Windows status `0xc0000374`.
- Windows CI run `29409867004` passed the complete 3.0.2 clean-process matrix
  on CPython 3.10, 3.11, 3.12, 3.13, and 3.14, including the adaptive-window
  module. The native teardown was therefore not reproduced on hosted Windows.
- Hosted Windows CI runs `29259146171` and `29257684156` passed the earlier
  baseline on CPython 3.10–3.14. They remain historical automated evidence and
  are not a substitute for CI on the 3.0.2 commit, Android hardware, or a
  physical Windows 10 host.
- Initial and final `adb devices -l` probes returned no connected physical
  target. A disposable API 36 emulator was used only for the virtual smoke
  described below, then stopped without saving its read-only AVD overlay.
- The local default `device_lab_smoke.py` invocation produced validated JSON
  and JUnit reports with `mode=read_only`, `status=not_run`, zero failures and
  zero transports. Tool/version probes passed and no mutation command ran; the
  disposable reports were kept outside the repository and removed after
  validation.
- The Stage 7 local one-file build and clean-profile launch smoke passed on the
  Windows 11 host at 100% DPI with one monitor and the Apps/System Dark
  appearance. It verified the title, startup, normal close, and absence of a
  crash log; it did not navigate every page. This is not a substitute for the
  complete DPI, multi-monitor, signed-build, Windows 10, or Android rows below.
- The local unsigned one-file 3.0.2 intermediate is 90,459,651 bytes with
  SHA-256
  `A95290646287FF32479B8F6EDE6F1A05063698FFC8536E6CD3C06F4496A07B51`.
  Its clean-profile Windows 11 smoke passed and Authenticode correctly reports
  `NotSigned`; this adds no signed-build or Android hardware evidence.
- A read-only API 36 emulator accepted ACBridge 3.0.2 (`versionCode 30201`) and
  completed a two-session nested-folder upload with three files, six entries,
  1,048,624 verified bytes, an empty directory, and matching SHA-256 values.
  Because emulator NAT required a test-only ADB forward for the data sockets,
  this is a virtual ACBridge/protocol proxy, not direct-LAN evidence.
- No physical Android device, OEM Android 17 build, Windows 10 host, signing
  certificate, removable Android storage, rooted disposable device,
  multi-monitor lab, or controlled network fault lab was available. Those
  results remain explicitly unclaimed.

Status meanings:

- **Passed — automated proxy**: deterministic local mock/offscreen coverage
  passed, but the physical scenario may still be pending.
- **Partial — local smoke only**: a real Windows process/build was exercised,
  but the complete physical matrix was not.
- **Not run — hardware unavailable**: no physical result exists.
- **Not run — certificate unavailable**: no signed binary was available.

## Evidence contract

Every physical run must record these fields in a sanitized JSON or JUnit report:

- scenario ID, UTC timestamp, OpenADB version, source commit, and outcome;
- Windows edition/build and DPI percentage, but no Windows username or home
  path;
- transport/mode and anonymized target ID, but no serial, IP address, pairing
  code, SSID, hostname, account name, or device nickname;
- expected and observed result, elapsed time, cancellation point when relevant,
  and a short sanitized note;
- build filename, SHA-256, and verified Authenticode status for build rows;
- disposable package marker for any approved app mutation, without local APK
  filenames or paths;
- cleanup result for temporary files, test folders, disposable packages, and
  P2P sessions.

Screenshots are optional and must be reviewed manually for serials, IPs, paths,
notifications, filenames, pairing codes, and log content before attachment.
Raw ADB/fastboot output and console logs are not release artifacts.

## Safety boundary

- The default device-lab smoke is read-only.
- Never run `flash`, `erase`, `format`, `sideload`, bootloader `unlock`/`lock`,
  destructive recovery actions, data wipes, or arbitrary custom commands.
- Never disable or uninstall a real system package. App mutation is allowed
  only for an explicitly installed disposable test APK and requires the tool's
  separate mutation flag, explicit target/package/path, disposable marker, and
  exact typed confirmation.
- File writes must use a dedicated disposable lab folder. Do not overwrite
  user files. Remove only files created by the same lab run.
- Root scenarios require a disposable lab device. Root must never be enabled or
  granted merely to satisfy this matrix.
- Fastboot coverage is detection or a documented read-only query only.

## Windows matrix

| ID | Scenario | Safe procedure and expected result | Automated evidence | Physical status | Required evidence |
|---|---|---|---|---|---|
| WIN-01 | Windows 10 physical | Launch the release candidate from a clean profile; navigate every page; close normally with no crash log. | Windows CI is defined, but no Windows 10 run exists yet. | **Not run — hardware unavailable** | OS build, EXE SHA-256, launch/close outcome, crash-log absence. |
| WIN-02 | Windows 11 physical | Launch, navigate, resize, and close the one-file build normally. | Local Windows 11 build/clean-profile launch smoke passed; offscreen adaptive tests passed. | **Partial — local smoke only** | OS build, EXE SHA-256, pages visited, close outcome. |
| WIN-03 | 100% DPI | Verify normal/minimized/maximized layouts, focus rings, menus, dialogs, and no clipping. | Local frozen-EXE title/start/close smoke at 100% plus `test_design_system`, `test_dashboard_page`, `test_main_window_adaptive`. | **Partial — local smoke only** | DPI, resolution, page/dialog checklist, sanitized screenshots if used. |
| WIN-04 | 125% DPI | Same checks as WIN-03 after sign-out/restart if Windows requires it. | Same layout proxies as WIN-03. | **Not run — hardware unavailable** | DPI, resolution, clipping/focus result. |
| WIN-05 | 150% DPI | Same checks as WIN-03, including long labels and paths. | Same layout proxies as WIN-03. | **Not run — hardware unavailable** | DPI, resolution, clipping/elision/tooltips result. |
| WIN-06 | 200% DPI | Same checks as WIN-03 at maximum supported scaling. | Same layout proxies as WIN-03. | **Not run — hardware unavailable** | DPI, resolution, reachable controls and dialog result. |
| WIN-07 | Single monitor | Save/restore window geometry and maximize state on one display. | Local frozen-EXE title/start/close smoke used one monitor; geometry persistence is covered in `test_main_window_adaptive`. | **Partial — local smoke only** | Monitor count, geometry before/after restart. |
| WIN-08 | Multiple monitors | Move between displays with different bounds/scales and restart. | Synthetic multi-screen bounds are covered in `test_main_window_adaptive`. | **Not run — hardware unavailable** | Sanitized display topology/scales and restored display. |
| WIN-09 | Monitor disconnect | Close on secondary display, disconnect it, relaunch, and verify recovery onto the remaining display. | Disconnected-screen geometry recovery has an automated proxy. | **Not run — hardware unavailable** | Before/after topology and recovered window bounds. |
| WIN-10 | Light theme | Select Light and inspect all pages, dialogs, disabled/hover/selected/focus states. | `test_design_system`, `test_main_window_adaptive`. | **Passed — automated proxy; physical run pending** | Theme, page/state checklist, contrast issues. |
| WIN-11 | Dark theme | Select Dark and perform the same state inspection. | Local frozen-EXE smoke observed Apps/System Dark without a full page sweep; `test_design_system`, `test_main_window_adaptive` passed. | **Partial — local smoke only** | Theme, page/state checklist, contrast issues. |
| WIN-12 | System theme live change | Keep OpenADB on System; change Windows Light to Dark and back without restarting; verify one refresh per change. | `test_system_theme` passed, including timer lifecycle and live mock changes. | **Passed — automated proxy; physical run pending** | Windows theme transitions, observed app transitions, icon/style result. |
| WIN-13 | Unsigned build behavior | Verify `NotSigned`, use the explicit `-unsigned.exe` name, launch cleanly, and never describe it as signed/stable. | Local unsigned one-file/clean-profile smoke passed; build workflow fails closed on naming/status mismatch. | **Partial — local smoke only** | Filename, SHA-256, `Get-AuthenticodeSignature` status, launch outcome. |
| WIN-14 | Signed build behavior | Verify Authenticode chain and timestamp before allowing the stable filename. | Signing workflow is defined but no certificate was available locally. | **Not run — certificate unavailable** | Stable filename, SHA-256, signer subject/thumbprint, timestamp and verification status. |
| WIN-15 | Clean profile | Redirect profile roots to a new temporary directory; launch, verify defaults/bundled tools, close, and check no crash log. | Local one-file clean-profile smoke and settings tests passed. | **Partial — local smoke only** | Temporary profile marker, defaults, tools selection, clean close. |
| WIN-16 | Migrated profile | Copy a sanitized legacy-layout fixture, launch once, and verify settings/backups remain separated and preserved. | Migration proxies in `test_settings_page` and settings-manager tests passed. | **Passed — automated proxy; physical run pending** | Fixture version, migrated keys/folders, preservation result. |
| WIN-17 | Corrupted settings recovery | Corrupt only a disposable settings file; verify backup/default recovery, one warning with path, preserved data, and recovery log. | `test_settings_recovery` and `test_main_window_adaptive` passed. | **Passed — automated proxy; packaged run pending** | Recovery source, preserved folders, warning count, sanitized recovery-log path. |

## Android transport matrix

The current `adb devices -l` and `fastboot devices` baselines were empty. All
physical transport rows therefore remain not run.

| ID | Scenario | Safe procedure and expected result | Automated evidence | Physical status | Required evidence |
|---|---|---|---|---|---|
| AND-01 | USB ADB authorized | Connect a lab device; use only `devices`, `get-state`, and read-only properties; UI shows ADB and the correct active target. | `test_device_context`, `test_device_status_bar`, `test_dashboard_page`. | **Not run — hardware unavailable** | Transport/mode, anonymized target ID, detection latency. |
| AND-02 | Unauthorized | Revoke authorization on the lab device; verify textual Unauthorized state and authorization guidance; do not bypass Android consent. | Dashboard/status mock coverage passed. | **Not run — hardware unavailable** | Observed state and guidance outcome. |
| AND-03 | Offline | Present an offline transport; verify textual Offline state and safe reconnect action without destructive fallback. | Dashboard/status and wireless offline tests passed. | **Not run — hardware unavailable** | Observed state, reconnect result, no wrong-target command. |
| AND-04 | Disconnect and reconnect | Physically disconnect/reconnect the same lab target; stale results must be ignored and context generation must advance as designed. | Context/lifecycle tests passed. | **Not run — hardware unavailable** | Event sequence, anonymized context IDs, final active state. |
| AND-05 | Two simultaneous devices | Attach two lab targets; verify no automatic dangerous choice and require explicit selection. | Multi-device/context mock tests passed. | **Not run — hardware unavailable** | Anonymous device count, selection prompt, chosen context. |
| AND-06 | Explicit device switch | Select the other lab target and verify page/profile reset and correct status. | Device status/MainWindow/context tests passed. | **Not run — hardware unavailable** | Anonymous before/after context and page states. |
| AND-07 | Device switch during operation | Start a read-only metadata/listing operation, switch targets, and verify cancellation/stale-result rejection. | Immutable-context and operation-coordinator tests passed. | **Not run — hardware unavailable** | Operation, switch point, cancellation/stale-result outcome. |
| AND-08 | Recovery | Boot a disposable lab device into Recovery outside OpenADB; verify detection and only supported read-only UI. | Mode/context mocks passed. | **Not run — hardware unavailable** | Detected mode and available/disabled action list. |
| AND-09 | Fastboot read-only detection | Put a disposable lab device in Fastboot outside OpenADB; run detection/read-only query only. | Fastboot context/dashboard/commands mocks passed. | **Not run — hardware unavailable** | Detection/query result; confirmation that no flash/erase/unlock command ran. |
| AND-10 | Modern Wireless QR | Pair a lab device on a trusted private network; verify one Wireless Debugging transport and clean disconnect. | `test_adb_wireless` QR deduplication/cancellation tests passed. | **Not run — hardware unavailable** | Pair/connect/disconnect outcome without SSID, IP, code, or serial. |
| AND-11 | Modern pairing code | Pair using Android's code dialog; redact code and endpoint from all evidence. | Pairing flow mock coverage passed. | **Not run — hardware unavailable** | Sanitized phase outcomes and elapsed time. |
| AND-12 | mDNS | Discover/connect on a controlled private LAN; verify Platform Tools and zeroconf fallback behavior. | mDNS candidate/normalization tests passed. | **Not run — hardware unavailable** | Discovery source, sanitized candidate count, connection outcome. |
| AND-13 | Legacy TCP/IP | On a disposable lab device already authorized over USB, exercise the documented legacy flow; never expose IP in evidence. | Legacy controls/validation have GUI mocks. | **Not run — hardware unavailable** | Sanitized connect/disconnect outcome and warning state. |
| AND-14 | Android TV discovery | Discover a lab TV on a trusted LAN and select it explicitly. | Android TV UI/discovery mocks passed. | **Not run — hardware unavailable** | Sanitized candidate count, selection and connection outcome. |
| AND-15 | Timeout | Block or withhold a read-only connection response; UI must time out and remain responsive. | Runner/wireless timeout tests passed. | **Not run — hardware unavailable** | Operation, configured/observed timeout, final state. |
| AND-16 | Cancel | Cancel pairing/discovery/read-only refresh; no later step or stale callback may run. | Wireless, worker, and context cancellation tests passed. | **Not run — hardware unavailable** | Cancellation point, latency, post-cancel command count. |

## Applications matrix

No real application mutation was performed. Any future uninstall row must use a
purpose-installed disposable APK; a real system package is never a valid target.

| ID | Scenario | Safe procedure and expected result | Automated evidence | Physical status | Required evidence |
|---|---|---|---|---|---|
| APP-01 | User app | List and inspect a disposable user app without changing it. | Loader/controller/page tests passed. | **Not run — hardware unavailable** | Anonymous package class, metadata fields present. |
| APP-02 | System app | List/inspect only; system mutation controls must warn or remain unavailable. | Type/filter/action-safety tests passed. | **Not run — hardware unavailable** | System classification and protected action state. |
| APP-03 | Split APK | Use a disposable split package; verify all APK paths are represented in backup planning. | Split backup/cancellation tests in `test_apps_device_context`. | **Not run — hardware unavailable** | Split count, backup completeness, no APK filenames. |
| APP-04 | Backup | Back up a disposable package; verify atomic metadata/APK set and cleanup. | Backup manager/coordinator tests passed. | **Not run — hardware unavailable** | Anonymous package ID, file count/bytes, atomic completion. |
| APP-05 | Restore | Restore only the disposable lab backup after the mutation gate and typed confirmation. | Restore and cancellation proxies passed. | **Not run — hardware unavailable** | Gate confirmation, restore result, package version class. |
| APP-06 | Enable and disable | Change state only for the disposable user package; restore its original state in cleanup. | Bulk/action/context tests passed. | **Not run — hardware unavailable** | Original/final state and cleanup success. |
| APP-07 | Install-existing | Use only the disposable package known to the lab device and the mutation gate. | Bound `install-existing` context tests passed. | **Not run — hardware unavailable** | Disposable marker, confirmation and result. |
| APP-08 | Profile switch during metadata load | Begin metadata load, switch targets, and verify cancellation and no cross-profile cache writes. | Apps device-context/controller tests passed. | **Not run — hardware unavailable** | Switch point, stale result count, cache isolation result. |
| APP-09 | Profile switch during icon load | Begin icon load, switch targets, and verify no stale icon/cache update. | Apps lifecycle/context/icon cache proxies passed. | **Not run — hardware unavailable** | Switch point and cache isolation result. |
| APP-10 | Profile switch during backup | Start a disposable-package backup, switch targets, and verify cancellation/partial cleanup. | Backup cancellation/context tests passed. | **Not run — hardware unavailable** | Switch point, target binding and cleanup result. |
| APP-11 | Profile switch between backup and uninstall | Force a switch after backup and before uninstall; uninstall must not run on either stale/new target. | Coordinator/context regression tests passed. | **Not run — hardware unavailable** | Step sequence and zero unintended uninstall calls. |
| APP-12 | Hidden selection | Select an app, filter it out, and verify selection/count/contextual bar remain coherent. | `test_app_actions` and filter tests passed. | **Not run — hardware unavailable** | Visible/hidden/selected counts before and after. |
| APP-13 | Dangerous package warning | Select a protected/dangerous package and cancel every confirmation; no mutation may execute. | Dangerous confirmation/action tests passed. | **Not run — hardware unavailable** | Warning text class and zero mutation commands. |

## File Manager matrix

All device-side writes require a dedicated disposable folder and explicit lab
approval. P2P is allowed only on a controlled trusted private network.

| ID | Scenario | Safe procedure and expected result | Automated evidence | Physical status | Required evidence |
|---|---|---|---|---|---|
| FILE-01 | ADB push and pull | Round-trip generated disposable content; compare hashes; remove only the lab copy. | `test_adb_transfer_strategy`, transfer controller/context tests. | **Not run — hardware unavailable** | Direction, bytes, hashes, cleanup. |
| FILE-02 | Folder transfer | Transfer a generated nested fixture with empty directories and verify structure. | Folder/tar strategy tests passed. | **Not run — hardware unavailable** | Entry counts, bytes, structure/hash result. |
| FILE-03 | Cancellation | Cancel mid-transfer; existing targets remain intact and staging is removed. | Extensive ADB/P2P cancellation tests passed. | **Not run — hardware unavailable** | Cancel point/latency, final target and staging state. |
| FILE-04 | Long Windows path | Use a generated path within current Windows policy; UI elides visually and retains full tooltip/value. | File Manager/layout mocks passed. | **Not run — hardware unavailable** | Path length only, result; never record the path. |
| FILE-05 | Large file | Transfer generated nonprivate data within lab capacity and verify hash/progress without logging filename. | Large-stream/progress proxies passed. | **Not run — hardware unavailable** | Size bucket, duration, hash result, cleanup. |
| FILE-06 | Many files | Transfer a generated many-file tree and verify counts, responsiveness, and cleanup. | Planner/listing/folder tests passed. | **Not run — hardware unavailable** | Entry count bucket, duration, failed count. |
| FILE-07 | P2P Auto | On trusted private LAN, verify deterministic Auto stream selection and authenticated transfer. | `test_p2p_parallelism`, ACBridge/P2P strategy tests passed. | **Not run — hardware unavailable** | Planned/actual streams, file count/bytes, hash result. |
| FILE-08 | P2P manual streams | Select 1–8 streams with enough generated files; actual streams must not exceed file count. | Manual clamping/planning tests passed. | **Not run — hardware unavailable** | Requested/actual stream count and result. |
| FILE-09 | Untrusted-network warning | Select P2P, read the authenticated-not-encrypted warning, cancel, and verify ADB remains selected. | File Manager warning persistence/cancel tests passed. | **Not run — hardware unavailable** | Warning shown, choice, resulting transport. |
| FILE-10 | Internal storage | Transfer only to a dedicated public lab folder and clean it afterward. | Internal-path normalization/P2P mocks passed. | **Not run — hardware unavailable** | Storage class, hash/result and cleanup. |
| FILE-11 | MicroSD | Use a disposable folder on lab media; require Android permission before opening P2P/data transfer. | SAF permission-state protocol tests passed. | **Not run — hardware unavailable** | Storage class, permission sequence, hash and cleanup. |
| FILE-12 | USB storage | Same boundary as FILE-11 on disposable USB media. | SAF/removable-storage proxies passed. | **Not run — hardware unavailable** | Storage class, permission sequence, hash and cleanup. |
| FILE-13 | SAF | Deny first, verify no bytes sent; grant the exact lab tree, retry, and verify persisted scoped access. | ACBridge SAF request/status source and protocol tests passed. | **Not run — hardware unavailable** | Deny/grant sequence and pre-grant bytes=`0`. |
| FILE-14 | Root unavailable | Keep root mode unavailable; verify safe normal-ADB fallback or clear disabled state. | Root-denial fallback tests passed. | **Not run — hardware unavailable** | Root availability=false and fallback/result. |
| FILE-15 | Root granted | Use only a rooted disposable lab device and lab path; verify target binding and cleanup. | Root-context mocks passed. | **Not run — hardware unavailable** | Disposable-device attestation, operation and cleanup. |
| FILE-16 | Device switch during transfer | Switch while a disposable transfer is active; captured target stays bound and stale UI result is rejected/cancelled. | Transfer context/controller/coordinator tests passed. | **Not run — hardware unavailable** | Switch point, anonymous contexts, final command target class. |
| FILE-17 | Checksum validation | Round-trip generated content and require SHA-256 equality before success. | ADB atomic replacement and P2P SHA-256 tests passed. | **Not run — hardware unavailable** | Source/destination digests without filename/path. |
| FILE-18 | Temporary file cleanup | Cancel/fail transfers and verify `.openadb-*`/ACBridge staging from that run is absent. | Partial/staging cleanup tests passed. | **Not run — hardware unavailable** | Staging count before/after and cleanup result. |
| FILE-19 | Partial failure | Inject failure after at least one generated entry; completed/failed counts are exact and existing files remain safe. | Partial folder-transfer/error-mapping tests passed. | **Not run — hardware unavailable** | Injection point, counts, rollback/cleanup result. |
| FILE-20 | Firewall block | In an isolated lab network, block the negotiated P2P port; timeout/error must be actionable and no partial target remains. | Socket timeout/error mapping proxies passed. | **Not run — hardware unavailable** | Firewall rule ID (not endpoint), timeout and cleanup. |
| FILE-21 | Client isolation | Use a dedicated isolated SSID/VLAN; verify P2P fails clearly while ADB control remains safe. | Client-isolation guidance/error mapping is covered by mocks/text tests. | **Not run — hardware unavailable** | Network class only, observed guidance and cleanup. |

## Commands matrix

The lab must not execute free-form dangerous text. Confirmation tests end by
cancelling the dialog; they do not proceed to the command.

| ID | Scenario | Safe procedure and expected result | Automated evidence | Physical status | Required evidence |
|---|---|---|---|---|---|
| CMD-01 | Safe ADB | Run a predefined read-only command such as version/device state/property query against a lab target. | Commands allowlist/bound-context tests passed. | **Not run — hardware unavailable** | Command ID (not raw target), exit code, output class. |
| CMD-02 | Safe fastboot query | On a disposable Fastboot target, run only predefined detection/read-only query. | Fastboot command/category/context tests passed. | **Not run — hardware unavailable** | Query ID/result and explicit no-mutation attestation. |
| CMD-03 | stdout | Run a safe fixture command with stdout and verify display/copy behavior. | `test_commands_page` output tests passed. | **Not run — hardware unavailable** | Exit code, stdout-present boolean, redaction result. |
| CMD-04 | stderr | Run a safe failing/read-only fixture and verify stderr remains distinct and sanitized. | Commands/error tests passed. | **Not run — hardware unavailable** | Exit code, stderr-present boolean, redaction result. |
| CMD-05 | Timeout | Use a safe read-only fixture with a bounded timeout; UI remains responsive and process ends. | Worker/command timeout proxies passed. | **Not run — hardware unavailable** | Timeout configured/observed and process cleanup. |
| CMD-06 | Cancel | Cancel a safe long-running read-only fixture; process and callbacks stop. | Command/worker cancellation tests passed. | **Not run — hardware unavailable** | Cancellation latency and process cleanup. |
| CMD-07 | Device switch | Switch during a safe command; captured context remains fixed and stale result is identified. | Bound device-context command tests passed. | **Not run — hardware unavailable** | Anonymous context before/after and result routing. |
| CMD-08 | Dangerous confirmation cancellation | Open a predefined dangerous action warning, press Cancel, and verify zero process starts. | Dangerous confirmation regression tests passed. | **Not run — hardware unavailable** | Action ID, cancel outcome, process-start count=`0`. |
| CMD-09 | Typed confirmation cancellation | Enter incorrect/partial confirmation or cancel; execution remains unavailable. | Typed-confirmation UI tests passed. | **Not run — hardware unavailable** | Confirmation-state booleans and process-start count=`0`. |
| CMD-10 | Custom command validation | Try empty, malformed, multi-command, and forbidden-token input; validation rejects it without execution. | Custom command validation/safety tests passed. | **Not run — hardware unavailable** | Input class only, validation reason, process-start count=`0`. |

## Manual self-hosted workflow

`.github/workflows/device-lab.yml` is manual-only and uses the protected GitHub
environment named `device-lab`. Repository administrators must configure that
environment with required reviewers before enabling a runner carrying all three
labels `self-hosted`, `windows`, and `device-lab`.

The repository environment is currently configured with a required reviewer
and self-review prevention disabled so the sole maintainer can explicitly
approve a run. No matching self-hosted runner is registered, therefore the
workflow has not executed and supplies no hardware evidence yet.

The workflow exposes no inputs and invokes only:

```powershell
python tools/device_lab_smoke.py `
  --json-report device-lab-output/device-lab-report.json `
  --junit-report device-lab-output/device-lab-report.xml
```

It never passes a serial, path, package, mutation flag, or arbitrary command.
The job validates the reports for private paths/IP addresses and uploads only
the validated JSON/JUnit files. Console output is not uploaded. The default
read-only report may legitimately say `Not run — hardware unavailable`.

Changing-device flags documented by the smoke tool are intentionally absent
from workflow inputs and steps. A mutation lab, if ever authorized, must be a
separate reviewed procedure against an explicit disposable target; editing a
workflow run input cannot enable it.
