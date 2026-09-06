#!/usr/bin/env python3
"""Mail hotline with durable IMAP and notification watermarks.

This script deliberately has no SMTP path.  It scans every new IMAP UID rather
than relying on the mutable ``UNSEEN`` flag, archives the original message, and
then sends a companion wake notice through Runtime Inbox.
"""

from __future__ import annotations

import email
import email.header
import hashlib
import imaplib
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterator
from contextlib import contextmanager

from mail_archive import MailArchiveError, MailArchiveStore
from mail_commitments import active_summary
from mail_identities import ProtectedIdentityStore, address_from_header
from mail_ledger import MessageLedger, message_key
from mail_owe_list import maybe_notify_overdue
from send_mail import (
    DEFAULT_OMBRE_ENV,
    DEFAULT_OMBRE_MCP_URL,
    _mcp_tool_json,
    read_env,
)

try:  # The production cron host is Linux; fixtures also run on Windows.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on Windows hosts.
    fcntl = None


RUNTIME_ROOT = Path(
    os.environ.get(
        "CLOUDE_MAIL_RUNTIME_DIR",
        str(Path.home() / ".config" / "cloude-mail-system"),
    )
)
GMAIL_USER = os.environ.get("GMAIL_USER") or os.environ.get("MAIL_USER", "")
PASSWORD_FILE = Path(os.environ.get("CLOUDE_MAIL_PASSWORD_FILE", str(RUNTIME_ROOT / "mail-password")))
INBOX_DIR = Path(os.environ.get("CLOUDE_MAIL_INBOX_DIR", str(RUNTIME_ROOT / "mail-inbox")))
LEDGER_FILE = Path(os.environ.get("CLOUDE_MAIL_LEDGER_FILE", str(RUNTIME_ROOT / "mail-message-ledger.json")))
STATE_FILE = Path(os.environ.get("CLOUDE_MAIL_STATE_FILE", str(RUNTIME_ROOT / "mail-hotline-state.json")))
ARCHIVE_DIR = Path(os.environ.get("CLOUDE_MAIL_ARCHIVE_DIR", str(RUNTIME_ROOT / "mail-archive")))
IDENTITY_FILE = Path(os.environ.get("CLOUDE_MAIL_IDENTITY_FILE", str(RUNTIME_ROOT / "correspondence-identities.json")))
RUN_LOCK_FILE = Path(os.environ.get("CLOUDE_MAIL_RUN_LOCK", str(RUNTIME_ROOT / "mail-hotline-run.lock")))
RUNTIME_INBOX = Path(os.environ.get("CLOUDE_RUNTIME_INBOX", str(Path.home() / "runtime-inbox.py")))
MAX_MESSAGES_PER_RUN = int(os.environ.get("CLOUDE_MAIL_MAX_PER_RUN", "10"))
SUMMARY_CONTAINER = os.environ.get("OMBRE_CONTAINER", "ombre-brain")
IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
RELAY_URL = os.environ.get("RELAY_URL", "http://127.0.0.1:3011")
WAKE_ENV = Path(os.environ.get("RELAY_WAKE_ENV", str(RUNTIME_ROOT / "relay-wake.env")))


def wake_secret() -> str:
    for raw in WAKE_ENV.read_text(encoding="utf-8").splitlines():
        if raw.startswith("RELAY_WAKE_SECRET="):
            return raw.split("=", 1)[1].strip()
    raise RuntimeError("RELAY_WAKE_SECRET is unavailable")


def read_password() -> str:
    return PASSWORD_FILE.read_text(encoding="utf-8").strip()


@contextmanager
def run_lock() -> Iterator[bool]:
    """Avoid concurrent cron runs fetching the same unread mailbox backlog."""
    if fcntl is None:
        yield True
        return
    RUN_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOCK_FILE.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _decoded_header(value: str | None, default: str) -> str:
    if not value:
        return default
    try:
        return str(email.header.make_header(email.header.decode_header(value)))
    except Exception:
        return value


def _uidvalidity(mail: imaplib.IMAP4_SSL) -> str:
    response = mail.response("UIDVALIDITY")
    values = response[1] if response else None
    if values and values[0]:
        return values[0].decode("ascii") if isinstance(values[0], bytes) else str(values[0])
    raise RuntimeError("IMAP server did not provide UIDVALIDITY")


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _numeric_uid(value: Any) -> int | None:
    try:
        uid = int(str(value))
    except (TypeError, ValueError):
        return None
    return uid if uid >= 0 else None


