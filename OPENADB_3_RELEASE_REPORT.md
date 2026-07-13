# OpenADB 3.0.0 release validation report

Report date: 13 July 2026. This report distinguishes deterministic software
evidence from physical Windows, Android, network, and signing evidence. It is
not a claim that OpenADB has an objective 10/10 quality score.

## 1. Version

- OpenADB package, GUI title, README, changelogs, PyInstaller configuration,
  Windows File/Product Version, screenshots, workflows, tools, and fixtures:
  `3.0.0`.
- Release EXE identity: `OpenADB-3.0.0.exe`; an unsigned build must use
  `OpenADB-3.0.0-unsigned.exe` when distributed.
- ACBridge: package `com.communism420.acbridge`, `versionName=3.0.0`,
  `versionCode=30002` (3.0.0 helper build 2).

`openadb/version.py` is the shared release-metadata source. Version consistency
is enforced by `tests/test_version_metadata.py` and the Windows workflows.

## 2. Architecture changes

- Device-bound work now captures immutable target/profile state instead of
  consulting a mutable active serial in the middle of an operation.
- A small thread-safe operation registry owns conflicts, cancellation,
  generation invalidation, shutdown, and token cleanup.
- Applications and backup loading/actions were moved into focused workflows,
  controllers, coordinators, selection/filter models, and asset/metadata
  loaders.
- File Manager state, listings, actions, transfer planning, progress/errors,
  and ADB/P2P transports were separated behind explicit models and
  controllers.
- Wireless pairing/connect attempts have an identity separate from the active
  device generation, so the transport created by a successful attempt does
  not invalidate its own result.
- Settings persistence and System-theme listening now have bounded recovery
  and shutdown lifecycles.

The migration preserved the existing settings/profile, backup, and cache
formats. The detailed rationale and outcome are in
`OPENADB_3_ARCHITECTURE_PLAN.md`.

## 3. DeviceContext design

`DeviceContext` is a frozen, slotted dataclass containing the exact serial,
mode, transport ID, profile key/kind, profile/backups/temp/log paths, and
generation captured for an operation. `ADBClient.for_context()` and
`FastbootClient.for_context()` create bound clients whose command target does
not change when the active device changes. Global discovery/server operations
remain deliberately context-free.

## 4. Generation behavior

`DeviceManager` owns a monotonic generation. It advances when the effective
device, transport, profile identity, or reset boundary changes, and remains
stable for an ordinary refresh of the same identity. UI/persistent callbacks
check their captured generation before applying results. Stale results cannot
replace a new profile's table, cache, path, storage state, command result, or
success message.

## 5. OperationRegistry design

Each `OperationToken` has a unique ID, owner, optional `DeviceContext`, one or
more conflict groups, a cancellation event, and a first-writer cancellation
reason. `OperationRegistry` provides atomic registration, conflict rejection,
owner/generation/all cancellation, context-managed cleanup, and fail-closed
shutdown. It stores no Qt widget references. Shutdown removes registry entries
after signalling cancellation, and late callbacks are independently guarded.

## 6. Extracted controllers and coordinators

- `AppsController`, `AppsFilterController`, `AppSelectionModel`,
  `AppsDataWorkflow`, and `AppsActionWorkflow`;
- `AppMetadataLoader`, `AppAssetLoader`, and `AppOperationCoordinator`;
- `BackupOperationCoordinator` plus the reduced Backups page lifecycle;
- `FileManagerState`, `FileListingController`, `FileManagerActionCoordinator`,
  `FileTransferController`, and the separated File Manager view/listing/action
  helpers;
- immutable `TransferPlan`, shared transfer progress/error models,
  `ADBTransferStrategy`, and `P2PTransferStrategy`;
- `SystemThemeController` and frozen `WirelessConnectionAttempt`.

These are responsibility boundaries rather than mechanical file splits.

## 7. P2P security UX

ADB remains the default PC-to-Android transport for new profiles. Selecting
P2P for the first time in a profile opens a warning that the channel is
authenticated and integrity-checked but **not encrypted**. Cancelling restores
ADB; acknowledgement can be stored only for that device profile. While P2P is
selected, File Manager keeps an `Authenticated, not encrypted` label visible
and recommends only a trusted private network.

