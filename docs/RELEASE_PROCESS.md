# OpenADB release process

This is the maintainer checklist for producing a Windows release. A green
workflow is required, but it never substitutes for truthful physical Windows
and Android validation recorded against the
[device-lab checklist](DEVICE_LAB_MATRIX.md).

## Release invariants

- Build only a reviewed commit and publish only from its immutable
  `v<version>` tag. The release job must accept successful `Windows CI` evidence
  for that exact tag commit, never merely for the same branch.
- Never move, recreate, or reuse a published tag, and never replace its assets.
  Release-process improvements under `Unreleased` take effect only after a
  monotonically newer version and tag are created. A narrow metadata-only
  policy clarification to a historical release description is permitted only
  under the rules in section 11.
- `openadb/version.py` is the canonical source for the OpenADB version, release
  EXE name, ACBridge APK name/build/versionCode, package identity, and expected
  ACBridge signer digest. The public release certificate at
  `openadb/resources/acbridge/acbridge-release-cert.der` must have that exact
  SHA-256 digest. Python metadata, Windows resources, ACBridge source and
  manifest, bundled APKs, README, and [CHANGELOG.md](../CHANGELOG.md) must agree
  with it.
- Build ACBridge from reviewed source and sign it only with the permanent
  external release key for `io.github.communism420.openadb.acbridge`. Never
  create a new helper by renaming an older APK, silently change its installed
  signing identity, or downgrade a newer helper already installed on a device.
- The historical `com.communism420.acbridge` package and its publicly exposed
  development signer are retired and untrusted. Do not create a signing lineage
  from that key, use it for compatibility signatures, start its components,
  migrate its private data, or uninstall it automatically. Users remove it
  manually after the new helper works and regrant any SAF, All files access,
  Root, or Shizuku permission required by the new package.
- A Windows executable is signed only after `signtool verify /pa /all /v /tw`
  succeeds. An unsigned build always keeps the `-unsigned.exe` suffix.
- Checksums are calculated from the final bytes after signing or after the
  unsigned filename has been selected.
- Release assets and logs must not contain usernames or profile paths, device
  serials/nicknames, IP addresses, SSIDs, pairing codes, P2P secrets, private
  logs, private signing containers, passwords, or private keys. The tracked
  DER certificate is a public trust anchor, not secret key material.
- The repository root `LICENSE` must contain the unmodified GNU GPL version 3
  license text. The project's `GPL-3.0-or-later` choice must be stated in the
  README and third-party notice index, and the release delivery must include or
  clearly link to `THIRD_PARTY_NOTICES.md` and the complete `LICENSES/` texts
  required for every distributed component. The project GPL declaration must
  not be used to relicense third-party code, data, APKs, or upstream tools that
  retain separate licenses.
- Before release, compare the notice index and license texts with the actual
  EXE and APK payloads, including bundled UAD data, Material Symbols, ACBridge
  dependencies, Android Platform Tools binaries/libraries, and Platform Tools
  `NOTICE.txt`. A missing or mismatched notice is a release blocker.
- Release smoke tests are read-only. They must never flash, erase, format,
  sideload, unlock/lock a bootloader, wipe data, or mutate a real package.

## 1. Prepare an isolated environment

OpenADB supports CPython 3.10 through 3.14. Validate every supported version in
CI, but produce the Windows release only with a fresh CPython 3.12.10 x86-64
environment and the reviewed artifact hashes:

