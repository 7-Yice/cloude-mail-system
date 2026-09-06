from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


TOOLS = Path(__file__).parents[1] / "src"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import send_mail  # noqa: E402
from correspondence_identity import mask_address  # noqa: E402
from mail_sent_reconcile import reconcile_receipt  # noqa: E402
from mail_archive import MailArchiveError, MailArchiveStore  # noqa: E402
from mail_identities import IdentityError, ProtectedIdentityStore  # noqa: E402


def _person(_: str) -> dict[str, object]:
    return {"business": {"kind": "person", "name": "Pen Pal"}}


def _identity_store(tmp_path: Path) -> ProtectedIdentityStore:
    store = ProtectedIdentityStore(
        tmp_path / "private" / "identities.json", constellation_lookup=_person
    )
    store.register(
        "cst_friend", "friend@example.test", actor="agent", reason="真实来信",
        source_kind="received_mail_header", source_id="arc_fixture",
        source_sha256="a" * 64,
    )
    return store


class FakeSMTP:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.recipients: list[str] = []
        self.raw = b""

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def login(self, account: str, password: str) -> None:
        assert account == "cloud@example.test"
        assert password == "fixture-secret"

    def sendmail(self, sender: str, recipients: list[str], raw: bytes) -> dict:
        if self.fail:
            raise OSError("fake smtp is offline")
        self.recipients = recipients
        self.raw = raw
        return {}


def test_identity_registry_requires_person_and_prevents_cross_person_reuse(tmp_path: Path) -> None:
    kinds = {"cst_friend": "person", "cst_topic": "topic", "cst_other": "person"}
    store = ProtectedIdentityStore(
        tmp_path / "identities.json",
        constellation_lookup=lambda value: {"business": {"kind": kinds[value]}},
    )
    first = store.register(
        "cst_friend", "Friend@example.TEST", actor="agent", reason="真实来信",
        source_kind="received_mail_header", source_id="arc_fixture",
        source_sha256="a" * 64,
    )
    assert first["address"] == "Friend@example.test"
    with pytest.raises(IdentityError, match="person"):
        store.register("cst_topic", "topic@example.test", actor="agent", reason="no", source_kind="received_mail_header", source_id="arc_topic", source_sha256="b" * 64)
    with pytest.raises(IdentityError, match="another constellation"):
        store.register("cst_other", "Friend@example.test", actor="agent", reason="no", source_kind="verified_user_message", source_id="message_id:7", source_sha256="c" * 64)
    with pytest.raises(IdentityError, match="constellation id"):
        store.register("../../escape", "x@example.test", actor="agent", reason="no", source_kind="verified_user_message", source_id="message_id:8", source_sha256="d" * 64)

    with pytest.raises(IdentityError, match="authoritative source"):
        store.register("cst_friend", "other@example.test", actor="agent", reason="no", source_kind="manual", source_id="free", source_sha256="e" * 64)


