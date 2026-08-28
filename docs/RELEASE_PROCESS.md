# OpenADB release process

This is the maintainer checklist for producing a Windows release. A green
workflow is required, but it never substitutes for truthful physical Windows
and Android validation recorded against the
[device-lab checklist](DEVICE_LAB_MATRIX.md).

## Release invariants

- Build only a reviewed commit and publish only from its immutable
  `v<version>` tag. The release job must accept successful `Windows CI` evidence
  for that exact tag commit, never merely for the same branch.
- `openadb/version.py` is the canonical source for the OpenADB version, release
  EXE name, ACBridge APK name/build/versionCode, package identity, and expected
  ACBridge signer digest. Python metadata, Windows resources, ACBridge source
  and manifest, bundled APKs, README, and [CHANGELOG.md](../CHANGELOG.md) must
  agree with it.
- Build ACBridge from reviewed source. Never create a new helper by renaming an
  older APK, rotate its installed signing identity as a build workaround, or
  downgrade a newer helper already installed on a device.
- A Windows executable is signed only after `signtool verify /pa /all /v /tw`
  succeeds. An unsigned build always keeps the `-unsigned.exe` suffix.
- Checksums are calculated from the final bytes after signing or after the
  unsigned filename has been selected.
- Release assets and logs must not contain usernames or profile paths, device
  serials/nicknames, IP addresses, SSIDs, pairing codes, P2P secrets, private
  logs, certificates, passwords, or private keys.
- Release smoke tests are read-only. They must never flash, erase, format,
  sideload, unlock/lock a bootloader, wipe data, or mutate a real package.

## 1. Prepare an isolated environment

OpenADB supports CPython 3.10 through 3.14. Validate every supported version in
CI and use a fresh local environment for release work:

```powershell
py -3.10 -m venv .venv-release
.\.venv-release\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements-dev.txt
.\.venv-release\Scripts\python.exe -m pip check
```

Runtime, build, and development requirements are intentionally separate. See
[DEPENDENCIES.md](DEPENDENCIES.md) before changing a pin; a dependency update
is a reviewed source change and must pass the complete Python matrix.

Read the active version without copying it into commands by hand:

```powershell
$version = python -c "from openadb.version import VERSION; print(VERSION)"
$tag = "v$version"
$signedExe = "OpenADB-$version.exe"
$unsignedExe = "OpenADB-$version-unsigned.exe"
$bridgeApk = "openadb\resources\acbridge\ACBridge-$version.apk"
```

## 2. Audit version metadata

1. Update `openadb/version.py`, including a monotonically increasing ACBridge
   build and Android versionCode.
2. Update ACBridge `AndroidManifest.xml` and `BuildInfo.java` from the same
   values.
3. Update Windows metadata, README references, and the English release section
   in the canonical [CHANGELOG.md](../CHANGELOG.md). Do not maintain a second
   language-suffixed changelog.
4. Search tracked files for the previous active version and artifact names.
   Historical changelog entries are valid; active metadata is not.
5. Run `python -m unittest -q tests.test_version_metadata`.

The metadata test reads the current versionCode from `openadb/version.py` and
is the authority that all corresponding locations and bundled artifacts agree.

## 3. Build and verify ACBridge

Set `ANDROID_HOME` or `ANDROID_SDK_ROOT` to a supported Android SDK containing a
platform and Build Tools, then run:

```powershell
python tools/build_acbridge.py
python -m unittest -q tests.test_version_metadata.VersionMetadataTests.test_bundled_apks_are_real_current_signed_builds
```

The builder verifies pinned third-party inputs, compiles reviewed Java sources,
creates DEX, packages, aligns, and signs the APK, validates package/version
metadata and v1/v2/v3 signatures, then atomically publishes the versioned APK
and byte-identical compatibility alias. Independently retain sanitized output
from `aapt dump badging`, `zipalign -c -v 4`, and
`apksigner verify --verbose --print-certs` for `$bridgeApk`.

The bundled helper deliberately uses the repository's public Android signing
identity for upgrade compatibility. That proves continuity, not private
publisher authenticity. ACBridge itself is not a debuggable application. Its
P2P and Shizuku control entry points are shell/DUMP protected and use bounded,
request-scoped IPC. The Android identity is unrelated to Authenticode and must
never be reused for Windows signing. Never commit a private keystore, PFX,
certificate password, or certificate bytes.

## 4. Validate source and privacy