The protocol binds authenticated request/ready/control/final transcripts,
per-file SHA-256, exact entry/file/byte totals, deadlines, request-scoped
cancellation, staging cleanup, and Android permission readiness. These checks
do not turn the data channel into encryption.

## 8. Secrets redaction

Pairing codes, QR passwords, P2P session keys, authenticated request material,
and URL credentials are redacted from previews, histories, logs, callbacks,
errors, workers, and object representations. Pairing secrets are sent through
standard input rather than command arguments. The release privacy guard scans
tracked/unignored bytes as UTF-8 and UTF-16, user profile paths, private IPv4
and IPv6 values, generated databases, key-container suffixes, and screenshot
metadata/EXIF. Documented demo placeholders remain allowlisted.

The active tree and built artifacts passed this audit. A generated Androguard
cache containing a pre-existing local path was removed and ignored. Its old
blob remains in historical Git objects; removing it from published history
would require a separately coordinated history rewrite.

## 9. Auto streams behavior

P2P defaults to `Auto (recommended)`. Planning is pure and deterministic from
captured file count, total bytes, and largest-file size. It returns one stream
for a single file or invalid/empty statistics, ordinarily two, up to three for
at least 6 files/32 MiB/1 MiB average, and up to four for at least 24
files/256 MiB/4 MiB average. A file holding at least 75% of all bytes caps Auto
at two. Manual 1–8 overrides remain profile-local and cannot exceed the file
count. No network benchmark or unsupported speed promise is made.

## 10. Dashboard UX

Dashboard keeps one dominant, text-labelled connection state and recommended
next action. Refresh, reboot variants, additional commands, Offline reconnect,
and Fastboot routing reflect the current mode without duplicating the primary
action or launching duplicate refresh work. Technical details and Wireless ADB
remain compact and collapsible.

## 11. Applications contextual actions

With no selected applications, the table retains its full content height.
Selecting visible or hidden rows opens a contextual action bar within the same
page area, with count, relevant actions, overflow, and Clear. Selection is
preserved across filtering/search/sort and device-profile state remains
separate. Compact-width tests cover keyboard access and action visibility.

## 12. Settings recovery

Settings saves use same-directory atomic replacement, retain a last-known-good
backup, serialize concurrent writes, and do not delete profiles, APK backups,
or logs. Corrupt files are preserved for diagnosis. Recovery tries the backup,
falls back to safe defaults only when needed, keeps global and profile data
separate, and emits one actionable runtime warning per incident. A subsequent
save after recovery is supported and tested.

## 13. System theme listener

`SystemThemeController` polls the Windows app-theme preference only while
`System` is selected, reapplies QSS/icons only when the resolved Light/Dark
value changes, stops its timer for explicit themes, and stops during shutdown.
Provider-driven live-change tests pass. A manual physical Windows theme toggle
was not available as separate lab evidence.

## 14. CI workflows

- `ci.yml`: Windows CPython 3.10–3.14, compileall, Ruff, strict isolated test
  modules, version/APK/spec checks, offscreen GUI smoke, privacy guard, diff
  check, failure-only logs, and stale-run cancellation.
- `windows-build.yml`: reusable/manual/tag one-file build with pinned Platform
  Tools hashes, payload inspection, clean-profile smoke, checksum/status
  generation, optional fail-closed signing, and always-run signing cleanup.
- `release.yml`: exact-tag CI gate, artifact/schema/hash revalidation,
  independent Authenticode verification, signed stable publication, or an
  explicitly labelled unsigned draft/approved override.
- `device-lab.yml`: manual-only protected environment and fixed read-only tool
  invocation on labelled self-hosted Windows hardware.

Hosted Windows CI runs `29257684156` and `29259146171` passed on Python
3.10, 3.11, 3.12, 3.13, and 3.14. They are hosted automation evidence, not
physical Windows 10 or Android evidence.

## 15. EXE build

Local artifact: `release/OpenADB-3.0.0-unsigned.exe`.

- Size: 90,452,041 bytes; PE x64; File/Product Version 3.0.0.
- One-file payload inspection found the required Qt/runtime resources,
  checksum-verified Platform Tools 37.0.0 files, and ACBridge 3.0.0.