def test_send_uses_registered_recipient_and_archives_exact_sent_original(tmp_path: Path) -> None:
    smtp = FakeSMTP()
    result = send_mail.send_and_archive(
        account="cloud@example.test",
        password="fixture-secret",
        constellation_id="cst_friend",
        subject="一封测试信",
        body="正文里写着 attacker@example.test，也不能改变真正收件人。",
        identity_store=_identity_store(tmp_path),
        archive_store=MailArchiveStore(tmp_path / "archive"),
        reconcile_root=tmp_path / "reconcile",
        smtp_factory=lambda: smtp,
    )

    assert result["status"] == "sent_and_archived"
    assert smtp.recipients == ["friend@example.test"]
    assert b"attacker@example.test" in smtp.raw
    manifests = list((tmp_path / "archive" / "sent").glob("*.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["direction"] == "sent"
    assert (manifests[0].parent / manifest["original_file"]).read_bytes() == smtp.raw
    ledger = json.loads((tmp_path / "archive" / "message-ledger.json").read_text("utf-8"))
    assert next(iter(ledger["messages"].values()))["direction"] == "sent"


def test_smtp_failure_is_not_archived(tmp_path: Path) -> None:
    result = send_mail.send_and_archive(
        account="cloud@example.test",
        password="fixture-secret",
        constellation_id="cst_friend",
        subject="不会发出的信",
        body="正文",
        identity_store=_identity_store(tmp_path),
        archive_store=MailArchiveStore(tmp_path / "archive"),
        reconcile_root=tmp_path / "reconcile",
        smtp_factory=lambda: FakeSMTP(fail=True),
    )
    assert result["status"] == "not_sent"
    assert not (tmp_path / "archive" / "sent").exists()


def test_archive_failure_reports_sent_archive_pending_without_inviting_retry(tmp_path: Path) -> None:
    class BrokenArchive:
        def archive_sent(self, _: dict[str, object]) -> dict[str, object]:
            raise MailArchiveError("fixture disk full")

    result = send_mail.send_and_archive(
        account="cloud@example.test",
        password="fixture-secret",
        constellation_id="cst_friend",
        subject="已经发出但待补账",
        body="正文",
        identity_store=_identity_store(tmp_path),
        archive_store=BrokenArchive(),  # type: ignore[arg-type]
        reconcile_root=tmp_path / "reconcile",
        smtp_factory=FakeSMTP,
    )
    assert result["status"] == "sent_archive_pending"
    assert result["must_not_retry_send"] is True
    receipt = Path(result["reconcile_receipt"])
    assert receipt.exists()
    assert json.loads(receipt.read_text("utf-8"))["status"] == "sent_archive_pending"

    repaired = reconcile_receipt(
        receipt, MailArchiveStore(tmp_path / "repaired-archive")
    )
    assert repaired["status"] == "archived_after_reconcile"
    assert len(list((tmp_path / "repaired-archive" / "sent").glob("*.eml"))) == 1
    # Reconciliation is idempotent and never invokes SMTP.
    assert reconcile_receipt(
        receipt, MailArchiveStore(tmp_path / "repaired-archive")
    ) == repaired


def test_cli_has_no_free_recipient_argument() -> None:
    with pytest.raises(SystemExit):
        send_mail.parse_args(
            ["--to", "attacker@example.test", "--subject", "x", "--body", "y"]
        )


def test_constellation_validation_uses_loopback_mcp_not_projection_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        send_mail,
        "_mcp_tool_json",
        lambda **kwargs: (
            calls.append(kwargs)
            or {"constellation_id": "cst_friend", "business": {"kind": "person", "status": "active", "statements": ["private"]}}
        ),
    )
    env = tmp_path / "ombre.env"
    env.write_text("OMBRE_MCP_TOKEN=fixture-token\n", encoding="utf-8")
    lookup = send_mail._projection_lookup(
        tmp_path / "root-owned-vault",
        mcp_env=env,
        mcp_url="http://127.0.0.1:18001/mcp",
    )
    assert lookup("cst_friend") == {
        "constellation_id": "cst_friend",
        "business": {"kind": "person", "status": "active"},
    }
    assert calls == [{
        "url": "http://127.0.0.1:18001/mcp",
        "token": "fixture-token",
        "name": "constellation_read",
        "arguments": {
            "action": "inspect",
            "constellation_id": "cst_friend",
            "max_tokens": 600,
        },
    }]


def test_constellation_validation_rejects_non_loopback_endpoint() -> None:
    with pytest.raises(IdentityError, match="loopback"):
        send_mail._mcp_tool_json(
            url="https://example.test/mcp",
            token="must-not-leave-host",
            name="constellation_read",
            arguments={"action": "inspect", "constellation_id": "cst_friend"},
        )


def test_constellation_validation_rejects_identity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        send_mail,
        "_mcp_tool_json",
        lambda **_: {"constellation_id": "cst_other", "business": {"kind": "person"}},
    )
    env = tmp_path / "ombre.env"
    env.write_text("OMBRE_MCP_TOKEN=fixture-token\n", encoding="utf-8")
    lookup = send_mail._projection_lookup(tmp_path / "unused", mcp_env=env)
    with pytest.raises(IdentityError, match="identity mismatch"):
        lookup("cst_friend")


def test_mail_send_refuses_closed_constellation() -> None:
    with pytest.raises(IdentityError, match="closed constellation"):
        send_mail._require_active_constellation({
            "business": {"kind": "person", "status": "closed"}
        })


def test_audit_output_masks_private_address() -> None:
    assert mask_address("friend@example.test") == "f***@example.test"


def test_smtp_factory_uses_only_explicit_ca_file(monkeypatch: pytest.MonkeyPatch) -> None:
    contexts: list[str | None] = []
    calls: list[tuple[str, int, object]] = []
    sentinel = object()
    monkeypatch.setattr(
        send_mail.ssl,
        "create_default_context",
        lambda *, cafile=None: (contexts.append(cafile) or sentinel),
    )
    monkeypatch.setattr(
        send_mail.smtplib,
        "SMTP_SSL",
        lambda host, port, *, context: (calls.append((host, port, context)) or object()),
    )

    send_mail.smtp_factory_from_env(
        {
            "SMTP_HOST": "localhost",
            "SMTP_PORT": "2465",
            "SMTP_CA_FILE": "/tmp/fake-ca.pem",
        }
    )()
    assert contexts == ["/tmp/fake-ca.pem"]
    assert calls == [("localhost", 2465, sentinel)]
