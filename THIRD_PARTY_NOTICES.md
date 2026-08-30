# OpenADB third-party notices

This document is the canonical index of third-party software, data, and
artwork distributed with OpenADB 3.1.0 and its bundled ACBridge helper. It
does not replace the original license texts. The corresponding texts are in
[`LICENSES/`](LICENSES/), in the component-specific paths named below, or in
the exact upstream notice shipped with the component.

OpenADB and ACBridge original code are licensed under
`GPL-3.0-or-later`; see [`LICENSE`](LICENSE). Third-party components are not
relicensed by OpenADB. If this index and an upstream license disagree, the
upstream license controls.

## Windows application runtime

| Component | Version / payload | License | Included license or notice |
| --- | --- | --- | --- |
| CPython | 3.12.10 in the reviewed 3.1.0 Windows build | PSF-2.0 and the third-party terms reproduced by CPython | `LICENSES/Python-3.12.10.txt` |
| PySide6, PySide6 Essentials, PySide6 Addons, Shiboken6, and Qt | 6.11.1; Qt Core, GUI, Widgets, Network, SVG, OpenGL, QML, Quick, PDF, Virtual Keyboard, plugins, translations, and software OpenGL payload collected by the reviewed PyInstaller build | PySide and the Qt modules that offer it use the GPL-3.0 option; QtPdf uses its LGPL-3.0 option; individual Qt third-party portions retain their own terms | `LICENSES/GPL-3.0.txt`, `LICENSES/LGPL-3.0.txt`, `LICENSES/Qt-PySide6-6.11.1-NOTICE.txt`, `LICENSES/Qt-6.11.1-THIRD-PARTY-NOTICES.md`, and the complete exact-source metadata snapshot under `LICENSES/Qt-6.11.1/` |
| tomli | 2.4.0 on Python 3.10 | MIT | `LICENSES/tomli-2.4.0.txt` |
| Pillow and compiled image/text libraries | Pillow 12.3.0; the official CPython 3.12 Windows x86-64 wheel reports FreeType 2.14.3, LittleCMS2 2.19, WebP 1.6.0, AVIF 1.4.2, libraqm 0.10.5, FriBidi 1.0.12, HarfBuzz 14.2.1, libjpeg-turbo 3.1.4.1, zlib-ng 2.3.3 / zlib 1.3.1, libpng 1.6.58, OpenJPEG 2.5.4, and libtiff 4.7.1 | MIT-CMU plus the FTL, LGPL-2.1-or-later, MIT, BSD, IJG, zlib, libpng, libtiff, and other terms reproduced by Pillow | `LICENSES/Pillow-12.3.0.txt`, `LICENSES/LGPL-2.1.txt` |
| apkutils2 | 1.0.0 | MIT; files derived from Google/enjarify retain Apache-2.0 headers | `LICENSES/apkutils2-1.0.0.txt`, `LICENSES/Apache-2.0.txt` |
| pyelftools and its vendored Construct parser | 0.33 | Public-domain dedication / Unlicense-style terms; vendored Construct code is MIT | `LICENSES/pyelftools-0.33.txt`, `LICENSES/pyelftools-construct.txt` |
| cigam | 0.0.3 | MIT | `LICENSES/cigam-0.0.3.txt` |
| xmltodict | 1.0.4 | MIT | `LICENSES/xmltodict-1.0.4.txt` |
| qrcode | 8.2 | BSD-3-Clause | `LICENSES/qrcode-8.2.txt` |
| colorama | 0.4.6 on Windows | BSD-3-Clause | `LICENSES/colorama-0.4.6.txt` |
| python-zeroconf | 0.149.16 | LGPL-2.1-or-later | `LICENSES/LGPL-2.1.txt` |
| ifaddr | 0.2.0 | MIT | `LICENSES/ifaddr-0.2.0.txt` |
| OpenSSL shared libraries supplied by CPython | The reviewed 3.1.0 build contains the OpenSSL 3.0.16 payload from CPython 3.12.10; Qt's OpenSSL TLS plugin dynamically uses an available OpenSSL installation but the PySide6 wheels do not add another OpenSSL library to this release | Apache-2.0 | `LICENSES/Apache-2.0.txt`, `LICENSES/OpenSSL-3-NOTICE.txt` |
| Android SDK Platform Tools | 37.0.0: `adb.exe`, `fastboot.exe`, `AdbWinApi.dll`, `AdbWinUsbApi.dll`, and `libwinpthread-1.dll` | AOSP Apache-2.0 and the permissive licenses enumerated by Google's exact archive notice | `platform-tools/NOTICE.txt` inside the EXE; the same verified file is added to the release `LICENSES.zip` as `Android-Platform-Tools-37.0.0-NOTICE.txt` |
| Microsoft Visual C++ Runtime | `VCRUNTIME140`, `VCRUNTIME140_1`, `MSVCP140`, `MSVCP140_1`, and `MSVCP140_2` DLLs collected from the official CPython, PySide6, and Shiboken6 Windows distributions when referenced by the reviewed payload | Microsoft Visual Studio 2022 redistributable terms; not OpenADB code | `LICENSES/Microsoft-Visual-Cpp-Runtime-NOTICE.txt` |

## Data and visual assets

