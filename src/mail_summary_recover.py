#!/usr/bin/env python3
"""Rebuild failed mail-summary sidecars without polling or notifying.

The command is dry-run by default.  ``--apply`` reads only already archived
RFC822 originals, invokes the existing Ombre summarizer, writes the disposable
sidecar, and atomically changes the existing ledger/marker status to ``ready``.
It never connects to IMAP and never calls the companion relay.
"""

from __future__ import annotations

import argparse
import email
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from mail_archive import MailArchiveStore
from mail_hotline import display_body, run_lock, summarize_with_ombre
from mail_ledger import MessageLedger


DEFAULT_RUNTIME = Path(
    os.environ.get(
        "CLOUDE_MAIL_RUNTIME_DIR",
        str(Path.home() / ".config" / "cloude-mail-system"),
    )
)


def summary_candidates(
    ledger_path: Path,
    inbox_dir: Path,
    archive_dir: Path,
    *,
    summary_status: str,
) -> list[dict[str, Any]]:
    value = json.loads(ledger_path.read_text(encoding="utf-8"))
    candidates: list[dict[str, Any]] = []
    for key, record in sorted(value.get("messages", {}).items()):
        if record.get("summary_status") != summary_status:
            continue
        marker_name = record.get("marker_path")
        if not isinstance(marker_name, str) or not marker_name:
            raise RuntimeError(f"pending record lacks marker: {key}")
        marker = json.loads((inbox_dir / marker_name).read_text(encoding="utf-8"))
        original_name = marker.get("archive_manifest")
        if not isinstance(original_name, str) or not original_name.endswith(".eml"):
            raise RuntimeError(f"pending record lacks original pointer: {key}")
        original = archive_dir / "incoming" / original_name
        manifest_path = original.with_suffix(".json")
        if not original.is_file() or not manifest_path.is_file():
            raise RuntimeError(f"archived original or manifest missing: {key}")
        candidates.append(
            {
                "key": key,
                "record": record,
                "marker": marker,
                "original": original,
                "manifest": json.loads(manifest_path.read_text(encoding="utf-8")),
            }
        )
    return candidates


def pending_candidates(
    ledger_path: Path, inbox_dir: Path, archive_dir: Path
) -> list[dict[str, Any]]:
    return summary_candidates(
        ledger_path,
        inbox_dir,
        archive_dir,
        summary_status="pending_after_failure",
    )


def has_note_v2(candidate: dict[str, Any], summary_root: Path) -> bool:
    archive_id = str(candidate.get("manifest", {}).get("archive_id") or "")
    if not archive_id:
        return False
    digest = hashlib.sha256(archive_id.encode("utf-8")).hexdigest()
    path = summary_root / f"{digest}.summary.json"
    if not path.is_file():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        value.get("quote_policy") == "exact_substring_verified"
        and isinstance(value.get("verbatim_quotes"), list)
        and bool(value["verbatim_quotes"])
        and isinstance(value.get("correspondence"), dict)
    )


def recover_one(
    candidate: dict[str, Any], store: MailArchiveStore, ledger: MessageLedger
) -> None:
    raw = candidate["original"].read_bytes()
    parsed = email.message_from_bytes(raw)
    message = dict(candidate["marker"])
    message.update(
        {
            "from": str(parsed.get("From") or message.get("from") or "unknown"),
            "to": str(parsed.get("To") or message.get("to") or "unknown"),
            "subject": str(parsed.get("Subject") or message.get("subject") or "(no subject)"),
            "date": str(parsed.get("Date") or message.get("date") or ""),
            "message_id": str(parsed.get("Message-ID") or message.get("message_id") or "").strip(),
            "in_reply_to": str(parsed.get("In-Reply-To") or "").strip(),
            "references": str(parsed.get("References") or "").strip(),
            "body": display_body(parsed),
            "raw_message": raw,
            "_archive_manifest": candidate["manifest"],
        }
    )
    summarize_with_ombre(message, store)
    ledger.update_ingest_metadata(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--refresh-ready",
        action="store_true",
        help="rebuild existing ready sidecars from archived originals without notifying",
    )
    parser.add_argument(
        "--only-legacy-format",
        action="store_true",
        help="with --refresh-ready, select only sidecars lacking the verified note format",
    )
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be positive")

    runtime = args.runtime.resolve()
    ledger_path = runtime / "mail-message-ledger.json"
    inbox_dir = runtime / "mail-inbox"
    archive_dir = runtime / "mail-archive"
    selected_status = "ready" if args.refresh_ready else "pending_after_failure"
    candidates = summary_candidates(
        ledger_path,
        inbox_dir,
        archive_dir,
        summary_status=selected_status,
    )
    if args.only_legacy_format:
        if not args.refresh_ready:
            parser.error("--only-legacy-format requires --refresh-ready")
        candidates = [
            candidate
            for candidate in candidates
            if not has_note_v2(candidate, runtime / "mail-derived-summaries")
        ]
    selected = candidates[: args.limit]
    print(
        json.dumps(
            {
                "mode": "refresh_ready" if args.refresh_ready else "recover_pending",
                "candidates": len(candidates),
                "selected": len(selected),
                "apply": args.apply,
            }
        )
    )
    if not args.apply:
        return 0

    store = MailArchiveStore(archive_dir)
    ledger = MessageLedger(ledger_path, inbox_dir)
    completed = 0
    failures = 0
    with run_lock() as acquired:
        if not acquired:
            raise RuntimeError("mail hotline is currently running; retry later")
        for candidate in selected:
            try:
                recover_one(candidate, store, ledger)
                completed += 1
            except Exception as exc:
                failures += 1
                print(
                    json.dumps(
                        {
                            "status": "failed",
                            "message_key": candidate["key"],
                            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                        },
                        ensure_ascii=False,
                    )
                )
    print(json.dumps({"completed": completed, "failed": failures}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
