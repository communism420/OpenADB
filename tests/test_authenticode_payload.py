from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from tools.verify_authenticode_payload import (
    AuthenticodePayloadError,
    verify_authenticode_payload,
)


def _unsigned_pe() -> bytes:
    data = bytearray(512)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    optional_offset = 0x80 + 4 + 20
    struct.pack_into("<H", data, 0x80 + 4 + 16, 0xF0)
    struct.pack_into("<H", data, optional_offset, 0x20B)
    struct.pack_into("<I", data, optional_offset + 108, 16)
    data[384:] = bytes(range(128))
    return bytes(data)


def _signed_pe(unsigned: bytes) -> bytes:
    data = bytearray(unsigned)
    optional_offset = 0x80 + 4 + 20
    struct.pack_into("<I", data, optional_offset + 64, 0x12345678)
    certificate_offset = (len(data) + 7) & ~7
    data.extend(b"\0" * (certificate_offset - len(data)))
    certificate = bytearray(16)
    struct.pack_into("<IHH", certificate, 0, 16, 0x0200, 0x0002)
    certificate[8:] = b"\x30\x06\x02\x01\x01\x02\x01\x02"
    data.extend(certificate)
    security_directory_offset = optional_offset + 112 + (4 * 8)
    struct.pack_into("<II", data, security_directory_offset, certificate_offset, 16)
    return bytes(data)


class AuthenticodePayloadTests(unittest.TestCase):
    def _verify(self, unsigned: bytes, signed: bytes) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            unsigned_path = root / "unsigned.exe"
            signed_path = root / "signed.exe"
            unsigned_path.write_bytes(unsigned)
            signed_path.write_bytes(signed)
            return verify_authenticode_payload(unsigned_path, signed_path)

    def test_accepts_only_authenticode_envelope_changes(self) -> None:
        unsigned = _unsigned_pe()
        signed = _signed_pe(unsigned)
        result = self._verify(unsigned, signed)
        self.assertEqual(result["unsigned_size"], len(unsigned))
        self.assertEqual(result["signed_size"], len(signed))
        self.assertNotEqual(result["unsigned_sha256"], result["signed_sha256"])

    def test_accepts_zero_alignment_padding_included_in_dwlength(self) -> None:
        unsigned = _unsigned_pe()
        signed = bytearray(unsigned)
        optional_offset = 0x80 + 4 + 20
        security_directory_offset = optional_offset + 112 + (4 * 8)
        certificate_offset = len(unsigned)
        der = b"\x30\x09\x02\x01\x01\x02\x01\x02\x02\x01\x03"
        certificate = bytearray(24)
        struct.pack_into("<IHH", certificate, 0, 24, 0x0200, 0x0002)
        certificate[8 : 8 + len(der)] = der
        signed.extend(certificate)
        struct.pack_into("<II", signed, security_directory_offset, certificate_offset, 24)

        result = self._verify(unsigned, bytes(signed))

        self.assertEqual(result["certificate_size"], 24)

    def test_rejects_payload_tampering(self) -> None:
        unsigned = _unsigned_pe()
        signed = bytearray(_signed_pe(unsigned))
        signed[400] ^= 0xFF
        with self.assertRaisesRegex(AuthenticodePayloadError, "bytes outside"):
            self._verify(unsigned, bytes(signed))

    def test_rejects_existing_unsigned_certificate_table(self) -> None:
        unsigned = bytearray(_unsigned_pe())
        optional_offset = 0x80 + 4 + 20
        security_directory_offset = optional_offset + 112 + (4 * 8)
        struct.pack_into("<II", unsigned, security_directory_offset, 496, 16)
        with self.assertRaisesRegex(AuthenticodePayloadError, "already has"):
            self._verify(bytes(unsigned), _signed_pe(_unsigned_pe()))

    def test_rejects_trailing_data_after_certificate(self) -> None:
        unsigned = _unsigned_pe()
        signed = _signed_pe(unsigned) + b"unexpected"
        with self.assertRaisesRegex(AuthenticodePayloadError, "after"):
            self._verify(unsigned, signed)

    def test_rejects_extra_declared_data_after_the_pkcs7_record(self) -> None:
        unsigned = _unsigned_pe()
        signed = bytearray(_signed_pe(unsigned))
        optional_offset = 0x80 + 4 + 20
        security_directory_offset = optional_offset + 112 + (4 * 8)
        signed.extend(b"EVILDATA")
        struct.pack_into("<I", signed, security_directory_offset + 4, 24)
        with self.assertRaisesRegex(AuthenticodePayloadError, "WIN_CERTIFICATE"):
            self._verify(unsigned, bytes(signed))

    def test_rejects_nonzero_win_certificate_padding(self) -> None:
        unsigned = _unsigned_pe()
        signed = bytearray(_signed_pe(unsigned))
        certificate_offset = len(unsigned)
        struct.pack_into("<I", signed, certificate_offset, 15)
        signed[certificate_offset + 8 : certificate_offset + 15] = (
            b"\x30\x05\x02\x01\x01\x05\x00"
        )
        signed[certificate_offset + 15] = 0x7F
        with self.assertRaisesRegex(AuthenticodePayloadError, "padding"):
            self._verify(unsigned, bytes(signed))

    def test_rejects_oversized_zero_gap_before_certificate(self) -> None:
        unsigned = _unsigned_pe()
        signed = bytearray(unsigned)
        optional_offset = 0x80 + 4 + 20
        security_directory_offset = optional_offset + 112 + (4 * 8)
        certificate_offset = len(unsigned) + 8
        signed.extend(b"\0" * 8)
        certificate = bytearray(16)
        struct.pack_into("<IHH", certificate, 0, 16, 0x0200, 0x0002)
        certificate[8:] = b"\x30\x06\x02\x01\x01\x02\x01\x02"
        signed.extend(certificate)
        struct.pack_into("<II", signed, security_directory_offset, certificate_offset, 16)
        with self.assertRaisesRegex(AuthenticodePayloadError, "immediately"):
            self._verify(unsigned, bytes(signed))

    def test_rejects_cve_2013_3900_style_bytes_inside_win_certificate(self) -> None:
        unsigned = _unsigned_pe()
        signed = bytearray(_signed_pe(unsigned))
        optional_offset = 0x80 + 4 + 20
        security_directory_offset = optional_offset + 112 + (4 * 8)
        certificate_offset = len(unsigned)
        signed.extend(b"EVILDATA")
        struct.pack_into("<I", signed, certificate_offset, 24)
        struct.pack_into("<I", signed, security_directory_offset + 4, 24)
        with self.assertRaisesRegex(AuthenticodePayloadError, "single DER object"):
            self._verify(unsigned, bytes(signed))


if __name__ == "__main__":
    unittest.main()
