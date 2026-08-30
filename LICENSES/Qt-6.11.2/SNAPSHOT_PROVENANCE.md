# Qt 6.11.2 legal-source snapshot provenance

This directory preserves the license, attribution, and REUSE metadata needed
to audit the Qt modules collected from the official PySide6 6.11.2 Windows
wheels. Files retain their paths relative to the named upstream repositories.
Source bytes are preserved except for the single documented Windows-1252 to
UTF-8 normalization in `qtwebengine-chromium-5170777/SOURCE_PATHS.md`. The
immutable peeled commits are:

| Directory | Official repository | Peeled commit |
| --- | --- | --- |
| `qtbase/` | <https://code.qt.io/cgit/qt/qtbase.git/> | `ef55f427f2c8b410d34f8a7681020a3000cf6866` |
| `qtdeclarative/` | <https://code.qt.io/cgit/qt/qtdeclarative.git/> | `4e3399c26ec57246c08de019cfcbda8d23604cfa` |
| `qtimageformats/` | <https://code.qt.io/cgit/qt/qtimageformats.git/> | `47b6139dda3b84d1d3ec15caf8d04eff8d744c8d` |
| `qtsvg/` | <https://code.qt.io/cgit/qt/qtsvg.git/> | `17ca512f903f935282ebeca496aac5d11ba4199a` |
| `qtvirtualkeyboard/` | <https://code.qt.io/cgit/qt/qtvirtualkeyboard.git/> | `043d7629ccb62ce09b8c30cba7cc2a97248dfea9` |
| `qtwebengine/` (QtPdf module metadata) | <https://code.qt.io/cgit/qt/qtwebengine.git/> | `a33fa2a897e5ee58e385b3f88dc247d99fca56db` |

QtWebEngine records its Chromium/PDFium source as the `src/3rdparty`
submodule at commit `5170777d28bee1ce92cc693a0dbf2ad01492e5cf` in
<https://code.qt.io/cgit/qt/qtwebengine-chromium.git/>. The
`qtwebengine-chromium-5170777/` directory contains the exact upstream license
and metadata files for the third-party components listed by Qt's QtPdf
licensing page: Chromium, PDFium, Abseil, FreeType, fast_float, ICU,
libjpeg-turbo, libpng, and zlib. Each filename is mapped to its original path
in that directory's `SOURCE_PATHS.md`.

This is a legal-metadata snapshot, not a substitute for corresponding source.
The complete source remains available at the immutable references above and
is indexed in the repository-root `THIRD_PARTY_SOURCES.md`.
