# Qt 6.11.1 legal-source snapshot provenance

This directory preserves the license, attribution, and REUSE metadata needed
to audit the Qt modules collected from the official PySide6 6.11.1 Windows
wheels. Files retain their paths relative to the named upstream repositories.
The immutable peeled commits are:

| Directory | Official repository | Peeled commit |
| --- | --- | --- |
| `qtbase/` | <https://code.qt.io/cgit/qt/qtbase.git/> | `59c81a3c2247b821b9b84b4eb8d939b77e07e276` |
| `qtdeclarative/` | <https://code.qt.io/cgit/qt/qtdeclarative.git/> | `a02bed441965ee1f18f856352c7d5ee5ba35d795` |
| `qtimageformats/` | <https://code.qt.io/cgit/qt/qtimageformats.git/> | `77f35e5694885844c3eb3a737769b5ed290b8ccc` |
| `qtsvg/` | <https://code.qt.io/cgit/qt/qtsvg.git/> | `2596f43da2dc72d2afecc084355b0f5f87922a6c` |
| `qtvirtualkeyboard/` | <https://code.qt.io/cgit/qt/qtvirtualkeyboard.git/> | `2eb5eda48077645a88609f7e6237cd5598253d8a` |
| `qtwebengine/` (QtPdf module metadata) | <https://code.qt.io/cgit/qt/qtwebengine.git/> | `eb0793cc4b76e93cf669f586fd68c76019f40ec9` |

QtWebEngine records its Chromium/PDFium source as the `src/3rdparty`
submodule at commit `58c11ad487f8a237cf0ac71cc3e818b52db150df` in
<https://code.qt.io/cgit/qt/qtwebengine-chromium.git/>. The
`qtwebengine-chromium-58c11ad/` directory contains the exact upstream license
and metadata files for the third-party components listed by Qt's QtPdf
licensing page: Chromium, PDFium, Abseil, FreeType, fast_float, ICU,
libjpeg-turbo, libpng, and zlib. Each filename is mapped to its original path
in that directory's `SOURCE_PATHS.md`.

This is a legal-metadata snapshot, not a substitute for corresponding source.
The complete source remains available at the immutable references above and
is indexed in the repository-root `THIRD_PARTY_SOURCES.md`.
