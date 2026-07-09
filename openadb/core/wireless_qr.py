from __future__ import annotations

import secrets
import string
from dataclasses import dataclass


_SERVICE_SUFFIX_ALPHABET = string.ascii_letters + string.digits
_PASSWORD_ALPHABET = string.ascii_letters + string.digits


@dataclass(frozen=True, slots=True)
class WirelessQrPayload:
    service_name: str
    password: str
    qr_text: str


def generate_wireless_qr_payload() -> WirelessQrPayload:
    service_name = "studio-" + _random_text(_SERVICE_SUFFIX_ALPHABET, 10)
    password = _random_text(_PASSWORD_ALPHABET, 12)
    qr_text = f"WIFI:T:ADB;S:{_escape_wifi_qr_value(service_name)};P:{_escape_wifi_qr_value(password)};;"
    return WirelessQrPayload(service_name=service_name, password=password, qr_text=qr_text)


def _random_text(alphabet: str, length: int) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _escape_wifi_qr_value(value: str) -> str:
    # ZXing-style Wi-Fi QR fields use semicolon-separated tokens. Keep this
    # generic so future password alphabets with separators remain safe.
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace(":", "\\:")
        .replace('"', '\\"')
    )