```powershell
py -3.12 -m venv .venv-dev
$devPython = '.\.venv-dev\Scripts\python.exe'
& $devPython -m pip install --disable-pip-version-check -r requirements-dev.txt
& $devPython -m pip check

py -3.12 -m venv .venv-release
$releasePython = '.\.venv-release\Scripts\python.exe'
$actualPython = (& $releasePython -c "import platform; print(platform.python_version())").Trim()
if ($actualPython -ne '3.12.10') { throw "Expected CPython 3.12.10, found $actualPython" }
& $releasePython -m pip install --disable-pip-version-check --no-cache-dir --require-hashes --no-deps --force-reinstall -r requirements-bootstrap-win-py312.lock
& $releasePython tools/verify_release_dependencies.py --phase bootstrap --lock requirements-bootstrap-win-py312.lock
& $releasePython -m pip install --disable-pip-version-check --no-cache-dir --require-hashes --no-deps --no-build-isolation --force-reinstall -r requirements-build-win-py312.lock
& $releasePython -m pip check
& $releasePython tools/verify_release_dependencies.py --phase build --lock requirements-build-win-py312.lock --bootstrap-lock requirements-bootstrap-win-py312.lock --requirements requirements-build.txt
```

Do not reuse `.venv-dev` for packaging: its unhashed tools and Ruff are
intentionally rejected as extra release distributions. Runtime, build, and
development requirements are intentionally separate. See
[DEPENDENCIES.md](DEPENDENCIES.md) before changing a pin; a dependency update
is a reviewed source change and must pass the complete Python matrix. The two
Windows lock files select the exact downloadable artifacts; never regenerate
or accept their hashes without comparing them with the upstream PyPI files.

Read the active version without copying it into commands by hand:

```powershell
$version = (& $releasePython -c "from openadb.version import VERSION; print(VERSION)").Trim()
$tag = "v$version"
$signedExe = "OpenADB-$version.exe"
$unsignedExe = "OpenADB-$version-unsigned.exe"
$bridgeApk = "openadb\resources\acbridge\ACBridge-$version.apk"
```

## 2. Audit version metadata

1. Update `openadb/version.py`, including a monotonically increasing ACBridge
   build and Android versionCode, the current package identity, and the pinned
   release-certificate SHA-256.
2. Update ACBridge `AndroidManifest.xml` and `BuildInfo.java` from the same
   values.
3. Update Windows metadata, README references, and the English release section
   in the canonical [CHANGELOG.md](../CHANGELOG.md). Do not maintain a second
   language-suffixed changelog.
4. Search tracked files for the previous active version and artifact names.
   Historical changelog entries and explicit legacy-package migration text are
   valid; active references to the retired package or signer are not.
5. Run `& $devPython -m unittest -q tests.test_version_metadata`.

The metadata test reads the current versionCode from `openadb/version.py` and
is the authority that all corresponding locations and bundled artifacts agree.

## 3. Build and verify ACBridge

ACBridge has one permanent Android release identity. The encrypted private key
must be maintained outside Git and backed up separately before its first public
release; only its public DER certificate is tracked. The protected GitHub
environment `acbridge-release` provides the release job with:

| Name | Kind | Purpose |
| --- | --- | --- |
| `ACBRIDGE_RELEASE_KEYSTORE_BASE64` | Secret | Base64 of the encrypted external release keystore |
| `ACBRIDGE_RELEASE_STORE_PASSWORD` | Secret | Keystore password |
| `ACBRIDGE_RELEASE_KEY_PASSWORD` | Secret | Private-key password |
| `ACBRIDGE_RELEASE_KEY_ALIAS` | Environment variable | Expected key alias |
| `ACBRIDGE_RELEASE_SIGNER_SHA256` | Environment variable | Expected public certificate SHA-256 |

The protected workflow decodes the Base64 value directly into its isolated
runner-temporary signing path and invokes `apksigner` without exposing that
path as a repository artifact. An authorized local release build instead sets
the transient `ACBRIDGE_RELEASE_KEYSTORE` variable to an external PKCS12 path,
plus the three password/alias variables above; the path must resolve outside
the repository.

Restrict this environment to the release workflow and required maintainer
approval. The signer variable must equal both `ACBRIDGE_SIGNER_SHA256` from
`openadb/version.py` and the SHA-256 of
`openadb/resources/acbridge/acbridge-release-cert.der`. Never place a keystore,
password, Base64 secret, or private-key bytes in a command line, tracked file,
artifact, cache, or log. A missing, partial, empty, malformed, or contradictory
signing configuration must fail closed rather than generate a replacement key.

