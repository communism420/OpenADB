# OpenADB release process

This document is the operator checklist for producing an OpenADB Windows
release. It describes the automated 3.0.1 pipeline and the manual evidence
that must be retained. A green workflow is necessary, but it does not replace
physical Windows or Android device-lab validation.

## Release invariants

- Build and release only from an immutable `v<version>` tag. OpenADB 3.0.1 is
  intentionally restricted to `v3.0.1` by the release workflow.
- The Python package, window title, Windows resources, PyInstaller filename,
  ACBridge source, manifest, APK, documentation, and changelog must name the
  same version.
- `openadb/version.py` is the canonical source for the OpenADB version,
  release EXE name, ACBridge APK name, Android versionCode, package identity,
  and expected ACBridge signer digest.
- Never obtain a new APK by renaming an older APK. Build it from the reviewed
  ACBridge source and verify the resulting package metadata and signature.
- Never label an EXE signed merely because a signing command ran. A signed
  stable artifact is allowed only after `signtool verify /pa /v` succeeds.
- Release assets must not contain a user profile path, serial, IP address,
  pairing code, P2P session key, private log, or signing material.
- Release smoke tests must not execute device-changing ADB or fastboot
  commands.

## Dependency environments

Supported releases are validated on CPython 3.10, 3.11, 3.12, 3.13, and 3.14.
Top-level packages are pinned separately by purpose:

- `requirements.txt`: source/runtime dependencies;
- `requirements-build.txt`: runtime dependencies plus PyInstaller;
- `requirements-dev.txt`: runtime, build, and validation dependencies,
  including Ruff.

Use a new virtual environment for local release validation:

```powershell
py -3.10 -m venv .venv-release
.\.venv-release\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements-dev.txt
.\.venv-release\Scripts\python.exe -m pip check
```

The exact dependency policy and update procedure are in
[`DEPENDENCIES.md`](DEPENDENCIES.md). A dependency update is a reviewed source
change and must pass the complete Windows Python matrix before release.

## 1. Version bump and metadata audit

1. Update `VERSION`, `VERSION_PARTS`, the ACBridge build number and
   `ACBRIDGE_VERSION_CODE` in `openadb/version.py`. The Android versionCode must
   be greater than every previously published helper.
2. Update ACBridge's `AndroidManifest.xml` and `BuildInfo.java` from the same
   values.
3. Update Windows version metadata, changelogs, README references, screenshot
   names, and any version-specific build or test expectations.
4. Search for stale identifiers before building:

   ```powershell
   rg -n "2\.0\.[01]|20004|20101|OpenADB-2|ACBridge-2" .
   ```

   Historical changelog entries are expected matches; active metadata and
   artifact names are not.
5. Run the consistency test:

   ```powershell
   python -m unittest -q tests.test_version_metadata
   ```

For OpenADB 3.0.1 the required helper identity is
`com.communism420.acbridge`, `versionName=3.0.1`, and `versionCode=30101`.

## 2. Build and verify ACBridge

ACBridge requires a supported JDK plus an Android SDK containing a platform and
Build Tools (`aapt`, D8, `zipalign`, and `apksigner`). Set `ANDROID_HOME` or
`ANDROID_SDK_ROOT`, then run:

```powershell
python tools/build_acbridge.py
python -m unittest -q tests.test_version_metadata.VersionMetadataTests.test_bundled_apks_are_real_current_signed_builds
```

`tools/build_acbridge.py` compiles the reviewed Java sources, creates DEX,
packages and aligns the APK, signs it with the established ACBridge helper
key, and verifies package name, versionName, versionCode, v1/v2/v3 signature
schemes, and signer SHA-256 before atomically publishing both
`ACBridge-<version>.apk` and the byte-identical compatibility `ACBridge.apk`.

Record the output of the following independent checks in the release evidence:

