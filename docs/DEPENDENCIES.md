# OpenADB dependencies

OpenADB supports CPython 3.10 through 3.14. The upper bound follows the
currently tested PySide6 and PyInstaller releases; expand the CI matrix before
advertising a newer Python version.

The dependency files use exact top-level pins. Updating a pin is an intentional
maintenance change and must pass the complete Windows Python matrix.

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

Install `requirements-build.txt` for a release build. It includes the runtime
packages and the exact PyInstaller release used by automation:

```powershell
py -m pip install -r requirements-build.txt
pyinstaller --noconfirm --clean OpenADB.spec
```

`OpenADB.spec` also requires a complete Android Platform Tools directory at
build time so the one-file executable can bundle ADB, fastboot, and their
Windows libraries.

## Android helper dependencies

ACBridge 3.1.0 vendors the unmodified official Shizuku API 13.1.5 AARs needed
by its non-Gradle Android build. It also vendors Google's core-library
desugaring 2.1.5 artifacts so the helper can preserve its Android API 23
minimum. `tools/build_acbridge.py` verifies a pinned SHA-256 for every archive
before compilation and never downloads dependencies during a release build.

Artifact URLs, checksums, upstream source links, and license files are recorded
under `openadb/resources/acbridge/third_party/`. The Shizuku API is MIT
licensed; the desugaring artifacts retain their documented upstream licenses.