Set `ANDROID_HOME` or `ANDROID_SDK_ROOT` to the reviewed Android SDK containing
the required platform and Build Tools. The protected workflow decodes the
keystore only into the isolated runner temporary directory, builds ACBridge
from the exact selected release tag, signs it, uploads only the versioned APK
plus sanitized status, removes the temporary keystore in an `always()` cleanup
step, and verifies the result again in a separate job. The main release
workflow calls this protected workflow first; its signing job remains bound to
the `acbridge-release` environment, so maintainer approval occurs before any
signing secret is available. It re-resolves the public tag through GitHub after
approval and again before final verification, failing if that tag no longer
identifies the workflow source commit. Artifact names include the exact source
SHA, release run ID, and run attempt so immutable GitHub artifact uploads cannot
collide during a rerun. The authoritative implementation is
`.github/workflows/acbridge-release.yml`. An authorized local recovery build
must use the same external key and verification rules; do not create a local
development key under the production package name.

The safe default produces an unsigned, non-publishable inspection build and is
not allowed to overwrite the bundled APKs:

```powershell
& $devPython tools/build_acbridge.py --signing-mode unsigned --output build/acbridge/ACBridge-inspection-unsigned.apk
```

Only after the external signing environment has been populated through an
approved secret source may a maintainer publish the byte-identical bundled
aliases:

```powershell
& $devPython tools/build_acbridge.py --signing-mode release
```

