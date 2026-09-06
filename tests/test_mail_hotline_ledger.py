from __future__ import annotations

import hashlib
import json
import multiprocessing
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).parents[1] / "src"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import mail_hotline  # noqa: E402
from mail_hotline import process_messages  # noqa: E402
from mail_archive import MailArchiveStore  # noqa: E402
from mail_ledger import LedgerError, MessageLedger, message_key  # noqa: E402


def _concurrent_claim(ledger_path: str, inbox_dir: str, gate: object, result: object) -> None:
    gate.wait(5)
    token = MessageLedger(Path(ledger_path), Path(inbox_dir)).claim_notification(sample_message())
    result.put(bool(token))


def sample_message(**changes: str) -> dict[str, str]:
    message = {
        "uid": "44",
        "uidvalidity": "991",
        "message_id": "<letter-44@example.test>",
        "from": "Pen Pal <penpal@example.test>",
        "subject": "A fixture letter",
        "date": "Tue, 25 Aug 2026 12:00:00 +0800",
    }
    message.update(changes)
    return message


def test_message_id_is_preferred_and_uidvalidity_is_the_safe_fallback() -> None:
    assert message_key(sample_message()) == "rfc822:<letter-44@example.test>"
    assert message_key(sample_message(message_id="")) == "imap:uidvalidity=991;uid=44"
    with pytest.raises(LedgerError, match="neither"):
        message_key(sample_message(message_id="", uidvalidity=""))


