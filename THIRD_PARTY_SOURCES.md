# OpenADB third-party source and provenance index

This file records where the source corresponding to bundled third-party
components can be obtained. License texts and attributions are indexed in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

For each OpenADB release, `BUILD_STATUS.json` identifies the exact OpenADB
source commit. The release workflow must use that immutable checkout and must
not substitute a mutable branch for any pinned source reference below. A
release maintainer must preserve a retrievable copy of every source required
by a component's license; an upstream link becoming unavailable is a release
blocker, not permission to omit source.

## Release automation services (not distributed)

These components execute only in GitHub Actions and are not embedded in the
OpenADB EXE or ACBridge APK:

- SignPath GitHub signing-request action `v2.3`, pinned to commit
  `c92b958760219087e01f8d67a1669ed57afe2627`:
  <https://github.com/SignPath/github-action-submit-signing-request/tree/c92b958760219087e01f8d67a1669ed57afe2627>
- GitHub `actions/checkout` `v7.0.1`, pinned to commit
  `3d3c42e5aac5ba805825da76410c181273ba90b1`:
  <https://github.com/actions/checkout/tree/3d3c42e5aac5ba805825da76410c181273ba90b1>.
- GitHub `actions/setup-java` `v6.0.0`, pinned to commit
  `dd06d9cba3e5552c54d9f8ea23572deb30010f7c`:
  <https://github.com/actions/setup-java/tree/dd06d9cba3e5552c54d9f8ea23572deb30010f7c>.
- GitHub `actions/setup-python` `v7.0.0`, pinned to commit
  `5fda3b95a4ea91299a34e894583c3862153e4b97`:
  <https://github.com/actions/setup-python/tree/5fda3b95a4ea91299a34e894583c3862153e4b97>.
- GitHub `actions/upload-artifact` `v7.0.1`, pinned to commit
  `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a`:
  <https://github.com/actions/upload-artifact/tree/043fb46d1a93c77aae656e7c1c64a875d1fc6a0a>.
- GitHub `actions/download-artifact` `v8.0.1`, pinned to commit
  `3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c`:
  <https://github.com/actions/download-artifact/tree/3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c>.

The SignPath action selects the signing payload only through the exact numeric
GitHub artifact ID chosen by the release workflow; that artifact contains one
unsigned EXE. The action also receives the protected API token, explicit
organization/project/policy/configuration identifiers, the GitHub token,
version parameter, and bounded wait settings required by the service. The API
token and SignPath-side certificate are not repository or release artifacts.

## OpenADB and ACBridge

- Repository: <https://github.com/communism420/OpenADB>
- Release source: the exact commit referenced by the public `v<version>` tag
  and `BUILD_STATUS.json`.
- ACBridge sources and non-Gradle build script:
  `openadb/resources/acbridge/` and `tools/build_acbridge.py` in that same
  commit.

## Windows Python and Qt runtime

