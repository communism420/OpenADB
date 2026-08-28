# OpenADB security policy

## Supported versions

Security fixes are developed on the current `main` branch and delivered in a
new release after review. Reports concerning the latest published release are
accepted. Older releases are not maintained independently; reporters and users
may be asked to reproduce an issue with current source or upgrade to the next
fixed release.

Never move or replace an existing release tag or asset as a security fix.
OpenADB publishes corrections from a new reviewed commit under a monotonically
higher version.

## Report a vulnerability privately

Use GitHub's
[private vulnerability reporting form](https://github.com/communism420/OpenADB/security/advisories/new).
Do not place exploit details, credentials, signing tokens, pairing codes,
device serials, private logs, or personal paths in a public issue. If the
private form is temporarily unavailable, open a minimal public issue asking
the maintainer to restore a private reporting channel, without including the
vulnerability details.

Include only the information needed to reproduce and assess the problem:

- affected OpenADB/ACBridge version, release tag, or source commit;
- Windows and Android versions and the relevant connection/access mode;
- concise reproduction steps, expected behavior, observed behavior, and
  security impact;
- affected artifact filename and SHA-256 when the report concerns a release;
- sanitized screenshots or logs with device, network, account, and filesystem
  identifiers removed; and
- for a signing or provenance problem, the public GitHub workflow URL,
  SignPath request ID/URL if already public, certificate details, and artifact
  hashes—but never an API token or private signing material.

Test only on systems and devices you own or are explicitly authorized to use.
Do not access, alter, or retain another person's data to demonstrate an issue.

## What happens next

The maintainer will validate scope, preserve relevant evidence, and coordinate
remediation through the private advisory. Response and release timing depends
on severity, reproducibility, hardware availability, and any required upstream
or certificate-provider coordination; this policy does not promise a fixed
service-level deadline.

Please avoid public disclosure until a fix or mutually agreed disclosure plan
is ready. The maintainer will credit reporters who request attribution and will
respect requests to remain anonymous.

Suspected Authenticode, SignPath, release-provenance, or signing-credential
incidents follow the containment matrix in the
[SignPath setup guide](docs/SIGNPATH_SETUP.md#9-signpath-incident-response) and
the immutable-tag rollback rules in the
[release process](docs/RELEASE_PROCESS.md#11-rollback-and-incident-handling).

## Scope notes

Security boundaries include the Windows application, ACBridge Android helper,
ADB/Fastboot command construction, Root and Shizuku routing, P2P
authentication, update/install verification, settings and backup handling,
GitHub release workflows, distributed artifacts, and their signatures.

ADB, Fastboot, Root, and Shizuku can legitimately perform powerful operations
after the user authorizes them. A documented, explicitly confirmed device
operation is not by itself a vulnerability, but bypassing a confirmation,
targeting a different device, crossing the selected access boundary, exposing
credentials or private data, or accepting untrusted release/helper bytes is in
scope.