def test_imap_fixture_reads_message_id_and_uidvalidity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_message = (
        "Message-ID: <fixture-55@example.test>\r\n"
        "From: =?utf-8?b?UGVuIFBhbA==?= <penpal@example.test>\r\n"
        "Subject: fixture subject\r\n"
        "Date: Tue, 25 Aug 2026 12:00:00 +0800\r\n\r\n"
        "这是一封完整的 fixture 来信。"
    ).encode("utf-8")

    class FakeImap:
        def __init__(self, host: str, port: int) -> None:
            assert host == "imap.gmail.com"
            assert port == 993
            self.logged_out = False

        def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
            assert (user, password) == (mail_hotline.GMAIL_USER, "fixture-password")
            return "OK", []

        def select(self, mailbox: str, *, readonly: bool) -> tuple[str, list[bytes]]:
            assert mailbox == "INBOX"
            assert readonly is True
            return "OK", []

        def response(self, code: str) -> tuple[str, list[bytes]]:
            assert code == "UIDVALIDITY"
            return "UIDVALIDITY", [b"451"]

        def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
            if command == "search":
                assert args == (None, "UID 1:*")
                return "OK", [b"55"]
            assert command == "fetch"
            assert args == ("55", "(BODY.PEEK[])")
            return "OK", [(b"message", raw_message)]

        def logout(self) -> None:
            self.logged_out = True

    monkeypatch.setattr(mail_hotline, "GMAIL_USER", "cloud@example.test")
    monkeypatch.setattr(mail_hotline, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(mail_hotline.imaplib, "IMAP4_SSL", FakeImap)
    messages, uidvalidity, scanned_through = mail_hotline.check_gmail("fixture-password")
    assert messages == [{
            "uid": "55",
            "uidvalidity": "451",
            "message_id": "<fixture-55@example.test>",
            "from": "Pen Pal <penpal@example.test>",
            "to": "unknown",
            "subject": "fixture subject",
            "date": "Tue, 25 Aug 2026 12:00:00 +0800",
            "in_reply_to": "",
            "references": "",
            "body": "这是一封完整的 fixture 来信。",
            "raw_message": raw_message,
    }]
    assert uidvalidity == "451"
    assert scanned_through == 55


def test_ingest_archives_original_before_summary_ledger_and_full_body_notice(tmp_path: Path) -> None:
    message = sample_message()
    message.update({"body": "完整来信正文，不是摘要。", "raw_message": b"Raw: complete fixture"})
    notices: list[dict[str, object]] = []
    summaries: list[str] = []

    def fake_summary(candidate: dict[str, object]) -> None:
        assert (tmp_path / "archive" / "incoming").exists()
        summaries.append(str(candidate["archive_id"]))

    assert process_messages(
        [message],
        MessageLedger(tmp_path / "ledger.json", tmp_path / "inbox"),
        lambda candidate, _: notices.append(candidate),
        archive_store=MailArchiveStore(tmp_path / "archive"),
        summarize=fake_summary,
    ) == 1

    manifest = next((tmp_path / "archive" / "incoming").glob("*.json"))
    archive = json.loads(manifest.read_text(encoding="utf-8"))
    assert archive["message_id"] == message["message_id"]
    assert (manifest.parent / archive["original_file"]).read_bytes() == b"Raw: complete fixture"
    assert summaries == [archive["archive_id"]]
    assert notices[0]["body"] == "完整来信正文，不是摘要。"
    marker = json.loads(next((tmp_path / "inbox").glob("*.json")).read_text(encoding="utf-8"))
    assert marker["archive_id"] == archive["archive_id"]
    assert "raw_message" not in marker and "body" not in marker


def test_summary_failure_does_not_block_archived_original_or_notice(tmp_path: Path) -> None:
    message = sample_message()
    message.update({"body": "完整来信", "raw_message": b"raw fixture"})
    notices: list[dict[str, object]] = []

    def failed_summary(_: dict[str, object]) -> None:
        raise RuntimeError("DeepSeek fixture unavailable")

    assert process_messages(
        [message],
        MessageLedger(tmp_path / "ledger.json", tmp_path / "inbox"),
        lambda candidate, _: notices.append(candidate),
        archive_store=MailArchiveStore(tmp_path / "archive"),
        summarize=failed_summary,
    ) == 1
    assert notices[0]["summary_status"] == "pending_after_failure"
    assert len(list((tmp_path / "archive" / "incoming").glob("*.eml"))) == 1


def test_repeated_unread_poll_does_not_repeat_archive_summary_or_notice(tmp_path: Path) -> None:
    message = sample_message()
    message.update({"body": "完整来信", "raw_message": b"raw fixture"})
    store = MailArchiveStore(tmp_path / "archive")
    ledger = MessageLedger(tmp_path / "ledger.json", tmp_path / "inbox")
    summaries: list[str] = []
    notices: list[str] = []

    for _ in range(2):
        process_messages(
            [message],
            ledger,
            lambda _, event_id: notices.append(event_id),
            archive_store=store,
            summarize=lambda _: summaries.append("called"),
        )

    assert summaries == ["called"]
    assert notices == ["rfc822:<letter-44@example.test>"]
    assert len(list((tmp_path / "archive" / "incoming").glob("*.eml"))) == 1


def test_backfill_deduplicates_by_message_id_not_uid_subject_or_path(tmp_path: Path) -> None:
    first = sample_message()
    first.update({"body": "完整来信", "raw_message": b"raw fixture"})
    replay = {
        **first,
        "uid": "999",
        "subject": "即使标题变了也不能重归档",
    }
    store = MailArchiveStore(tmp_path / "archive")
    ledger = MessageLedger(tmp_path / "ledger.json", tmp_path / "inbox")
    notices: list[str] = []

    assert process_messages([first], ledger, lambda _, event_id: notices.append(event_id), archive_store=store) == 1
    assert process_messages([replay], ledger, lambda _, event_id: notices.append(event_id), archive_store=store) == 0
    assert len(list((tmp_path / "archive" / "incoming").glob("*.eml"))) == 1
    assert notices == ["rfc822:<letter-44@example.test>"]


def test_ombre_summary_bridge_writes_a_disposable_sidecar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    message = sample_message()
    message.update({"body": "完整来信", "raw_message": b"raw fixture"})
    store = MailArchiveStore(tmp_path / "archive")
    manifest = store.archive(message)
    message["_archive_manifest"] = manifest

    class Completed:
        returncode = 0
        stdout = (
            '{"model":"deepseek-chat","summary":"派生摘要","key_points":[],'
            '"open_questions":[],"verbatim_quotes":["完整来信"],'
            '"correspondence":{"sender":"Pen Pal <penpal@example.test>",'
            '"recipient":"Cloude","in_reply_to":"","awaiting_reply":true,'
            '"awaiting_reply_basis":"明确提问"}}'
        ).encode("utf-8")
        stderr = b""

    monkeypatch.setattr(mail_hotline.subprocess, "run", lambda *_, **__: Completed())
    mail_hotline.summarize_with_ombre(message, store)

    sidecar = next((tmp_path / "mail-derived-summaries").glob("*.summary.json"))
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    assert message["summary_status"] == "ready"
    assert record["original_pointer"] == {
        "archive_id": manifest["archive_id"],
        "message_id": message["message_id"],
    }
    assert record["citation_policy"] == "original_only"
    assert record["quote_policy"] == "exact_substring_verified"
    assert record["verbatim_quotes"] == ["完整来信"]
    assert record["correspondence"]["awaiting_reply"] is True
    assert "source_links" not in record


def test_archive_failure_is_not_reported_as_a_successful_archive(tmp_path: Path) -> None:
    message = sample_message()
    message.update({"body": "这封信仍须完整呈现", "raw_message": b"raw fixture"})
    notices: list[dict[str, object]] = []

    class BrokenArchive:
        def archive(self, _: dict[str, object]) -> dict[str, object]:
            from mail_archive import MailArchiveError

            raise MailArchiveError("disk full")

    assert process_messages(
        [message],
        MessageLedger(tmp_path / "ledger.json", tmp_path / "inbox"),
        lambda candidate, _: notices.append(candidate),
        archive_store=BrokenArchive(),  # type: ignore[arg-type]
    ) == 1
    assert notices[0]["archive_error"] == "disk full"
    assert notices[0]["body"] == "这封信仍须完整呈现"
    assert not (tmp_path / "ledger.json").exists()


def test_notification_is_full_original_not_a_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        mail_hotline.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    mail_hotline.notify_companion(
        {**sample_message(), "body": "完整原信本体在首次呈现。"},
        "rfc822:<letter-44@example.test>",
    )
    command = calls[0][0][0]
    options = calls[0][1]
    assert "mail_hotline" in command
    assert "完整原信本体在首次呈现。" in options["input"]
    assert "summary" not in options["input"]


def test_repeat_poll_and_restart_write_one_marker_and_one_notice(tmp_path: Path) -> None:
    message = sample_message()
    notices: list[tuple[str, str]] = []

    def fake_relay(candidate: dict[str, str], event_id: str) -> None:
        notices.append((candidate["message_id"], event_id))

    first = MessageLedger(tmp_path / "ledger.json", tmp_path / "inbox")
    assert process_messages([message], first, fake_relay) == 1
    restarted = MessageLedger(tmp_path / "ledger.json", tmp_path / "inbox")
    assert process_messages([message], restarted, fake_relay) == 0

    markers = list((tmp_path / "inbox").glob("*.json"))
    assert len(markers) == 1
    assert json.loads(markers[0].read_text(encoding="utf-8"))["message_key"] == message_key(message)
    assert notices == [("<letter-44@example.test>", "rfc822:<letter-44@example.test>")]


def test_failed_relay_releases_claim_for_a_retry(tmp_path: Path) -> None:
    message = sample_message()
    ledger = MessageLedger(tmp_path / "ledger.json", tmp_path / "inbox")

    def failed_relay(_: dict[str, str], __: str) -> None:
        raise OSError("fixture relay is down")

    with pytest.raises(OSError, match="down"):
        process_messages([message], ledger, failed_relay)

    delivered: list[str] = []
    assert process_messages([message], ledger, lambda _, key: delivered.append(key)) == 1
    assert delivered == ["rfc822:<letter-44@example.test>"]


def test_stale_claim_recovers_after_process_crash_without_rewriting_marker(tmp_path: Path) -> None:
    message = sample_message()
    ledger = MessageLedger(
        tmp_path / "ledger.json", tmp_path / "inbox", notification_lease_seconds=10
    )
    token = ledger.claim_notification(message, now=100)
    assert token
    assert ledger.claim_notification(message, now=105) is None
    retry = ledger.claim_notification(message, now=111)
    assert retry and retry != token
    ledger.acknowledge_notification(message, retry, now=112)
    assert len(list((tmp_path / "inbox").glob("*.json"))) == 1


def test_cross_process_lock_allows_only_one_notification_claim(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    gate = context.Event()
    result = context.Queue()
    args = (str(tmp_path / "ledger.json"), str(tmp_path / "inbox"), gate, result)
    workers = [context.Process(target=_concurrent_claim, args=args) for _ in range(2)]
    for worker in workers:
        worker.start()
    gate.set()
    outcomes = [result.get(timeout=10) for _ in workers]
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0
    assert sorted(outcomes) == [False, True]


def test_existing_marker_must_prove_the_same_message_identity(tmp_path: Path) -> None:
    message = sample_message()
    ledger = MessageLedger(tmp_path / "ledger.json", tmp_path / "inbox")
    key = message_key(message)
    marker = tmp_path / "inbox" / f"{hashlib.sha256(key.encode()).hexdigest()}.json"
    marker.parent.mkdir()
    marker.write_text('{"message_key":"rfc822:<different@example.test>"}', encoding="utf-8")
    with pytest.raises(LedgerError, match="collision"):
        ledger.claim_notification(message)
