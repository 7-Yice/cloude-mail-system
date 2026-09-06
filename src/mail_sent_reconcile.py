#!/usr/bin/env python3
"""Repair a sent-but-not-archived receipt without sending mail again."""

from __future__ import annotations

import argparse
import json
import os
import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from mail_archive import MailArchiveStore, _atomic_json


DEFAULT_ARCHIVE = Path(
    os.environ.get(
        "CLOUDE_MAIL_ARCHIVE_DIR",
        str(Path.home() / ".config" / "cloude-mail-system" / "mail-archive"),
    )
)


def reconcile_receipt(
    receipt_path: Path, archive_store: MailArchiveStore
) -> dict[str, Any]:
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read reconcile receipt: {exc}") from exc
    if receipt.get("schema_version") != 1:
        raise ValueError("unsupported reconcile receipt")
    if receipt.get("status") == "archived_after_reconcile":
        return receipt
    if receipt.get("status") != "sent_archive_pending":
        raise ValueError("receipt is not pending sent-mail reconciliation")
    original_name = str(receipt.get("original_file") or "")
    if not original_name or Path(original_name).name != original_name:
        raise ValueError("receipt original_file is unsafe")
    raw_path = receipt_path.parent / original_name
    raw = raw_path.read_bytes()
    if not raw:
        raise ValueError("pending sent original is empty")
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    message_id = str(parsed.get("Message-ID") or "").strip()
    if not message_id or message_id != str(receipt.get("message_id") or "").strip():
        raise ValueError("pending original Message-ID does not match receipt")
    manifest = archive_store.archive_sent(
        {
            "message_id": message_id,
            "from": str(parsed.get("From") or "unknown"),
            "to": str(parsed.get("To") or "unknown"),
            "subject": str(parsed.get("Subject") or "(no subject)"),
            "date": str(parsed.get("Date") or ""),
            "in_reply_to": str(parsed.get("In-Reply-To") or ""),
            "references": str(parsed.get("References") or ""),
            "constellation_id": str(receipt.get("constellation_id") or ""),
            "raw_message": raw,
        }
    )
    completed = {
        **receipt,
        "status": "archived_after_reconcile",
        "archive_id": manifest["archive_id"],
        "archive_error": "",
    }
    _atomic_json(receipt_path, completed)
    return completed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive one already-sent recovery receipt; never invokes SMTP."
    )
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--receipt", type=Path, required=True)
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    try:
        result = reconcile_receipt(
            args.receipt, MailArchiveStore(args.archive_root)
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "reconcile_failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