```powershell
$buildTools = Get-ChildItem "$env:ANDROID_HOME\build-tools" -Directory |
  Sort-Object Name -Descending | Select-Object -First 1
& "$($buildTools.FullName)\aapt.exe" dump badging openadb\resources\acbridge\ACBridge-3.0.1.apk
& "$($buildTools.FullName)\zipalign.exe" -c -v 4 openadb\resources\acbridge\ACBridge-3.0.1.apk
java -jar "$($buildTools.FullName)\lib\apksigner.jar" verify --verbose --print-certs openadb\resources\acbridge\ACBridge-3.0.1.apk
```

The bundled ACBridge APK uses the repository's intentionally public Android
debug signing identity so existing helper installs remain upgrade-compatible.
Its signature check proves build/identity continuity, not private publisher
authenticity. The helper is also deliberately `debuggable` because its current
private status-file protocol uses Android `run-as`; that lifecycle dependency
is separate from the choice of signing key. This public debug identity is not
the Windows Authenticode certificate and must never be reused as one. Do not
rotate either installed identity as an incidental build fix, and never add a
private publisher keystore, PFX, password, or certificate data to the
repository or workflow logs.

## 3. Run source validation

Run the same classes of checks used by Windows CI before creating the tag:

```powershell
git diff --check
python -m compileall -q openadb tests tools
ruff check openadb tests tools
python -m unittest discover -v
python -W error::ResourceWarning -m unittest -q tests.test_final_regressions tests.test_design_system tests.test_system_theme
$env:QT_QPA_PLATFORM = 'offscreen'
$testFiles = git ls-files 'tests/test_*.py' | Where-Object { $_ -match '^tests/test_[^/]+\.py$' } | Sort-Object
foreach ($testFile in $testFiles) {
  $module = ($testFile -replace '\.py$', '') -replace '[/\\]', '.'
  python -W error::ResourceWarning -m unittest -q $module
  if ($LASTEXITCODE -ne 0) { throw "Failed unittest module: $module" }
}
$environmentType = 'physical' # Use 'virtual-machine' on a virtualized host.
python tools/release_performance.py --environment-type $environmentType --json-report release-performance.json
```

Choose the environment label from the measured host instead of copying the
example blindly; record hypervisor and host-model evidence separately when the
classification is ambiguous.

Also review the CI privacy check and inspect demo screenshots for metadata,
personal paths, real device identifiers, real network addresses, or personal
filenames. The guard scans tracked/unignored bytes as UTF-8 and UTF-16, rejects
generated Androguard databases and private key containers, and must be tested
with a disposable negative fixture. Remove that fixture immediately after the
expected failure. The release gate accepts only a successful `Windows CI` push run
for the exact tag commit. Failure logs are retained by CI for seven days; test
logs from successful jobs are not uploaded.

## 4. Build and smoke-test the Windows EXE

For a local equivalent of the automated build, make Platform Tools available
through `ANDROID_HOME`, `ANDROID_SDK_ROOT`, the standard Android SDK location,
or `PATH`, then use the pinned build dependencies:

```powershell
python -m pip install --disable-pip-version-check -r requirements-build.txt
python -m pip check
python -m PyInstaller --noconfirm --clean OpenADB.spec
```

`OpenADB.spec` produces a one-file `OpenADB-3.0.1.exe` build intermediate and
bundles the current ADB/fastboot binaries and DLLs, their notice when
available, the versioned ACBridge APK, UI resources, and required Python
packages. Until Authenticode succeeds, that stable-looking intermediate is not
a publishable stable artifact: inspect it, then rename it to
`OpenADB-3.0.1-unsigned.exe`. Do not commit the large EXE; publish it as an
Actions/release artifact.

Automation downloads the exact stable Platform Tools 37.0.0 Windows archive.
It requires both Google's repository-metadata SHA-1 and the independently
recorded SHA-256 of those same bytes before extracting or executing anything;
either mismatch stops the build.

The `Windows release build` workflow runs on `workflow_dispatch`, calls from
the release workflow, and the exact `v3.0.1` tag. Its smoke test uses a clean
temporary OpenADB profile and a read-only startup path. It checks:

- process startup and clean shutdown;
- the exact `OpenADB 3.0.1` window title;
- bundled ADB, fastboot, Platform Tools libraries, and notice;
- bundled ACBridge package/version metadata;
- absence of a crash log.

