# OpenADB device-lab checklist

Use this reusable matrix for every release candidate. Record results in the
sanitized JSON/JUnit evidence produced for that candidate; do not turn this
document into a release history. Automated tests, source inspection, offscreen
Qt runs, emulators, and physical devices are different evidence classes and
must be reported separately.

## Evidence contract

Every recorded run must include:

- scenario ID, UTC timestamp, OpenADB version, source commit, evidence class,
  and outcome;
- Windows edition/build and DPI, without a username or home path;
- transport, Android/API class, and anonymized target ID, without serial, IP,
  hostname, SSID, pairing code, account name, or device nickname;
- expected and observed result, elapsed time, cancellation point when relevant,
  and a short sanitized note;
- release filename, SHA-256, and verified Authenticode state for EXE rows;
- a disposable-package marker for approved app mutation, never its local path;
- cleanup outcome for generated files, test folders, packages, forwards,
  ACBridge sessions, and temporary profiles.

Use only these outcome labels: `Passed`, `Failed`, `Blocked`, or `Not run`.
Include the evidence class (`automated`, `emulator`, or `physical`) beside the
outcome. `Not run — hardware unavailable` is truthful but is not a pass.

Screenshots are optional and require manual review for paths, filenames,
notifications, serials, endpoints, pairing codes, and log content. Raw
ADB/fastboot output and console logs are not release artifacts.

## Safety boundary

- The default smoke is read-only.
- Never execute flash, erase, format, sideload, bootloader unlock/lock,
  destructive recovery actions, data wipes, or arbitrary custom commands.
- Never disable or uninstall a real system package. Mutations are allowed only
  for an explicitly installed disposable APK, with the mutation gate, exact
  target/package/path, disposable marker, and typed confirmation.
- Device-side writes use a dedicated disposable lab folder. Never overwrite
  user files; remove only data created by the same run.
- Root tests require a disposable device where root is already available. Do
  not root a device to satisfy the matrix.
- Fastboot coverage is detection or a documented read-only query only.
- P2P runs require a controlled trusted private network. Removable-storage
  writes require the exact user-approved SAF tree.

## Windows and packaged application

| ID | Coverage | Acceptance criteria and evidence to record |
| --- | --- | --- |
| WIN-01 | Windows 10 | Start the release EXE with a clean profile, visit every page, and close normally; record build, hash, page sweep, and absence of a crash log. |
| WIN-02 | Windows 11 | Repeat WIN-01 and exercise normal, reduced, and maximized window sizes. |
| WIN-03 | DPI 100/125/150/200% | At every available scale verify adaptive layout, long labels/paths, tooltips, focus rings, menus, dialogs, and reachable controls. |
| WIN-04 | Displays | Verify geometry restore on one monitor, mixed-scale monitors, and recovery after the previously used display is disconnected. |
| WIN-05 | Themes | Inspect Light, Dark, and System on all pages, including disabled, hover, selected, focus, warning, and error states; change the Windows theme live in System mode. |
| WIN-06 | Signed candidate | Require the stable filename, valid chain/timestamp, successful `signtool` verification, and matching SHA-256/status metadata. |
| WIN-07 | Unsigned candidate | Require `-unsigned.exe`, `NotSigned`, explicit disclosure, matching metadata, and no signed/stable claim. |
| WIN-08 | Clean profile | Redirect profile roots to a new temporary directory; verify defaults, bundled Platform Tools, ACBridge, normal shutdown, and cleanup. |
| WIN-09 | Migrated profile | Use a sanitized legacy fixture; verify settings, profiles, backups, and cache locations migrate without data loss or cross-profile mixing. |
| WIN-10 | Corrupt settings | Corrupt only a disposable settings file; verify backup/default recovery, one actionable warning, preserved user data, and a sanitized recovery log. |
| WIN-11 | Missing Platform Tools | Use an isolated profile/path environment; verify guidance and disabled device actions without a crash or install attempt. |
| WIN-12 | Long values and taskbar | Exercise long paths/device names and both transfer directions; verify elision/tooltips and native Windows taskbar progress start/update/clear behavior. |

## Devices, transports, and access modes