def last_processed_uid(
    uidvalidity: str,
    *,
    state_file: Path = STATE_FILE,
    ledger_file: Path = LEDGER_FILE,
) -> int:
    """Return the safest known mailbox watermark for this UIDVALIDITY.

    The notification ledger is used as a migration source because older
    installations only stored ``seen_uids`` and may have already archived newer
    messages.  Taking the maximum keeps the migration idempotent without making
    read/unread state part of delivery correctness.
    """
    candidates = [0]
    state = _load_json_object(state_file)
    if str(state.get("uidvalidity") or "") == uidvalidity:
        value = _numeric_uid(state.get("last_uid"))
        if value is not None:
            candidates.append(value)
    for value in state.get("seen_uids", []):
        numeric = _numeric_uid(value)
        if numeric is not None:
            candidates.append(numeric)

    ledger = _load_json_object(ledger_file)
    records = ledger.get("messages", {})
    if isinstance(records, dict):
        for record in records.values():
            if not isinstance(record, dict):
                continue
            if str(record.get("uidvalidity") or "") != uidvalidity:
                continue
            numeric = _numeric_uid(record.get("uid"))
            if numeric is not None:
                candidates.append(numeric)
    return max(candidates)


def save_uid_cursor(
    uidvalidity: str,
    last_uid: int,
    *,
    state_file: Path = STATE_FILE,
) -> None:
    """Atomically persist the IMAP cursor after processing has succeeded."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    previous = _load_json_object(state_file)
    payload = {
        "schema_version": 2,
        "uidvalidity": uidvalidity,
        "last_uid": int(last_uid),
        "updated_at": time.time(),
    }
    # Preserve the legacy field for rollback compatibility; it is no longer used
    # as the primary scanner.
    if isinstance(previous.get("seen_uids"), list):
        payload["seen_uids"] = previous["seen_uids"]
    temporary = state_file.with_name(f".{state_file.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, state_file)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class _PlainTextHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")


def _part_text(part: email.message.Message) -> str:
    raw = part.get_payload(decode=True)
    if raw is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def display_body(parsed: email.message.Message) -> str:
    """Return the full readable letter body; raw RFC822 remains in the archive."""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    parts = parsed.walk() if parsed.is_multipart() else [parsed]
    for part in parts:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            plain_parts.append(_part_text(part))
        elif content_type == "text/html":
            html_parts.append(_part_text(part))
    if plain_parts:
        return "\n\n".join(part.strip() for part in plain_parts).strip()
    rendered: list[str] = []
    for html in html_parts:
        parser = _PlainTextHTML()
        parser.feed(html)
        parser.close()
        rendered.append("".join(parser.parts))
    return "\n\n".join(rendered).strip()


def check_gmail(password: str) -> tuple[list[dict[str, Any]], str, int | None]:
    if not GMAIL_USER:
        raise RuntimeError("GMAIL_USER or MAIL_USER is required")
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        mail.login(GMAIL_USER, password)
        status, _ = mail.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("cannot select INBOX")
        uidvalidity = _uidvalidity(mail)
        after_uid = last_processed_uid(uidvalidity)
        status, data = mail.uid("search", None, f"UID {after_uid + 1}:*")
        if status != "OK":
            raise RuntimeError("cannot search new mail UIDs")
        candidate_uids = sorted(
            {
                int(raw_uid)
                for raw_uid in (data[0].split() if data and data[0] else [])
                if raw_uid.isdigit() and int(raw_uid) > after_uid
            }
        )[:MAX_MESSAGES_PER_RUN]
        messages: list[dict[str, Any]] = []
        scanned_through: int | None = None
        for numeric_uid in candidate_uids:
            uid = str(numeric_uid)
            status, msg_data = mail.uid(
                "fetch",
                uid,
                "(BODY.PEEK[])",
            )
            if status != "OK" or not msg_data or msg_data[0] is None:
                # Do not jump the durable cursor across a message we could not
                # fetch; the next cron run must retry from this exact boundary.
                break
            raw = msg_data[0][1]
            if not isinstance(raw, bytes):
                break
            parsed = email.message_from_bytes(raw)
            messages.append(
                {
                    "uid": uid,
                    "uidvalidity": uidvalidity,
                    "message_id": str(parsed.get("Message-ID") or "").strip(),
                    "from": _decoded_header(parsed.get("From"), "unknown"),
                    "to": _decoded_header(parsed.get("To"), "unknown"),
                    "subject": _decoded_header(parsed.get("Subject"), "(no subject)"),
                    "date": str(parsed.get("Date") or "").strip(),
                    "in_reply_to": str(parsed.get("In-Reply-To") or "").strip(),
                    "references": str(parsed.get("References") or "").strip(),
                    "body": display_body(parsed),
                    "raw_message": raw,
                }
            )
            scanned_through = numeric_uid
        return messages, uidvalidity, scanned_through
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def notify_companion(message: dict[str, Any], notification_id: str) -> None:
    archive_error = str(message.get("archive_error") or "").strip()
    headline = "📬 新邮件到了！"
    if archive_error:
        headline = "⚠️ 已收到邮件，但原件归档失败（请勿当作已归档）："
    text = "\n".join(
        [
            headline,
            f"  来自: {message['from']}",
            f"  主题: {message['subject']}",
            f"  日期: {message['date']}",
            "",
            "—— 来信完整正文 ——",
            str(message.get("body") or "（此邮件无可读文本正文；完整 MIME 原件见归档指针）"),
            "",
            *_reply_entry_lines(message),
        ]
    )
    if archive_error:
        text += f"\n\n归档错误：{archive_error}"
    event_id = "mail-" + hashlib.sha256(
        notification_id.encode("utf-8")
    ).hexdigest()[:32]
    subprocess.run(
        [
            sys.executable,
            str(RUNTIME_INBOX),
            "enqueue",
            "--source", "mail_hotline",
            "--event-id", event_id,
            "--mode", "immediate",
            "--trust", "untrusted_external_data",
            "--metadata-json",
            json.dumps({"mail_event_id": notification_id}, ensure_ascii=False),
        ],
        input=text, text=True, check=True, timeout=12,
        stdout=subprocess.PIPE,
    )


def _reply_entry_lines(message: dict[str, Any]) -> list[str]:
    constellation_id = str(message.get("constellation_id") or "").strip()
    if not constellation_id:
        return [
            "一步回信：未登记，需 register-from-mail",
            "上次聊到：首封",
        ]
    note = " ".join(str(message.get("reply_note") or "暂不可用").split())
    lines = [
        f"一步回信：constellation_id={constellation_id}",
        f"上次聊到：{note}",
    ]
    context = message.get("commitment_context")
    if isinstance(context, dict):
        active = [str(item) for item in context.get("active", []) if str(item).strip()]
        if active:
            lines.append("尚欠对方：" + "；".join(active[:3]))
            if len(active) > 3:
                lines.append(f"另有 {len(active) - 3} 项进行中承诺")
        pending = int(context.get("pending_review_count") or 0)
        if pending:
            lines.append(f"待确认承诺：{pending} 条（在 dream 审核抽屉逐条核原话）")
    return lines


def prepare_reply_note(
    constellation_id: str,
    *,
    mcp_env: Path = DEFAULT_OMBRE_ENV,
    mcp_url: str = DEFAULT_OMBRE_MCP_URL,
) -> str:
    token = str(read_env(mcp_env).get("OMBRE_MCP_TOKEN") or "")
    result = _mcp_tool_json(
        url=mcp_url,
        token=token,
        name="prepare_mail_reply",
        arguments={
            "constellation_id": constellation_id,
            "cursor": 0,
            "max_tokens": 600,
        },
    )
    note = " ".join(str(result.get("latest_sent_note") or "").split())
    if not note:
        raise RuntimeError("prepare_mail_reply returned no latest_sent_note")
    return note


def summarize_with_ombre(message: dict[str, Any], archive_store: MailArchiveStore) -> None:
    """Use the running Ombre image's configured Dehydrator; no new service or key."""
    completed = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            SUMMARY_CONTAINER,
            "/bin/sh",
            "-lc",
            "PYTHONPATH=/app/src python -m ombrebrain.mail.ingest_summarizer",
        ],
        input=json.dumps(
            {
                "body": message["body"],
                "metadata": {
                    key: str(message.get(key) or "")
                    for key in (
                        "from",
                        "to",
                        "subject",
                        "date",
                        "message_id",
                        "in_reply_to",
                        "references",
                    )
                },
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=150,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[:300]
        raise RuntimeError(f"mail summary worker failed: {detail or completed.returncode}")
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("mail summary worker returned invalid JSON") from exc
    manifest = message.get("_archive_manifest")
    if not isinstance(manifest, dict):
        raise RuntimeError("mail summary is missing its original archive manifest")
    archive_store.store_summary(manifest, result, source_body=str(message["body"]))
    message["summary_status"] = "ready"


def classify_from_protected_identity(
    message: dict[str, Any], identity_store: ProtectedIdentityStore
) -> dict[str, Any] | None:
    address = address_from_header(str(message.get("from") or ""))
    if address is None:
        return None
    identity = identity_store.resolve_address(address)
    if identity is None:
        return None
    return {
        "constellation_id": identity["constellation_id"],
        "constellation_assignment": {
            "schema_version": 1,
            "method": "protected_identity_exact_sender",
            "identity_id": identity["identity_id"],
            "identity_source_kind": identity["source_kind"],
            "identity_source_id": identity["source_id"],
            "identity_recorded_at": identity["recorded_at"],
            "classified_by": "mail_hotline",
            "classified_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def process_messages(
    messages: list[dict[str, Any]],
    ledger: MessageLedger,
    notify: Callable[[dict[str, Any], str], None] = notify_companion,
    *,
    archive_store: MailArchiveStore | None = None,
    summarize: Callable[[dict[str, Any]], None] | None = None,
    classify: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    prepare_reply: Callable[[str], str] | None = None,
    commitment_context: Callable[[str], dict[str, Any]] | None = None,
) -> int:
    delivered = 0
    for message in messages:
        notification_id = message_key(message)
        if archive_store is not None:
            if classify is not None:
                try:
                    assignment = classify(message)
                except Exception as exc:
                    assignment = None
                    print(
                        f"mail_hotline: classification deferred for {notification_id}: "
                        f"{type(exc).__name__}: {str(exc)[:240]}",
                        file=sys.stderr,
                    )
                if assignment:
                    message = {**message, **assignment}
            try:
                manifest = archive_store.archive(message)
            except MailArchiveError as exc:
                # The body is still shown, but never presented as a successful archive.
                failed = dict(message)
                failed["archive_error"] = str(exc)
                notify(failed, notification_id)
                delivered += 1
                continue
            message = dict(message)
            message["archive_id"] = manifest["archive_id"]
            message["archive_manifest"] = manifest["original_file"]
            message["_archive_manifest"] = manifest
            message["summary_status"] = "pending"
        constellation_id = str(message.get("constellation_id") or "").strip()
        if constellation_id and prepare_reply is not None:
            try:
                message["reply_note"] = prepare_reply(constellation_id)
            except Exception as exc:
                message["reply_note"] = "暂不可用"
                print(
                    f"mail_hotline: reply entry note deferred for {notification_id}: "
                    f"{type(exc).__name__}: {str(exc)[:240]}",
                    file=sys.stderr,
                )
        if constellation_id and commitment_context is not None:
            try:
                message["commitment_context"] = commitment_context(constellation_id)
            except Exception as exc:
                print(
                    f"mail_hotline: commitment context deferred for {notification_id}: "
                    f"{type(exc).__name__}: {str(exc)[:240]}",
                    file=sys.stderr,
                )
        token = ledger.claim_notification(message)
        if token is None:
            continue
        if archive_store is not None:
            if summarize is not None:
                try:
                    summarize(message)
                except Exception as exc:
                    # Summary is a rebuildable derivative and must never block the original.
                    message["summary_status"] = "pending_after_failure"
                    print(
                        f"mail_hotline: summary deferred for {notification_id}: "
                        f"{type(exc).__name__}: {str(exc)[:240]}",
                        file=sys.stderr,
                    )
            ledger.update_ingest_metadata(message)
        try:
            notify(message, notification_id)
        except Exception:
            ledger.release_notification(message, token)
            raise
        ledger.acknowledge_notification(message, token)
        delivered += 1
    return delivered


def main() -> int:
    try:
        password = read_password()
    except FileNotFoundError:
        return 0
    try:
        with run_lock() as acquired:
            if not acquired:
                return 0
            messages, uidvalidity, scanned_through = check_gmail(password)
            archive_store = MailArchiveStore(ARCHIVE_DIR)
            identity_store = ProtectedIdentityStore(
                IDENTITY_FILE,
                constellation_lookup=lambda _: {},
            )
            delivered = process_messages(
                messages,
                MessageLedger(LEDGER_FILE, INBOX_DIR),
                archive_store=archive_store,
                summarize=lambda message: summarize_with_ombre(message, archive_store),
                classify=lambda message: classify_from_protected_identity(
                    message, identity_store
                ),
                prepare_reply=prepare_reply_note,
                commitment_context=lambda constellation_id: active_summary(
                    RUNTIME_ROOT / "mail-commitments.json", constellation_id
                ),
            )
            # Advancing only after archive/ledger/Runtime Inbox processing avoids
            # permanently skipping a message when any downstream step fails.
            if scanned_through is not None:
                save_uid_cursor(uidvalidity, scanned_through)
    except Exception as exc:
        print(f"mail_hotline: {exc}", file=sys.stderr)
        return 1
    try:
        maybe_notify_overdue()
    except Exception as exc:
        # Owe reminders are derivative and must never block real-mail ingest.
        print(
            f"mail_hotline: owe-list reminder deferred: "
            f"{type(exc).__name__}: {str(exc)[:240]}",
            file=sys.stderr,
        )
    if delivered:
        print(f"mail_hotline: {delivered} new message(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