| Component | Exact source reference |
| --- | --- |
| CPython 3.12.10 | <https://github.com/python/cpython/tree/v3.12.10> |
| PySide6 / Shiboken6 6.11.1 | Qt for Python tag `v6.11.1`, peeled commit `73fb12a067c2e8f7a464a310aaee2860fa2b64d2`: <https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.11.1>. The 6.11.1 wheel metadata declares `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`; OpenADB records its GPL-3.0 selection in `LICENSES/Qt-PySide6-6.11.1-NOTICE.txt`. |
| Qt 6.11.1 modules used by the PySide wheels | Qt supermodule tag `v6.11.1`, peeled commit `bfde7b892add48396756dc44a3e3fa03d98c5710`: <https://code.qt.io/cgit/qt/qt5.git/tag/?h=v6.11.1>. Its immutable submodule revisions are the authority for Qt Base, SVG, Declarative/Quick/QML, Virtual Keyboard, PDF, and their third-party trees. |
| tomli 2.4.1 (Python 3.10 only) | Tag commit `c5f44690c68c5ed29534faa8f9df18882113728c`: <https://github.com/hukkin/tomli/tree/2.4.1>; reviewed universal wheel `tomli-2.4.1-py3-none-any.whl`, SHA-256 `0d85819802132122da43cb86656f8d1f8c6587d54ae7dcaf30e90533028b49fe`: <https://pypi.org/project/tomli/2.4.1/#files> |
| Pillow 12.3.0 | Tag commit `bb1d8e8ab8d29048624d96e3ee53cecf7c13d13d`: <https://github.com/python-pillow/Pillow/tree/12.3.0>; reviewed wheel `pillow-12.3.0-cp312-cp312-win_amd64.whl`, SHA-256 `a2b55dd6b2a4c4b7d87ffa56bdb33fdc5fdb9a462173861a7bc097f17d91cb09`: <https://pypi.org/project/pillow/12.3.0/#files> |
| apkutils2 1.0.0 | Immutable PyPI files: <https://pypi.org/project/apkutils2/1.0.0/#files>; upstream repository: <https://github.com/codeskyblue/apkutils2> |
| pyelftools 0.33 and vendored Construct | Tag commit `52ce435186022bd6b32012a998931fe86c985ec8`: <https://github.com/eliben/pyelftools/tree/v0.33> |
| cigam 0.0.3 | Immutable PyPI files: <https://pypi.org/project/cigam/0.0.3/#files>; upstream repository: <https://github.com/mikusjelly/cigam> |
| xmltodict 1.0.4 | <https://pypi.org/project/xmltodict/1.0.4/#files> |
| qrcode 8.2 | <https://pypi.org/project/qrcode/8.2/#files> |
| colorama 0.4.6 | <https://pypi.org/project/colorama/0.4.6/#files> |
| python-zeroconf 0.149.16 | Peeled tag commit `78670f7d05df4b4592677e88d57a82403286be8b`: <https://github.com/python-zeroconf/python-zeroconf/tree/0.149.16> |
| ifaddr 0.2.0 | <https://pypi.org/project/ifaddr/0.2.0/#files> |
| OpenSSL 3.0.16 | <https://github.com/openssl/openssl/tree/openssl-3.0.16> |

The exact Qt module commits, retained `REUSE.toml` and
`qt_attribution.json` files, module license texts, and QtPdf's pinned
Chromium/PDFium license files are stored under `LICENSES/Qt-6.11.1/`. Its
`SNAPSHOT_PROVENANCE.md` records the upstream mapping and its
`SNAPSHOT_MANIFEST.sha256` rejects local drift. QtPdf uses QtWebEngine source
commit `eb0793cc4b76e93cf669f586fd68c76019f40ec9` and that source's
`src/3rdparty` gitlink commit
`58c11ad487f8a237cf0ac71cc3e818b52db150df`; this identifies QtPdf/PDFium
source and does not claim that QtWebEngine browser binaries are shipped.

The official Pillow Windows wheel statically or dynamically incorporates
image and text libraries. The reviewed 12.3.0 CPython 3.12 Windows x86-64
wheel reports the versions below;
their source references are:

- FreeType 2.14.3: <https://gitlab.freedesktop.org/freetype/freetype/-/tree/VER-2-14-3>
- Little CMS 2.19: peeled tag commit
  `b76633e60c8387a77268fb3359277ca25b5fd75c` at
  <https://github.com/mm2/Little-CMS/tree/lcms2.19>
- libwebp 1.6.0: <https://github.com/webmproject/libwebp/tree/v1.6.0>
- libavif 1.4.2: peeled tag commit
  `c5240fc79fe5c2407e10afd35f5505ef6333ea49` at
  <https://github.com/AOMediaCodec/libavif/tree/v1.4.2>
- libraqm 0.10.5: peeled tag commit
  `3a6b891a3db0e0db1364aa38088422f68d8d81e6` at
  <https://github.com/HOST-Oman/libraqm/tree/v0.10.5>
- FriBidi 1.0.12: peeled tag commit
  `6428d8469e536bcbb6e12c7b79ba6659371c435a` at
  <https://github.com/fribidi/fribidi/tree/v1.0.12>
- HarfBuzz 14.2.1: peeled tag commit
  `56feae4035bdd48f62ba2b8d8c16232d4d89b3a4` at
  <https://github.com/harfbuzz/harfbuzz/tree/14.2.1>
- libjpeg-turbo 3.1.4.1: <https://github.com/libjpeg-turbo/libjpeg-turbo/tree/3.1.4.1>
- zlib-ng 2.3.3: <https://github.com/zlib-ng/zlib-ng/tree/2.3.3>
- zlib 1.3.1: <https://github.com/madler/zlib/tree/v1.3.1>
- libpng 1.6.58: peeled tag commit
  `3061454d980de7d53608f594194cfac722721d2a` at
  <https://github.com/pnggroup/libpng/tree/v1.6.58>
