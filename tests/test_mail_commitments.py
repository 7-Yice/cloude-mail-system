from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

import pytest

from mail_archive import MailArchiveStore
from mail_commitments import (
    CommitmentError,
    EXTRACTION_PROMPT,
    active_summary,
    dream_card,
    extract_candidates,
    list_items,
    propose,
    read_item,
    review,
    close_item,
)


def test_extraction_prompt_includes_owned_unfinished_intentions() -> None:
    assert "我打算" in EXTRACTION_PROMPT
    assert "这个我记着" in EXTRACTION_PROMPT
    assert "还没办" in EXTRACTION_PROMPT
    assert "单纯愿望、可能性" in EXTRACTION_PROMPT


def archived_sent(tmp_path: Path) -> tuple[Path, str]:
    message = EmailMessage(policy=SMTP)
    message["From"] = "agent@example.test"
    message["To"] = "friend@example.test"
    message["Subject"] = "Plans"
    message["Message-ID"] = "<plans@example.test>"
    message.set_content("谢谢你的信。我答应周五把修改稿发给你。天气好的话也许去散步。", charset="utf-8")
    root = tmp_path / "archive"
    manifest = MailArchiveStore(root).archive_sent(
        {
            "message_id": str(message["Message-ID"]),
            "from": str(message["From"]),
            "to": str(message["To"]),
            "subject": str(message["Subject"]),
            "constellation_id": "cst_friend",
            "raw_message": message.as_bytes(policy=SMTP),
        }
    )
    return root, str(manifest["archive_id"])


def test_proposal_requires_exact_quote_and_is_idempotent(tmp_path: Path) -> None:
    archive, archive_id = archived_sent(tmp_path)
    ledger = tmp_path / "commitments.json"
    first = propose(
        ledger,
        archive,
        constellation_id="cst_friend",
        archive_id=archive_id,
        quote="我答应周五把修改稿发给你。",
        commitment="周五发送修改稿",
    )
    second = propose(
        ledger,
        archive,
        constellation_id="cst_friend",
        archive_id=archive_id,
        quote="我答应周五把修改稿发给你。",
        commitment="换一种模型措辞也不能重复建账",
    )
    assert first["created"] is True
    assert second["created"] is False
    with pytest.raises(CommitmentError, match="exact substring"):
        propose(
            ledger,
            archive,
            constellation_id="cst_friend",
            archive_id=archive_id,
            quote="我答应下周发给你。",
            commitment="模型猜出来的承诺",
        )


def test_review_is_one_item_at_a_time_and_keeps_original_visible(tmp_path: Path) -> None:
    archive, archive_id = archived_sent(tmp_path)
    ledger = tmp_path / "commitments.json"
    candidate = propose(
        ledger,
        archive,
        constellation_id="cst_friend",
        archive_id=archive_id,
        quote="我答应周五把修改稿发给你。",
        commitment="周五发送修改稿",
    )
    card = dream_card(ledger, archive)
    assert card["bulk_review_available"] is False
    assert card["item"]["quote"] == "我答应周五把修改稿发给你。"
    assert "source_content" not in card["item"]
    assert card["open_original"].startswith("read --id cmt_")
    assert card["item"]["source_content_stale"] is False

    confirmed = review(ledger, archive, candidate["commitment_id"], "confirm")
    assert confirmed["status"] == "active"
    assert dream_card(ledger, archive)["status"] == "empty"
    assert active_summary(ledger, "cst_friend") == {
        "active": ["周五发送修改稿"],
        "pending_review_count": 0,
    }


def test_reject_and_defer_do_not_create_active_debt(tmp_path: Path) -> None:
    archive, archive_id = archived_sent(tmp_path)
    ledger = tmp_path / "commitments.json"
    candidate = propose(
        ledger,
        archive,
        constellation_id="cst_friend",
        archive_id=archive_id,
        quote="我答应周五把修改稿发给你。",
        commitment="周五发送修改稿",
    )
    deferred = review(ledger, archive, candidate["commitment_id"], "defer")
    assert deferred["status"] == "pending_review"
    rejected = review(ledger, archive, candidate["commitment_id"], "reject")
    assert rejected["status"] == "rejected"
    assert list_items(ledger, status="active") == []


def test_active_commitment_needs_explicit_closure_evidence(tmp_path: Path) -> None:
    archive, archive_id = archived_sent(tmp_path)
    ledger = tmp_path / "commitments.json"
    candidate = propose(
        ledger,
        archive,
        constellation_id="cst_friend",
        archive_id=archive_id,
        quote="我答应周五把修改稿发给你。",
        commitment="周五发送修改稿",
    )
    review(ledger, archive, candidate["commitment_id"], "confirm")
    with pytest.raises(CommitmentError, match="evidence"):
        close_item(ledger, candidate["commitment_id"], "complete", "")
    closed = close_item(
        ledger,
        candidate["commitment_id"],
        "complete",
        "已发送修改稿，见对应发件归档",
    )
    assert closed["status"] == "completed"


def test_model_can_only_propose_quotes_present_in_original(tmp_path: Path) -> None:
    archive, archive_id = archived_sent(tmp_path)
    ledger = tmp_path / "commitments.json"
    created = extract_candidates(
        ledger,
        archive,
        constellation_id="cst_friend",
        archive_id=archive_id,
        extractor=lambda _: [
            {"quote": "我答应周五把修改稿发给你。", "commitment": "周五发送修改稿"},
            {"quote": "我保证明天完成。", "commitment": "幻觉候选"},
        ],
    )
    assert [item["commitment"] for item in created] == ["周五发送修改稿"]