- A clean temporary-profile launch verified the exact window title, bundled
  tool selection, WM_CLOSE/exit 0, no crash log, no new adb/fastboot process,
  and no leftover extraction or signing directory.
- The Stage 9-only changes after the frozen build input are documentation,
  tests, CI/privacy tooling, benchmarks, and screenshots; runtime/spec/
  dependency/APK inputs used by the EXE did not change.

The EXE is intentionally ignored by Git and was not committed.

## 16. Authenticode status

**Unsigned.** No code-signing certificate or timestamp credential was
available. `Get-AuthenticodeSignature` returned `NotSigned`.
`signtool verify /pa /all /v /tw` returned exit code 1, as expected for an
unsigned file. The artifact therefore retains `-unsigned`; it must not be
presented as a final signed stable build. The all-or-none signing pipeline and
failure paths are implemented, but real signing/timestamp success is externally
blocked.

## 17. EXE SHA-256

`B48BCB48F868581384D68EFAA2DC373317C347E90967AA7F11B393F4B8C01A5B`

This digest is for the exact unsigned file named above. Signing would change
the bytes and requires a new digest.

## 18. APK version verification

Artifact: `openadb/resources/acbridge/ACBridge-3.0.0.apk` (byte-identical to
`ACBridge.apk`).

- Size: 45,613 bytes.
- SHA-256:
  `74F36F0224A0EF21FEEE6ED2D8EF276560CB3A45FCC64ECCE960F1D937DE4FB7`.
- Package/version: `com.communism420.acbridge`, 3.0.0, 30002.
- `zipalign -c` passed; apksigner reported v1/v2/v3 verification success.
- Signer SHA-256:
  `57D0F9154B24FA9E5AEBF40E4E4B8F83C42B281E08E22D4CC34EE842C030ECD7`.

The APK uses the project's intentionally public Android debug identity and is
`debuggable` for the current `run-as` status protocol. Verification proves
build/package continuity, not private publisher authenticity. A same-source
rebuild produced different bytes; non-normalized APK/ZIP timestamps are the
likely cause, but runtime equivalence was not claimed without a device test.
The reviewed bundled bytes were restored and verified rather than silently
replacing them.

## 19. Automated tests

The final frozen-tree strict run passed **39/39 modules and 564/564 tests** with
zero failures/errors (`ResourceWarning` treated as an error, Qt offscreen):
91.294 seconds summed unittest time and 109.375 seconds summed process wall
time. Coverage includes version/APK, context/generation, stale results,
registry/shutdown, settings recovery, System theme, P2P planning/warnings/
redaction, Applications contextual actions, Dashboard action uniqueness,
device-lab safety, screenshot metadata, and release-performance validation.

The exact required `python -m unittest discover -v` command also completed
with exit code 0: **564/564 passed**, `OK`, 993.206 seconds unittest time and
994.444 seconds wall time. CI retains isolated modules because they avoid the
large cumulative Qt-state overhead demonstrated by the monolithic run.

The host's global `pip check` reported conflicts only between unrelated
user-installed packages. A disposable no-cache clean-venv retry was stopped
after remaining network-bound; it was removed completely. Hosted Windows CI
already passed `pip check` in its clean pinned environment on Python 3.10–3.14.

## 20. Performance results

Environment: Windows 11 Pro build 26200, AMD64, CPython 3.14.3, PySide6 6.11.1,
16 logical CPUs, 32 GiB RAM. The host was classified as physical from its
system model; Windows reports a hypervisor present, which can also reflect VBS.
Method: deterministic generated mock data and empty temporary filesystem
entries, `perf_counter_ns`, two warmups and seven measured repetitions. No ADB,
fastboot, device, network, or user data was used.

| Scenario | Rows | Average ms | Maximum ms |
|---|---:|---:|---:|
| Apps filter | 1,200 | 0.453371 | 0.4577 |
| Apps name sort | 1,200 | 0.220371 | 0.3083 |
| Apps size sort | 1,200 | 1.352743 | 1.4155 |
| Apps selection | 1,200 | 0.221271 | 0.2356 |
| Apps metadata progress | 1,200 | 6.944586 | 7.2241 |
| Apps filter | 3,000 | 1.153614 | 1.1676 |
| Apps name sort | 3,000 | 0.536229 | 0.5395 |
| Apps size sort | 3,000 | 3.532600 | 4.1479 |
| Apps selection | 3,000 | 0.589600 | 0.6024 |
| Apps metadata progress | 3,000 | 20.227886 | 25.8803 |
| Auto stream planning | 4,096 | 5.684814 | 5.8077 |
| Stale-result filtering | 4,096 | 4.206957 | 4.2599 |
| Registry register/finish | 2,000 | 4.665629 | 5.4105 |
| Local File Manager tree | 5,000 | 14.344171 | 22.7878 |
| Immutable transfer plans | 3,000 | 15.901414 | 20.7542 |