- OpenJPEG 2.5.4: <https://github.com/uclouvain/openjpeg/tree/v2.5.4>
- libtiff 4.7.1: <https://gitlab.com/libtiff/libtiff/-/tree/v4.7.1>

The exact feature/version report must be regenerated against the wheel used by
each release. A changed wheel payload requires a reviewed notice update.

## Android Platform Tools

- Version: 37.0.0
- Archive:
  <https://dl.google.com/android/repository/platform-tools_r37.0.0-win.zip>
- SHA-1: `f29bfb58d0d6f9a57d7dbcba6cc259f9ca6f58f1`
- SHA-256:
  `4fe305812db074cea32903a489d061eb4454cbc90a49e8fea677f4b7af764918`
- AOSP source browser: <https://android.googlesource.com/platform/packages/modules/adb/>

The exact `NOTICE.txt` from that verified archive is embedded with Platform
Tools and copied into the release legal archive. Its component-specific source
references and licenses control over any summary in this file.

## ACBridge dependencies

### Shizuku API 13.1.5

Official Maven artifacts are stored unmodified and verified before use:

| Artifact | URL | SHA-256 |
| --- | --- | --- |
| `api-13.1.5.aar` | <https://repo1.maven.org/maven2/dev/rikka/shizuku/api/13.1.5/api-13.1.5.aar> | `4def9bde498ef8626614c2fc5db9af4749c86f16f6c33e3f5658d35e70bab59b` |
| `provider-13.1.5.aar` | <https://repo1.maven.org/maven2/dev/rikka/shizuku/provider/13.1.5/provider-13.1.5.aar> | `b0f18cd9812464ec171c53cac93a819fe411718a3965c311f01eb4de265381b3` |
| `aidl-13.1.5.aar` | <https://repo1.maven.org/maven2/dev/rikka/shizuku/aidl/13.1.5/aidl-13.1.5.aar> | `33fe7191cdd69fcb66d649264f3b0c47acb2f3d6343afc05b98dbbff6f221963` |
| `shared-13.1.5.aar` | <https://repo1.maven.org/maven2/dev/rikka/shizuku/shared/13.1.5/shared-13.1.5.aar> | `4659642c9339be0a26e9c65bb8648f7ad6d8f4a465f557993ccbc78802381635` |

Upstream source: <https://github.com/RikkaApps/Shizuku-API>. The Maven POMs
identify this repository and the MIT license; the artifact hashes above are
the immutable build inputs when no matching upstream tag is published.

### desugar_jdk_libs 2.1.5

| Artifact | URL | SHA-256 |
| --- | --- | --- |
| `desugar_jdk_libs-2.1.5.jar` | <https://dl.google.com/dl/android/maven2/com/android/tools/desugar_jdk_libs/2.1.5/desugar_jdk_libs-2.1.5.jar> | `d8044befae095781b9a80bf1faa92edc30382d75d437476784c1bf991598a976` |
| `desugar_jdk_libs_configuration-2.1.5.jar` | <https://dl.google.com/dl/android/maven2/com/android/tools/desugar_jdk_libs_configuration/2.1.5/desugar_jdk_libs_configuration-2.1.5.jar> | `7bc9051b3a1ec19806311dcb6aa9b9ba7ef9c22caa6f4810da55bde285fb7770` |

Corresponding upstream source revision used for the 2.1.5 release:
`73170c345e6a762fc6a1f0301bb15218850023ef` at
<https://github.com/google/desugar_jdk_libs/tree/73170c345e6a762fc6a1f0301bb15218850023ef>.

## Data and artwork

### Universal Android Debloater data

- Exact upstream file:
  <https://github.com/Universal-Debloater-Alliance/universal-android-debloater-next-generation/blob/5492d39683626448e093279a0fea5e0942901526/resources/assets/uad_lists.json>
- Commit: `5492d39683626448e093279a0fea5e0942901526`
- Local snapshot SHA-256:
  `3155ca688ab91ac68eaab1ca47693094ede053b66418f6b1e122c99e32848d8b`

### Material Symbols Rounded

- Source project commit: `84ccef280841abfac506afc4ad4a2782f6d0a1d0`
- Source tree:
  <https://github.com/google/material-design-icons/tree/84ccef280841abfac506afc4ad4a2782f6d0a1d0>
