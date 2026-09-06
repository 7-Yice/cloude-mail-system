from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from correspondence_identity import (
    IdentityError,
    address_from_received_mail,
    parse_args,
)


class ArchiveIdValidationTests(unittest.TestCase):
    def test_raw_sha256_gets_specific_format_error(self) -> None:
        with self.assertRaisesRegex(IdentityError, "64-character raw_sha256"):
            address_from_received_mail(Path("/unused"), "a" * 64)

    def test_archive_filename_gets_format_hint(self) -> None:
        with self.assertRaisesRegex(IdentityError, "archive filename or raw_sha256"):
            address_from_received_mail(Path("/unused"), "mail-record.json")

    def test_well_formed_unknown_id_keeps_lookup_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(IdentityError, "was not found uniquely"):
                address_from_received_mail(
                    Path(directory), "arc_0123456789abcdef01234567"
                )

    def test_well_formed_id_still_reads_received_mail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            incoming = Path(directory) / "incoming"
            incoming.mkdir()
            raw = b"From: Pen Pal <friend@example.com>\r\nSubject: hello\r\n\r\nHi\r\n"
            (incoming / "message.eml").write_bytes(raw)
            (incoming / "manifest.json").write_text(
                json.dumps(
                    {
                        "archive_id": "arc_0123456789abcdef01234567",
                        "direction": "incoming",
                        "original_file": "message.eml",
                        "raw_sha256": hashlib.sha256(raw).hexdigest(),
                        "from": "Pen Pal <friend@example.com>",
                    }
                ),
                encoding="utf-8",
            )

            address, digest = address_from_received_mail(
                Path(directory), "arc_0123456789abcdef01234567"
            )

            self.assertEqual(address, "friend@example.com")
            self.assertEqual(digest, hashlib.sha256(raw).hexdigest())

    def test_help_shows_archive_id_example(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            parse_args(["register-from-mail", "--help"])
        self.assertIn("arc_0123456789abcdef01234567", output.getvalue())


if __name__ == "__main__":
    unittest.main()