Temporary benchmark data was removed. These are local planning/filtering
latencies, not P2P or ADB throughput measurements.

## 21. Manual and source runtime checks

- Clean source launch with no connected device: title 3.0.0, responsive UI,
  normal close/exit 0, no crash log, and no new device-tool process.
- Safely mocked absence of Platform Tools and real discovered/saved Platform
  Tools selection; no device command or settings migration was triggered.
- Light, Dark, System, live provider change, compact/expanded navigation,
  narrow/maximized geometry, long demo values, quick close, mocked worker/
  command/transfer close, and switch-during-operation scenarios were exercised
  by source/offscreen tests.
- Seven README screenshots were generated in isolated temporary profiles and
  visually reviewed using only `DEMO-ANDROID-001`, Demo Pixel, and `C:\Demo`
  paths. Each is RGB 1280×820, decodes successfully, has empty EXIF, and keeps
  only benign DPI metadata.
- Frozen EXE clean-profile startup and shutdown passed as described in section
  15.

No dangerous ADB/fastboot command was executed.

## 22. Device-lab results

The local default read-only lab tool found Platform Tools and zero ADB/fastboot
targets. Its sanitized JSON/JUnit validation recorded 2 passed probes, 0
failures, and 3 not-run hardware checks, then the disposable reports were
removed. The protected GitHub `device-lab` environment has a required reviewer,
but no matching self-hosted runner is registered, so no workflow hardware run
exists. The complete 77-scenario matrix remains in
`docs/DEVICE_LAB_MATRIX.md`.

## 23. Scenarios not executed

- Physical Windows 10; alternate 125/150/200% DPI; multiple-monitor and
  monitor-disconnect testing; physical signed/unsigned warning comparison.
- Real USB/Wireless/Recovery/Fastboot devices, unauthorized/offline reconnect,
  concurrent devices, real profile switching, or disposable app mutation.
- Real QR/pairing-code/mDNS/legacy/TV connection, ADB/P2P transfers, large-file
  throughput, SAF/MicroSD/USB, root, firewall, client isolation, cancellation,
  partial failure, or checksum round-trip.
- Real Authenticode signing, RFC 3161 timestamping, successful signtool verify,
  tag creation, draft/stable GitHub release, rollback, and post-download audit.

## 24. Known limitations

- The local EXE is unsigned and is not a final signed stable release.
- Hardware compatibility and device/network behavior remain unverified where
  listed above; mock/offscreen evidence is not substituted for them.
- Android-to-PC P2P is not implemented; that direction continues through ADB.
- P2P is authenticated and integrity-checked, not encrypted, and can be blocked
  by firewall/client isolation.
- ACBridge's public debug signing identity and `debuggable`/`run-as` status
  protocol are compatibility/security trade-offs; the signature is not a
  private publisher attestation.
- A same-source APK rebuild produced different bytes, likely because ZIP
  timestamps are not normalized; byte reproducibility remains unresolved.
- A removed privacy-sensitive cache remains in pre-existing Git history. A
  safe history rewrite requires maintainer coordination and force-update policy.
- The largest remaining UI integration modules are still substantial and are
  candidates for later responsibility-driven refactoring, not release-time
  mechanical splitting.

## 25. Commit list

