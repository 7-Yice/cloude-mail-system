from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS = Path(__file__).parents[1] / "src"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import mail_summary_recover  # noqa: E402
from mail_archive import MailArchiveStore  # noqa: E402
from mail_hotline import process_messages  # noqa: E402
from mail_ledger import MessageLedger  # noqa: E402


def failed_fixture(runtime: Path) -> None:
    raw = (
        "Message-ID: <recover@example.test>\r\n"
        "From: Pen Pal <penpal@example.test>\r\n"
        "Subject: Recovery fixture\r\n"
        "Date: Tue, 25 Aug 2026 12:00:00 +0800\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        "完整归档正文。"
    ).encode("utf-8")
    message = {
        "uid": "7",
        "uidvalidity": "9",
        "message_id": "<recover@example.test>",
        "from": "Pen Pal <penpal@example.test>",
        "subject": "Recovery fixture",
        "date": "Tue, 25 Aug 2026 12:00:00 +0800",
        "body": "完整归档正文。",
        "raw_message": raw,
    }
    process_messages(
        [message],
        MessageLedger(runtime / "mail-message-ledger.json", runtime / "mail-inbox"),
        lambda *_: None,
        archive_store=MailArchiveStore(runtime / "mail-archive"),
        summarize=lambda _: (_ for _ in ()).throw(RuntimeError("fixture failure")),
    )


def test_recovery_uses_archived_original_and_never_notifies(
    monkeypatch, tmp_path: Path
) -> None:
    failed_fixture(tmp_path)
    candidates = mail_summary_recover.pending_candidates(
        tmp_path / "mail-message-ledger.json",
        tmp_path / "mail-inbox",
        tmp_path / "mail-archive",
    )
    assert len(candidates) == 1
    seen: list[str] = []

    def summarize(message, store) -> None:
        seen.append(message["body"])
        store.store_summary(
            message["_archive_manifest"],
            {"model": "fixture", "summary": "恢复摘要", "key_points": [], "open_questions": []},
        )
        message["summary_status"] = "ready"

    monkeypatch.setattr(mail_summary_recover, "summarize_with_ombre", summarize)
    ledger = MessageLedger(tmp_path / "mail-message-ledger.json", tmp_path / "mail-inbox")
    mail_summary_recover.recover_one(
        candidates[0], MailArchiveStore(tmp_path / "mail-archive"), ledger
    )

    assert seen == ["完整归档正文。"]
    value = json.loads((tmp_path / "mail-message-ledger.json").read_text(encoding="utf-8"))
    assert next(iter(value["messages"].values()))["summary_status"] == "ready"
    assert len(list((tmp_path / "mail-derived-summaries").glob("*.summary.json"))) == 1
    assert len(list((tmp_path / "mail-inbox").glob("*.json"))) == 1


def test_recovery_rejects_missing_archive_pointer(tmp_path: Path) -> None:
    failed_fixture(tmp_path)
    marker = next((tmp_path / "mail-inbox").glob("*.json"))
    value = json.loads(marker.read_text(encoding="utf-8"))
    value.pop("archive_manifest")
    marker.write_text(json.dumps(value), encoding="utf-8")
    try:
        mail_summary_recover.pending_candidates(
            tmp_path / "mail-message-ledger.json",
            tmp_path / "mail-inbox",
            tmp_path / "mail-archive",
        )
    except RuntimeError as exc:
        assert "original pointer" in str(exc)
    else:
        raise AssertionError("missing archive pointer must fail closed")


def test_ready_summary_can_be_selected_for_explicit_refresh(monkeypatch, tmp_path: Path) -> None:
    failed_fixture(tmp_path)
    pending = mail_summary_recover.pending_candidates(
        tmp_path / "mail-message-ledger.json",
        tmp_path / "mail-inbox",
        tmp_path / "mail-archive",
    )

    def summarize(message, store) -> None:
        store.store_summary(
            message["_archive_manifest"],
            {
                "model": "fixture",
                "summary": "旧摘要",
                "key_points": [],
                "open_questions": [],
                "verbatim_quotes": [message["body"]],
                "correspondence": {},
            },
            source_body=message["body"],
        )
        message["summary_status"] = "ready"

    monkeypatch.setattr(mail_summary_recover, "summarize_with_ombre", summarize)
    mail_summary_recover.recover_one(
        pending[0],
        MailArchiveStore(tmp_path / "mail-archive"),
        MessageLedger(tmp_path / "mail-message-ledger.json", tmp_path / "mail-inbox"),
    )
    ready = mail_summary_recover.summary_candidates(
        tmp_path / "mail-message-ledger.json",
        tmp_path / "mail-inbox",
        tmp_path / "mail-archive",
        summary_status="ready",
    )
    assert len(ready) == 1
    assert ready[0]["original"].read_bytes().startswith(b"Message-ID:")
    assert mail_summary_recover.has_note_v2(
        ready[0], tmp_path / "mail-derived-summaries"
    ) is True