On the configured Windows maintainer host, the repository wrapper loads the
password from its per-user DPAPI-protected file, exposes it only through the
builder's temporary environment, and clears that environment afterward:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/build_acbridge_release.ps1
```

The wrapper's default PKCS12 and DPAPI paths are both under
`$HOME/.openadb-signing/`, outside the repository. Restrict their ACLs to the
maintainer account and Windows SYSTEM; neither file is a substitute for a
separate protected recovery backup.

The builder verifies pinned third-party inputs, compiles reviewed Java sources,
creates DEX, packages, aligns, and signs the APK. Verification must require the
expected package/version metadata, exactly one current signer matching the
pinned DER certificate, the required v1/v2/v3 schemes, exact embedded legal
files, and byte identity between both published APK names. Repeating the build
with identical source, SDK inputs, and signing identity must produce the same
APK SHA-256; generated ZIP entry metadata must not depend on wall-clock time.
Retain sanitized output from `aapt dump badging`, `zipalign -c -v 4`, and
`apksigner verify --verbose --print-certs` for `$bridgeApk`, then run:

```powershell
& $devPython -m unittest -q tests.test_version_metadata.VersionMetadataTests.test_bundled_apks_are_real_current_signed_builds
```

ACBridge itself is not a debuggable application. Its P2P and Shizuku control
entry points are shell/DUMP protected and use bounded, request-scoped IPC. The
Android signing identity is unrelated to Authenticode and must never be reused
for Windows signing. The public DER certificate may be committed and published
for verification; the corresponding private key and its containers may not.

## 4. Validate source and privacy

Run the same classes of checks as Windows CI:

```powershell
git diff --check
& $devPython -m compileall -q openadb tests tools
& $devPython -m ruff check openadb tests tools
& $devPython -m unittest discover -v
& $devPython -W error::ResourceWarning -m unittest -q tests.test_final_regressions tests.test_design_system tests.test_system_theme
$env:QT_QPA_PLATFORM = 'offscreen'
$testFiles = git ls-files 'tests/test_*.py' | Where-Object { $_ -match '^tests/test_[^/]+\.py$' } | Sort-Object
foreach ($testFile in $testFiles) {
  $module = ($testFile -replace '\.py$', '') -replace '[/\\]', '.'
  & $devPython -W error::ResourceWarning -m unittest -q $module
  if ($LASTEXITCODE -ne 0) { throw "Failed unittest module: $module" }
}
& $devPython tools/release_performance.py --environment-type physical --json-report release-performance.json
```

Use `virtual-machine` instead of `physical` when that is the measured host; do
not copy an evidence label blindly. Review screenshots and the CI privacy scan.
The privacy guard must scan tracked/unignored UTF-8 and UTF-16 content, reject
generated analysis databases and private-key containers, and be verified with
a disposable negative fixture that is removed immediately afterward. Do not
upload successful test logs; keep failure-log retention bounded.

## 5. Build and smoke-test the Windows EXE

Make the pinned Platform Tools input available and build from the already
verified release environment prepared in step 1:

```powershell
& $releasePython -m pip check
& $releasePython tools/verify_release_dependencies.py --phase build --lock requirements-build-win-py312.lock --bootstrap-lock requirements-bootstrap-win-py312.lock --requirements requirements-build.txt
& $releasePython -m PyInstaller --noconfirm --clean OpenADB.spec
```

The spec must bundle ADB, fastboot, required DLLs/notices, the current ACBridge
APK, UI resources, and Python packages. Before verified Authenticode signing,
rename the one-file intermediate to `$unsignedExe`; never publish it under
`$signedExe` and never commit large binaries.

The EXE and APK are separate delivered artifacts with separate provenance and
license obligations. Record which source commit and build inputs produced each
artifact, and ensure the release delivery provides the root GPL license,
readable third-party indexes, and the deterministic `LICENSES.zip` generated by
`tools/build_license_bundle.py`. Verify that archive independently with
`tools/verify_license_bundle.py` against the immutable checkout and the
checksum-verified Platform Tools `NOTICE.txt`. Do not describe either artifact
as SignPath-approved or SignPath-signed unless that fact is independently
verified from the release metadata and signature; a pending or planned
SignPath application is not approval.

The automated builder must verify both the trusted upstream archive digest and
the independently pinned SHA-256 before extracting Platform Tools. Its clean,
temporary-profile smoke test checks startup, exact title/version, bundled
tools and notices, ACBridge package/signer/APK metadata, clean shutdown, and
absence of a crash log. The Windows builder accepts only the independently
verified ACBridge artifact produced earlier in the same release run. It checks
the strict status schema, exact source commit/ref, package/version, signer,
signature schemes, and APK hash before replacing both bundled aliases;
PyInstaller therefore embeds those exact approved bytes. The uploaded unsigned
Windows bundle contains exactly one correctly named EXE, that same versioned
APK, `BUILD_STATUS.json`, legal delivery, and `SHA256SUMS.txt`. A separate
GitHub artifact eligible for SignPath contains exactly the one unsigned EXE and
is addressed only by its numeric artifact ID. Missing, malformed, or
contradictory build status is a failed build, not an unsigned success.
`BUILD_STATUS.json` must record CPython 3.12.10 and the SHA-256 of both release
lock files. Its ACBridge object must record the current package, versionName,
versionCode, APK filename and SHA-256, signer-certificate SHA-256, and verified
signature schemes, unsigned-source hash, source ref, and same-run approval
artifact/run identity. The first release-verification job downloads the independently verified
ACBridge artifact again and requires hash equality with both the Windows
artifact and its embedded metadata before accepting either artifact.

## 6. SignPath Authenticode signing

The retired PFX-in-CI path is prohibited. The repository contains no Windows
private key and the workflow has no local-signing fallback. Follow the complete
[SignPath setup and activation guide](SIGNPATH_SETUP.md); leave the repository
variable `SIGNPATH_ENABLED` absent or `false` until the Foundation application,
protected environment, manual approval policy, exact identifiers, and
certificate pins are all ready. The workflow additionally requires
`SIGNPATH_IDEMPOTENCY_CONFIRMED=true`, which may be set only after written
SignPath assurance that repeated submissions for the same repository, workflow
run, and immutable artifact ID are deduplicated server-side. A successful test
request or `disallow_reruns: true` is not equivalent to that guarantee.

The protected `signpath-release` environment contains only the submitter-only
`SIGNPATH_API_TOKEN` secret. Exact organization/project/policy/configuration
values and the approved certificate SHA-256/subject are environment variables.
The token is passed only as the `api-token` input of the official SignPath
action pinned to full commit
`c92b958760219087e01f8d67a1669ed57afe2627` (`v2.3`); it must never enter a
shell environment, file, artifact, status document, or log.
Keep `service-unavailable-timeout-in-seconds` positive. Zero disables the
underlying HTTP timeout and does not disable the action's automatic retries.

The SignPath job:

1. runs only on a GitHub-hosted runner after exact-tag Windows CI and the
   verified unsigned build succeed;
2. rejects workflow re-runs and re-resolves the annotated tag to the exact
   source commit;
3. verifies that the immutable numeric GitHub artifact ID belongs to this
   first-attempt run and contains exactly one unsigned EXE;
4. submits that artifact with the version parameter to the explicit artifact
   configuration;
5. waits for the mandatory SignPath approval; and
6. returns exactly one EXE or fails without an unsigned/PFX fallback.

Two separate read-only Windows jobs verify the response before publication.
Each requires a valid timestamped signature, Code Signing EKU, exact approved
certificate SHA-256 and subject, protected Windows version metadata,
`signtool verify /pa /all /v /tw`, same-run artifact/source provenance,
cross-checked request ID/URL and configured SignPath identifiers, and a
byte-exact comparison proving that only the standard Authenticode PE envelope
changed. The first assigns `$signedExe`, recomputes all hashes, and records the
SignPath request, action commit, input/result artifacts, unsigned/signed hashes,
leaf certificate, timestamp certificate, source ref/run, and verification
method in `BUILD_STATUS.json`. The second starts on a fresh runner, downloads
both the assembled bundle and original unsigned input by immutable numeric
artifact ID, and repeats the protected signature and payload checks. It neither
repacks nor substitutes the assembled artifact; the final publisher receives
that exact same artifact ID and SHA-256 archive digest after the fresh-runner
job succeeds.

The only job with `contents: write` runs no checkout, repository code, Python,
or third-party Action. Its single PowerShell step downloads that same verified
artifact directly, verifies its same-run origin, archive digest, exact file
allowlist, checksums, status, tag, and release absence, and then creates the
release. Manual cancellation stops the release-verification and publication
chain.

When SignPath is disabled, automation produces `$unsignedExe` with
`"signed": false` and `"provider": "none"`. An automatic tag run may create
only a clearly labelled draft/prerelease preview. No workflow input can promote
an unsigned build to a stable release. An enabled request that is rejected,
cancelled, timed out, or malformed stops publication, and only the primary push
run of a new immutable tag may create that request. Never falsify status
metadata or rename an unsigned file as signed.

## 7. Verify checksums and build metadata

After the final filename is fixed, recompute SHA-256 and require agreement
between the EXE, `BUILD_STATUS.json`, and the builder's `SHA256SUMS.txt`. The
builder checksum list already covers the EXE, versioned ACBridge APK, legal
delivery, and sanitized status inputs; the publication job recomputes the final
list after its independent checks. The status file must also identify the pinned
Platform Tools input hashes. Independently verify that the released ACBridge
APK hash, package, signer certificate, and v1/v2/v3 results agree with the
tagged public DER certificate, the same-run protected artifact, and the
ACBridge object in `BUILD_STATUS.json`.

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

For the permanent ACBridge identity cutover, additionally retain physical
evidence for Android 6–8, 9–12, and 13 or later, including a phone and Android
TV where available. Confirm that the retired package is never launched or
removed automatically, the new package installs independently, permissions are
requested again, every ACBridge feature uses the verified new package, and a
subsequent APK signed by the same permanent key updates in place. Missing
hardware evidence must be disclosed; it must not be inferred from a unit test.

## 9. Tag and publish

Require reviewed changes, green branch CI, reviewed device-lab evidence, and a
clean worktree. Create an annotated tag at the exact commit:

```powershell
git status --short
git tag -a $tag -m "OpenADB $version"
git show --no-patch --decorate $tag
git push origin $tag
```

The tag starts CI and the release pipeline. The pipeline first enters the
approval-gated ACBridge environment, then builds/signs/verifies ACBridge and
passes that exact same-run artifact to the reusable unsigned Windows builder.
Only after successful exact-tag CI may the protected SignPath job submit the
one-file EXE artifact. Two read-only Windows jobs revalidate the strict
build-status schema, artifact origins, tag, hashes, PE payload, timestamp and
Authenticode on independent runners. The fresh-runner gate authorizes the same
immutable, digest-bound artifact assembled by the first job without repacking
it. The minimal write-scoped publisher verifies that bundle again before
publishing the same approved APK bytes with the final EXE.
Release notes
come from the matching section of
[CHANGELOG.md](../CHANGELOG.md) and disclose signed/unsigned state, hashes,
Platform Tools and ACBridge metadata, exact-tag CI, hardware/security
limitations, privacy-gate result, and the license/notice delivery. They must
identify the exact source/provenance relationship for the EXE and APK and must
not imply that SignPath has approved the project before approval is actually
recorded. Every rendered download/release page must contain the literal term
`Code signing policy` linked to the maintained policy, the required `Free code
signing provided by SignPath.io, certificate by SignPath Foundation`
attribution with a truthful status qualifier, and an absolute link to the
privacy policy. Generated release notes must pin the policy and privacy links
to the exact source commit and must name or link the Authors,
committers/reviewers, and approver roles maintained in the policy.

The asset allowlist is one EXE, the versioned ACBridge APK,
`BUILD_STATUS.json`, `SHA256SUMS.txt`, the root GPL license, and the required
third-party notice/license files (or a documented archive containing exactly
those notices). Never publish PFX/key material, passwords, crash logs,
temporary profiles, or raw test logs.

## 10. Post-release verification

1. Download every asset into a new empty directory and verify every checksum.
2. When signed status is claimed, verify the downloaded EXE with `signtool`.
3. Compare build status and APK metadata with the release notes; independently
   verify the APK signer against the tagged public DER certificate.
4. Verify that the downloaded license and notice files match the actual EXE/APK
   payload and identify all required upstream license texts.
5. Start and close the EXE with a new profile on physical Windows 10 and 11;
   record DPI, theme, and device results separately.
6. Confirm every unsigned preview remains draft/prerelease; no workflow input
   may promote it to a stable release.
7. Inspect the release and workflow artifacts for private data or secrets, and
   confirm no release note claims SignPath approval that has not occurred.
8. Inspect the rendered GitHub release page and confirm that `Code signing
   policy`, its exact-commit link, the required SignPath attribution and its
   truthful status qualifier, the project roles, and the absolute privacy link
   are visible and resolve successfully.

Announce the release only after these checks pass.

## 11. Rollback and incident handling

Never silently move a published tag to different bytes. Before publication,
leave or convert a failed release to draft, remove faulty assets, fix the
source, and rerun validation. Delete an unannounced tag only with a recorded
reason; otherwise publish a monotonically newer patch release.

A metadata-only clarification may be added to a published release description
to correct or complete a public policy disclosure. It must preserve the
historical signed/unsigned state, source commit, title, tag, asset set,
checksums, and provenance claims, and the correction must be recorded in the
current changelog. If the historical tag predates the required policy file, the
description must disclose that fact and link an immutable commit containing
the maintained policy rather than a mutable branch URL. It is never a
substitute for fixing the next release from a new reviewed commit.

For a defective published release, stop distribution or add a prominent
warning, preserve checksums/workflow URLs/status/failure evidence, remove only
compromised downloadable assets, and fix on a new reviewed commit. Revoke and
rotate any exposed signing material. If ACBridge identity or integrity is in
doubt, stop helper distribution and investigate; never auto-uninstall it from
user devices. Never reactivate the retired development key or use it to create
a lineage; recover with the protected permanent key or move to a newly reviewed
package identity and require explicit permission grants again.

Local rollback must not use destructive Git commands on a dirty worktree.
Release rollback never deletes OpenADB profiles, APK backups, logs, or Android
user data.