| Component | Local use | License | Included license or notice |
| --- | --- | --- | --- |
| Universal Android Debloater Next Generation / Universal Debloat List | Immutable `uad_lists.json` snapshot used to classify package names; Copyright (C) 2023 Universal Debloater Alliance | GPL-3.0-or-later | `LICENSES/GPL-3.0.txt`, `openadb/resources/UAD_LIST_SOURCE.txt` |
| Material Symbols Rounded | Vector path data rendered and recolored by OpenADB | Apache-2.0 | `LICENSES/Apache-2.0.txt`, `LICENSES/Material-Symbols-NOTICE.txt`, `openadb/resources/material_symbols/NOTICE.md` |
| Android robot-derived branding | Current OpenADB and ACBridge logo artwork; conservatively treated as modified Android-robot artwork because its exact pre-repository design source was not preserved | CC BY 3.0; Android trademark remains Google LLC's | `LICENSES/CC-BY-3.0.txt`, `LICENSES/Android-Robot-ATTRIBUTION.txt`; local introduction commits and hashes are recorded in `THIRD_PARTY_SOURCES.md` |

The USB-style trident in the current desktop branding is treated as a visual
identifier only. No USB Implementers Forum certification, endorsement, or
trademark ownership is claimed.

## ACBridge APK

| Component | Version / use | License | Included license or notice |
| --- | --- | --- | --- |
| ACBridge original Java, AIDL, manifest, and resources | Bundled helper for P2P, SAF, Root, and Shizuku operations | GPL-3.0-or-later | `LICENSE`, also stored in the APK as `assets/legal/LICENSE.txt` |
| Shizuku API (`api`, `provider`, `aidl`, `shared`) | 13.1.5 official Maven AARs | MIT | `LICENSES/Shizuku-API-13.1.5.txt`, component copy under `openadb/resources/acbridge/third_party/`, and APK asset |
| desugar_jdk_libs | 2.1.5 library transformed by D8/L8 | GPL-2.0-only WITH Classpath-exception-2.0 | `LICENSES/desugar_jdk_libs-2.1.5.txt`, component copy under `openadb/resources/acbridge/third_party/`, and APK asset |
| desugar_jdk_libs_configuration | 2.1.5 configuration/helper payload | BSD-3-Clause | `LICENSES/desugar_jdk_libs_configuration-2.1.5.txt`, component copy under `openadb/resources/acbridge/third_party/`, and APK asset |

Every newly built ACBridge APK contains byte-for-byte copies of its applicable
license, notice, and source-index files under `assets/legal/`. The builder and
tests reject missing, duplicate, empty, or stale legal assets.

## Build and packaging tools

The following tools are used to produce the Windows artifact. They are listed
for build provenance even when their complete Python packages are not shipped.

| Component | Version | License / effect on output | Included license |
| --- | --- | --- | --- |
| pip | 26.2.1, hash-locked bootstrap environment only | MIT; vendored components retain the licenses included in pip's wheel; pip itself is not shipped in the application | Exact wheel license inventory under `LICENSES/pip-26.2.1/` |
| wheel | 0.46.2, hash-locked build environment only | MIT; used to build the pinned APKUtils2 source distribution and not shipped in the application | `LICENSES/wheel-0.46.2.txt` |
| PyInstaller | 6.20.0 | GPL-2.0-or-later with the PyInstaller Bootloader Exception; the exception permits distribution of the generated executable | `LICENSES/PyInstaller-6.20.0.txt` |
| PyInstaller community hooks | 2026.6 | Hooks GPL-2.0-or-later; runtime hooks Apache-2.0 | `LICENSES/PyInstaller-hooks-contrib-2026.6.txt` |
| altgraph | 0.17.5 | MIT | `LICENSES/altgraph-0.17.5.txt` |
| packaging | 26.3 in the reviewed environment | Apache-2.0 OR BSD-2-Clause | `LICENSES/packaging-26.3.txt` |
| pefile | 2024.8.26 | MIT | `LICENSES/pefile-2024.8.26.txt` |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause | `LICENSES/pywin32-ctypes-0.2.3.txt` |
| setuptools | 83.0.0, build environment only | MIT | `LICENSES/setuptools-83.0.0.txt` |
| Ruff | 0.16.4, validation only | MIT | Not shipped in the application; upstream source is indexed in `THIRD_PARTY_SOURCES.md` |

Android's `aapt`, AIDL compiler, D8/L8, `zipalign`, `apksigner`, Java compiler,
and JDK are build tools. Android framework classes are compile-time APIs and
are not copied into ACBridge except where the separately listed desugared
library supplies its own implementation.

## Distribution requirements

The release process treats the following as required, reviewed payload:

- `LICENSE`
- `THIRD_PARTY_NOTICES.md`
- `THIRD_PARTY_SOURCES.md`
- `LICENSES.zip`, generated from the tracked `LICENSES/` directory plus the
  exact Platform Tools notice used by that build

Those files are copied beside the EXE, included in `SHA256SUMS.txt`, checked
against the immutable release checkout, and published as GitHub Release
assets. `LICENSE`, this notice, the source index, and tracked `LICENSES/` files
are also embedded in the one-file EXE. The nested APK carries its own legal
assets independently.

The release is blocked when an actual packaged component is missing from this
index, when a listed component is absent from the package but still claimed as
present, or when a legal file differs from the reviewed source copy.