1. `8c3792b` — `release: bump OpenADB and ACBridge to 3.0.0`
2. `587e932` — `docs: plan OpenADB 3 device-context migration`
3. `298dddd` — `core: bind device operations to immutable contexts`
4. `99044f2` — `refactor: split application and backup workflows`
5. `2ecdba5` — `refactor: split file manager and transfer workflows`
6. `db6f5e3` — `ui: improve P2P safety and automatic parallelism`
7. `07ff7fb` — `ui: finish contextual actions and Windows integration`
8. `3249742` — `ci: add Windows validation signing and release pipeline`
9. `2eaf02b` — `test: canonicalize Windows runner paths`
10. `a0487d2` — `ci: pin Platform Tools archive SHA-256`
11. `9c8d838` — `docs: add OpenADB 3.0.0 device lab and release validation`
12. `HEAD` — `release: finalize OpenADB 3.0.0`

## 26. Release gate status

| Gate | Status | Evidence or blocker |
|---|---|---|
| OpenADB is consistently 3.0.0 | **Passed** | Version test and metadata audit. |
| ACBridge APK is a real 3.0.0 build | **Passed** | Manifest/package/code/signature/alignment/size checks. |
| Device-bound long operations use immutable context | **Passed — automated/source audit** | Bound-client and workflow regressions. |
| Stale results cannot update a new device/profile | **Passed — automated** | UI/cache/profile/listing/transfer/command tests. |
| Generation tokens exist and behave monotonically | **Passed — automated** | Same-device refresh and identity-change tests. |
| Cancellation registry exists | **Passed — automated** | Conflict/cancel/reason/cleanup/shutdown tests. |
| Apps and File Manager responsibilities are split | **Passed** | Controllers/coordinators/models/strategies listed above. |
| P2P warning is in the GUI | **Passed — automated/visual** | Accept/cancel/profile warning and screenshot checks. |
| P2P is not enabled by default | **Passed — automated** | New/legacy profile defaults retain ADB. |
| P2P/pairing secrets stay out of logs | **Passed — automated/privacy scan** | Runner/worker/error/history/repr regressions. |
| Auto streams is implemented and tested | **Passed** | Pure planner, migration, clamping, benchmark. |
| Dashboard primary action is not duplicated | **Passed — automated/visual** | Mode/action uniqueness tests and screenshots. |
| Applications bulk actions are contextual | **Passed — automated/visual** | Selection/compact-layout tests and screenshots. |
| Corrupt settings recover safely | **Passed — automated** | Primary/backup/default/concurrency/warning/preservation tests. |
| System theme changes without restart | **Passed — automated** | Live provider tests; physical toggle remains unrun. |
| CI runs tests on Windows | **Passed** | Two hosted 3.10–3.14 matrix runs. |
| Release workflow builds an EXE | **Passed** | Workflow audit plus real local one-file build/smoke. |
| SHA-256 is generated automatically | **Passed** | Build/release workflow revalidation and local digest. |
| Signing pipeline is implemented | **Passed — infrastructure** | All-or-none secrets, timestamp, verify, cleanup gates. |
| Signed status is never fabricated | **Passed** | Local file is honestly named/reported unsigned. |
| Device-lab matrix is prepared | **Passed** | 77 rows plus fixed read-only workflow/tool. |
| Existing and new automated tests are green | **Passed** | Strict 564/564 and monolithic 564/564; zero failures/errors. |
| Syntax and lint are clean | **Passed** | compileall 134 files, Ruff, Python 3.10 grammar, 4 YAML/15 PowerShell blocks. |
| No private data | **Partial** | Active tree/artifacts pass; pre-existing historical blob remains. |
| No new adb/fastboot process remains after shutdown | **Passed — local smoke** | Source and frozen clean-close process snapshots. |
| Operation registry is empty after shutdown | **Passed — automated** | Shutdown/late-callback lifecycle regressions. |
| Documentation matches the real UI | **Passed — visual/metadata audit** | Seven current 3.0.0 demo screenshots and README references. |
| Version consistency test passes | **Passed** | Dedicated version suite 6/6. |
| APK metadata test passes | **Passed** | Both APK aliases, package/version/code/alignment/v1-v2-v3 signer checks. |
| PyInstaller smoke test passes | **Passed — local Windows 11** | Real one-file clean-profile title/tools/close smoke. |

### Decision

All programmatically implementable software checks passed, but this report
does **not** approve a fully validated signed stable release. The history
privacy exception needs a maintainer decision, and physical Windows 10,
Android/device-lab, plus real Authenticode evidence are externally outstanding.
An unsigned artifact may be used only as a clearly labelled preview under the
documented release policy.
