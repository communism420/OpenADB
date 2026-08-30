# Qt 6.11.2 third-party notices for the OpenADB Windows payload

This file is an offline attribution record for the Qt modules collected by the
PySide6 6.11.2 wheels used by OpenADB. The Qt source references below are
immutable `v6.11.2` tag commits. The standard license texts named here are
copied in full in the adjacent `LICENSES/` files in this repository; this file
preserves the component copyright, license, and disclaimer notices rather than
substituting links for those texts.

## Qt module source revisions

The Qt supermodule `v6.11.2` peeled commit is
[`713a36536903d172f9e6737584d428753c119496`](https://code.qt.io/cgit/qt/qt5.git/tag/?h=v6.11.2). The Qt for Python superproject
`v6.11.2` peeled commit is
[`24627cd36e1593adf22eb1f2950e4248e7bcc1ec`](https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.11.2).

The release workflow's PyInstaller archive audit requires `Qt6Pdf.dll` and
rejects QtWebEngine browser binaries; therefore QtPdf is shipped while the
browser runtime is not claimed. The payload contains the following modules and
plugins from the official PySide6 Community Edition wheels:

| Module/source tree | Pinned peeled commit | Primary open-source terms |
| --- | --- | --- |
| qtbase | `ef55f427f2c8b410d34f8a7681020a3000cf6866` | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only; selected downstream basis: GPL-3.0-only |
| qtdeclarative | `4e3399c26ec57246c08de019cfcbda8d23604cfa` | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only; selected downstream basis: GPL-3.0-only |
| qtimageformats | `47b6139dda3b84d1d3ec15caf8d04eff8d744c8d` | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only; bundled image codecs retain their own terms |
| qtsvg | `17ca512f903f935282ebeca496aac5d11ba4199a` | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| qtvirtualkeyboard | `043d7629ccb62ce09b8c30cba7cc2a97248dfea9` | GPL-3.0-only; bundled input engines retain their own terms |
| QtPdf sources in the QtWebEngine source tree | `a33fa2a897e5ee58e385b3f88dc247d99fca56db`; Chromium/PDFium submodule `5170777d28bee1ce92cc693a0dbf2ad01492e5cf` | LGPL-3.0-only OR GPL-2.0-only; OpenADB uses the LGPL-3.0 option. `Qt6Pdf.dll` is present in the reviewed PyInstaller payload; QtWebEngine browser binaries are not claimed. |

The Qt supermodule records the exact submodule revisions for each module. The
complete retained legal-source snapshot, including every collected
`qt_attribution.json`, `REUSE.toml`, module `LICENSES/` file, and the exact
QtPdf Chromium/PDFium license files, is under `LICENSES/Qt-6.11.2/`; see its
`SNAPSHOT_PROVENANCE.md`. A release must regenerate and review this inventory
if the PySide wheel or PyInstaller payload changes. `QtWebEngine` browser
binaries are not asserted as shipped; the QtWebEngine source reference is
needed because it contains the source of the shipped `Qt6Pdf.dll`.

## Qt copyright and license notice

Copyright (C) The Qt Company Ltd. and other Qt Project contributors.

Qt is available under the GNU General Public License version 3, and many Qt
modules are additionally available under the GNU Lesser General Public License
version 3. OpenADB selects the GPL v3 option for the Qt/PySide distributions
used in this GPL distribution where that option is offered. QtPdf is used
under its LGPL v3 option. The complete GPL text is reproduced in
`LICENSES/GPL-3.0.txt`; the complete LGPL v3 text is reproduced in
`LICENSES/LGPL-3.0.txt`. Qt Virtual Keyboard is GPL-3.0-only in the Qt
open-source source tree.

Qt and Qt for Python are provided “AS IS”, without warranty of any kind, under
the applicable license. OpenADB does not claim authorship or ownership of Qt,
PySide6, Shiboken6, or their third-party code. The complete standard license
texts and Qt GPL exception text are retained in the repository files named
above and the exact Qt exception at
`LICENSES/Qt-6.11.2/qtbase/LICENSES/Qt-GPL-exception-1.0.txt`.

## Component notices retained from Qt v6.11.2 REUSE metadata

The following prominent notices are reproduced for convenience. The
source-derived metadata under `LICENSES/Qt-6.11.2/` is authoritative; its
provenance file documents the single display-safe encoding normalization
and contains the complete offline inventory, including notices not repeated
in this summary. Copyright, conditions, and disclaimer wording must remain
intact in every release legal bundle.

### JavaScriptCore MASM (qtdeclarative)

Copyright (C) 2012 Apple Inc. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the above copyright notice, the
conditions, and the following disclaimer are retained in source and binary
redistributions.

THIS SOFTWARE IS PROVIDED BY APPLE INC. “AS IS” AND ANY EXPRESS OR IMPLIED
WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE, ARE DISCLAIMED. IN NO
EVENT SHALL APPLE INC. OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

### Yoga (qtdeclarative)

MIT License. Copyright (c) Facebook, Inc. and its affiliates.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the “Software”), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

### libwebp (qtimageformats)

Copyright (c) 2010, Google Inc. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the copyright notice, conditions,
and disclaimer are retained. Neither the name of Google nor the names of its
contributors may be used to endorse or promote products derived from this
software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS “AS IS”
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE,
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE
GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT
OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH
DAMAGE.

### Qt Virtual Keyboard input engines

OpenWnn notice: Copyright (C) 2008-2012 OMRON SOFTWARE Co., Ltd. The source
tree supplies the Apache License, Version 2.0 and requires retention of
copyright, patent, trademark, and attribution notices. Its exact upstream
notice is retained at
`Qt-6.11.2/qtvirtualkeyboard/src/plugins/openwnn/3rdparty/openwnn/NOTICE`.

Pinyin notice: Copyright (c) 2009, The Android Open Source Project. The source
tree supplies the Apache License, Version 2.0 and its standard “AS IS”,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND disclaimer. Its exact upstream
notice is retained at
`Qt-6.11.2/qtvirtualkeyboard/src/plugins/pinyin/3rdparty/pinyin/NOTICE`.

TCIME notice: Copyright 2010 Google Inc. The bundled libTabe data also retains
Copyright (c) 1999 TaBE Project, Copyright (c) 1999 Pai-Hsiang Hsiao,
Copyright (c) 1999 Computer Systems and Communication Lab, Institute of
Information Science, Academia Sinica, and Copyright 1996 Chih-Hao Tsai,
Beckman Institute, University of Illinois. The exact Apache and BSD-style
terms, conditions, and disclaimers are retained at
`Qt-6.11.2/qtvirtualkeyboard/src/plugins/tcime/3rdparty/tcime/COPYING`.

## License-text inventory

The complete, unabridged standard texts needed by the above notices are kept
in the pinned source snapshots under `LICENSES/Qt-6.11.2/<module>/LICENSES/`.
The repository-level copies used by the broader OpenADB notice bundle are
`LICENSES/Apache-2.0.txt`, `LICENSES/GPL-3.0.txt`, and
`LICENSES/LGPL-3.0.txt`; all other Qt license IDs are preserved at their exact
source paths under this directory. The module-specific notices above preserve
the copyright and disclaimer text that is not supplied by generic license
files.

QtPdf is claimed because `Qt6Pdf.dll` is present in the release archive that
the workflow inspects. No QtWebEngine browser binary is claimed. Every future
release must rerun that archive audit before retaining either claim.
