# QtPdf Chromium/PDFium license-file mapping

Upstream repository:
<https://code.qt.io/cgit/qt/qtwebengine-chromium.git/>

Immutable commit: `5170777d28bee1ce92cc693a0dbf2ad01492e5cf`

All paths below are relative to the upstream `chromium/` directory.

| Local file | Exact upstream path |
| --- | --- |
| `chromium-LICENSE.txt` | `LICENSE` |
| `PDFium-LICENSE.txt` | `third_party/pdfium/LICENSE` |
| `abseil-LICENSE.txt` | `third_party/abseil-cpp/LICENSE` |
| `PDFium-FreeType-FTL.txt` | `third_party/pdfium/third_party/freetype/FTL.TXT` |
| `fast_float-README.chromium.txt` | `third_party/fast_float/README.chromium` |
| `fast_float-LICENSE-APACHE.txt` | `third_party/fast_float/src/LICENSE-APACHE` |
| `fast_float-LICENSE-BOOST.txt` | `third_party/fast_float/src/LICENSE-BOOST` |
| `fast_float-LICENSE-MIT.txt` | `third_party/fast_float/src/LICENSE-MIT` |
| `ICU-LICENSE.txt` | `third_party/icu/LICENSE` |
| `libjpeg-turbo-LICENSE.md` | `third_party/libjpeg_turbo/LICENSE.md` |
| `libjpeg-turbo-README.ijg.txt` | `third_party/libjpeg_turbo/README.ijg` |
| `libpng-LICENSE.txt` | `third_party/libpng/LICENSE` |
| `zlib-LICENSE.txt` | `third_party/zlib/LICENSE` |

All files are retained byte-for-byte except `PDFium-FreeType-FTL.txt`.
Upstream `FTL.TXT` contains a Windows-1252 copyright byte (`0xA9`), so OpenADB
transcodes that one file to UTF-8 for deterministic in-application display.
The raw upstream SHA-256 is
`f4b133e25df1f86ad3ffea453aa0e613f0474f34778dbbb3e437e7b2724937d8`;
the normalized local SHA-256 is
`4a9a548027a2c1d37788519dea833294c9c81f1ebc280e817f41f50d0c642d78`.

Qt's generated QtPdf licensing inventory is the authority for which of the
Chromium snapshot's third-party components are part of QtPdf. This repository
does not claim that the separate QtWebEngine browser binaries are shipped.