The workflow uploads either `OpenADB-3.0.1-windows-signed` or
`OpenADB-3.0.1-windows-unsigned`. The artifact contains exactly one
appropriately named EXE plus `BUILD_STATUS.json` and `SHA256SUMS.txt`; its
dynamic artifact name, signed state, and filename are also exposed as reusable
workflow outputs and must agree with the status file. Treat a missing or
malformed status file as a failed build, not as an unsigned build.

## 5. Configure optional Authenticode signing

Store signing values as protected repository or environment secrets with
access limited to release maintainers:

| Secret | Required content |
| --- | --- |
| `WINDOWS_SIGNING_PFX_BASE64` | Base64 of the complete code-signing PFX bytes |
| `WINDOWS_SIGNING_PFX_PASSWORD` | Password for that PFX |
| `WINDOWS_SIGNING_TIMESTAMP_URL` | HTTPS RFC 3161 timestamp service URL |

The certificate must be valid for Windows code signing and include its usable
private key and required chain. Generate Base64 locally without printing the
PFX or its password into a shared terminal log. For example, assign the result
directly to a private variable or clipboard, then clear it after storing the
secret:

```powershell
$pfxBase64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes('<certificate.pfx>'))
$pfxBase64 | Set-Clipboard
# Add it to the protected secret, then clear both values from this session.
$pfxBase64 = $null
Set-Clipboard -Value ''
```

The build accepts either all three signing secrets or none. A partial
configuration is an error. When all are present, automation must:

1. decode the PFX into the isolated runner temporary directory;
2. use it without echoing the password or certificate bytes;
3. sign the temporary `OpenADB-3.0.1-unsigned.exe` candidate with SHA-256 and
   the configured timestamp;
4. run `signtool verify /pa /all /v /tw` and require exit code zero;
5. only after verification, rename it to the stable `OpenADB-3.0.1.exe`;
6. independently check Authenticode again in the release job;
7. delete the temporary PFX in an always-run cleanup step (and delete any
   temporary certificate-store entry if a future implementation imports one).

If signing, timestamping, or verification fails, the job fails before an
artifact can be described as signed. Inspect the runner cleanup log and revoke
the certificate immediately if private material may have escaped its protected
scope.

## 6. Verify SHA-256 and build metadata

The build computes SHA-256 after the final signed/unsigned filename is chosen.
The release job recomputes it and rejects any disagreement between the file,
`BUILD_STATUS.json`, and the original `SHA256SUMS.txt`. It then generates the
published `SHA256SUMS.txt` for the EXE, versioned ACBridge APK, and sanitized
build-status metadata. `BUILD_STATUS.json` also records both pinned Platform
Tools archive hashes so the release gate can reject a substituted build input.

Local verification uses:

```powershell
$executables = @(Get-ChildItem . -File -Filter 'OpenADB-3.0.1*.exe')
if ($executables.Count -ne 1) { throw 'Expected exactly one signed or unsigned release EXE.' }
$digest = Get-FileHash $executables[0].FullName -Algorithm SHA256
"$($digest.Hash) *$($executables[0].Name)" | Set-Content .\SHA256SUMS.txt -Encoding ascii
Get-Content .\SHA256SUMS.txt
```

For a verified signed build, first require the stable filename and then retain
this command's successful output:

```powershell
if ($executables[0].Name -ne 'OpenADB-3.0.1.exe') { throw 'Unsigned EXE cannot pass the signed gate.' }
signtool verify /pa /all /v /tw .\OpenADB-3.0.1.exe
```

Do not copy a checksum from an earlier build: signing changes the executable
bytes, so the hash must be calculated after signing and verification.

## 7. Approve device-lab evidence

The manual `.github/workflows/device-lab.yml` job must use the protected
`device-lab` environment and a runner labelled `self-hosted`, `windows`, and
`device-lab`. Configure a required reviewer before registering that runner.
The workflow exposes no inputs, checks out only the default branch, and invokes
the smoke tool without serial, package, path, mutation flag, or free-form
command text.

