#!/usr/bin/env python3
"""Read-only correspondence debt report backed by Gmail mailbox history.

The report compares the newest received and sent message per protected
correspondence address.  Subjects and threads are deliberately irrelevant.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email
import email.header
import email.utils
import hashlib
import imaplib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from mail_identities import IdentityError
from send_mail import (
    DEFAULT_OMBRE_ENV,
    DEFAULT_OMBRE_MCP_URL,
    _mcp_tool_json,
    read_env,
)


RUNTIME_ROOT = Path(
    os.environ.get(
        "CLOUDE_MAIL_RUNTIME_DIR",
        str(Path.home() / ".config" / "cloude-mail-system"),
    )
)
ACCOUNT = os.environ.get("GMAIL_USER") or os.environ.get("MAIL_USER", "")
PASSWORD_FILE = Path(os.environ.get("CLOUDE_MAIL_PASSWORD_FILE", str(RUNTIME_ROOT / "mail-password")))
IDENTITY_FILE = Path(os.environ.get("CLOUDE_MAIL_IDENTITY_FILE", str(RUNTIME_ROOT / "correspondence-identities.json")))
SUMMARY_ROOT = Path(os.environ.get("CLOUDE_MAIL_SUMMARY_DIR", str(RUNTIME_ROOT / "mail-derived-summaries")))
SENT_ARCHIVE_ROOT = Path(os.environ.get("CLOUDE_MAIL_SENT_ARCHIVE_DIR", str(RUNTIME_ROOT / "mail-archive" / "sent")))
SCAN_STATE = Path(os.environ.get("CLOUDE_MAIL_OWE_STATE", str(RUNTIME_ROOT / "mail-owe-scan-state.json")))
RUNTIME_INBOX = Path(os.environ.get("CLOUDE_RUNTIME_INBOX", str(Path.home() / "runtime-inbox.py")))
IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
SCAN_INTERVAL_SECONDS = int(os.environ.get("CLOUDE_MAIL_OWE_INTERVAL", str(6 * 60 * 60)))


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(value)))
    except Exception:
        return value


def _internal_date(metadata: bytes) -> dt.datetime:
    match = re.search(rb'INTERNALDATE "([^"]+)"', metadata)
    if not match:
        raise ValueError("IMAP response has no INTERNALDATE")
    return dt.datetime.strptime(
        match.group(1).decode("ascii"), "%d-%b-%Y %H:%M:%S %z"
    ).astimezone(dt.timezone.utc)


def _active_identities(path: Path = IDENTITY_FILE) -> dict[str, dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or not isinstance(value.get("events"), list):
        raise ValueError("unsupported correspondence identity ledger")
    active: dict[str, dict[str, Any]] = {}
    for raw in value["events"]:
        if not isinstance(raw, dict):
            continue
        identity_id = str(raw.get("identity_id") or "")
        if raw.get("event") == "registered" and identity_id:
            active[identity_id] = raw
        elif raw.get("event") == "revoked":
            active.pop(identity_id, None)
    return {
        str(row["address"]).casefold(): row
        for row in active.values()
        if str(row.get("address") or "").strip()
    }


def _sent_mailbox(connection: imaplib.IMAP4_SSL) -> str:
    status, rows = connection.list()
    if status != "OK":
        raise RuntimeError("cannot list IMAP mailboxes")
    for raw in rows or []:
        decoded = raw.decode("utf-8", errors="replace")
        if "\\Sent" not in decoded:
            continue
        quoted = re.search(r'"([^"]+)"\s*$', decoded)
        if quoted:
            return quoted.group(1)
        return decoded.rsplit(" ", 1)[-1]
    raise RuntimeError("cannot find the IMAP sent mailbox")


def _fetch_records(
    connection: imaplib.IMAP4_SSL,
    mailbox: str,
    direction: str,
) -> list[dict[str, Any]]:
    status, _ = connection.select(mailbox, readonly=True)
    if status != "OK":
        raise RuntimeError(f"cannot select {direction} mailbox")
    status, search = connection.uid("search", None, "ALL")
    if status != "OK":
        raise RuntimeError(f"cannot search {direction} mailbox")
    uids = search[0].decode("ascii").split() if search and search[0] else []
    if not uids:
        return []
    status, parts = connection.uid(
        "fetch",
        ",".join(uids),
        "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])",
    )
    if status != "OK":
        raise RuntimeError(f"cannot fetch {direction} mailbox headers")
    records: list[dict[str, Any]] = []
    for part in parts or []:
        if not isinstance(part, tuple) or not isinstance(part[0], bytes):
            continue
        parsed = email.message_from_bytes(part[1])
        header = parsed.get("From") if direction == "incoming" else parsed.get("To")
        recipients = email.utils.getaddresses([_decode_header(header)])
        for display_name, address in recipients:
            normalized = address.strip().casefold()
            if not normalized:
                continue
            records.append(
                {
                    "direction": direction,
                    "timestamp": _internal_date(part[0]),
                    "address": normalized,
                    "display_name": display_name.strip(),
                    "subject": _decode_header(parsed.get("Subject")) or "(no subject)",
                    "message_id": str(parsed.get("Message-ID") or "").strip(),
                }
            )
    return records


def fetch_mail_history(
    *,
    account: str = ACCOUNT,
    password_file: Path = PASSWORD_FILE,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    del now
    if not account:
        raise RuntimeError("GMAIL_USER or MAIL_USER is required")
    connection = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        connection.login(account, password_file.read_text(encoding="utf-8").strip())
        sent_mailbox = _sent_mailbox(connection)
        return _fetch_records(connection, "INBOX", "incoming") + _fetch_records(
            connection, sent_mailbox, "sent"
        )
    finally:
        try:
            connection.logout()
        except Exception:
            pass


def _summary_by_message_id(root: Path = SUMMARY_ROOT) -> dict[str, str]:
    summaries: dict[str, str] = {}
    for path in root.glob("*.summary.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pointer = value.get("original_pointer")
        if not isinstance(pointer, dict):
            continue
        message_id = str(pointer.get("message_id") or "").strip()
        summary = " ".join(str(value.get("summary") or "").split())
        if message_id and summary:
            summaries[message_id] = summary
    return summaries


def _plain_body(parsed: email.message.Message) -> str:
    parts = parsed.walk() if parsed.is_multipart() else [parsed]
    rendered: list[str] = []
    for part in parts:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() != "text/plain":
            continue
        raw = part.get_payload(decode=True)
        if raw is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            rendered.append(raw.decode(charset, errors="replace"))
        except LookupError:
            rendered.append(raw.decode("utf-8", errors="replace"))
    return "\n\n".join(rendered).strip()


def _two_sentence_excerpt(body: str, *, limit: int = 500) -> str:
    normalized = " ".join(str(body or "").split())
    if not normalized:
        return "暂无可读正文"
    pieces = [
        piece.strip()
        for piece in re.split(r"(?<=[。！？!?])\s*", normalized)
        if piece.strip()
    ]
    excerpt = " ".join(pieces[:2]) if pieces else normalized
    return excerpt if len(excerpt) <= limit else excerpt[: limit - 1].rstrip() + "…"


def _sent_excerpt_by_message_id(root: Path = SENT_ARCHIVE_ROOT) -> dict[str, str]:
    excerpts: dict[str, str] = {}
    for manifest_path in root.glob("*.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            message_id = str(manifest.get("message_id") or "").strip()
            original_name = str(manifest.get("original_file") or "").strip()
            if not message_id or not original_name:
                continue
            original_path = manifest_path.parent / Path(original_name).name
            parsed = email.message_from_bytes(original_path.read_bytes())
            excerpts[message_id] = _two_sentence_excerpt(_plain_body(parsed))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            continue
    return excerpts


def _constellation_names(
    identities: dict[str, dict[str, Any]],
    *,
    mcp_env: Path = DEFAULT_OMBRE_ENV,
    mcp_url: str = DEFAULT_OMBRE_MCP_URL,
) -> dict[str, str]:
    try:
        token = str(read_env(mcp_env).get("OMBRE_MCP_TOKEN") or "")
    except (OSError, UnicodeError, ValueError):
        return {}
    names: dict[str, str] = {}
    constellation_ids = {
        str(row.get("constellation_id") or "").strip()
        for row in identities.values()
    }
    for constellation_id in sorted(item for item in constellation_ids if item):
        try:
            value = _mcp_tool_json(
                url=mcp_url,
                token=token,
                name="constellation_read",
                arguments={
                    "action": "inspect",
                    "constellation_id": constellation_id,
                    "max_tokens": 600,
                },
            )
        except (OSError, UnicodeError, ValueError, IdentityError):
            continue
        business = value.get("business")
        name = str(business.get("name") or "").strip() if isinstance(business, dict) else ""
        if name:
            names[constellation_id] = name
    return names


def _masked_address(address: str) -> str:
    local, separator, domain = address.partition("@")
    if not separator:
        return "***"
    shown = local[:1] if local else ""
    return f"{shown}***@{domain}"


def build_owe_rows(
    records: Iterable[dict[str, Any]],
    identities: dict[str, dict[str, Any]],
    summaries: dict[str, str],
    *,
    now: dt.datetime | None = None,
    constellation_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {
        address: {"incoming": [], "sent": []} for address in identities
    }
    for record in records:
        address = str(record.get("address") or "").casefold()
        direction = str(record.get("direction") or "")
        if address in grouped and direction in grouped[address]:
            grouped[address][direction].append(record)

    rows: list[dict[str, Any]] = []
    for address, directions in grouped.items():
        if not directions["incoming"]:
            continue
        their_last = max(directions["incoming"], key=lambda item: item["timestamp"])
        my_last = max(directions["sent"], key=lambda item: item["timestamp"], default=None)
        owes = my_last is None or their_last["timestamp"] > my_last["timestamp"]
        age_seconds = max(0.0, (current - their_last["timestamp"]).total_seconds())
        constellation_id = str(identities[address].get("constellation_id") or "").strip()
        display_name = str((constellation_names or {}).get(constellation_id) or "").strip()
        if not display_name:
            display_name = str(their_last.get("display_name") or "").strip()
        if not display_name:
            display_name = _masked_address(address)
        rows.append(
            {
                "display_name": display_name,
                "address_masked": _masked_address(address),
                "address_key": hashlib.sha256(address.encode("utf-8")).hexdigest()[:16],
                "status": "欠" if owes else "等他",
                "owes_reply": owes,
                "owed_days": int(age_seconds // 86400) if owes else 0,
                "overdue_for_reminder": owes and age_seconds > 3 * 86400,
                "their_last_at": their_last["timestamp"].isoformat(),
                "their_last_subject": their_last["subject"],
                "their_last_summary": summaries.get(
                    str(their_last.get("message_id") or ""), "暂无已生成摘要"
                ),
                "their_last_message_id": str(their_last.get("message_id") or ""),
                "my_last_at": my_last["timestamp"].isoformat() if my_last else None,
                "my_last_subject": my_last["subject"] if my_last else None,
                "my_last_summary": summaries.get(
                    str(my_last.get("message_id") or ""), "暂无已生成摘要"
                ) if my_last else "尚未发过",
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            0 if row["owes_reply"] else 1,
            -row["owed_days"] if row["owes_reply"] else 0,
            row["display_name"].casefold(),
        ),
    )


def collect_report(*, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    identities = _active_identities()
    summaries = _sent_excerpt_by_message_id()
    summaries.update(_summary_by_message_id())
    return build_owe_rows(
        fetch_mail_history(now=now),
        identities,
        summaries,
        now=now,
        constellation_names=_constellation_names(identities),
    )


def render_text(rows: Iterable[dict[str, Any]]) -> str:
    lines: list[str] = []
    for row in rows:
        status = f"欠 {row['owed_days']} 天" if row["owes_reply"] else "等他"
        my_last = row["my_last_subject"] or "尚未发过"
        lines.append(
            f"{status}｜{row['display_name']} <{row['address_masked']}>｜"
            f"他最后：{row['their_last_subject']}｜模型摘要：{row['their_last_summary']}｜"
            f"我最后：{my_last}｜正文摘要：{row['my_last_summary']}"
        )
    return "\n".join(lines) if lines else "暂无通信记录"


def notify_overdue(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for row in rows:
        if not row["overdue_for_reminder"]:
            continue
        identity = "\0".join(
            [row["address_key"], row["their_last_message_id"], row["their_last_at"]]
        )
        event_id = "mail-owe-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        text = (
            f"📮 欠信提醒：{row['display_name']} <{row['address_masked']}> 的最后一封信"
            f"已经等了 {row['owed_days']} 天。\n"
            f"主题：{row['their_last_subject']}\n"
            f"模型摘要：{row['their_last_summary']}\n"
            "这是欠信表的单次提醒；不会自动发信。"
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(RUNTIME_INBOX),
                "enqueue",
                "--source", "mail_hotline",
                "--event-id", event_id,
                "--mode", "immediate",
                "--trust", "untrusted_external_data",
                "--metadata-json",
                json.dumps(
                    {
                        "kind": "mail_owe_reminder",
                        "address_key": row["address_key"],
                        "owed_days": row["owed_days"],
                    },
                    ensure_ascii=False,
                ),
            ],
            input=text,
            text=True,
            check=True,
            capture_output=True,
            timeout=12,
        )
        results.append(json.loads(completed.stdout))
    return results


def _atomic_scan_state(path: Path, checked_at: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": 1, "checked_at": checked_at}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def maybe_notify_overdue(
    *, state_file: Path = SCAN_STATE, interval_seconds: int = SCAN_INTERVAL_SECONDS
) -> list[dict[str, Any]]:
    current = time.time()
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        checked_at = float(state.get("checked_at") or 0)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        checked_at = 0
    if current - checked_at < interval_seconds:
        return []
    results = notify_overdue(collect_report())
    _atomic_scan_state(state_file, current)
    return results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List correspondence debt by address; never sends or mutates mail."
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--notify-overdue", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rows = collect_report()
    if args.notify_overdue:
        notification_results = notify_overdue(rows)
    else:
        notification_results = []
    if args.json:
        print(
            json.dumps(
                {"status": "ok", "rows": rows, "notifications": notification_results},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(render_text(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