| ID | Coverage | Acceptance criteria and evidence to record |
| --- | --- | --- |
| DEV-01 | No device | Every page remains usable; device actions are disabled with clear guidance and no worker churn. |
| DEV-02 | USB ADB authorized | Read-only detection and properties identify exactly the selected target and ADB mode. |
| DEV-03 | Unauthorized/offline | Each state is textual, distinct, actionable, and never bypasses Android consent or targets another device. |
| DEV-04 | Disconnect/reconnect | Context generation advances, workers cancel or become stale, and no old result reaches the new target/UI. |
| DEV-05 | Multiple devices | Explicit selection is required; switching isolates profiles, caches, selections, and operations. |
| DEV-06 | Recovery | Detect Recovery and expose only actions supported safely in that mode. |
| DEV-07 | Fastboot | Detect a disposable Fastboot target and run only the approved read-only query; attest that no mutation command ran. |
| NET-01 | Modern Wireless QR | Pair once on a trusted LAN, show one Wireless Debugging transport, reconnect on a clean first attempt, cancel/reopen the dialog, and disconnect cleanly. |
| NET-02 | Pairing code and mDNS | Pair/connect through Platform Tools and zeroconf fallback; redact all codes/endpoints and verify bounded timeout/cancel. |
| NET-03 | Legacy TCP/IP | On an already authorized disposable target, enable/connect/disconnect with warnings and no endpoint in evidence. |
| NET-04 | Android TV | Discover/select the intended lab TV explicitly and verify disconnect, timeout, and cancellation without stale callbacks. |
| ACCESS-01 | Standard | On shell-adbd, root-adbd, and any nonstandard lab target, verify and report the raw ADB-shell UID, permit shell work only for UID 2000, fail closed otherwise, and make zero explicit `su` or Shizuku elevation calls. |
| ACCESS-02 | Root unavailable | Root selection reports unavailable and safely disables/falls back according to the feature contract; it never loops or blocks the UI. |
| ACCESS-03 | Root available | On a disposable direct-root and/or `su` target, verify UID 0, exactly one elevation layer, foreground permission completion, cancellation, and no double `su`. |
| ACCESS-04 | Shizuku absent/stopped | Report the state and recovery guidance without silent Shizuku installation or repeated permission prompts. |
| ACCESS-05 | Shizuku shell | Approve through Android, require UID 2000 (not Root), keep the request foreground only until completion, and reuse stable authorization without loops. |
| ACCESS-06 | Root-backed Shizuku/Sui | On a disposable rooted device require UID 0 classification while retaining every root-action risk gate. |
| ACCESS-07 | Live mode switch | Switch Standard/Root/Shizuku from every page and while disconnected; old leases/workers cancel, stale results are rejected, and the final choice is applied immediately and persisted to the correct profile. |
| ACCESS-08 | Switch/cancel during work | Cancel or change target/mode during a safe long operation; verify bounded cleanup of Shizuku UserService requests, shell processes, and UI callbacks. |
| BRIDGE-01 | Install/update policy | Across missing, older, exact, newer, and unreadable-version states: install missing, update older, no-op exact, preserve newer, and never install blindly or downgrade. Repeat over USB and wireless. |
| BRIDGE-02 | Maintenance barrier | Reconnect during an active ACBridge export/session and start page work during maintenance; verify serialization, eventual resumption, and no mid-session replacement/conflict error. |
| BRIDGE-03 | ACBridge privilege request | Changing to Root or Shizuku requests both shell and ACBridge access once, keeps the temporary foreground activity until the decision, and closes it afterward. |

## Applications and backups

Use only a purpose-installed disposable package for mutation. A real system
package is never a valid mutation target.

| ID | Coverage | Acceptance criteria and evidence to record |
| --- | --- | --- |
| APP-01 | Inventory and metadata | Load user/system packages, labels, icons, versions, split paths, size, state, and UAD category in Standard, Root, and Shizuku where supported; reject truncated results. |
| APP-02 | Filters/search/sort | Combine type/state/UAD filters, search label and package, retain sorting and hidden checkbox selections, reset filters, and verify visible/total/selected counts. |
| APP-03 | Device/mode switch | Switch during list, metadata, and icon loading; no stale table row or cross-profile cache write is accepted. |
| APP-04 | Critical-package safety | Protected packages stay highlighted and mutation requires the stronger warning; cancelling starts zero commands. |
| APP-05 | Backup | Back up the disposable package, including every split APK, metadata, icon/log as applicable, atomic completion, and partial cleanup. |
| APP-06 | Restore | Restore only the disposable lab backup after confirmation; verify single/split install choice and resulting version/state. |
| APP-07 | Enable/disable | Change only the disposable package and restore its original state; record backend/access UID and cleanup. |
| APP-08 | Uninstall/install-existing | Require backup and all confirmations; inject backup failure to prove uninstall is skipped, then use only the disposable package and restore it. |
| BACKUP-01 | Backup browser | Refresh, inspect metadata, open location, restore, and delete the disposable backup without affecting another profile. |
| BACKUP-02 | Full cleanup option | Verify ordinary settings/cache reset preserves APK backups; the optional backup purge names the irreversible scope, requires the strongest warning, and removes only approved OpenADB backup data. |

## File Manager

All writes below use generated, nonprivate data in a dedicated disposable
folder and end with cleanup.

