#!/usr/bin/env python3
"""Agent-owned correspondence identity registration from authoritative evidence.

There is intentionally no address argument. A recipient can only be extracted
from an immutable received-mail header or one exact verified inbound user row.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path

from mail_identities import IdentityError, ProtectedIdentityStore, normalize_address
from send_mail import DEFAULT_VAULT, _projection_lookup


RUNTIME_ROOT = Path(
    os.environ.get(
        "CLOUDE_MAIL_RUNTIME_DIR",
        str(Path.home() / ".config" / "cloude-mail-system"),
    )
)
DEFAULT_ARCHIVE = Path(os.environ.get("CLOUDE_MAIL_ARCHIVE_DIR", str(RUNTIME_ROOT / "mail-archive")))
DEFAULT_RELAY_DB = Path(os.environ.get("CLOUDE_RELAY_DB", str(RUNTIME_ROOT / "relay.db")))
ARCHIVE_ID_RE = re.compile(r"^arc_[0-9a-f]{24}$")
NOT_HER_PREFIXES = (
    "【玩耍铃", "【做梦闹钟", "【中间层事实", "<channel",
    "<task-notification", "[传话筒", "mcp__companion__reply:",
)


def mask_address(address: str) -> str:
    local, domain = address.rsplit("@", 1)
    return f"{local[:1]}{'***' if len(local) > 1 else '*'}@{domain}"


def _one_address(values: list[str]) -> str:
    addresses = {normalize_address(address) for _, address in getaddresses(values) if address}
    if len(addresses) != 1:
        raise IdentityError("authoritative source must contain exactly one email address")
    return addresses.pop()


def _one_address_in_text(text: str) -> str:
    candidates = {
        normalize_address(value)
        for value in re.findall(
            r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
            r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
            r"(?![A-Za-z0-9-])",
            text,
        )
    }
    if len(candidates) != 1:
        raise IdentityError("authoritative source must contain exactly one email address")
    return candidates.pop()


def _validate_archive_id(archive_id: str) -> None:
    if ARCHIVE_ID_RE.fullmatch(archive_id):
        return
    detail = (
        "received a 64-character raw_sha256"
        if re.fullmatch(r"[0-9a-fA-F]{64}", archive_id)
        else "do not pass an archive filename or raw_sha256"
    )
    raise IdentityError(
        "--archive-id expects the manifest archive_id "
        "(example: arc_0123456789abcdef01234567); " + detail
    )


def address_from_received_mail(root: Path, archive_id: str) -> tuple[str, str]:
    _validate_archive_id(archive_id)
    matches = []
    for path in (root / "incoming").glob("*.json"):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("archive_id") == archive_id:
            matches.append((path, manifest))
    if len(matches) != 1:
        raise IdentityError("received mail archive id was not found uniquely")
    manifest_path, manifest = matches[0]
    if manifest.get("direction") != "incoming":
        raise IdentityError("only a received mail can authorize registration")
    raw_path = manifest_path.parent / str(manifest.get("original_file") or "")
    raw = raw_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != manifest.get("raw_sha256"):
        raise IdentityError("received mail original does not match its manifest")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    address = _one_address(message.get_all("From", []))
    if address != _one_address([str(manifest.get("from") or "")]):
        raise IdentityError("received mail From header conflicts with its manifest")
    return address, digest


def address_from_user_message(database: Path, message_id: int) -> tuple[str, str]:
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            row = connection.execute(
                "SELECT text, meta FROM messages WHERE id=? AND direction='in'", (message_id,)
            ).fetchone()
    except sqlite3.Error as exc:
        raise IdentityError(f"cannot read verified inbound message: {exc}") from exc
    if row is None:
        raise IdentityError("verified inbound user message was not found")
    text, meta_raw = str(row[0] or ""), str(row[1] or "{}")
    try:
        meta = json.loads(meta_raw)
    except json.JSONDecodeError as exc:
        raise IdentityError("verified inbound message metadata is malformed") from exc
    if not isinstance(meta, dict) or meta.get("system_notice") or str(meta.get("handoff_exclude", "")).lower() == "true":
        raise IdentityError("message is not an eligible direct user statement")
    head = text.lstrip()[:40]
    if any(marker in head for marker in NOT_HER_PREFIXES):
        raise IdentityError("message is not an eligible direct user statement")
    return _one_address_in_text(text), hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage correspondence identities from evidence.")
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--actor", default=os.environ.get("CLOUDE_MAIL_ACTOR", "agent"))
    sub = parser.add_subparsers(dest="action", required=True)
    mail = sub.add_parser("register-from-mail")
    mail.add_argument("--constellation-id", required=True)
    mail.add_argument(
        "--archive-id",
        required=True,
        metavar="arc_0123456789abcdef01234567",
        help="archive_id from the received-mail JSON manifest; not the filename or raw_sha256",
    )
    mail.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE)
    user = sub.add_parser("register-from-user-message")
    user.add_argument("--constellation-id", required=True)
    user.add_argument("--message-id", required=True, type=int)
    user.add_argument("--relay-db", type=Path, default=Path(os.environ.get("CLOUDE_RELAY_DB", DEFAULT_RELAY_DB)))
    audit = sub.add_parser("audit")
    audit.add_argument("--constellation-id", required=True)
    revoke = sub.add_parser("revoke")
    revoke.add_argument("--identity-id", required=True)
    revoke.add_argument("--reason", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    store = ProtectedIdentityStore(
        args.vault / "_private" / "correspondence-identities.json",
        constellation_lookup=_projection_lookup(args.vault),
    )
    try:
        if args.action == "register-from-mail":
            address, digest = address_from_received_mail(args.archive_root, args.archive_id)
            row = store.register(args.constellation_id, address, actor=args.actor,
                reason="从真实收到的信件信头登记", source_kind="received_mail_header",
                source_id=args.archive_id, source_sha256=digest)
            result = {"status": "registered", **row, "masked_address": mask_address(address)}
            result.pop("address", None)
        elif args.action == "register-from-user-message":
            address, digest = address_from_user_message(args.relay_db, args.message_id)
            row = store.register(args.constellation_id, address, actor=args.actor,
                reason="从用户明确给出的真实入站消息登记", source_kind="verified_user_message",
                source_id=f"message_id:{args.message_id}", source_sha256=digest)
            result = {"status": "registered", **row, "masked_address": mask_address(address)}
            result.pop("address", None)
        elif args.action == "audit":
            result = {"status": "ok", "events": [
                {**row, **({"masked_address": mask_address(row["address"])} if row.get("address") else {})}
                for row in store.audit(args.constellation_id)
            ]}
            for row in result["events"]:
                row.pop("address", None)
        else:
            result = {"status": "revoked", **store.revoke(args.identity_id, actor=args.actor, reason=args.reason)}
    except (OSError, UnicodeError, IdentityError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
