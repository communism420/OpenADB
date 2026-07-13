# OpenADB release process

This document is the operator checklist for producing an OpenADB Windows
release. It describes the automated 3.0.0 pipeline and the manual evidence
that must be retained. A green workflow is necessary, but it does not replace
physical Windows or Android device-lab validation.

## Release invariants

- Build and release only from an immutable `v<version>` tag. OpenADB 3.0.0 is
  intentionally restricted to `v3.0.0` by the release workflow.
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

For OpenADB 3.0.0 the required helper identity is
`com.communism420.acbridge`, `versionName=3.0.0`, and `versionCode=30002`.

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
& "$($buildTools.FullName)\aapt.exe" dump badging openadb\resources\acbridge\ACBridge-3.0.0.apk
& "$($buildTools.FullName)\zipalign.exe" -c -v 4 openadb\resources\acbridge\ACBridge-3.0.0.apk
java -jar "$($buildTools.FullName)\lib\apksigner.jar" verify --verbose --print-certs openadb\resources\acbridge\ACBridge-3.0.0.apk
```

The ACBridge helper key is distinct from the Windows Authenticode certificate.
Do not rotate or replace either identity as an incidental build fix. Never put
new keystore passwords, PFX files, or private certificate data in the
repository or workflow logs.

## 3. Run source validation

Run the same classes of checks used by Windows CI before creating the tag:

```powershell
git diff --check
python -m compileall -q openadb tests tools
python -m ruff check openadb tests tools
python -W error::ResourceWarning -m unittest -q tests.test_final_regressions tests.test_design_system tests.test_system_theme
$env:QT_QPA_PLATFORM = 'offscreen'
$testFiles = git ls-files 'tests/test_*.py' | Where-Object { $_ -match '^tests/test_[^/]+\.py$' } | Sort-Object
foreach ($testFile in $testFiles) {
  $module = ($testFile -replace '\.py$', '') -replace '[/\\]', '.'
  python -W error::ResourceWarning -m unittest -q $module
  if ($LASTEXITCODE -ne 0) { throw "Failed unittest module: $module" }
}
```

Also review the CI privacy check and inspect demo screenshots for metadata,
personal paths, real device identifiers, real network addresses, or personal
filenames. The release gate accepts only a successful `Windows CI` push run
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

`OpenADB.spec` produces a one-file `OpenADB-3.0.0.exe` and bundles the current
ADB/fastboot binaries and DLLs, their notice when available, the versioned
ACBridge APK, UI resources, and required Python packages. Do not commit the
large EXE; publish it as an Actions/release artifact.

Automation downloads the exact stable Platform Tools 37.0.0 Windows archive.
It requires both Google's repository-metadata SHA-1 and the independently
recorded SHA-256 of those same bytes before extracting or executing anything;
either mismatch stops the build.

The `Windows release build` workflow runs on `workflow_dispatch`, calls from
the release workflow, and the exact `v3.0.0` tag. Its smoke test uses a clean
temporary OpenADB profile and a read-only startup path. It checks:

- process startup and clean shutdown;
- the exact `OpenADB 3.0.0` window title;
- bundled ADB, fastboot, Platform Tools libraries, and notice;
- bundled ACBridge package/version metadata;
- absence of a crash log.

The workflow uploads either `OpenADB-3.0.0-windows-signed` or
`OpenADB-3.0.0-windows-unsigned`. The artifact contains exactly one
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
3. sign `OpenADB-3.0.0.exe` with SHA-256 and the configured timestamp;
4. run `signtool verify /pa /v` and require exit code zero;
5. independently check Authenticode again in the release job;
6. delete the temporary PFX in an always-run cleanup step (and delete any
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
Get-FileHash .\OpenADB-3.0.0.exe -Algorithm SHA256
Get-Content .\SHA256SUMS.txt
```

For a signed build also retain this command's successful output:

```powershell
signtool verify /pa /v .\OpenADB-3.0.0.exe
```

Do not copy a checksum from an earlier build: signing changes the executable
bytes, so the hash must be calculated after signing and verification.

## 7. Tag and publish

Before tagging, require reviewed changes, green branch CI, approved device-lab
evidence, and a clean worktree. Create an annotated tag at the exact reviewed
commit and push only that tag:

```powershell
git status --short
git tag -a v3.0.0 -m "OpenADB 3.0.0"
git show --no-patch --decorate v3.0.0
git push origin v3.0.0
```

The tag starts `Windows CI`, the standalone Windows build, and the release
pipeline. The release pipeline waits for successful exact-tag CI, calls the
same reusable Windows builder, downloads its artifact, validates the strict
metadata schema, recomputes hashes, independently verifies Authenticode, and
only then calls GitHub Releases.

Release notes are generated from the English 3.0.0 changelog section and add:

- signed/unsigned state and executable SHA-256;
- Platform Tools and ACBridge metadata;
- a link to exact-tag CI and its validation classes;
- known hardware/security limitations;
- the private-data gate result.

The allowlisted published assets are the single EXE, versioned ACBridge APK,
`BUILD_STATUS.json`, and `SHA256SUMS.txt`. The PFX, crash logs, temporary
profiles, signing password, and successful-test logs are never release assets.

## 8. Behavior without a signing certificate

With all three signing secrets absent, the builder creates
`OpenADB-3.0.0-unsigned.exe`, records `"signed": false`, and never uses the
stable signed filename. An automatic tag run creates only a clearly labelled
draft/prerelease unsigned preview. Reviewers must check its checksum, metadata,
limitations, and Windows warning behavior before deciding what to do next.

The preferred resolution is to configure a protected certificate and rerun
the pipeline. If project policy explicitly permits an unsigned stable release,
a maintainer must delete the existing draft preview after preserving its audit
record, select the `v3.0.0` ref in Actions, manually dispatch
`OpenADB 3.0.0 release`, and enable `allow_unsigned_stable`. That explicit
input is unavailable to an automatic tag run. The published executable still
keeps the `-unsigned` suffix and the release notes prominently disclose its
state.

Never rename an unsigned EXE to `OpenADB-3.0.0.exe`, manually set
`"signed": true`, or publish an unsigned automatic preview as a final signed
release.

## 9. Post-release verification

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

## 10. Rollback and incident handling

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