| ID | Coverage | Acceptance criteria and evidence to record |
| --- | --- | --- |
| FILE-01 | ADB push/pull | Round-trip files in both directions, compare SHA-256, and verify progress/dialog/taskbar completion and cleanup. |
| FILE-02 | Folders and empty directories | Round-trip a generated nested Unicode fixture; verify entry counts, structure, hashes, and empty folders. |
| FILE-03 | Large and many files | Exercise at least one large generated file and a many-file tree; verify responsiveness, exact byte/file counts, and no long filename disclosure. |
| FILE-04 | Cancel/failure/retry | Cancel and inject partial failure; existing targets remain safe, staging is removed, counts are exact, and a second transfer/delete can start immediately without operation-conflict residue. |
| FILE-05 | Context freshness | Switch device, access mode, directory, or page during listing/transfer; refresh as needed without false stale-folder errors, wrong-target work, or stale callbacks. |
| FILE-06 | Standard/Root/Shizuku actions | Browse, storage-query, create, rename, properties, and delete in each supported mode; verify correct UID/backend, fast directory changes, and cleanup. |
| FILE-07 | Transfer data-plane separation | With Root/Shizuku selected, ADB push/pull bytes still use Platform Tools and P2P bytes still use ACBridge/SAF unless the documented strategy explicitly says otherwise. |
| FILE-08 | P2P trust warning | On first selection explain authenticated-but-unencrypted transport; cancellation keeps/restores ADB and acknowledgement remains profile-scoped. |
| FILE-09 | P2P streams | Verify Auto planning and manual 1–8 selection; actual streams never exceed file count and one file uses one stream. |
| FILE-10 | P2P internal storage | Transfer generated files/folders to public internal storage, verify integrity/commit, consecutive transfers, cancellation, and no staging residue. |
| FILE-11 | MicroSD/USB and SAF | Deny access first and require zero bytes sent; approve only the exact tree, retry files/folders, verify persisted scoped access, hashes, Unicode paths, replacement fallback, and cleanup. |
| FILE-12 | P2P network faults | In an isolated lab network test firewall block, timeout, disconnect, and client isolation; errors are actionable and leave no partial committed target. |
| FILE-13 | Long paths and unavailable storage | Verify visual elision/full tooltip, safe path handling, explicit empty/unavailable states, and no expensive reload loop. |

## Commands, settings, logs, and lifecycle

Free-form dangerous text is not executed in this lab. Confirmation tests end by
cancelling the dialog.

| ID | Coverage | Acceptance criteria and evidence to record |
| --- | --- | --- |
| CMD-01 | Safe ADB/fastboot | Run predefined read-only state/property/version queries only; keep stdout/stderr distinct and sanitized. |
| CMD-02 | Timeout/cancel/switch | A bounded safe command ends its process, ignores stale output, and leaves no worker after cancel, device switch, or access-mode switch. |
| CMD-03 | Dangerous confirmations | Cancel ordinary and typed confirmations and verify process-start count remains zero. |
| CMD-04 | Custom validation | Empty, malformed, chained, and forbidden-token inputs are rejected before process creation. |
| SET-01 | Settings and profiles | Change themes, paths, monitoring, filters, transfer/access preferences, and restart; values persist only at their intended global/profile scope and old settings receive safe defaults. |
| SET-02 | Access selector without device | Change Standard/Root/Shizuku while disconnected; no device command starts and only the final selection is consumed after connection. |
| LOG-01 | Logs and errors | Verify timestamps, severity, copy/export/clear behavior, actionable dialogs, and redaction of private identifiers/secrets. |
| LIFE-01 | Navigation and refresh | Visit every page repeatedly; no duplicate worker, ACBridge maintenance storm, or expensive refresh starts solely because of navigation. |
| LIFE-02 | Shutdown | Close during idle and bounded safe work; workers/processes/forwards terminate, settings/geometry persist, and no crash log is created. |

## Manual self-hosted workflow

`.github/workflows/device-lab.yml` is manual-only. It uses the protected
`device-lab` environment and a runner labelled `self-hosted`, `windows`, and
`device-lab`; configure required reviewers before registering or enabling that
runner. The self-hosted runner must be GitHub Actions Runner `v2.327.1` or
newer because every GitHub-owned action in this workflow uses the Node.js 24
runtime. Verify and update the runner before approving a device-lab run.

The workflow exposes no inputs and invokes only the read-only reporter:

```powershell
python tools/device_lab_smoke.py `
  --json-report device-lab-output/device-lab-report.json `
  --junit-report device-lab-output/device-lab-report.xml
```

It passes no serial, path, package, mutation flag, or arbitrary command. The job
privacy-validates and uploads only the sanitized JSON/JUnit pair, not console
output. Device-changing flags remain absent from workflow inputs and steps. Any
future mutation lab must be a separately reviewed procedure against an explicit
disposable target; a workflow-dispatch input must never enable it.
