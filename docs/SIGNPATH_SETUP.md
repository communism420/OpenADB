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
`SIGNPATH_IDEMPOTENCY_CONFIRMED` repository variable is `true`.

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

## 2. Configure the protected GitHub environment

Create an environment named exactly `signpath-release` and restrict deployment
to reviewed release tags. Add required reviewers appropriate to the current
maintainer team. If GitHub cannot provide separation of duties for a
single-maintainer repository, do not describe that environment gate as an
independent second person; the mandatory SignPath approval still applies.
Where the repository plan and UI allow it, disable administrator bypass for
this environment.

Add exactly one environment secret:

| Secret | Purpose |
| --- | --- |
| `SIGNPATH_API_TOKEN` | Submitter-only token used directly by the pinned SignPath action |

Add these environment variables:

| Variable | Purpose |
| --- | --- |
| `SIGNPATH_ORGANIZATION_ID` | Exact SignPath organization UUID |
| `SIGNPATH_PROJECT_SLUG` | Exact OpenADB project slug |
| `SIGNPATH_SIGNING_POLICY_SLUG` | Exact approval-gated release policy slug |
| `SIGNPATH_ARTIFACT_CONFIGURATION_SLUG` | Exact one-EXE configuration slug |
| `SIGNPATH_CERTIFICATE_SHA256` | Lowercase SHA-256 of the approved leaf certificate |
| `SIGNPATH_CERTIFICATE_SUBJECT` | Exact publisher subject of that certificate |

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
```

Do not treat `git push --dry-run` as proof that server-side rules are active;
compare the live ruleset with the checked-in JSON and require
`"enabled": true` from the immutable-releases endpoint.

## 4. Activate last

Leave the repository variable `SIGNPATH_ENABLED` absent or set to `false` while
the application is pending or any protected value is incomplete. After the
SignPath project, policy, approver, GitHub environment, certificate pins,
release tag protections, and written same-submission deduplication guarantee
have all been reviewed, set both repository variables:

```text
SIGNPATH_IDEMPOTENCY_CONFIRMED=true
SIGNPATH_ENABLED=true
```

Set `SIGNPATH_IDEMPOTENCY_CONFIRMED` back to `false` before changing the pinned
SignPath action or its submission API behavior, and repeat the review before
re-enabling it. Never set this variable merely because a test request happened
to succeed once.

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
SignPath project and Foundation account first. Update
`SIGNPATH_CERTIFICATE_SHA256` and, if it changed legitimately,
`SIGNPATH_CERTIFICATE_SUBJECT` through a reviewed configuration change before
creating the new release tag. Never weaken the pin or accept any merely valid
Windows publisher certificate.

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
- `SIGNPATH_IDEMPOTENCY_CONFIRMED=true` was set only after that assurance was
  reviewed.
- `SIGNPATH_ENABLED=true` was set only after the checks above.
- The new tag is immutable, new, and points at the reviewed release commit.
- Exact-tag CI is green; physical device evidence is reviewed separately and is
  not placed in the SignPath upstream dependency chain.
- No GitHub Release already exists for the tag.
- The approver sees exactly one request for the run/artifact pair and has
  checked the repository, run ID, artifact ID, commit, and policy.

Official references:

- [SignPath GitHub integration](https://docs.signpath.io/trusted-build-systems/github)
- [SignPath Artifact Configuration syntax](https://docs.signpath.io/artifact-configuration/syntax)
- [SignPath Artifact Configuration reference](https://docs.signpath.io/artifact-configuration/reference)
- [SignPath Foundation terms](https://signpath.org/terms.html)
