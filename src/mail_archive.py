"""Durable raw-message archive used by the mail-hotline ingest boundary."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mail_ledger import _file_lock, message_key


class MailArchiveError(RuntimeError):
    """A raw original could not be made durable or conflicts with its identity."""


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))


class MailArchiveStore:
    """Store immutable raw RFC822 originals plus a separate, verifiable manifest."""

    def __init__(self, root: Path, summary_root: Path | None = None) -> None:
        self.root = root
        self.summary_root = summary_root or root.parent / "mail-derived-summaries"
        self.lock_path = root / ".archive.lock"

    def archive(self, message: dict[str, Any]) -> dict[str, Any]:
        """Archive an incoming original (backwards-compatible ingest entrypoint)."""
        return self._archive(message, direction="incoming", record_ledger=False)

    def archive_sent(self, message: dict[str, Any]) -> dict[str, Any]:
        """Archive a sent original and its durable identity under one lock.

        SMTP success and archive success are distinct outcomes.  Callers must
        report a failure here as ``sent_archive_pending`` and must not resend.
        """
        return self._archive(message, direction="sent", record_ledger=True)

    def _archive(
        self,
        message: dict[str, Any],
        *,
        direction: str,
        record_ledger: bool,
    ) -> dict[str, Any]:
        if direction not in {"incoming", "sent"}:
            raise MailArchiveError("unsupported mail archive direction")
        raw = message.get("raw_message")
        if not isinstance(raw, bytes) or not raw:
            raise MailArchiveError("mail ingest did not provide a complete raw message")
        identity = message_key(message)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        archive_id = f"arc_{digest[:24]}"
        original_path = self.root / direction / f"{digest}.eml"
        manifest_path = self.root / direction / f"{digest}.json"
        raw_sha256 = hashlib.sha256(raw).hexdigest()

        with _file_lock(self.lock_path):
            if original_path.exists():
                existing_raw = original_path.read_bytes()
                if hashlib.sha256(existing_raw).hexdigest() != raw_sha256:
                    raise MailArchiveError("archive identity already exists with different raw content")
            else:
                _atomic_bytes(original_path, raw)

            manifest = {
                "schema_version": 1,
                "kind": "mail_original",
                "direction": direction,
                "archived_at": datetime.now(timezone.utc).isoformat(),
                "archive_id": archive_id,
                "message_key": identity,
                "message_id": str(message.get("message_id") or "").strip(),
                "uid": str(message.get("uid") or "").strip(),
                "uidvalidity": str(message.get("uidvalidity") or "").strip(),
                "from": str(message.get("from") or "unknown"),
                "to": str(message.get("to") or "unknown"),
                "subject": str(message.get("subject") or "(no subject)"),
                "date": str(message.get("date") or "").strip(),
                "in_reply_to": str(message.get("in_reply_to") or "").strip(),
                "references": str(message.get("references") or "").strip(),
                **(
                    {"constellation_id": str(message.get("constellation_id") or "")}
                    if message.get("constellation_id")
                    else {}
                ),
                **(
                    {"constellation_assignment": dict(message["constellation_assignment"])}
                    if message.get("constellation_id")
                    and isinstance(message.get("constellation_assignment"), dict)
                    else {}
                ),
                "raw_sha256": raw_sha256,
                "original_file": original_path.name,
            }
            if manifest_path.exists():
                try:
                    existing = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise MailArchiveError(f"cannot validate archive manifest: {exc}") from exc
                if existing.get("message_key") != identity or existing.get("raw_sha256") != raw_sha256:
                    raise MailArchiveError("archive manifest conflicts with raw original")
                incoming_constellation = str(message.get("constellation_id") or "")
                existing_constellation = str(existing.get("constellation_id") or "")
                if incoming_constellation:
                    if existing_constellation and existing_constellation != incoming_constellation:
                        raise MailArchiveError("archive already belongs to another constellation")
                    if not existing_constellation:
                        assignment = message.get("constellation_assignment")
                        if not isinstance(assignment, dict):
                            raise MailArchiveError("constellation assignment evidence is required")
                        existing = dict(existing)
                        existing["constellation_id"] = incoming_constellation
                        existing["constellation_assignment"] = dict(assignment)
                        _atomic_json(manifest_path, existing)
                if record_ledger:
                    self._record_archive_identity_locked(identity, existing)
                return existing
            _atomic_json(manifest_path, manifest)
            if record_ledger:
                self._record_archive_identity_locked(identity, manifest)
            return manifest

    def _record_archive_identity_locked(
        self, identity: str, manifest: dict[str, Any]
    ) -> None:
        ledger_path = self.root / "message-ledger.json"
        if ledger_path.exists():
            try:
                ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MailArchiveError(f"cannot validate archive ledger: {exc}") from exc
        else:
            ledger = {"schema_version": 1, "messages": {}}
        if ledger.get("schema_version") != 1 or not isinstance(
            ledger.get("messages"), dict
        ):
            raise MailArchiveError("unsupported or malformed archive ledger")
        existing = ledger["messages"].get(identity)
        record = {
            "archive_id": manifest["archive_id"],
            "message_id": manifest.get("message_id", ""),
            "direction": manifest.get("direction", ""),
            "raw_sha256": manifest["raw_sha256"],
            "manifest_file": f"{manifest.get('direction', '')}/{hashlib.sha256(identity.encode('utf-8')).hexdigest()}.json",
        }
        if existing is not None and existing != record:
            raise MailArchiveError("archive ledger identity conflicts with existing record")
        ledger["messages"][identity] = record
        _atomic_json(ledger_path, ledger)

    def store_summary(
        self,
        manifest: dict[str, Any],
        result: dict[str, Any],
        *,
        source_body: str = "",
    ) -> dict[str, Any]:
        """Write a disposable model summary beside, never inside, mail evidence."""
        archive_id = str(manifest.get("archive_id") or "")
        message_id = str(manifest.get("message_id") or "")
        summary = str(result.get("summary") or "").strip()
        if not archive_id or not message_id or not summary:
            raise MailArchiveError("summary requires an archived original and non-empty model output")
        digest = hashlib.sha256(archive_id.encode("utf-8")).hexdigest()
        quotes = [
            str(item).strip()
            for item in result.get("verbatim_quotes", [])
            if str(item).strip() and str(item).strip() in source_body
        ][:2]
        if source_body and not quotes:
            raise MailArchiveError("mail summary has no verbatim quote verified against its original")
        correspondence = result.get("correspondence")
        if not isinstance(correspondence, dict):
            correspondence = {}
        record = {
            "schema_version": 1,
            "kind": "mail_history_summary",
            "generated_by": "model",
            "model": str(result.get("model") or "unknown"),
            "warning": "由模型生成，可能失真；引用或核对对方原话时必须回到原件。",
            "citation_policy": "original_only",
            "quote_policy": "exact_substring_verified",
            "original_pointer": {"archive_id": archive_id, "message_id": message_id},
            "original_sha256": str(manifest.get("raw_sha256") or ""),
            "summary": summary,
            "verbatim_quotes": quotes,
            "correspondence": {
                "sender": str(correspondence.get("sender") or manifest.get("from") or "unknown"),
                "recipient": str(correspondence.get("recipient") or manifest.get("to") or "unknown"),
                "in_reply_to": str(
                    correspondence.get("in_reply_to") or manifest.get("in_reply_to") or ""
                ),
                "awaiting_reply": correspondence.get("awaiting_reply")
                if isinstance(correspondence.get("awaiting_reply"), bool)
                else None,
                "awaiting_reply_basis": str(correspondence.get("awaiting_reply_basis") or ""),
            },
            "key_points": [str(item) for item in result.get("key_points", []) if str(item).strip()],
            "open_questions": [
                str(item) for item in result.get("open_questions", []) if str(item).strip()
            ],
        }
        with _file_lock(self.summary_root / ".summary.lock"):
            _atomic_json(self.summary_root / f"{digest}.summary.json", record)
        return record


def public_marker_fields(message: dict[str, Any]) -> dict[str, Any]:
    """Keep raw mail bodies out of the hotline marker and ID ledger."""
    return {
        key: message[key]
        for key in (
            "uid",
            "uidvalidity",
            "message_id",
            "from",
            "to",
            "subject",
            "date",
            "in_reply_to",
            "references",
            "archive_id",
            "archive_manifest",
            "summary_status",
        )
        if key in message
    }
