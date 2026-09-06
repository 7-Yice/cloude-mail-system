#!/usr/bin/env python3
"""Send mail only to a protected Ombre correspondence identity."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from mail_archive import MailArchiveError, MailArchiveStore, _atomic_bytes, _atomic_json
from mail_identities import IdentityError, ProtectedIdentityStore


RUNTIME_ROOT = Path(
    os.environ.get(
        "CLOUDE_MAIL_RUNTIME_DIR",
        str(Path.home() / ".config" / "cloude-mail-system"),
    )
)
DEFAULT_ENV = Path(os.environ.get("CLOUDE_MAIL_ENV_FILE", str(RUNTIME_ROOT / "mail.env")))
DEFAULT_VAULT = Path(os.environ.get("OMBRE_BUCKETS_DIR", str(Path.home() / "ombre-brain" / "buckets")))
DEFAULT_OMBRE_ENV = Path(os.environ.get("OMBRE_ENV_FILE", str(Path.home() / "ombre-brain" / ".env")))
DEFAULT_OMBRE_MCP_URL = os.environ.get("OMBRE_MCP_URL", "http://127.0.0.1:18001/mcp")
DEFAULT_ARCHIVE = Path(os.environ.get("CLOUDE_MAIL_ARCHIVE_DIR", str(RUNTIME_ROOT / "mail-archive")))
DEFAULT_RECONCILE = Path(os.environ.get("CLOUDE_MAIL_RECONCILE_DIR", str(RUNTIME_ROOT / "mail-sent-reconcile")))
USER_KEYS = ("GMAIL_USER", "MAIL_USER", "IMAP_USER")
PASSWORD_KEYS = ("GMAIL_APP_PASSWORD", "MAIL_PASS", "IMAP_PASS")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"invalid env line {line_number}: missing =")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if not key:
            raise ValueError(f"invalid env line {line_number}: empty key")
        values[key] = value
    return values


def first_value(values: dict[str, str], keys: tuple[str, ...]) -> str | None:
    return next((values[key] for key in keys if values.get(key)), None)


def smtp_factory_from_env(values: dict[str, str]) -> Callable[[], Any]:
    """Return the SSL SMTP transport, optionally trusting one explicit test CA."""
    ca_file = str(values.get("SMTP_CA_FILE") or "").strip()

    def create() -> Any:
        context = ssl.create_default_context(cafile=ca_file or None)
        return smtplib.SMTP_SSL(
            values.get("SMTP_HOST", "smtp.gmail.com"),
            int(values.get("SMTP_PORT", "465")),
            context=context,
        )

    return create


def _mcp_tool_json(
    *,
    url: str,
    token: str,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    target = urlsplit(url)
    if target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise IdentityError("Ombre constellation validation endpoint must be loopback HTTP")
    if not token:
        raise IdentityError("Ombre constellation validation token is missing")
    path = target.path or "/mcp"
    if target.query:
        path = f"{path}?{target.query}"
    connection = http.client.HTTPConnection(
        target.hostname,
        target.port or 80,
        timeout=15,
    )
    request = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }, separators=(",", ":"))
    try:
        connection.request(
            "POST",
            path,
            body=request,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        body = response.read().decode("utf-8")
    except (OSError, UnicodeError, http.client.HTTPException) as exc:
        raise IdentityError("cannot reach Ombre constellation validation service") from exc
    finally:
        connection.close()
    if response.status != 200:
        raise IdentityError(f"Ombre constellation validation returned HTTP {response.status}")
    if body.startswith("event:"):
        body = "\n".join(
            line[6:] for line in body.splitlines() if line.startswith("data: ")
        )
    try:
        envelope = json.loads(body)
    except json.JSONDecodeError as exc:
        raise IdentityError("Ombre constellation validation returned malformed JSON") from exc
    result = envelope.get("result") or {}
    if envelope.get("error") or result.get("isError"):
        raise IdentityError("Ombre constellation validation rejected the request")
    texts = [
        str(block.get("text") or "")
        for block in result.get("content") or []
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    try:
        value = json.loads("\n".join(texts))
    except json.JSONDecodeError as exc:
        raise IdentityError("Ombre constellation validation payload is malformed") from exc
    if not isinstance(value, dict):
        raise IdentityError("Ombre constellation validation payload is not an object")
    return value


def _projection_lookup(
    vault: Path,
    *,
    mcp_env: Path = DEFAULT_OMBRE_ENV,
    mcp_url: str = DEFAULT_OMBRE_MCP_URL,
) -> Callable[[str], dict[str, Any]]:
    # ``vault`` remains in the signature for compatibility with existing CLI
    # callers.  Validation deliberately crosses the Ombre service boundary;
    # the host process must never read root-owned projection files directly.
    del vault

    def lookup(constellation_id: str) -> dict[str, Any]:
        try:
            values = read_env(mcp_env)
            value = _mcp_tool_json(
                url=mcp_url,
                token=str(values.get("OMBRE_MCP_TOKEN") or ""),
                name="constellation_read",
                arguments={
                    "action": "inspect",
                    "constellation_id": constellation_id,
                    "max_tokens": 600,
                },
            )
        except (OSError, UnicodeError, ValueError, IdentityError) as exc:
            raise IdentityError(f"cannot validate constellation {constellation_id}: {exc}") from exc
        if value.get("constellation_id") != constellation_id:
            raise IdentityError("constellation projection identity mismatch")
        business = value.get("business")
        if not isinstance(business, dict):
            raise IdentityError("constellation projection business value is malformed")
        # Return only what the identity gate needs.  Statements, members and
        # other memory contents never leave the validation helper.
        return {
            "constellation_id": constellation_id,
            "business": {
                "kind": business.get("kind"),
                "status": business.get("status"),
            },
        }

    return lookup


def build_message(account: str, recipient: str, subject: str, body: str) -> EmailMessage:
    if not subject.strip():
        raise ValueError("subject is empty")
    if not body:
        raise ValueError("body is empty")
    message = EmailMessage(policy=SMTP)
    message["From"] = account
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    domain = account.rsplit("@", 1)[-1] if "@" in account else None
    message["Message-ID"] = make_msgid(domain=domain)
    message.set_content(body, charset="utf-8")
    return message


def _require_active_constellation(projection: dict[str, Any]) -> None:
    business = projection.get("business") or {}
    if business.get("status") != "active":
        raise IdentityError("cannot send mail for a closed constellation")


def _archive_payload(message: EmailMessage, constellation_id: str) -> dict[str, Any]:
    return {
        "message_id": str(message["Message-ID"]),
        "from": str(message["From"]),
        "to": str(message["To"]),
        "subject": str(message["Subject"]),
        "date": str(message["Date"]),
        "in_reply_to": str(message.get("In-Reply-To") or ""),
        "references": str(message.get("References") or ""),
        "constellation_id": constellation_id,
        "raw_message": message.as_bytes(policy=SMTP),
    }


def _spool_reconcile(root: Path, payload: dict[str, Any], error: str) -> Path:
    digest = str(payload["message_id"]).strip("<>").replace("@", "_")
    safe = "".join(ch for ch in digest if ch.isalnum() or ch in "._-")[:160]
    eml_path = root / f"{safe}.eml"
    receipt_path = root / f"{safe}.json"
    _atomic_bytes(eml_path, payload["raw_message"])
    _atomic_json(
        receipt_path,
        {
            "schema_version": 1,
            "status": "sent_archive_pending",
            "archive_error": error,
            "message_id": payload["message_id"],
            "constellation_id": payload["constellation_id"],
            "original_file": eml_path.name,
        },
    )
    return receipt_path


def send_and_archive(
    *,
    account: str,
    password: str,
    constellation_id: str,
    subject: str,
    body: str,
    identity_store: ProtectedIdentityStore,
    archive_store: MailArchiveStore,
    reconcile_root: Path,
    smtp_factory: Callable[[], Any],
) -> dict[str, Any]:
    identity = identity_store.resolve(constellation_id)
    message = build_message(account, identity["address"], subject, body)
    payload = _archive_payload(message, constellation_id)
    try:
        with smtp_factory() as smtp:
            smtp.login(account, password)
            refused = smtp.sendmail(account, [identity["address"]], payload["raw_message"])
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        return {"status": "not_sent", "error": str(exc)}

    try:
        manifest = archive_store.archive_sent(payload)
    except (MailArchiveError, OSError) as exc:
        try:
            receipt = _spool_reconcile(reconcile_root, payload, str(exc))
            receipt_value = str(receipt)
        except OSError as spool_exc:
            receipt_value = f"reconcile spool also failed: {spool_exc}"
        return {
            "status": "sent_archive_pending",
            "message_id": payload["message_id"],
            "archive_error": str(exc),
            "reconcile_receipt": receipt_value,
            "must_not_retry_send": True,
        }
    return {
        "status": "sent_and_archived",
        "message_id": payload["message_id"],
        "archive_id": manifest["archive_id"],
        "must_not_retry_send": True,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send through a protected Ombre correspondence identity."
    )
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--reconcile-root", type=Path, default=DEFAULT_RECONCILE)
    parser.add_argument("--constellation-id", required=True)
    parser.add_argument("--subject", required=True)
    body_group = parser.add_mutually_exclusive_group(required=True)
    body_group.add_argument("--body")
    body_group.add_argument("--body-file", type=Path)
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    try:
        env = read_env(args.env)
        account = first_value(env, USER_KEYS)
        password = first_value(env, PASSWORD_KEYS)
        if not account or not password:
            raise ValueError("mail account or app password is missing")
        body = (
            args.body_file.read_text(encoding="utf-8")
            if args.body_file is not None
            else str(args.body or "")
        )
        constellation_lookup = _projection_lookup(args.vault)
        _require_active_constellation(constellation_lookup(args.constellation_id))
        store = ProtectedIdentityStore(
            args.vault / "_private" / "correspondence-identities.json",
            constellation_lookup=constellation_lookup,
        )
        result = send_and_archive(
            account=account,
            password=password,
            constellation_id=args.constellation_id,
            subject=args.subject,
            body=body,
            identity_store=store,
            archive_store=MailArchiveStore(args.archive_root),
            reconcile_root=args.reconcile_root,
            smtp_factory=smtp_factory_from_env(env),
        )
    except (OSError, UnicodeError, ValueError, IdentityError) as exc:
        result = {"status": "not_sent", "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    # sent_archive_pending is success-with-warning: a nonzero exit can induce a
    # dangerous retry and duplicate the already-delivered email.
    return 0 if result["status"] != "not_sent" else 1


if __name__ == "__main__":
    raise SystemExit(main())
