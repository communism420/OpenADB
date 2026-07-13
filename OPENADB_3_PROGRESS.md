# OpenADB 3.0.0 release progress

Updated: 13 July 2026.

Statuses: `not started`, `in progress`, `completed`, `blocked`.

> A commit cannot contain its own final SHA because changing the recorded SHA
> changes that commit. The current stage therefore uses `HEAD` plus its exact
> commit subject; the next stage replaces it with the stable short SHA.

| Stage | Name | Status | Files | Tests | Limitations | Commit SHA |
|---:|---|---|---|---|---|---|
| 0 | OpenADB and ACBridge 3.0.0 version bump | **completed** | `openadb/version.py`, package/version metadata, ACBridge manifest/`BuildInfo.java`/APKs/build tool, PyInstaller/Windows metadata, README/changelogs/versioned screenshots, `tests/test_version_metadata.py` | Real ACBridge rebuild; package/name/code/size/readability; byte-identical aliases; `zipalign -c`; apksigner v1/v2/v3 and signer fingerprint; 6 version tests; `compileall`; touched-file Ruff; `git diff --check`; six screenshots visually reviewed and PNG metadata inspected | APK remains `debuggable` and uses the repository's debug certificate because the current P2P status-file protocol depends on `run-as`; EXE Authenticode is handled in stages 7/9 | `HEAD` — `release: bump OpenADB and ACBridge to 3.0.0` |
| 1 | Baseline validation and migration plan | not started | — | — | — | — |
| 2 | Immutable device contexts and generation tokens | not started | — | — | — | — |
| 3 | Applications and backups refactor | not started | — | — | — | — |
| 4 | File Manager and transfer pipeline refactor | not started | — | — | — | — |
| 5 | P2P security UX and Auto streams | not started | — | — | — | — |
| 6 | Final UX polish and Windows integration | not started | — | — | — | — |
| 7 | CI, Windows build, signing, and release pipeline | not started | — | — | — | — |
| 8 | Device lab and Windows 10 validation | not started | — | — | — | — |
| 9 | Final OpenADB 3.0.0 release validation | not started | — | — | — | — |
