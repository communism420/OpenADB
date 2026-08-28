from __future__ import annotations

import argparse
import json
import struct
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


class AuthenticodePayloadError(ValueError):
    """Raised when signing changed bytes outside the PE Authenticode envelope."""


@dataclass(frozen=True)
class PeSecurityLayout:
    checksum_offset: int
    security_directory_offset: int
    certificate_offset: int
    certificate_size: int


def _u16(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise AuthenticodePayloadError("truncated PE header")
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise AuthenticodePayloadError("truncated PE header")
    return struct.unpack_from("<I", data, offset)[0]


def _parse_layout(data: bytes) -> PeSecurityLayout:
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise AuthenticodePayloadError("input is not a DOS/PE image")
    pe_offset = _u32(data, 0x3C)
    if pe_offset < 0x40 or pe_offset + 24 > len(data):
        raise AuthenticodePayloadError("invalid PE header offset")
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise AuthenticodePayloadError("missing PE signature")

    coff_offset = pe_offset + 4
    optional_size = _u16(data, coff_offset + 16)
    optional_offset = coff_offset + 20
    optional_end = optional_offset + optional_size
    if optional_end > len(data):
        raise AuthenticodePayloadError("truncated PE optional header")

    magic = _u16(data, optional_offset)
    if magic == 0x10B:
        number_of_directories_offset = optional_offset + 92
        data_directories_offset = optional_offset + 96
    elif magic == 0x20B:
        number_of_directories_offset = optional_offset + 108
        data_directories_offset = optional_offset + 112
    else:
        raise AuthenticodePayloadError(f"unsupported PE optional-header magic: 0x{magic:04x}")

    if _u32(data, number_of_directories_offset) <= 4:
        raise AuthenticodePayloadError("PE image has no certificate-table directory")
    checksum_offset = optional_offset + 64
    security_directory_offset = data_directories_offset + (4 * 8)
    if security_directory_offset + 8 > optional_end:
        raise AuthenticodePayloadError("certificate-table directory is outside the optional header")

    return PeSecurityLayout(
        checksum_offset=checksum_offset,
        security_directory_offset=security_directory_offset,
        certificate_offset=_u32(data, security_directory_offset),
        certificate_size=_u32(data, security_directory_offset + 4),
    )


def _normalized_prefix(data: bytes, layout: PeSecurityLayout, length: int) -> bytes:
    if length > len(data):
        raise AuthenticodePayloadError("normalized prefix exceeds the input")
    normalized = bytearray(data[:length])
    for offset, size in (
        (layout.checksum_offset, 4),
        (layout.security_directory_offset, 8),
    ):
        if offset + size > length:
            raise AuthenticodePayloadError("mutable Authenticode field is outside the PE prefix")
        normalized[offset : offset + size] = b"\0" * size
    return bytes(normalized)


def _der_object_length(data: bytes) -> int:
    """Return the size of one canonical definite-length DER SEQUENCE."""

    if len(data) < 2 or data[0] != 0x30:
        raise AuthenticodePayloadError("the PKCS#7 payload is not a DER SEQUENCE")
    first_length = data[1]
    if first_length < 0x80:
        header_size = 2
        content_size = first_length
    else:
        length_octets = first_length & 0x7F
        if (
            first_length == 0x80
            or length_octets == 0
            or length_octets > 4
            or 2 + length_octets > len(data)
        ):
            raise AuthenticodePayloadError("the PKCS#7 DER length is invalid")
        encoded_length = data[2 : 2 + length_octets]
        if encoded_length[0] == 0:
            raise AuthenticodePayloadError("the PKCS#7 DER length is not canonical")
        content_size = int.from_bytes(encoded_length, "big")
        if content_size < 0x80:
            raise AuthenticodePayloadError("the PKCS#7 DER length is not canonical")
        header_size = 2 + length_octets

    object_size = header_size + content_size
    if object_size > len(data):
        raise AuthenticodePayloadError("the PKCS#7 DER object is truncated")
    return object_size


def verify_authenticode_payload(unsigned_path: Path, signed_path: Path) -> dict[str, object]:
    unsigned = unsigned_path.read_bytes()
    signed = signed_path.read_bytes()
    if not unsigned or not signed:
        raise AuthenticodePayloadError("unsigned and signed inputs must be non-empty")

    unsigned_layout = _parse_layout(unsigned)
    signed_layout = _parse_layout(signed)
    if (
        unsigned_layout.checksum_offset != signed_layout.checksum_offset
        or unsigned_layout.security_directory_offset
        != signed_layout.security_directory_offset
    ):
        raise AuthenticodePayloadError("PE header layout changed during signing")
    if unsigned_layout.certificate_offset != 0 or unsigned_layout.certificate_size != 0:
        raise AuthenticodePayloadError("the purportedly unsigned input already has a certificate table")

    certificate_offset = signed_layout.certificate_offset
    certificate_size = signed_layout.certificate_size
    certificate_end = certificate_offset + certificate_size
    expected_certificate_offset = (len(unsigned) + 7) & ~7
    if certificate_offset != expected_certificate_offset:
        raise AuthenticodePayloadError(
            "the certificate table does not immediately follow the aligned unsigned payload"
        )
    if certificate_offset % 8 != 0 or certificate_size < 8 or certificate_size % 8 != 0:
        raise AuthenticodePayloadError("the certificate table is missing or not 8-byte aligned")
    if certificate_end != len(signed):
        raise AuthenticodePayloadError("unexpected data exists after the certificate table")
    if any(signed[len(unsigned) : certificate_offset]):
        raise AuthenticodePayloadError("non-zero bytes were inserted before the certificate table")

    if _normalized_prefix(unsigned, unsigned_layout, len(unsigned)) != _normalized_prefix(
        signed, signed_layout, len(unsigned)
    ):
        raise AuthenticodePayloadError(
            "bytes outside the PE checksum and certificate directory changed during signing"
        )

    win_certificate_length = _u32(signed, certificate_offset)
    win_certificate_revision = _u16(signed, certificate_offset + 4)
    win_certificate_type = _u16(signed, certificate_offset + 6)
    win_certificate_end = certificate_offset + win_certificate_length
    aligned_win_certificate_end = (win_certificate_end + 7) & ~7
    if (
        win_certificate_length <= 8
        or aligned_win_certificate_end != certificate_end
        or win_certificate_revision != 0x0200
        or win_certificate_type != 0x0002
    ):
        raise AuthenticodePayloadError("the WIN_CERTIFICATE header is invalid")
    payload_offset = certificate_offset + 8
    der_size = _der_object_length(signed[payload_offset:win_certificate_end])
    der_end = payload_offset + der_size
    aligned_der_end = (der_end + 7) & ~7
    if aligned_der_end != certificate_end or win_certificate_end < der_end:
        raise AuthenticodePayloadError(
            "the WIN_CERTIFICATE contains bytes outside the single DER object"
        )
    if any(signed[der_end:certificate_end]):
        raise AuthenticodePayloadError("the WIN_CERTIFICATE alignment padding is not zero")

    return {
        "unsigned_sha256": sha256(unsigned).hexdigest(),
        "signed_sha256": sha256(signed).hexdigest(),
        "unsigned_size": len(unsigned),
        "signed_size": len(signed),
        "certificate_offset": certificate_offset,
        "certificate_size": certificate_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that Authenticode only changed permitted PE envelope bytes."
    )
    parser.add_argument("unsigned", type=Path)
    parser.add_argument("signed", type=Path)
    args = parser.parse_args()
    result = verify_authenticode_payload(args.unsigned, args.signed)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
