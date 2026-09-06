"""Durable, cross-process idempotency ledger for the mail hotline.

The ledger is intentionally outside the Ombre container: it protects the host
mail doorbell before a message is turned into any Ombre candidate.  It uses only
the standard library so the cron job can run with the system Python.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1


class LedgerError(RuntimeError):
    """The ledger or an existing marker violates its idempotency contract."""


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Hold an advisory OS lock which also works in the Windows test workspace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.tell() == 0 and path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def message_key(message: dict[str, Any]) -> str:
    """Return the durable identity for an IMAP message.

    A Message-ID is the preferred identity.  A standards-compliant fallback is
    tied to both IMAP UIDVALIDITY and UID, so an IMAP UID reuse cannot collide
    with a message from an earlier mailbox incarnation.
    """
    rfc_message_id = str(message.get("message_id") or "").strip()
    if rfc_message_id:
        return f"rfc822:{rfc_message_id}"
    uidvalidity = str(message.get("uidvalidity") or "").strip()
    uid = str(message.get("uid") or "").strip()
    if not uidvalidity or not uid:
        raise LedgerError("message has neither Message-ID nor UIDVALIDITY/UID fallback")
    return f"imap:uidvalidity={uidvalidity};uid={uid}"


class MessageLedger:
    """A small JSON ledger with atomic replacement and a sibling lock file."""

    def __init__(
        self,
        path: Path,
        inbox_dir: Path,
        *,
        notification_lease_seconds: int = 180,
    ) -> None:
        self.path = path
        self.inbox_dir = inbox_dir
        self.lock_path = path.with_name(f"{path.name}.lock")
        self.notification_lease_seconds = notification_lease_seconds

    def claim_notification(self, message: dict[str, Any], now: float | None = None) -> str | None:
        """Persist its marker and claim a notification attempt.

        ``None`` means that another process has already completed (or currently
        owns) this message.  A returned token must be acknowledged after a relay
        success, or released when the relay fails.  The lease makes a crash
        recoverable without reverting to a finite in-memory UID window.
        """
        now = time.time() if now is None else now
        key = message_key(message)
        with _file_lock(self.lock_path):
            ledger = self._read_locked()
            record = ledger["messages"].setdefault(
                key,
                {
                    "message_key": key,
                    "message_id": str(message.get("message_id") or "").strip(),
                    "uid": str(message.get("uid") or "").strip(),
                    "uidvalidity": str(message.get("uidvalidity") or "").strip(),
                    "first_seen_at": now,
                },
            )
            marker_name = f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"
            marker_path = self.inbox_dir / marker_name
            self._ensure_marker(marker_path, key, message)
            record["marker_path"] = marker_name
            record["marker_written_at"] = record.get("marker_written_at") or now

            if record.get("notified_at") is not None:
                self._write_locked(ledger)
                return None

            claimed_at = record.get("notification_claimed_at")
            if claimed_at is not None and now - float(claimed_at) < self.notification_lease_seconds:
                self._write_locked(ledger)
                return None

            token = uuid.uuid4().hex
            record["notification_token"] = token
            record["notification_claimed_at"] = now
            self._write_locked(ledger)
            return token

    def acknowledge_notification(self, message: dict[str, Any], token: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        key = message_key(message)
        with _file_lock(self.lock_path):
            ledger = self._read_locked()
            record = ledger["messages"].get(key)
            if record is None or record.get("notification_token") != token:
                raise LedgerError("notification acknowledgement does not own this message")
            record["notified_at"] = now
            record.pop("notification_token", None)
            record.pop("notification_claimed_at", None)
            self._write_locked(ledger)

    def release_notification(self, message: dict[str, Any], token: str) -> None:
        """Allow a failed relay call to be retried on the next poll."""
        key = message_key(message)
        with _file_lock(self.lock_path):
            ledger = self._read_locked()
            record = ledger["messages"].get(key)
            if record is not None and record.get("notification_token") == token:
                record.pop("notification_token", None)
                record.pop("notification_claimed_at", None)
                self._write_locked(ledger)

    def update_ingest_metadata(self, message: dict[str, Any]) -> None:
        """Refresh non-authoritative marker fields after a summary attempt."""
        key = message_key(message)
        with _file_lock(self.lock_path):
            ledger = self._read_locked()
            record = ledger["messages"].get(key)
            if record is None:
                raise LedgerError("cannot update metadata for an unclaimed message")
            marker_name = record.get("marker_path")
            if not isinstance(marker_name, str) or not marker_name:
                raise LedgerError("claimed message has no marker path")
            marker_path = self.inbox_dir / marker_name
            self._ensure_marker(marker_path, key, message, replace_existing=True)
            record["summary_status"] = str(message.get("summary_status") or "pending")
            self._write_locked(ledger)

    def _read_locked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": SCHEMA_VERSION, "messages": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LedgerError(f"cannot read mail ledger {self.path}: {exc}") from exc
        if value.get("schema_version") != SCHEMA_VERSION or not isinstance(value.get("messages"), dict):
            raise LedgerError("unsupported or malformed mail ledger")
        return value

    def _write_locked(self, ledger: dict[str, Any]) -> None:
        _atomic_json(self.path, ledger)

    def _ensure_marker(
        self, path: Path, key: str, message: dict[str, Any], *, replace_existing: bool = False
    ) -> None:
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise LedgerError(f"cannot inspect existing marker {path}: {exc}") from exc
            if existing.get("message_key") != key:
                raise LedgerError(f"marker collision at {path}")
            if not replace_existing:
                return
        # Raw RFC822 bytes are private archive material, never marker/ledger data.
        from mail_archive import public_marker_fields

        payload = public_marker_fields(message)
        payload["message_key"] = key
        _atomic_json(path, payload)
