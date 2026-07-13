# OpenADB 3.0.0 release progress

Updated: 13 July 2026.

Statuses: `not started`, `in progress`, `completed`, `blocked`.

> A commit cannot contain its own final SHA because changing the recorded SHA
> changes that commit. The current stage therefore uses `HEAD` plus its exact
> commit subject; the next stage replaces it with the stable short SHA.

| Stage | Name | Status | Files | Tests | Limitations | Commit SHA |
|---:|---|---|---|---|---|---|
| 0 | OpenADB and ACBridge 3.0.0 version bump | **completed** | `openadb/version.py`, package/version metadata, ACBridge manifest/`BuildInfo.java`/APKs/build tool, PyInstaller/Windows metadata, README/changelogs/versioned screenshots, `tests/test_version_metadata.py` | Real ACBridge rebuild; package/name/code/size/readability; byte-identical aliases; `zipalign -c`; apksigner v1/v2/v3 and signer fingerprint; 6 version tests; `compileall`; touched-file Ruff; `git diff --check`; six screenshots visually reviewed and PNG metadata inspected | APK remains `debuggable` and uses the repository's debug certificate because the current P2P status-file protocol depends on `run-as`; EXE Authenticode is handled in stages 7/9 | `8c3792b` |
| 1 | Baseline validation and migration plan | **completed** | `OPENADB_3_ARCHITECTURE_PLAN.md`, `OPENADB_3_PROGRESS.md` | 127 unittest in 147.361 s; full `compileall`; full Ruff; clean-profile `pythonw -m openadb.main`; title/responsiveness; graceful WM_CLOSE exit 0; no new adb/fastboot processes or crash log; module-size/mutable-state audit | Baseline hardware is Windows 11/Python 3.14.3 only; Windows 10 and real-device scenarios remain for stages 7–9 | `587e932` |
| 2 | Immutable device contexts and generation tokens | **completed** | `openadb/core/device_context.py`, `operations.py`, bound ADB/fastboot/runner/file transfer, device/profile transactions, ACBridge/P2P/backup cancellation, worker lifecycle and stale-result guards across Apps, Backups, File Manager, Commands, Dashboard, status and Wireless ADB; context/lifecycle regression tests | 263 unittest in 246.164 s; focused context/registry/runner/P2P/FM/Apps/Wireless/settings/Main suites (178/178); full `compileall`; full Ruff; `git diff --check` | Offscreen/mock validation only for device switching and transfers; no real Android hardware, network pairing, or dangerous ADB/fastboot command was used | `298dddd` |
| 3 | Applications and backups refactor | **completed** | `openadb/ui/apps_page.py`, `apps_data_workflow.py`, `apps_action_workflow.py`, `backups_page.py`, filter/selection controllers, metadata/asset loaders, app/backup operation coordinators, `AppTable`, and focused coordinator/context/lifecycle tests | 327 unittest in 257.453 s; 113/113 Stage 3 focused regressions; 75/75 strict context/lifecycle tests after final review; full `compileall`; full Ruff; `git diff --check`; `AppsPage` reduced from 2679 to 711 lines | Offscreen/mock validation only; no real Android device, ACBridge installation, package mutation, backup restore, or dangerous ADB/fastboot command was executed | `HEAD` — `refactor: split application and backup workflows` |
| 4 | File Manager and transfer pipeline refactor | not started | — | — | — | — |
| 5 | P2P security UX and Auto streams | not started | — | — | — | — |
| 6 | Final UX polish and Windows integration | not started | — | — | — | — |
| 7 | CI, Windows build, signing, and release pipeline | not started | — | — | — | — |
| 8 | Device lab and Windows 10 validation | not started | — | — | — | — |
| 9 | Final OpenADB 3.0.0 release validation | not started | — | — | — | — |
