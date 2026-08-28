# SignPath release-signing setup

OpenADB's SignPath Foundation application is pending. The repository-side
integration is deliberately disabled until SignPath approves the project and
issues the real organization, project, policy, artifact-configuration, and
certificate values. No current OpenADB artifact is SignPath-signed.

The public `v3.1.0` tag is now protected by the active no-bypass release-tag
ruleset. Its already-published release predates GitHub Release Immutability and
its assets were not made immutable retroactively. Do not move, rebuild,
replace, or re-sign either historical artifact. The first live SignPath request
must be for a monotonically newer release tag whose tagged source already
contains this workflow.

## Current activation status

Repository-side policy, artifact metadata restrictions, legal-source bundling,
release-tag protection, and the fail-closed signing path are implemented. The
repository also requires every external GitHub Action to be pinned to a full
commit SHA. Signing must nevertheless remain disabled until every external gate
below is complete:

| Gate | Current state | Required evidence |
| --- | --- | --- |
| Current source CI | Verified baseline | Commit [`8800a0e4bcf919366860702cef2ea0cb62ca6160`](https://github.com/communism420/OpenADB/commit/8800a0e4bcf919366860702cef2ea0cb62ca6160) passed the exact-commit [Windows CI run 33192889229](https://github.com/communism420/OpenADB/actions/runs/33192889229) on attempt 2; the eventual release commit must have its own exact-commit green run |
| GitHub Actions source policy | Ready live | `allowed_actions=selected`, full-SHA enforcement enabled, blanket GitHub/verified access disabled, and the live allowlist exactly matches [the six reviewed action SHAs](../.github/actions-allowlist.json) |
| Default-branch history | Ready live | The active no-bypass [`Protected OpenADB main history`](../.github/rulesets/protected-main-history.json) ruleset prevents deletion and non-fast-forward updates without blocking ordinary fast-forward maintainer pushes |
| Repository security intake | Ready live | Dependabot security updates/fixes and GitHub private vulnerability reporting are enabled; reports follow [`SECURITY.md`](../SECURITY.md) |
| Protected `signpath-release` environment | Partially ready | Reviewer and `v*` tag policy exist; disable administrator bypass in the GitHub UI, then add protected values only after approval |
| SignPath Foundation application | Pending | Project is approved and active |
| Released-form eligibility | Pending SignPath decision | Obtain written confirmation whether public `v3.1.0` is sufficient. If not, stop and implement a separately reviewed bootstrap-publication path; the current disabled workflow creates only a private draft. Never modify `v3.1.0` |
| SignPath project and GitHub.com trusted build system | Not provisioned | Exact organization ID, project/policy/configuration slugs, certificate pins, and origin policy issued by SignPath |
| Signing credentials | Intentionally absent | Submitter-only token stored only in the `signpath-release` environment |
| Submission idempotency | Unconfirmed | Written SignPath assurance for the pinned action/API behavior, recorded against its exact commit in `SIGNPATH_IDEMPOTENCY_REVIEWED_ACTION_SHA` |
| Human controls | Must be verified by the maintainer | MFA on GitHub and SignPath, a named Approver, and manual approval for every request |

Do not use the historical locally built `OpenADB-3.1.0.exe` as activation
evidence: it is unsigned and predates the current release/legal pipeline. A
future unsigned artifact must retain the `-unsigned` filename suffix until a
verified Authenticode signature has been returned by SignPath.

## Trust boundary

The release workflow builds and verifies an unsigned Windows bundle without
access to SignPath credentials. It also uploads a separate GitHub Actions
artifact containing exactly one file:

```text
OpenADB-<version>-unsigned.exe
```

The protected signing job starts only after exact-tag Windows CI succeeds. It
passes the immutable numeric `artifact-id` of that one-file artifact to the
official SignPath GitHub action pinned at commit
`c92b958760219087e01f8d67a1669ed57afe2627` (`v2.3`). The job waits for the
mandatory SignPath approval and accepts exactly one returned EXE. APKs, legal
files, settings, logs, Android data, PFX files, and private keys are never part
of the signing request.

Push and manual runs for the same tag share one concurrency group. The signing
job independently rejects every GitHub workflow re-run, and immediately before
submission it resolves the tag again and proves that no GitHub Release already
exists. This prevents duplicate or stale signing requests from the normal
GitHub workflow retry paths. It does not, by itself, prove that an HTTP retry
inside the pinned SignPath action cannot create a second request after an
ambiguous network response. The action's service-unavailable timeout must stay
positive: setting it to zero disables the HTTP timeout, not its retry policy.
For that reason the workflow also refuses to enable signing unless the separate
`SIGNPATH_IDEMPOTENCY_CONFIRMED` repository variable is `true` and
`SIGNPATH_IDEMPOTENCY_REVIEWED_ACTION_SHA` exactly equals the checked-in action
commit. Updating the action therefore invalidates the previous review without
depending on a maintainer remembering to reset a boolean.

Two separate read-only Windows jobs then assemble and independently re-check
the publication bundle on fresh runners. Together they require all of the
following:

- the public release tag still resolves to the exact source commit;
- the input and result artifact IDs belong to the same first-attempt workflow;
- the signing request ID and HTTPS SignPath URL are present;
- `Get-AuthenticodeSignature` reports `Valid` and a timestamp certificate;
- the leaf certificate's exact SHA-256 and subject match protected values;
- the certificate contains the Code Signing EKU;
- `signtool verify /pa /all /v /tw` succeeds in each Windows verification job;
- PE product/version metadata matches the tagged source; and
- only the checksum, Security Directory, alignment padding, and Authenticode
  certificate table differ from the verified unsigned PE. The parser accepts
  only one canonical DER signature record plus zero alignment padding, so
  appended executable or certificate-table data is rejected.

Only after those checks does the workflow use the stable
`OpenADB-<version>.exe` name, regenerate checksums, and write detailed action,
request, artifact, source, certificate, timestamp, and verification metadata to
`BUILD_STATUS.json`. The second Windows job verifies the exact same immutable
artifact ID and SHA-256 archive digest emitted by the first job; it does not
repack or substitute those files. A final minimal publisher has
`contents: write`, runs no checkout, repository code, Python, or third-party
Actions, downloads that same artifact, and verifies its ID, origin, digest,
file allowlist, checksums, tag, and release absence before calling GitHub's
release API. All build and signature jobs remain read-only. An enabled
signing failure has no PFX or unsigned fallback. When signing is disabled, only
a clearly named unsigned draft/prerelease is permitted; there is no stable
unsigned override. Cancellation prevents both verification jobs and the
publisher from advancing.

## 1. Configure SignPath after approval

Use only values supplied by SignPath's generated CI integration snippet; do not
guess slugs or identifiers.

1. Add the predefined **GitHub.com** Trusted Build System to the SignPath
   organization and link it to the OpenADB project.
2. Install the SignPath GitHub App for `communism420/OpenADB` if required by the
   selected origin/source policy.
3. Create or import an Artifact Configuration from
   [`.signpath/artifact-configuration.xml`](../.signpath/artifact-configuration.xml).
   GitHub stores `upload-artifact` payloads as ZIP archives, so `<zip-file>` must
   remain the root element. Keep a stable, explicit slug.
4. Create a release Signing Policy that uses that configuration, the Foundation
   certificate, GitHub origin verification, and a manual approval process for
   **every** request. Configure `disallow_reruns: true` when the assigned plan
   exposes GitHub build policies.
5. Give a dedicated CI identity only the OpenADB `Submitter` role for that
   policy. It must not be an Approver, Configurator, or organization
   administrator. Keep the interactive Approver account separate and protect it
   with MFA.

The GitHub environment approval is an additional control; it does not replace
SignPath's mandatory approval.

Before activation, obtain written SignPath confirmation that repeated
submission POSTs from the pinned action for the same repository, workflow run,
and immutable artifact ID are deduplicated server-side. If SignPath cannot
provide that guarantee, keep signing disabled until an official action release
offers a documented idempotency key or a supported no-retry submission mode.
`disallow_reruns: true` blocks GitHub workflow re-runs but is not evidence of
HTTP-request deduplication.

### SignPath administrator handoff template

Use the following checklist when SignPath responds. Ask for exact OpenADB
values and written policy decisions; do not fill gaps from memory. A support
ticket or account export may contain account information, so keep the full
response outside Git and record only the reviewed non-secret values and a
stable evidence reference in the release record.

Questions for SignPath:

1. Is the Foundation application approved, and is the OpenADB project active?
2. What are the exact organization ID, project slug, Artifact Configuration
   slug, and release Signing Policy slug for OpenADB?
3. Is the predefined GitHub.com Trusted Build System linked to the project,
   and must the SignPath GitHub App be installed for
   `communism420/OpenADB`?
4. Which Foundation leaf certificate is assigned to the release policy? Supply
   its exact subject and SHA-256 fingerprint through an authenticated SignPath
   channel.
5. Does the policy require a manual approval for every request, and can the CI
   identity be restricted to the Submitter role without Approver,
   Configurator, or organization-administrator privileges?
6. Does the assigned plan support a GitHub build policy with
   `require_github_hosted: true` and `disallow_reruns: true`? If so, provide
   the exact project and policy slugs required for the policy-file path.
7. Are repeated submission POSTs from the pinned GitHub action deduplicated
   server-side for the same repository, workflow run, and immutable numeric
   artifact ID? Request a written answer that identifies the covered action or
   API behavior.
8. Does the existing public unsigned `v3.1.0` executable satisfy the
   Foundation requirement that the project already be released in the form to
   be signed, or is a newer public unsigned prerelease from the current legal
   and permanent-ACBridge pipeline required first?
9. Does SignPath accept the bundled official Microsoft Visual C++ Runtime as a
   System Library in this one-file application? It is separately disclosed and
   is not presented as OpenADB-owned or open-source code.
10. Are any additional restrictions required because OpenADB operates only
    through user-enabled Android debugging, pre-existing Root access, or a
    separately installed and user-authorized Shizuku service?

Concise project response template:

```text
Repository: https://github.com/communism420/OpenADB
Download page: https://github.com/communism420/OpenADB#downloads
Code signing policy: https://github.com/communism420/OpenADB#code-signing-policy
Privacy policy: https://github.com/communism420/OpenADB/blob/main/PRIVACY.md
License: GPL-3.0-or-later for OpenADB original code; third-party components
retain their documented licenses.

Build boundary: GitHub-hosted release workflow -> immutable numeric Actions
artifact containing exactly one OpenADB-<version>-unsigned.exe -> SignPath.
The Artifact Configuration signs only that outer PE and pins product, version,
company, copyright, and original-filename metadata. APKs, user data, logs,
keys, and other release files are excluded from the request.

Safety boundary: OpenADB does not obtain Root access, install or start
Shizuku, exploit vulnerabilities, bypass Android permission prompts, or scan
for insecure services. ADB must be enabled and authorized by the user. Root
and Shizuku routes work only after the user has separately provided those
capabilities. Destructive device operations remain warning-gated.

Please provide the exact OpenADB identifiers and certificate pins, confirm the
manual-approval and least-privilege roles, answer the same-submission
deduplication question, and confirm the released-form and Microsoft runtime
eligibility points above.
```

## 2. Configure the protected GitHub environment

Create an environment named exactly `signpath-release` and restrict deployment
to reviewed release tags. Add required reviewers appropriate to the current
maintainer team. If GitHub cannot provide separation of duties for a
single-maintainer repository, do not describe that environment gate as an
independent second person; the mandatory SignPath approval still applies.
Where the repository plan and UI allow it, disable administrator bypass for
this environment.

### Protected-value map and activation order

The current safe state is shown below. `Absent` is intentional while the
application is pending. Never create example values in GitHub, commit a token,
or copy an identifier from another SignPath project. Populate each row only
from the approved OpenADB project or a reviewed maintainer decision, in the
listed order.

| Order | Name | GitHub scope and kind | Required format | Authoritative source | Current safe state |
| ---: | --- | --- | --- | --- | --- |
| 1 | `SIGNPATH_ORGANIZATION_ID` | `signpath-release` environment variable | Non-zero UUID in canonical `D` form | Organization ID supplied by SignPath | Absent |
| 2 | `SIGNPATH_PROJECT_SLUG` | `signpath-release` environment variable | `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` | Approved OpenADB project | Absent |
| 3 | `SIGNPATH_ARTIFACT_CONFIGURATION_SLUG` | `signpath-release` environment variable | Same slug expression | Imported one-EXE Artifact Configuration | Absent |
| 4 | `SIGNPATH_SIGNING_POLICY_SLUG` | `signpath-release` environment variable | Same slug expression | Manual-approval release Signing Policy | Absent |
| 5 | `SIGNPATH_CERTIFICATE_SHA256` | `signpath-release` environment variable | Exactly 64 lowercase hexadecimal characters | Approved leaf certificate exported or identified by SignPath | Absent |
| 5 | `SIGNPATH_CERTIFICATE_SUBJECT` | `signpath-release` environment variable | Exact non-empty subject, at most 512 characters, with no CR/LF | Same approved leaf certificate | Absent |
| 6 | `SIGNPATH_API_TOKEN` | `signpath-release` environment secret | Opaque value; never validate, print, or persist outside the protected secret store | Dedicated Submitter-only CI identity | Absent |
| 7 | `SIGNPATH_RELEASED_FORM_ACCEPTED_TAG` | Repository variable | Exact public version tag such as `v3.1.0`; never a placeholder | Written SignPath decision identifying the public release accepted as released-form evidence | Absent |
| 8 | `SIGNPATH_IDEMPOTENCY_REVIEWED_ACTION_SHA` | Repository variable | Exact 40-character lowercase commit SHA | Full commit of the SignPath action/API behavior covered by written assurance | Absent |
| 9 | `SIGNPATH_IDEMPOTENCY_CONFIRMED` | Repository variable | Exactly `false` or `true` | Maintainer decision backed by written SignPath assurance for the exact SHA in row 8 | `false` |
| 10 | `SIGNPATH_ENABLED` | Repository variable | Exactly `false` or `true` | Maintainer activation decision after every preceding gate passes | `false` |

The two certificate rows share one order because they must be captured and
reviewed together. The environment values are configuration rather than
secrets, but they are still protected release inputs and must not be copied
from an unrelated project. The four repository-side activation controls
deliberately remain outside the environment because the initial workflow graph
reads them before the protected signing job starts. In preapproval state the
reviewed-action SHA remains absent; it is not an example or placeholder value.

| Audit mode | `SIGNPATH_ENABLED` | `SIGNPATH_IDEMPOTENCY_CONFIRMED` | Accepted release tag | Reviewed action SHA | Protected environment |
| --- | --- | --- | --- | --- | --- |
| `preapproval` | `false` | `false` | Absent; decision pending | Absent | SignPath values and token absent |
| `activation` | `false` | `true` | Exact public tag accepted in writing | Exact pinned SHA | Complete |
| `active` | `true` | `true` | Exact public tag accepted in writing | Exact pinned SHA | Complete |

Run `preapproval` in the current state. After approval, provision rows 1–6,
record the accepted public release tag, set the reviewed SHA, and set
confirmation to `true`. Row 10 is forbidden until
`--mode activation` exits `0`. Then enable signing last and require
`--mode active` to exit `0` immediately afterward.

The API token appears only as the `api-token` input of the pinned SignPath
action. It must never be copied into a shell environment, file, artifact,
release status, or log.

## 3. Verify repository release protections

The active repository ruleset is named exactly `Immutable OpenADB release
tags`. Its reviewed import/API payload is checked in at
[`.github/rulesets/immutable-release-tags.json`](../.github/rulesets/immutable-release-tags.json).
It protects every published legacy release tag and future `v*` tags with
`update`, `deletion`, and `non_fast_forward`; it has no bypass actors and no
`creation` rule. New release tags can therefore be created, but a matching tag
cannot be moved, force-updated, or deleted after it reaches GitHub.

The release workflow reads the live ruleset through GitHub's API before the
build, immediately before any SignPath request, and again immediately before
publication. A missing, disabled, duplicated, bypassable, or otherwise drifted
ruleset stops the workflow. GitHub deliberately hides `bypass_actors` from a
read-only workflow token, so the workflow also pins the exact numeric ruleset
ID and its server-controlled `updated_at` revision. Any administrative edit,
including adding a hidden bypass actor or deleting and recreating the ruleset,
therefore requires an explicit reviewed source update before another release.
Do not add an administrator, maintainer, GitHub Actions, or SignPath bypass to
make a failed release reusable; fix the source and create a monotonically newer
version instead.

GitHub Release Immutability is also enabled for the repository. GitHub applies
it only to future releases: it locks a published release's assets and tag and
creates release-attestation evidence, but drafts remain editable. The stable
publisher uses `gh release create --verify-tag` with all assets, then reads the
release back and requires the exact expected stable state with `immutable:
true`, even when the create command itself returns an ambiguous error. The
remote title and body must match exactly, and every asset must match the
approved allowlist by unique name, byte size, GitHub-reported SHA-256 digest,
and uploaded state. If that identity and state cannot be proven, the publisher
attempts to withdraw the release and reads it back again. The workflow treats
the recovery as successful only after it verifies a mutable draft; if
withdrawal or readback cannot be proven, it fails with an explicit
security-incident warning that requires immediate manual review. This setting
complements the ruleset because it does not protect the interval between tag
creation and publication, and it does not retrofit `v3.1.0`.

Audit both repository controls before activation and before every release:

```powershell
gh api repos/communism420/OpenADB/rulesets
gh api repos/communism420/OpenADB/immutable-releases
gh api repos/communism420/OpenADB/actions/permissions
gh api repos/communism420/OpenADB/actions/permissions/selected-actions
gh api repos/communism420/OpenADB/actions/permissions/fork-pr-contributor-approval
gh api repos/communism420/OpenADB/private-vulnerability-reporting
gh api repos/communism420/OpenADB/automated-security-fixes
```

Do not treat `git push --dry-run` as proof that server-side rules are active;
compare the live ruleset with the checked-in JSON and require
`"enabled": true` from the immutable-releases endpoint. The Actions permissions
response must report `"sha_pinning_required": true`; GitHub documents this as
the repository-level enforcement for full-length action commit pins. It must
also report `"allowed_actions": "selected"`. Both blanket GitHub-owned and
verified-creator categories must be false, and `patterns_allowed` must exactly
match [`.github/actions-allowlist.json`](../.github/actions-allowlist.json).
This means that updating even an official Action requires a reviewed workflow
SHA update and a matching live allowlist update. Fork policy must require
approval for `all_external_contributors`.

The separate active no-bypass default-branch ruleset is checked in at
[`.github/rulesets/protected-main-history.json`](../.github/rulesets/protected-main-history.json).
It prevents deletion and non-fast-forward updates of the default branch. It
intentionally does not contain an `update`, `creation`, pull-request, or status
check rule, because this single-maintainer repository still permits normal
fast-forward pushes. Do not describe it as mandatory-PR protection. The release
workflow verifies this exact live ruleset before build, before SignPath, and
before publication; it also peels the release tag and proves its commit remains
reachable from the exact current default-branch head at each security boundary.

For a sanitized consolidated check, run:

```powershell
python tools/audit_signpath_readiness.py --mode preapproval
```

Exit code `0` means every checked gate for that lifecycle mode passed; `1`
means at least one actionable check failed; `2` means there are no failures but
at least one deliberately pending external or future-release gate. Add
`--offline` to inspect only the checked-in policy, or use `--json-report <path>`
for a sanitized machine-readable report. After SignPath provisioning and the
written idempotency review, leave `SIGNPATH_ENABLED=false`, record the reviewed
action SHA and confirmation, and run `--mode activation`. The audit reads secret
names but never secret values. Set `SIGNPATH_ENABLED=true` only after that mode
passes, then run `--mode active` to verify the final state.

## 4. Activate last

Keep the repository variable `SIGNPATH_ENABLED=false` while
the application is pending or any protected value is incomplete. After the
SignPath project, policy, approver, GitHub environment, certificate pins,
release tag protections, and written same-submission deduplication guarantee
have all been reviewed, set the exact reviewed action SHA first and confirm the
review while signing remains disabled:

```text
SIGNPATH_RELEASED_FORM_ACCEPTED_TAG=<exact public tag accepted in writing>
SIGNPATH_IDEMPOTENCY_REVIEWED_ACTION_SHA=c92b958760219087e01f8d67a1669ed57afe2627
SIGNPATH_IDEMPOTENCY_CONFIRMED=true
SIGNPATH_ENABLED=false
```

Run `python tools/audit_signpath_readiness.py --mode activation`. Only after it
passes may `SIGNPATH_ENABLED` be changed to `true`; immediately run the same
auditor with `--mode active`. This makes activation a separately verifiable
state instead of changing all four repository-side trust controls at once.

Set `SIGNPATH_IDEMPOTENCY_CONFIRMED` back to `false` and remove
`SIGNPATH_IDEMPOTENCY_REVIEWED_ACTION_SHA` before changing the pinned SignPath
action or its submission API behavior. Repeat the written review and record the
new literal action commit before re-enabling it. Never set either review control
merely because a test request happened to succeed once.

This switch is intentionally outside the protected environment because the
initial release graph must decide whether a signing job is required. Any value
other than absent, `false`, or `true` fails validation. When enabled, workflow
re-runs and manual dispatches are rejected before submission; only the primary
new-tag push may create a request. A rejected, cancelled, timed-out, or malformed
SignPath response prevents publication. Recovery requires review and a new
monotonically higher release tag rather than resubmitting the same bytes.

The official action uses bounded automatic retries for selected network and
service-unavailable failures. An approver must compare repository, GitHub run
ID, immutable input artifact ID, source commit, and policy before approval. If
more than one request represents the same run/artifact pair, reject all of
them, stop the release, inspect the SignPath queue, and do not re-run or reuse
the tag. This operational check is defense in depth; it does not replace the
activation requirement for documented server-side deduplication.

## 5. Certificate rotation

For a legitimate certificate renewal, verify the new certificate through the
SignPath project and Foundation account first. Set `SIGNPATH_ENABLED=false`
first, cancel active GitHub runs and outstanding SignPath requests, then update
`SIGNPATH_CERTIFICATE_SHA256` and, if it changed legitimately,
`SIGNPATH_CERTIFICATE_SUBJECT` through a reviewed configuration change before
creating the new release tag. Run the `activation` audit, enable signing last,
and immediately run the `active` audit. Never weaken the pin or accept any
merely valid Windows publisher certificate.

## 6. Pre-release checklist

- SignPath application approved and project state active.
- Submitter token is least-privilege, and MFA is enabled for every GitHub and
  SignPath role holder.
- Every signing request requires explicit SignPath approval.
- GitHub.com origin verification and the exact repository are selected.
- Artifact Configuration is semantically equivalent to the checked-in XML and
  has the reviewed explicit slug.
- Protected environment and all six non-secret values are present.
- The live `Immutable OpenADB release tags` ruleset exactly matches the
  checked-in no-bypass policy, and GitHub Release Immutability is enabled.
- Written SignPath assurance covers server-side deduplication of repeated POSTs
  for the same repository, workflow run, and immutable artifact ID; the pinned
  action/API combination has not changed since that review.
- `SIGNPATH_IDEMPOTENCY_REVIEWED_ACTION_SHA` exactly equals the literal action
  commit in the release workflow.
- `SIGNPATH_RELEASED_FORM_ACCEPTED_TAG` identifies the exact existing public
  release accepted in SignPath's written eligibility decision.
- `SIGNPATH_IDEMPOTENCY_CONFIRMED=true` was set only after that assurance was
  reviewed.
- `SIGNPATH_ENABLED=true` was set only after the checks above.
- The new tag is immutable, new, and points at the reviewed release commit.
- Exact-tag CI is green; physical device evidence is reviewed separately and is
  not placed in the SignPath upstream dependency chain.
- No GitHub Release already exists for the tag.
- The approver sees exactly one request for the run/artifact pair and has
  checked the repository, run ID, artifact ID, commit, and policy.

## 7. Manual approval runbook

The SignPath Approver uses an interactive MFA-protected account, not the CI
Submitter token. GitHub environment review and SignPath approval are separate
decisions. Before approving a request:

1. Open the GitHub run from the immutable release tag, not from a link copied
   from an untrusted comment or log. Require run attempt `1` and confirm the
   repository is exactly `communism420/OpenADB`.
2. Resolve the annotated tag and confirm it names the reviewed source commit.
   The tag must be monotonically newer than every published OpenADB tag, and no
   GitHub Release may already exist for it.
3. Require the exact-tag Windows CI, ACBridge protected build, unsigned Windows
   build, and `wait-for-ci` gate to be successful before the SignPath job.
4. Compare the SignPath request with the GitHub run: organization, project,
   release Signing Policy, Artifact Configuration, source commit, version,
   workflow run ID, immutable numeric input artifact ID, and artifact name.
5. Confirm that the input artifact contains exactly the expected non-empty
   `OpenADB-<version>-unsigned.exe` and that its SHA-256 matches the verified
   Windows-build output. Never approve an APK, ZIP of release assets, or a
   stable-name EXE as the signing input.
6. Search the SignPath queue for the same repository, run ID, and artifact ID.
   There must be exactly one request. Reject every duplicate rather than
   choosing one arbitrarily.
7. Approve only when all values agree and no security or release incident is
   open. Record the decision time, approver account, request ID and HTTPS URL,
   GitHub run URL, tag, commit, artifact ID/name/SHA-256, project, policy, and
   configuration. Do not record the API token.
8. After approval, watch both independent Windows verification jobs and the
   minimal publisher. Approval is not success: a certificate, timestamp,
   payload, provenance, checksum, or publication mismatch must still stop the
   release.

Reject the request and start the incident procedure below if any identity is
missing, a workflow re-run created the request, the source or policy is
unexpected, the input has more than one file, a duplicate request exists, or
the release/tag state cannot be proven. Never re-run a failed signing workflow
or reuse its tag.

## 8. First signed release checklist

The checked-in release workflow is intentionally version-bound to historical
`v3.1.0`, which already has a GitHub Release. It cannot be used for the first
signed release. Before creating another tag:

1. Obtain SignPath's written decision on whether public `v3.1.0` satisfies the
   released-form eligibility requirement. If it does not, stop and design a
   separately reviewed bootstrap-publication path. Do not activate signing,
   manually publish an unsigned stable release, or assume the current disabled
   workflow supplies public evidence: it creates only a private draft.
2. Select a monotonically higher OpenADB version. Update the canonical version,
   ACBridge version/build identity, changelog, metadata, Artifact Configuration
   parameter expectations, and every version-bound release-workflow check in
   one reviewed source change. Do not make the workflow accept arbitrary tags
   merely to avoid this review.
3. Run the full CI and release-validation suite on the prospective commit.
   Verify the permanent ACBridge signer, deterministic legal bundle, exact
   Platform Tools inputs, privacy gate, unsigned Authenticode boundary, and
   clean packaged-EXE smoke test.
4. Populate only the six protected environment values and the Submitter token.
   Keep the reviewed action SHA absent and both repository flags `false`.
   Confirm the GitHub.com Trusted Build System, exact repository origin,
   dedicated Submitter, separate interactive Approver, MFA, and manual approval
   for every request.
5. Re-audit the live no-bypass tag ruleset, GitHub Release Immutability, selected
   Actions policy, full-SHA enforcement, `signpath-release` reviewers and `v*`
   tag policy, and disabled administrator bypass.
6. Record the exact public tag identified in SignPath's written released-form
   decision as `SIGNPATH_RELEASED_FORM_ACCEPTED_TAG`. Review the written
   same-submission deduplication assurance against the still-pinned action/API
   behavior. Set the exact reviewed action SHA, then
   `SIGNPATH_IDEMPOTENCY_CONFIRMED=true`, while signing remains disabled.
   Require the `activation` audit to exit `0`; set `SIGNPATH_ENABLED=true` last
   and immediately require the `active` audit to exit `0`.
7. Inspect the final clean worktree, annotated tag name, annotation, and target
   commit before the irreversible tag push. Never move, delete, or reuse the
   tag after it reaches GitHub.
8. Follow the manual approval runbook exactly once. If the request fails,
   times out, is rejected, or appears more than once, stop and prepare a higher
   patch version rather than re-running it.
9. After publication, download every asset into a clean directory; verify
   checksums, `BUILD_STATUS.json`, Authenticode, timestamp, pinned leaf
   certificate, PE metadata, ACBridge signer, legal bundle, exact source tag,
   and GitHub release immutability. Announce the release only after all checks
   pass.

## 9. SignPath incident response

Changing a repository variable does not stop a workflow whose `prepare` job
already captured `SIGNPATH_ENABLED=true`. For any suspected signing incident,
perform both immediate controls:

```powershell
gh variable set SIGNPATH_ENABLED --body false --repo communism420/OpenADB
gh run cancel <run-id> --repo communism420/OpenADB
```

Then reject or cancel the corresponding request in SignPath, if it exists.
Preserve the request ID/URL, workflow run and attempt, tag/commit, artifact
IDs/names/hashes, policy/configuration, timestamps, sanitized logs, signature
details, release state, and every action taken. Never preserve the API token in
the incident record. Do not delete or move the protected tag; recovery uses a
reviewed, monotonically higher version.

| Trigger | Immediate containment | Required recovery |
| --- | --- | --- |
| More than one request for the same run/artifact | Disable signing, cancel the GitHub run, and reject every duplicate | Ask SignPath to investigate deduplication; keep `SIGNPATH_IDEMPOTENCY_CONFIRMED=false` until written assurance covers the exact action/API behavior |
| Unexpected repository, tag, commit, run attempt, artifact, version, project, policy, or configuration | Reject the request and freeze the release | Investigate repository, workflow, account, and protected-value changes; rotate affected credentials and release only from a new reviewed commit/tag |
| Ambiguous submission result, HTTP timeout, or lost response | Cancel the run and inspect SignPath before any retry | Treat any matching request as potentially live; do not re-run or reuse the tag, even if GitHub reported failure |
| Unexpected certificate, missing timestamp, failed Code Signing EKU, invalid signature, or PE payload mismatch | Stop publication and distribution; preserve both unsigned and returned bytes | Contact SignPath through its official support channel, rotate/revoke affected material when directed, repair the pipeline, and use a higher version |
| Submitter token or SignPath/GitHub account may be exposed | Disable signing, cancel active runs, revoke or rotate the token, and review account sessions/MFA | Audit recent requests and repository/environment changes before issuing a least-privilege replacement token |
| Published release has wrong assets, notes, signature, provenance, or unverifiable immutable state | Do not announce it; use the workflow's verified draft-withdrawal path when still possible | If withdrawal cannot be proven, treat the public state as a security incident, warn users, preserve evidence, and coordinate remediation without moving the tag |
| Release workflow, pinned action, Artifact Configuration, or signing policy changed unexpectedly | Set `SIGNPATH_ENABLED=false` first and cancel every active release run; then set `SIGNPATH_IDEMPOTENCY_CONFIRMED=false` and remove `SIGNPATH_IDEMPOTENCY_REVIEWED_ACTION_SHA` | Review the complete trust boundary again; restore the reviewed SHA, set confirmation to `true`, require `activation` to pass, enable signing last, and require `active` to pass |

Security reports should follow the repository [security policy](../SECURITY.md).
For a defective but non-security release, also follow the rollback rules in the
[release process](RELEASE_PROCESS.md#11-rollback-and-incident-handling).

## 10. Foundation policy compliance matrix

This matrix records repository evidence against the current
[SignPath Foundation conditions](https://signpath.org/terms.html). It is a
maintainer aid, not a claim of acceptance; SignPath makes the eligibility
decision.

| Condition | Repository evidence | Current state or follow-up |
| --- | --- | --- |
| No malware or potentially unwanted behavior | Public source, verified GitHub-hosted build, CI, privacy checks, immutable provenance, and warning-gated device mutations | Implemented controls; continue review for every release |
| OSI-approved licensing and no commercial dual license for OpenADB code | Root [`LICENSE`](../LICENSE), [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md), and [`THIRD_PARTY_SOURCES.md`](../THIRD_PARTY_SOURCES.md) | OpenADB original code is `GPL-3.0-or-later`; dependencies retain their own documented licenses |
| No proprietary project component; System Libraries may be included | Complete payload/license inventory and one-file build verification | The official Microsoft Visual C++ Runtime is disclosed as a separately licensed System Library, not OpenADB-owned OSS; obtain SignPath's written eligibility confirmation before activation |
| Maintained, released, and documented in the form to be signed | Public repository, changelog, Downloads section, feature documentation, and historical `v3.1.0` executable | Active and documented; whether `v3.1.0` is sufficient released-form evidence remains a SignPath decision |
| Sign only the team's own project binary from verifiable source | One outer OpenADB PE selected by [the Artifact Configuration](../.signpath/artifact-configuration.xml); upstream OSS binaries remain bundled dependencies rather than separate signing targets | Implemented repository boundary; SignPath Trusted Build System provisioning pending |
| No hacking or vulnerability-exploitation tool | OpenADB uses Android's user-enabled and user-authorized debugging interfaces. It does not exploit vulnerabilities, scan for insecure services, obtain Root, install/start Shizuku, unlock a bootloader, or bypass Android permission prompts. Root must already exist and be explicitly granted; Shizuku is separately installed and authorized by the user; destructive actions retain warnings and stronger confirmation. | Documented; ask SignPath to confirm that this device-management scope is eligible |
| Respect privacy, announce system changes, and provide removal instructions | [`PRIVACY.md`](../PRIVACY.md), warning/confirmation UX, Downloads removal guidance, and ACBridge permission-removal instructions | Implemented and covered by release checks |
| MFA and explicit Authors/Reviewers/Approvers | Public roles in the README Code signing policy | Roles documented; each human must verify MFA on GitHub and SignPath before activation |
| Manual approval for every signing request | Fail-closed waiting action plus this approval runbook | Repository side ready; SignPath approval policy and roles pending |
| Literal Code signing policy, attribution, roles, and privacy link on home/download/release pages | README policy and generated exact-commit release notes | Implemented; post-release rendered-page check remains mandatory |
| Product and version metadata restrictions | The one-PE Artifact Configuration pins product name, product/file versions, company, copyright, and original filename | Implemented; import under the exact approved slug after provisioning |
| Investigate reported violations | [`SECURITY.md`](../SECURITY.md), preserved release evidence, and the incident matrix above | Repository procedure ready; GitHub private vulnerability reporting must remain enabled |

Official references:

- [SignPath GitHub integration](https://docs.signpath.io/trusted-build-systems/github)
- [SignPath Artifact Configuration syntax](https://docs.signpath.io/artifact-configuration/syntax)
- [SignPath Artifact Configuration reference](https://docs.signpath.io/artifact-configuration/reference)
- [SignPath Foundation terms](https://signpath.org/terms.html)
