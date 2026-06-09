from __future__ import annotations

import io
import json
import re
import shutil
import threading
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from PIL import Image

from .path_utils import ensure_dir, safe_filename
from .quiet_output import quiet_third_party_output
from .settings_manager import SettingsManager


class IconExtractor:
    CACHE_VERSION = "v5"

    def __init__(self, settings: SettingsManager) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._index: dict[str, str] | None = None
        self.refresh_root()
        self._migrate_legacy_cache(settings.temp_folder / "icon-cache")

    def refresh_root(self) -> None:
        with self._lock:
            self.cache_dir = ensure_dir(self.settings.config_dir / "icon-cache")
            self.index_path = self.cache_dir / "icon-index.json"
            self._index = None

    def cache_path(
        self,
        package_name: str,
        version_name: str = "",
        version_code: str = "",
        source_key: str = "",
    ) -> Path:
        key = safe_filename("_".join(part for part in [self.CACHE_VERSION, source_key, package_name, version_code] if part))
        return self.cache_dir / f"{key}.png"

    def cached_icon_path(
        self,
        package_name: str,
        version_name: str = "",
        version_code: str = "",
        source_keys: list[str] | None = None,
    ) -> Path | None:
        source_keys = source_keys or [""]
        with self._lock:
            for source_key in source_keys:
                path = self.cache_path(package_name, version_name, version_code, source_key=source_key)
                if self._valid_icon_file(path):
                    self._remember_icon(package_name, path)
                    return path

            indexed = Path(self._load_index().get(package_name, ""))
            if self._valid_icon_file(indexed):
                return indexed

            scanned = self._scan_latest_icon_for_package(package_name)
            if scanned:
                self._remember_icon(package_name, scanned)
                return scanned
        return None

    def extract_from_apk(
        self,
        apk_path: str | Path,
        package_name: str,
        version_name: str = "",
        version_code: str = "",
    ) -> Path | None:
        target = self.cache_path(package_name, version_name, version_code)
        if self._valid_icon_file(target):
            self._remember_icon(package_name, target)
            return target
        apk_path = Path(apk_path)
        if not apk_path.exists():
            return None
        apk_package = self._apk_package_name(apk_path)
        if apk_package and apk_package != package_name:
            return None
        try:
            with zipfile.ZipFile(apk_path) as archive:
                resource_id_map = self._resource_id_map(apk_path)
                candidates = self._icon_candidates(archive, apk_path, resource_id_map)
                for name in candidates:
                    try:
                        image = self._load_icon_image(archive, name, seen=set(), resource_id_map=resource_id_map)
                        if image is None:
                            continue
                        image.thumbnail((96, 96), Image.LANCZOS)
                        rgba = image.convert("RGBA")
                        self._save_icon(rgba, target, package_name)
                        return target
                    except Exception:
                        continue
        except (OSError, zipfile.BadZipFile):
            return None
        return None

    def import_icon_bytes(
        self,
        package_name: str,
        data: bytes,
        version_name: str = "",
        version_code: str = "",
        source_key: str = "",
    ) -> Path | None:
        target = self.cache_path(package_name, version_name, version_code, source_key=source_key)
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
            image.thumbnail((96, 96), Image.LANCZOS)
            self._save_icon(image.convert("RGBA"), target, package_name)
            return target
        except Exception:
            return None

    def import_pre_rendered_icon_batch(
        self,
        icons: list[tuple[str, bytes, str, str, str]],
    ) -> dict[str, Path]:
        """Store trusted PNG icons that were already rendered on the device.

        ACBridge exports normalized PNG files, so re-decoding and resizing every
        icon with Pillow only burns CPU and delays the Apps cache. This fast path
        validates the PNG signature, writes files atomically, and saves the icon
        index once for the whole batch.
        """
        saved: dict[str, Path] = {}
        if not icons:
            return saved
        with self._lock:
            index = self._load_index()
            for package_name, data, version_name, version_code, source_key in icons:
                if not package_name or not self._looks_like_png(data):
                    continue
                target = self.cache_path(package_name, version_name, version_code, source_key=source_key)
                try:
                    ensure_dir(target.parent)
                    temp = target.with_name(f"{target.stem}.{threading.get_ident()}.tmp{target.suffix}")
                    temp.write_bytes(data)
                    temp.replace(target)
                except OSError:
                    continue
                if self._valid_icon_file(target):
                    index[package_name] = str(target)
                    saved[package_name] = target
            self._save_index()
        return saved

    def _load_icon_image(
        self,
        archive: zipfile.ZipFile,
        name: str,
        seen: set[str],
        resource_id_map: dict[int, str] | None = None,
    ) -> Image.Image | None:
        if name in seen:
            return None
        seen.add(name)
        lower = name.lower()
        if lower.endswith((".png", ".webp", ".jpg", ".jpeg")):
            with archive.open(name) as fh:
                image = Image.open(fh)
                image.load()
                return image
        if lower.endswith(".xml"):
            refs = self._drawable_refs_from_xml(archive, name)
            images: list[Image.Image] = []
            for ref in refs:
                for candidate in self._paths_for_resource_ref(archive, ref, resource_id_map):
                    image = self._load_icon_image(archive, candidate, seen, resource_id_map)
                    if image is not None:
                        images.append(image.convert("RGBA"))
                        break
            if not images:
                for candidate in self._heuristic_xml_layer_candidates(archive, name):
                    image = self._load_icon_image(archive, candidate, seen, resource_id_map)
                    if image is not None:
                        images.append(image.convert("RGBA"))
                        break
            if len(images) >= 2:
                return self._composite_icon_layers(images[0], images[1])
            if images:
                return images[0]
        return None

    def clear_cache(self) -> None:
        with self._lock:
            for item in self.cache_dir.glob("*"):
                try:
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                except OSError:
                    continue
            ensure_dir(self.cache_dir)
            self._index = {}

    def _save_icon(self, image: Image.Image, target: Path, package_name: str) -> None:
        with self._lock:
            ensure_dir(target.parent)
            temp = target.with_name(f"{target.stem}.{threading.get_ident()}.tmp{target.suffix}")
            image.save(temp, "PNG")
            temp.replace(target)
            self._remember_icon(package_name, target)

    def _valid_icon_file(self, path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def _looks_like_png(self, data: bytes) -> bool:
        return len(data) > 8 and data.startswith(b"\x89PNG\r\n\x1a\n")

    def _load_index(self) -> dict[str, str]:
        if self._index is not None:
            return self._index
        try:
            loaded = json.loads(self.index_path.read_text(encoding="utf-8"))
            self._index = {str(key): str(value) for key, value in loaded.items()} if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            self._index = {}
        return self._index

    def _save_index(self) -> None:
        if self._index is None:
            return
        try:
            self.index_path.write_text(json.dumps(self._index, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _remember_icon(self, package_name: str, path: Path) -> None:
        if not package_name or not self._valid_icon_file(path):
            return
        index = self._load_index()
        index[package_name] = str(path)
        self._save_index()

    def _scan_latest_icon_for_package(self, package_name: str) -> Path | None:
        safe_package = safe_filename(package_name)
        candidates: list[Path] = []
        patterns = [
            f"{self.CACHE_VERSION}_{safe_package}.png",
            f"{self.CACHE_VERSION}_{safe_package}_*.png",
            f"{self.CACHE_VERSION}_*_{safe_package}.png",
            f"{self.CACHE_VERSION}_*_{safe_package}_*.png",
        ]
        for pattern in patterns:
            candidates.extend(path for path in self.cache_dir.glob(pattern) if self._valid_icon_file(path))
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def _migrate_legacy_cache(self, legacy_dir: Path) -> None:
        try:
            if legacy_dir.resolve() == self.cache_dir.resolve() or not legacy_dir.exists():
                return
        except OSError:
            return
        for item in legacy_dir.glob("*.png"):
            target = self.cache_dir / item.name
            if target.exists():
                continue
            try:
                shutil.copy2(item, target)
            except OSError:
                continue

    def _icon_candidates(
        self,
        archive: zipfile.ZipFile,
        apk_path: Path,
        resource_id_map: dict[int, str] | None = None,
    ) -> list[str]:
        names = archive.namelist()
        candidates: list[str] = []

        for ref in self._manifest_icon_refs(apk_path):
            candidates.extend(self._paths_for_resource_ref(archive, ref, resource_id_map))

        app_icon_path = self._apkutils_app_icon_path(apk_path)
        if app_icon_path:
            candidates.append(app_icon_path)

        # Do not scan arbitrary "icon"/"launcher" image names. That can pick
        # unrelated artwork from split APKs and show another app's icon. If the
        # manifest does not expose a usable launcher icon, the UI uses fallback.
        unique = [name for name in dict.fromkeys(candidates) if name in names]
        unique.sort(key=self._candidate_rank)
        return unique

    def _manifest_icon_refs(self, apk_path: Path) -> list[str]:
        try:
            import apkutils2
        except ImportError:
            return []
        refs: list[str] = []
        try:
            with quiet_third_party_output():
                apk = apkutils2.APK(str(apk_path))
                manifest = apk.get_manifest() or {}
                org_manifest = apk.get_org_manifest() or ""
            application = manifest.get("application", {}) if isinstance(manifest, dict) else {}
            if isinstance(application, list):
                application = application[0] if application else {}
            if isinstance(application, dict):
                for name in ("icon", "roundIcon"):
                    value = self._attr(application, name)
                    if value:
                        refs.append(value)
            refs.extend(self._icon_refs_from_xml(org_manifest))
        except Exception:
            return refs
        return list(dict.fromkeys(refs))

    def _heuristic_xml_layer_candidates(self, archive: zipfile.ZipFile, xml_name: str) -> list[str]:
        stem = Path(xml_name).stem
        wanted = {
            stem.replace("ic_launcher", "ic_launcher_foreground"),
            stem.replace("launcher", "adaptive_foreground"),
            stem.replace("launcher", "foreground"),
            f"{stem}_foreground",
        }
        tokens = [token for token in re.split(r"[_\\W]+", stem.lower()) if token and token not in {"ic", "launcher"}]
        candidates = []
        for name in archive.namelist():
            lower = name.lower()
            if not lower.endswith((".png", ".webp", ".jpg", ".jpeg", ".xml")):
                continue
            if not (lower.startswith("res/mipmap") or lower.startswith("res/drawable")):
                continue
            candidate_stem = Path(name).stem
            candidate_lower = candidate_stem.lower()
            if candidate_stem in wanted:
                candidates.append(name)
                continue
            if tokens and "foreground" in candidate_lower and all(token in candidate_lower for token in tokens):
                candidates.append(name)
        candidates.sort(key=self._candidate_rank)
        return candidates

    def _apkutils_app_icon_path(self, apk_path: Path) -> str:
        try:
            import apkutils2
        except ImportError:
            return ""
        try:
            with quiet_third_party_output():
                return str(apkutils2.APK(str(apk_path)).get_app_icon() or "")
        except Exception:
            return ""

    def _apk_package_name(self, apk_path: Path) -> str:
        try:
            import apkutils2
        except ImportError:
            return ""
        try:
            with quiet_third_party_output():
                manifest = apkutils2.APK(str(apk_path)).get_manifest() or {}
            if not isinstance(manifest, dict):
                return ""
            value = manifest.get("@package") or manifest.get("package")
            return str(value or "").strip()
        except Exception:
            return ""

    def _icon_refs_from_xml(self, xml_text: str) -> list[str]:
        if not xml_text:
            return []
        application_match = re.search(r"<application\b[^>]*>", xml_text, re.IGNORECASE)
        if not application_match:
            return []
        tag = application_match.group(0)
        refs: list[str] = []
        for attr in ("icon", "roundIcon", "logo"):
            match = re.search(rf'(?:android:)?{attr}="([^"]+)"', tag)
            if match:
                refs.append(match.group(1).strip())
        return refs

    def _attr(self, data, name: str) -> str:
        if not isinstance(data, dict):
            return ""
        for key in (f"@android:{name}", f"android:{name}", f"@{name}", name):
            value = data.get(key)
            if value is not None:
                return str(value).strip()
        return ""

    def _paths_for_resource_ref(
        self,
        archive: zipfile.ZipFile,
        ref: str,
        resource_id_map: dict[int, str] | None = None,
    ) -> list[str]:
        ref = (ref or "").strip()
        if not ref.startswith("@"):
            return []
        raw = ref.lstrip("@")
        if "/" not in raw:
            resource_id = self._parse_resource_id(ref)
            mapped_ref = resource_id_map.get(resource_id, "") if resource_id_map and resource_id is not None else ""
            if mapped_ref and mapped_ref != ref:
                return self._paths_for_resource_ref(archive, mapped_ref, resource_id_map)
            return []
        if ":" in raw:
            raw = raw.split(":", 1)[1]
        resource_type, resource_name = raw.split("/", 1)
        resource_name = resource_name.split("#", 1)[0]
        result = []
        for name in archive.namelist():
            path = Path(name)
            lower = name.lower()
            if not lower.startswith(f"res/{resource_type}".lower()):
                continue
            if path.stem == resource_name and lower.endswith((".png", ".webp", ".jpg", ".jpeg", ".xml")):
                result.append(name)
        result.sort(key=self._candidate_rank)
        return result

    def _resource_id_map(self, apk_path: Path) -> dict[int, str]:
        try:
            import apkutils2
        except ImportError:
            return {}
        try:
            with quiet_third_party_output():
                apk = apkutils2.APK(str(apk_path))
                arsc = apk.get_arsc()
                if arsc is None:
                    return {}
                packages = arsc.get_packages_names()
                xml_chunks = [arsc.get_public_resources(package) for package in packages]
        except Exception:
            return {}
        result: dict[int, str] = {}
        for xml_text in xml_chunks:
            if isinstance(xml_text, bytes):
                xml_text = xml_text.decode("utf-8", "replace")
            for resource_type, resource_name, resource_id in re.findall(
                r'<public\s+type="([^"]+)"\s+name="([^"]+)"\s+id="([^"]+)"', xml_text or ""
            ):
                parsed = self._parse_resource_id(resource_id)
                if parsed is not None:
                    result[parsed] = f"@{resource_type}/{resource_name}"
        return result

    def _drawable_refs_from_xml(self, archive: zipfile.ZipFile, name: str) -> list[str]:
        try:
            with archive.open(name) as fh:
                data = fh.read()
        except Exception:
            return []
        text = self._decode_resource_xml(data)
        if not text:
            return []
        refs: list[str] = []
        try:
            root = ET.fromstring(text)
            for element in root.iter():
                for key, value in element.attrib.items():
                    local_name = key.rsplit("}", 1)[-1]
                    if local_name in {"foreground", "background", "src", "drawable"} and value.startswith("@"):
                        refs.append(value)
        except ET.ParseError:
            refs.extend(re.findall(r'="(@(?:mipmap|drawable)/[^"]+)"', text))
        return list(dict.fromkeys(refs))

    def _decode_resource_xml(self, data: bytes) -> str:
        text = data.decode("utf-8", "replace")
        if "<" in text[:80]:
            return text
        try:
            from apkutils2.axml.axmlparser import AXML
        except ImportError:
            return ""
        try:
            with quiet_third_party_output():
                axml = AXML(data)
                return axml.get_xml() if axml.is_valid else ""
        except Exception:
            return ""

    def _parse_resource_id(self, value: str) -> int | None:
        raw = value.strip().lstrip("@")
        if ":" in raw:
            raw = raw.split(":", 1)[1]
        if raw.startswith("+"):
            raw = raw[1:]
        if raw.startswith("0x"):
            raw = raw[2:]
        if not raw:
            return None
        try:
            if raw.isdigit() and len(raw) > 8:
                return int(raw, 10)
            return int(raw, 16)
        except ValueError:
            return None

    def _composite_icon_layers(self, background: Image.Image, foreground: Image.Image) -> Image.Image:
        size = max(background.width, background.height, foreground.width, foreground.height, 96)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        bg = background.copy()
        bg.thumbnail((size, size), Image.LANCZOS)
        canvas.alpha_composite(bg, ((size - bg.width) // 2, (size - bg.height) // 2))
        fg = foreground.copy()
        fg.thumbnail((int(size * 0.82), int(size * 0.82)), Image.LANCZOS)
        canvas.alpha_composite(fg, ((size - fg.width) // 2, (size - fg.height) // 2))
        return canvas

    def _candidate_rank(self, value: str) -> tuple[int, int, int, int]:
        lower = value.lower()
        density_order = ["xxxhdpi", "xxhdpi", "xhdpi", "hdpi", "mdpi", "nodpi", "anydpi"]
        density = next((index for index, token in enumerate(density_order) if token in lower), len(density_order))
        exact = 0 if Path(value).stem.lower() in {"ic_launcher", "ic_launcher_round"} else 1
        raster = 0 if lower.endswith((".png", ".webp", ".jpg", ".jpeg")) else 1
        return exact, density, raster, len(value)
