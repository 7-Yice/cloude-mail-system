import datetime as dt
from email.message import EmailMessage
import json
from pathlib import Path
import unittest

from mail_owe_list import _sent_excerpt_by_message_id, build_owe_rows, render_text


UTC = dt.timezone.utc


def record(direction, address, when, subject, message_id, name=""):
    return {
        "direction": direction,
        "address": address,
        "timestamp": when,
        "subject": subject,
        "message_id": message_id,
        "display_name": name,
    }


class OweListTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 9, 4, 12, tzinfo=UTC)
        self.identities = {
            "friend@example.com": {"constellation_id": "cst_friend"},
            "other@example.com": {"constellation_id": "cst_other"},
        }

    def test_sent_after_incoming_means_waiting_even_with_another_subject(self):
        rows = build_owe_rows(
            [
                record(
                    "incoming",
                    "friend@example.com",
                    dt.datetime(2026, 9, 3, 10, tzinfo=UTC),
                    "original thread",
                    "<in@example>",
                    "Friend",
                ),
                record(
                    "sent",
                    "friend@example.com",
                    dt.datetime(2026, 9, 3, 11, tzinfo=UTC),
                    "a brand new subject",
                    "<out@example>",
                ),
            ],
            self.identities,
            {"<in@example>": "one-line summary"},
            now=self.now,
        )
        self.assertEqual(rows[0]["status"], "等他")
        self.assertEqual(rows[0]["my_last_subject"], "a brand new subject")

    def test_my_last_summary_is_rendered_next_to_my_last_subject(self):
        rows = build_owe_rows(
            [
                record("incoming", "friend@example.com", self.now - dt.timedelta(hours=2), "their subject", "<in@example>", "Friend"),
                record("sent", "friend@example.com", self.now - dt.timedelta(hours=1), "my subject", "<out@example>"),
            ],
            self.identities,
            {"<in@example>": "their summary", "<out@example>": "我答应考完一起研究。也问了他近况。"},
            now=self.now,
        )

        self.assertEqual(rows[0]["my_last_summary"], "我答应考完一起研究。也问了他近况。")
        self.assertIn("我最后：my subject｜正文摘要：我答应考完一起研究。也问了他近况。", render_text(rows))

    def test_name_prefers_constellation_then_mail_then_masked_address(self):
        identity = {"friend@example.com": {"constellation_id": "cst_person_internal"}}
        record_without_name = record(
            "incoming", "friend@example.com", self.now, "hello", "<in@example>"
        )
        named = build_owe_rows(
            [record_without_name], identity, {}, now=self.now,
            constellation_names={"cst_person_internal": "笔友甲"},
        )
        mail_fallback = build_owe_rows(
            [{**record_without_name, "display_name": "Shen Yuan"}],
            identity, {}, now=self.now,
        )
        address_fallback = build_owe_rows(
            [record_without_name], identity, {}, now=self.now,
        )

        self.assertEqual(named[0]["display_name"], "笔友甲")
        self.assertEqual(mail_fallback[0]["display_name"], "Shen Yuan")
        self.assertEqual(address_fallback[0]["display_name"], "f***@example.com")
        self.assertNotIn("cst_person_internal", json.dumps(named + mail_fallback + address_fallback))

    def test_sent_archive_yields_two_sentence_excerpt(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            message = EmailMessage()
            message["Message-ID"] = "<sent@example>"
            message.set_content("第一句是我答过的事。第二句是接下来要做的事！第三句不应出现。")
            original = root / "sent.eml"
            original.write_bytes(message.as_bytes())
            (root / "sent.json").write_text(
                json.dumps({
                    "message_id": "<sent@example>",
                    "original_file": original.name,
                }),
                encoding="utf-8",
            )

            excerpts = _sent_excerpt_by_message_id(root)

        self.assertEqual(excerpts["<sent@example>"], "第一句是我答过的事。 第二句是接下来要做的事！")

    def test_newer_incoming_is_owed_and_sorted_by_age(self):
        rows = build_owe_rows(
            [
                record(
                    "incoming",
                    "friend@example.com",
                    dt.datetime(2026, 8, 28, 5, tzinfo=UTC),
                    "older debt",
                    "<old@example>",
                    "Friend",
                ),
                record(
                    "incoming",
                    "other@example.com",
                    dt.datetime(2026, 9, 3, 5, tzinfo=UTC),
                    "newer debt",
                    "<new@example>",
                    "Other",
                ),
            ],
            self.identities,
            {},
            now=self.now,
        )
        self.assertEqual([row["their_last_subject"] for row in rows], ["older debt", "newer debt"])
        self.assertEqual(rows[0]["owed_days"], 7)

    def test_unregistered_automated_mail_is_not_a_correspondent(self):
        rows = build_owe_rows(
            [
                record(
                    "incoming",
                    "noreply@example.net",
                    dt.datetime(2026, 8, 20, tzinfo=UTC),
                    "welcome",
                    "<robot@example>",
                )
            ],
            self.identities,
            {},
            now=self.now,
        )
        self.assertEqual(rows, [])

    def test_reminder_becomes_due_immediately_after_three_days(self):
        rows = build_owe_rows(
            [
                record(
                    "incoming",
                    "friend@example.com",
                    self.now - dt.timedelta(days=3, seconds=1),
                    "three days ago",
                    "<three@example>",
                    "Friend",
                )
            ],
            self.identities,
            {},
            now=self.now,
        )
        self.assertEqual(rows[0]["owed_days"], 3)
        self.assertTrue(rows[0]["overdue_for_reminder"])


if __name__ == "__main__":
    unittest.main()
