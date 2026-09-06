"""Protected correspondence identities attached to person constellations.

This is private operational metadata inside the existing Ombre vault. Full
addresses are deliberately absent from constellation covers, pointers and
retrieval. Dashboard audit views must mask them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Any, Callable

from mail_ledger import _file_lock


SCHEMA_VERSION = 1
_CONSTELLATION_ID_RE = re.compile(r"^cst_[a-z0-9][a-z0-9_-]{2,63}$")


class IdentityError(ValueError):
    pass


def normalize_address(value: str) -> str:
    candidate = str(value or "").strip()
    if not candidate or "\n" in candidate or "\r" in candidate:
        raise IdentityError("correspondence address is empty or contains a newline")
    display, address = parseaddr(candidate)
    if display or address != candidate or address.count("@") != 1:
        raise IdentityError("use one bare email address without a display name")
    local, domain = address.rsplit("@", 1)
    if not local or not domain or "." not in domain or any(ch.isspace() for ch in address):
        raise IdentityError("correspondence address is invalid")
    return f"{local}@{domain.lower()}"


def address_from_header(value: str) -> str | None:
    """Return one normalized address from a mail header, or no match.

    Classification is deliberately exact: malformed and multi-address From
    headers stay unclassified instead of being guessed from display names.
    """
    candidates = {
        address.strip()
        for _, address in getaddresses([str(value or "")])
        if address.strip()
    }
    if len(candidates) != 1:
        return None
    try:
        return normalize_address(candidates.pop())
    except IdentityError:
        return None


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
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


class ProtectedIdentityStore:
    """Append-only identity history keyed by an existing person constellation."""

    def __init__(
        self,
        path: Path,
        *,
        constellation_lookup: Callable[[str], dict[str, Any]],
    ) -> None:
        self.path = path
        self.lock_path = path.with_name(f"{path.name}.lock")
        self.constellation_lookup = constellation_lookup

    def register(
        self,
        constellation_id: str,
        address: str,
        *,
        actor: str,
        reason: str,
        source_kind: str,
        source_id: str,
        source_sha256: str,
    ) -> dict[str, Any]:
        if not _CONSTELLATION_ID_RE.fullmatch(str(constellation_id or "")):
            raise IdentityError("constellation id is invalid")
        constellation = self.constellation_lookup(constellation_id)
        business = constellation.get("business") or constellation
        if business.get("kind") != "person":
            raise IdentityError("correspondence identities require a person constellation")
        normalized = normalize_address(address)
        if not str(reason or "").strip():
            raise IdentityError("registration reason is required")
        if source_kind not in {"received_mail_header", "verified_user_message"}:
            raise IdentityError("registration requires an authoritative source")
        if not str(source_id or "").strip():
            raise IdentityError("registration source id is required")
        if not re.fullmatch(r"[0-9a-f]{64}", str(source_sha256 or "")):
            raise IdentityError("registration source sha256 is invalid")
        now = datetime.now(timezone.utc).isoformat()
        identity_id = "cid_" + hashlib.sha256(
            f"{constellation_id}\0{normalized}".encode("utf-8")
        ).hexdigest()[:20]
        with _file_lock(self.lock_path):
            ledger = self._read_locked()
            active = self._active(ledger)
            owner = next(
                (
                    item
                    for item in active.values()
                    if item["address"].casefold() == normalized.casefold()
                ),
                None,
            )
            if owner and owner["constellation_id"] != constellation_id:
                raise IdentityError("address is already registered to another constellation")
            if owner and owner["constellation_id"] == constellation_id:
                return dict(owner)
            event = {
                "event": "registered",
                "identity_id": identity_id,
                "constellation_id": constellation_id,
                "address": normalized,
                "actor": str(actor or "human").strip() or "human",
                "reason": str(reason).strip(),
                "source_kind": source_kind,
                "source_id": str(source_id).strip(),
                "source_sha256": source_sha256,
                "recorded_at": now,
            }
            ledger["events"].append(event)
            self._write_locked(ledger)
            return dict(event)

    def revoke(self, identity_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        if not str(reason or "").strip():
            raise IdentityError("revocation reason is required")
        with _file_lock(self.lock_path):
            ledger = self._read_locked()
            active = self._active(ledger)
            if identity_id not in active:
                raise IdentityError("active correspondence identity not found")
            event = {
                "event": "revoked",
                "identity_id": identity_id,
                "constellation_id": active[identity_id]["constellation_id"],
                "actor": str(actor or "human").strip() or "human",
                "reason": str(reason).strip(),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            ledger["events"].append(event)
            self._write_locked(ledger)
            return dict(event)

    def resolve(self, constellation_id: str) -> dict[str, Any]:
        if not _CONSTELLATION_ID_RE.fullmatch(str(constellation_id or "")):
            raise IdentityError("constellation id is invalid")
        with _file_lock(self.lock_path):
            matches = [
                row
                for row in self._active(self._read_locked()).values()
                if row["constellation_id"] == constellation_id
            ]
        if not matches:
            raise IdentityError("constellation has no active correspondence identity")
        if len(matches) != 1:
            raise IdentityError("constellation has multiple identities; choose one explicitly")
        return dict(matches[0])

    def resolve_address(self, address: str) -> dict[str, Any] | None:
        """Resolve an address only through the active protected identity ledger."""
        normalized = normalize_address(address)
        with _file_lock(self.lock_path):
            matches = [
                row
                for row in self._active(self._read_locked()).values()
                if row["address"].casefold() == normalized.casefold()
            ]
        if not matches:
            return None
        if len(matches) != 1:
            raise IdentityError("address has multiple active correspondence identities")
        return dict(matches[0])

    def audit(self, constellation_id: str) -> list[dict[str, Any]]:
        if not _CONSTELLATION_ID_RE.fullmatch(str(constellation_id or "")):
            raise IdentityError("constellation id is invalid")
        with _file_lock(self.lock_path):
            rows = [
                dict(row) for row in self._read_locked()["events"]
                if isinstance(row, dict) and row.get("constellation_id") == constellation_id
            ]
        return rows

    def _read_locked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": SCHEMA_VERSION, "events": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IdentityError(f"cannot read correspondence identity ledger: {exc}") from exc
        if value.get("schema_version") != SCHEMA_VERSION or not isinstance(
            value.get("events"), list
        ):
            raise IdentityError("unsupported or malformed correspondence identity ledger")
        return value

    def _write_locked(self, value: dict[str, Any]) -> None:
        _atomic_json(self.path, value)

    @staticmethod
    def _active(ledger: dict[str, Any]) -> dict[str, dict[str, Any]]:
        active: dict[str, dict[str, Any]] = {}
        for raw in ledger["events"]:
            if not isinstance(raw, dict):
                continue
            identity_id = str(raw.get("identity_id") or "")
            if raw.get("event") == "registered" and identity_id:
                active[identity_id] = raw
            elif raw.get("event") == "revoked":
                active.pop(identity_id, None)
        return active
