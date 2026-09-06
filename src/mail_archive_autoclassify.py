#!/usr/bin/env python3
"""Backfill incoming mail ownership from active protected identities only."""

from __future__ import annotations

import argparse
import email
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mail_archive import MailArchiveError, MailArchiveStore
from mail_hotline import classify_from_protected_identity
from mail_identities import ProtectedIdentityStore


def backfill(
    archive_root: Path,
    identity_file: Path,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    incoming = archive_root / "incoming"
    archive_store = MailArchiveStore(archive_root)
    identity_store = ProtectedIdentityStore(identity_file, constellation_lookup=lambda _: {})
    stats = {
        "status": "applied" if apply else "preview",
        "scanned": 0,
        "classified": 0,
        "already_classified": 0,
        "unmatched": 0,
        "invalid": 0,
        "classified_by_constellation": {},
    }
    for manifest_path in sorted(incoming.glob("*.json")):
        stats["scanned"] += 1
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("direction") != "incoming":
                raise MailArchiveError("manifest is not incoming mail")
            if manifest.get("constellation_id"):
                stats["already_classified"] += 1
                continue
            original_name = str(manifest.get("original_file") or "")
            if not original_name or Path(original_name).name != original_name:
                raise MailArchiveError("invalid original filename")
            raw = (incoming / original_name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != manifest.get("raw_sha256"):
                raise MailArchiveError("original hash does not match manifest")
            parsed = email.message_from_bytes(raw)
            candidate = {
                **manifest,
                "from": str(parsed.get("From") or ""),
                "raw_message": raw,
            }
            assignment = classify_from_protected_identity(candidate, identity_store)
            if assignment is None:
                stats["unmatched"] += 1
                continue
            constellation_id = str(assignment["constellation_id"])
            assignment["constellation_assignment"] = {
                **assignment["constellation_assignment"],
                "classified_by": "mail_archive_autoclassify",
                "classified_at": datetime.now(timezone.utc).isoformat(),
            }
            stats["classified"] += 1
            counts = stats["classified_by_constellation"]
            counts[constellation_id] = int(counts.get(constellation_id, 0)) + 1
            if apply:
                archive_store.archive({**candidate, **assignment})
        except (OSError, ValueError, MailArchiveError):
            stats["invalid"] += 1
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--identity-file", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = backfill(args.archive_root, args.identity_file, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["invalid"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