- OpenADB's extracted/compacted path mapping is in
  `openadb/ui/material_icons.py`; its reviewed local SHA-256 when this index was
  created was
  `21d72e1c38dd33588c6c919bb3ad14a928f8075f07d58ed4659cb884d079e132`.

### Android robot-derived branding

- Google brand guidance:
  <https://developer.android.com/distribute/marketing-tools/brand-guidelines>
- CC BY 3.0 legal code:
  <https://creativecommons.org/licenses/by/3.0/legalcode>
- The exact external design file from which the current artwork was initially
  prepared was not preserved. OpenADB therefore does not claim that the robot
  shapes are wholly original and conservatively treats them as modifications
  of the Android robot under CC BY 3.0.
- `logo.png` first appears in OpenADB commit
  `a9852d35218a228f4a878ef605fb36bea6e79f5c`; its reviewed SHA-256 is
  `93b81cb66bf6a5c05112fd9911b86f6701be45104a10ce64f18b037c3682c959`.
- ACBridge's vector robot artwork first appears in commit
  `7c6471af6136440f44bc365851be25ababcf8991`; reviewed SHA-256 values are
  `23c884ecbd01ad96d416e7887553a2e96a201e207dce49af82c2a88732dd72be`
  for `acbridge_icon.xml` and
  `ff13050fcf3beb73800e6e5f6fe7baf4a456028c6c93d0f4ee43222857e4e552`
  for `acbridge_banner.xml`.

## Build tools

| Component | Exact source/files |
| --- | --- |
| pip 26.2.1 | Wheel `pip-26.2.1-py3-none-any.whl`, SHA-256 `71138adf1f4ca900cdb7d289c21b7494329f2332b6d85f0e1c42108c0384ed3e`: <https://pypi.org/project/pip/26.2.1/#files> |
| wheel 0.48.0 | Tag commit `21c4da4c54c3b354cc22dc7f6f6374ffcd560e84`: <https://github.com/pypa/wheel/tree/0.48.0>; reviewed wheel `wheel-0.48.0-py3-none-any.whl`, SHA-256 `3217dcc807155e45db462d7ef2431f5ddda0d7273b700d05a67b271ceb1287ab`: <https://pypi.org/project/wheel/0.48.0/#files> |
| PyInstaller 6.20.0 | <https://pypi.org/project/pyinstaller/6.20.0/#files> and <https://github.com/pyinstaller/pyinstaller/tree/v6.20.0> |
| PyInstaller community hooks 2026.6 | <https://pypi.org/project/pyinstaller-hooks-contrib/2026.6/#files> |
| altgraph 0.17.5 | <https://pypi.org/project/altgraph/0.17.5/#files> |
| packaging 26.3 | Wheel `packaging-26.3-py3-none-any.whl`, SHA-256 `d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c`: <https://pypi.org/project/packaging/26.3/#files> |
| pefile 2024.8.26 | <https://pypi.org/project/pefile/2024.8.26/#files> |
| pywin32-ctypes 0.2.3 | <https://pypi.org/project/pywin32-ctypes/0.2.3/#files> |
| setuptools 84.0.0 | Tag commit `72e919a8b10aaafc041205d4e3ae0e6a2e1e5f87`: <https://github.com/pypa/setuptools/tree/v84.0.0>; reviewed wheel `setuptools-84.0.0-py3-none-any.whl`, SHA-256 `51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670`: <https://pypi.org/project/setuptools/84.0.0/#files> |
| Ruff 0.16.4 | Tag commit `11c76bf48fdac06b2f240cba502eda96da4dce77`: <https://github.com/astral-sh/ruff/tree/0.16.4> |

`requirements-bootstrap-win-py312.lock` and
`requirements-build-win-py312.lock` are the authoritative filename/SHA-256
inventory for every Python artifact used by the CPython 3.12.10 Windows
release. Automation installs them with `--require-hashes`, `--no-cache-dir`,
`--force-reinstall`, and no dependency resolution. It verifies the exact
bootstrap environment before building the APKUtils2 sdist, then rejects any
missing, unexpected, version/extras-mismatched, or bootstrap-drifted
distribution. A build that changes an artifact,
hash, or installed distribution must fail or update this index and the legal
bundle in a reviewed source commit before publication.