Review the sanitized JSON/JUnit pair against `docs/DEVICE_LAB_MATRIX.md`.
`Not run — hardware unavailable` is a valid truthful report, but it is not
hardware evidence and must stay in release limitations. Never convert a mock,
offscreen test, or empty device probe into a passed physical row.

## 8. Tag and publish

Before tagging, require reviewed changes, green branch CI, approved device-lab
evidence, and a clean worktree. Create an annotated tag at the exact reviewed
commit and push only that tag:

```powershell
git status --short
git tag -a v3.0.1 -m "OpenADB 3.0.1"
git show --no-patch --decorate v3.0.1
git push origin v3.0.1
```

The tag starts `Windows CI`, the standalone Windows build, and the release
pipeline. The release pipeline waits for successful exact-tag CI, calls the
same reusable Windows builder, downloads its artifact, validates the strict
metadata schema, recomputes hashes, independently verifies Authenticode, and
only then calls GitHub Releases.

Release notes are generated from the English 3.0.1 changelog section and add:

- signed/unsigned state and executable SHA-256;
- Platform Tools and ACBridge metadata;
- a link to exact-tag CI and its validation classes;
- known hardware/security limitations;
- the private-data gate result.

The allowlisted published assets are the single EXE, versioned ACBridge APK,
`BUILD_STATUS.json`, and `SHA256SUMS.txt`. The PFX, crash logs, temporary
profiles, signing password, and successful-test logs are never release assets.

## 9. Behavior without a signing certificate

With all three signing secrets absent, the builder creates
`OpenADB-3.0.1-unsigned.exe`, records `"signed": false`, and never uses the
stable signed filename. An automatic tag run creates only a clearly labelled
draft/prerelease unsigned preview. Reviewers must check its checksum, metadata,
limitations, and Windows warning behavior before deciding what to do next.

The preferred resolution is to configure a protected certificate and rerun
the pipeline. If project policy explicitly permits an unsigned stable release,
a maintainer must delete the existing draft preview after preserving its audit
record, select the `v3.0.1` ref in Actions, manually dispatch
`OpenADB 3.0.1 release`, and enable `allow_unsigned_stable`. That explicit
input is unavailable to an automatic tag run. The published executable still
keeps the `-unsigned` suffix and the release notes prominently disclose its
state.

Never rename an unsigned EXE to `OpenADB-3.0.1.exe`, manually set
`"signed": true`, or publish an unsigned automatic preview as a final signed
release.

## 10. Post-release verification

After publication:

1. download every asset from GitHub into a new empty directory;
2. recompute and compare every line of `SHA256SUMS.txt`;
3. run `signtool verify /pa /v` on the downloaded EXE when the release claims
   it is signed;
4. inspect `BUILD_STATUS.json` and the APK metadata against the release notes;
5. run the EXE with a new temporary profile on physical Windows 10 and Windows
   11, recording DPI/theme/device-lab results separately;
6. confirm the release is still draft when the only artifact is an automatic
   unsigned preview;
7. confirm no PFX, key material, private path, real device ID, or private log
   is present in the release or workflow artifacts.

Only after these checks should announcements link to the release.

## 11. Rollback and incident handling

Do not silently move an already published tag to different bytes. If a release
gate fails before publication, leave or convert the release to draft, remove
the faulty release assets, fix the source, and rerun validation. If the tag has
not been consumed or announced, maintainers may delete it after documenting
why; otherwise publish a new patch version and tag.

For a defective published release:

1. mark it as draft or clearly warn users and stop distribution;
2. preserve checksums, workflow URLs, `BUILD_STATUS.json`, and failure evidence;
3. remove compromised downloadable assets without deleting unrelated source
   history or user data;
4. fix on a new reviewed commit and publish a monotonically newer version;
5. if signing material might be exposed, revoke the certificate, remove the
   affected secrets, rotate credentials, and do not reuse that PFX;
6. if ACBridge identity or APK integrity is affected, stop helper distribution
   and investigate before shipping another helper—never auto-uninstall an
   existing Android package from user devices.

Local rollback must not use destructive Git commands against a dirty worktree.
Release rollback never deletes OpenADB profiles, APK backups, logs, or Android
user data.
