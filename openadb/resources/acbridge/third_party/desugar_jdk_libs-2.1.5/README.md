# Core-library desugaring 2.1.5

Shizuku API 13.1.5 requires core-library desugaring when an app keeps Android
API 23 support. ACBridge's non-Gradle build therefore vendors these unmodified
Google Maven artifacts and verifies their SHA-256 before running D8/L8.

| Artifact | Google Maven URL | SHA-256 |
| --- | --- | --- |
| `desugar_jdk_libs-2.1.5.jar` | `https://dl.google.com/dl/android/maven2/com/android/tools/desugar_jdk_libs/2.1.5/desugar_jdk_libs-2.1.5.jar` | `d8044befae095781b9a80bf1faa92edc30382d75d437476784c1bf991598a976` |
| `desugar_jdk_libs_configuration-2.1.5.jar` | `https://dl.google.com/dl/android/maven2/com/android/tools/desugar_jdk_libs_configuration/2.1.5/desugar_jdk_libs_configuration-2.1.5.jar` | `7bc9051b3a1ec19806311dcb6aa9b9ba7ef9c22caa6f4810da55bde285fb7770` |

Upstream source: <https://github.com/google/desugar_jdk_libs>

The library is distributed under GPL-2.0 with the Classpath Exception; see
`LICENSE-desugar_jdk_libs.txt`. The configuration helper carries the R8 BSD
license; see `LICENSE-configuration.txt`.