Run the same classes of checks as Windows CI:

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
python tools/release_performance.py --environment-type physical --json-report release-performance.json
```

Use `virtual-machine` instead of `physical` when that is the measured host; do
not copy an evidence label blindly. Review screenshots and the CI privacy scan.
The privacy guard must scan tracked/unignored UTF-8 and UTF-16 content, reject
generated analysis databases and private-key containers, and be verified with
a disposable negative fixture that is removed immediately afterward. Do not
upload successful test logs; keep failure-log retention bounded.

## 5. Build and smoke-test the Windows EXE

Make the pinned Platform Tools input available and build with the reviewed build
requirements:

```powershell
python -m pip install --disable-pip-version-check -r requirements-build.txt
python -m pip check
python -m PyInstaller --noconfirm --clean OpenADB.spec
```

The spec must bundle ADB, fastboot, required DLLs/notices, the current ACBridge
APK, UI resources, and Python packages. Before verified Authenticode signing,
rename the one-file intermediate to `$unsignedExe`; never publish it under
`$signedExe` and never commit large binaries.

The automated builder must verify both the trusted upstream archive digest and
the independently pinned SHA-256 before extracting Platform Tools. Its clean,
temporary-profile smoke test checks startup, exact title/version, bundled
tools and notices, ACBridge metadata, clean shutdown, and absence of a crash
log. The uploaded artifact contains exactly one correctly named EXE plus
`BUILD_STATUS.json` and `SHA256SUMS.txt`. Missing, malformed, or contradictory
build status is a failed build, not an unsigned success.

## 6. Optional Authenticode signing

Store these as protected repository or environment secrets with release-
maintainer access only:

| Secret | Content |
| --- | --- |
| `WINDOWS_SIGNING_PFX_BASE64` | Base64 of the complete code-signing PFX |
| `WINDOWS_SIGNING_PFX_PASSWORD` | PFX password |
| `WINDOWS_SIGNING_TIMESTAMP_URL` | HTTPS RFC 3161 timestamp URL |

All three secrets must be present or all absent; partial configuration fails.
When present, automation decodes the PFX only into the isolated runner temp
directory, signs the unsigned candidate with SHA-256 and the timestamp service,
verifies it with `signtool`, and only then renames it to `$signedExe`. The
release job verifies Authenticode independently. Cleanup always removes the
temporary PFX and any temporary certificate-store entry without logging secret
material. Signing, timestamp, or verification failure stops publication. If
private material may have escaped, revoke the certificate and rotate secrets.

With all three secrets absent, automation produces `$unsignedExe` with
`"signed": false`. An automatic tag run may create only a clearly labelled
draft/prerelease preview. A policy-approved unsigned stable release requires
the workflow's explicit manual override; it still keeps the unsigned suffix
and prominent disclosure. Never falsify status metadata or rename it as signed.

## 7. Verify checksums and build metadata

After the final filename is fixed, recompute SHA-256 and require agreement
between the EXE, `BUILD_STATUS.json`, and the builder's `SHA256SUMS.txt`. The
release job publishes a new checksum list for the EXE, versioned ACBridge APK,
and sanitized status metadata. The status file must also identify the pinned
Platform Tools input hashes.

```powershell
$executables = @(Get-ChildItem . -File -Filter "OpenADB-$version*.exe")
if ($executables.Count -ne 1) { throw 'Expected exactly one release EXE.' }
$digest = Get-FileHash $executables[0].FullName -Algorithm SHA256
"$($digest.Hash) *$($executables[0].Name)" | Set-Content .\SHA256SUMS.txt -Encoding ascii
if ($executables[0].Name -eq $signedExe) {
  signtool verify /pa /all /v /tw $executables[0].FullName
}
```

Signing changes bytes, so never reuse a checksum from an earlier build.

## 8. Approve device-lab evidence

Run the manual protected `device-lab` workflow only on a reviewed self-hosted
Windows runner. Review its sanitized JSON/JUnit output against
[DEVICE_LAB_MATRIX.md](DEVICE_LAB_MATRIX.md). `Not run — hardware unavailable`
is truthful but is not physical evidence and must be disclosed as a release
limitation. Never convert a mock, offscreen test, emulator proxy, or empty
device probe into a passed physical row.

## 9. Tag and publish

Require reviewed changes, green branch CI, reviewed device-lab evidence, and a
clean worktree. Create an annotated tag at the exact commit:

```powershell
git status --short
git tag -a $tag -m "OpenADB $version"
git show --no-patch --decorate $tag
git push origin $tag
```

The tag starts CI, the reusable Windows builder, and the release pipeline. The
release job must wait for successful exact-tag CI, validate the strict build-
status schema, recompute hashes, verify Authenticode independently, and only
then publish. Release notes come from the matching section of
[CHANGELOG.md](../CHANGELOG.md) and disclose signed/unsigned state, hashes,
Platform Tools and ACBridge metadata, exact-tag CI, hardware/security
limitations, and privacy-gate result.

The asset allowlist is one EXE, the versioned ACBridge APK,
`BUILD_STATUS.json`, and `SHA256SUMS.txt`. Never publish PFX/key material,
passwords, crash logs, temporary profiles, or raw test logs.

## 10. Post-release verification

1. Download every asset into a new empty directory and verify every checksum.
2. When signed status is claimed, verify the downloaded EXE with `signtool`.
3. Compare build status and APK metadata with the release notes.
4. Start and close the EXE with a new profile on physical Windows 10 and 11;
   record DPI, theme, and device results separately.
5. Confirm an automatic unsigned preview remains draft/prerelease unless the
   explicit policy override was used.
6. Inspect the release and workflow artifacts for private data or secrets.

Announce the release only after these checks pass.

## 11. Rollback and incident handling

Never silently move a published tag to different bytes. Before publication,
leave or convert a failed release to draft, remove faulty assets, fix the
source, and rerun validation. Delete an unannounced tag only with a recorded
reason; otherwise publish a monotonically newer patch release.

For a defective published release, stop distribution or add a prominent
warning, preserve checksums/workflow URLs/status/failure evidence, remove only
compromised downloadable assets, and fix on a new reviewed commit. Revoke and
rotate any exposed signing material. If ACBridge identity or integrity is in
doubt, stop helper distribution and investigate; never auto-uninstall it from
user devices.

Local rollback must not use destructive Git commands on a dirty worktree.
Release rollback never deletes OpenADB profiles, APK backups, logs, or Android
user data.
