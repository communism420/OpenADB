# OpenADB dependencies

OpenADB supports CPython 3.10 through 3.14. The upper bound follows the
currently tested PySide6 and PyInstaller releases; expand the CI matrix before
advertising a newer Python version.

The runtime and build requirement files use exact pins for the complete direct
and transitive Python distribution set. They are the human-readable review
source. Windows release builds are narrower: they use CPython 3.12.10 x86-64
and the artifact-level SHA-256 locks
`requirements-bootstrap-win-py312.lock` and
`requirements-build-win-py312.lock`. Updating a pin, marker, selected wheel,
or sdist hash is an intentional maintenance change and must pass the complete
Windows Python matrix plus the release-lock tests.

## License and delivered notices

The original OpenADB source is licensed under the GNU General Public License,
version 3 or later (`GPL-3.0-or-later`), as stated in the README. The
repository-root `LICENSE` file contains the unmodified GPLv3 license text. That
project declaration does not relicense bundled third-party code, data, Android
artifacts, or upstream tools. Every such component retains its own license and
required attribution.

`THIRD_PARTY_NOTICES.md` is the canonical index of the components and notices
distributed by OpenADB. The `LICENSES/` directory contains the corresponding
license texts where redistribution requires a text copy. A release is
incomplete if either the notice index or any required license text is absent
from the source tree or from the documented release delivery.

## Runtime

Install `requirements.txt` to run OpenADB from source:

```powershell
py -m pip install -r requirements.txt
```

- PySide6 provides the Qt desktop UI.
- Pillow and `qrcode[pil]` render icons and Wireless ADB pairing QR codes.
- apkutils2 reads APK metadata when Android-side metadata is unavailable.
- zeroconf provides the mDNS fallback used by Wireless ADB discovery.

## Development and validation

Install `requirements-dev.txt` to reproduce the CI validation environment. It
includes the runtime and build dependencies plus Ruff:

```powershell
py -m pip install -r requirements-dev.txt
```

The unittest suite uses only Python's standard-library test runner and the
runtime packages; it does not require pytest.

## Windows build

Use a fresh CPython 3.12.10 x86-64 environment for a release build. Install the
reviewed bootstrap artifacts first, then the exact Windows build closure. The
no-cache and no-dependency flags are part of the release contract; in
particular, they prevent pip from substituting a cached locally built
APKUtils2 wheel for its reviewed sdist:

```powershell
python -m pip install --disable-pip-version-check --no-cache-dir --require-hashes --no-deps --force-reinstall -r requirements-bootstrap-win-py312.lock
python tools/verify_release_dependencies.py --phase bootstrap --lock requirements-bootstrap-win-py312.lock
python -m pip install --disable-pip-version-check --no-cache-dir --require-hashes --no-deps --no-build-isolation --force-reinstall -r requirements-build-win-py312.lock
python -m pip check
python tools/verify_release_dependencies.py --phase build --lock requirements-build-win-py312.lock --bootstrap-lock requirements-bootstrap-win-py312.lock --requirements requirements-build.txt
python -m PyInstaller --noconfirm --clean OpenADB.spec
```

The bootstrap gate runs before APKUtils2 is built, so that sdist cannot be
compiled by unreviewed build tools. `--force-reinstall` prevents an
already-installed same-version package from bypassing pip's artifact hash
check. The final verifier fails on the wrong Python patch release, a missing
or extra distribution, a version or extras mismatch, changed lock options,
bootstrap-artifact drift, a missing hash, or drift between the human-readable
requirements and the artifact lock. Ordinary development may still install
`requirements-build.txt`; those unhashed installs are not valid release
inputs.

`OpenADB.spec` also requires a complete Android Platform Tools directory at
build time so the one-file executable can bundle ADB, fastboot, and their
Windows libraries. The release workflow downloads the pinned archive, verifies
both its SHA-1 and SHA-256, and distributes its exact `NOTICE.txt` in the EXE
and deterministic `LICENSES.zip`.

## Android helper dependencies

ACBridge 3.1.0 vendors the unmodified official Shizuku API 13.1.5 AARs needed
by its non-Gradle Android build. It also vendors Google's core-library
desugaring 2.1.5 artifacts so the helper can preserve its Android API 23
minimum. `tools/build_acbridge.py` verifies a pinned SHA-256 for every archive
before compilation and never downloads dependencies during a release build.

Artifact URLs, checksums, upstream source links, and license files are recorded
under `openadb/resources/acbridge/third_party/`. The Shizuku API is MIT
licensed; the desugaring artifacts retain their documented upstream licenses.

Before publishing, compare `THIRD_PARTY_NOTICES.md` with the actual packaged
payload. At minimum, account for the UAD data, Material Symbols notice, the
ACBridge APK and its embedded dependencies, and every Android Platform Tools
binary/library and its `NOTICE.txt`. Do not claim that the root GPL license
covers any of these components unless their own license expressly permits it.
