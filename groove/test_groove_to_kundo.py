import gzip
import io
import json
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import groove_to_kundo
from groove_to_kundo import (
    IMPORT_LIMITS,
    TRUNCATION_NOTICE,
    Limits,
    Summary,
    _is_line_delimited,
    _iter_raw_tickets,
    convert_export,
    format_summary,
    main,
)

CUSTOMER = {
    "id": "5001",
    "type": "Customer",
    "email": "joe@example.com",
    "name": "Joe",
}
AGENT = {
    "id": "5002",
    "type": "Agent",
    "email": "alice@example.com",
    "name": "Alice",
}
FILENAME = "support-1001.ndjson"


def _message_action(
    *,
    id="9001",
    created_at="2024-01-15T10:00:00Z",
    actor=None,
    body="<p>hej</p>",
    note=False,
    conversation_type="enduser",
    message_type="email",
    attachments=None,
    deleted_at=None,
):
    actor = actor or CUSTOMER
    return {
        "id": f"action-{id}",
        "actor": actor,
        "created_at": created_at,
        "change": {
            "id": id,
            "note": note,
            "agent_response": actor.get("type") == "Agent",
            "message_type": message_type,
            "conversation_type": conversation_type,
            "body": body,
            "attachments": attachments or [],
            "deleted_at": deleted_at,
            "type": "Message",
        },
    }


def _state_action(state="closed"):
    return {
        "id": "state-1",
        "actor": {"id": "r1", "type": "Rule", "name": "#Close directly"},
        "created_at": "2024-01-15T10:05:00Z",
        "change": {"state": state, "state_int": 5000, "type": "State"},
    }


def _ticket(
    *,
    id=750102,
    title="Help with login",
    created_at="2024-01-15T09:00:00Z",
    state="closed",
    mailbox="Support",
    mailbox_id="1001",
    customer=None,
    tags=None,
    actions=None,
):
    return {
        "id": id,
        "title": title,
        "created_at": created_at,
        "state": state,
        "mailbox": mailbox,
        "mailbox_id": mailbox_id,
        "tags": tags or [],
        "customer": {"customer": customer or CUSTOMER},
        "actions": actions
        if actions is not None
        else [_message_action(id=f"message-{id}"), _state_action()],
    }


def _attachment(**overrides):
    defaults = dict(
        id="att1",
        url="https://files.example.com/f/attachment/short-lived",
        download_url="https://files.example.com/f/download/stable",
        file_name="report.pdf",
        file_type="application/pdf",
        content_id=None,
    )
    return {**defaults, **overrides}


def _write_pretty_printed(path, tickets):
    """Mirror Groove's real export shape: `[`, then one ticket per line with
    the comma leading (not trailing), then `]`."""
    lines = ["["]
    for i, ticket in enumerate(tickets):
        prefix = "" if i == 0 else ","
        lines.append(prefix + json.dumps(ticket))
    lines.append("]")
    path.write_text("\n".join(lines), encoding="utf-8")


def _lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class ConverterTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.export = self.tmp_path / "export.json"
        self.output_dir = self.tmp_path / "out"

    def convert(self, tickets, **kwargs):
        _write_pretty_printed(self.export, tickets)
        return convert_export(str(self.export), self.output_dir, **kwargs)

    def conversations(self, filename=FILENAME):
        return _lines(self.output_dir / filename)

    def manifest(self):
        return json.loads(
            (self.output_dir / "manifest.json").read_text(encoding="utf-8")
        )


class ReadingTheExportTest(ConverterTestCase):
    def test_reads_pretty_printed_leading_comma_format(self):
        self.convert([_ticket(id=1), _ticket(id=2)])

        self.assertEqual([c["external_id"] for c in self.conversations()], ["1", "2"])

    def test_falls_back_to_whole_file_json_load(self):
        self.export.write_text(
            json.dumps([_ticket(id=1), _ticket(id=2)]), encoding="utf-8"
        )

        convert_export(str(self.export), self.output_dir)

        self.assertEqual([c["external_id"] for c in self.conversations()], ["1", "2"])

    def test_reads_gzipped_export(self):
        _write_pretty_printed(self.export, [_ticket(id=1), _ticket(id=2)])
        gzipped = self.tmp_path / "export.json.gz"
        gzipped.write_bytes(gzip.compress(self.export.read_bytes()))

        convert_export(str(gzipped), self.output_dir)

        self.assertEqual([c["external_id"] for c in self.conversations()], ["1", "2"])

    def test_is_line_delimited_false_for_malformed_first_line(self):
        self.export.write_text("[\nnot json\n]", encoding="utf-8")

        self.assertIs(_is_line_delimited(str(self.export)), False)

    def test_empty_array_produces_no_tickets(self):
        self.export.write_text("[\n]", encoding="utf-8")

        convert_export(str(self.export), self.output_dir)

        self.assertEqual(list(_iter_raw_tickets(str(self.export))), [])
        self.assertEqual(self.manifest(), [])


class MessagesAndNotesTest(ConverterTestCase):
    def test_non_message_actions_are_ignored(self):
        self.convert(
            [
                _ticket(
                    actions=[
                        _message_action(),
                        _state_action(),
                        _state_action("opened"),
                    ]
                )
            ]
        )

        (conversation,) = self.conversations()
        self.assertEqual(len(conversation["messages"]), 1)

    def test_deleted_messages_are_skipped(self):
        self.convert(
            [
                _ticket(
                    actions=[
                        _message_action(id="1", created_at="2024-01-15T10:00:00Z"),
                        _message_action(
                            id="2",
                            created_at="2024-01-15T10:05:00Z",
                            deleted_at="2024-01-15T10:06:00Z",
                        ),
                    ]
                )
            ]
        )

        (conversation,) = self.conversations()
        self.assertEqual([m["id"] for m in conversation["messages"]], ["1"])

    def test_note_true_becomes_a_note_not_a_message(self):
        self.convert(
            [
                _ticket(
                    actions=[
                        _message_action(id="1", created_at="2024-01-15T10:00:00Z"),
                        _message_action(
                            id="2",
                            created_at="2024-01-15T10:05:00Z",
                            actor=AGENT,
                            note=True,
                            conversation_type="internal",
                            body="internal note",
                        ),
                    ]
                )
            ]
        )

        (conversation,) = self.conversations()
        self.assertEqual([m["id"] for m in conversation["messages"]], ["1"])
        self.assertEqual(
            [n["html_body"] for n in conversation["notes"]], ["internal note"]
        )

    def test_third_party_and_bounce_become_messages(self):
        self.convert(
            [
                _ticket(
                    actions=[
                        _message_action(id="1", created_at="2024-01-15T10:00:00Z"),
                        _message_action(
                            id="2",
                            created_at="2024-01-15T10:05:00Z",
                            actor=AGENT,
                            conversation_type="third_party",
                            body="asking the supplier",
                        ),
                        _message_action(
                            id="3",
                            created_at="2024-01-15T10:10:00Z",
                            message_type="BounceComment",
                            body="mail delivery failed",
                        ),
                    ]
                )
            ]
        )

        (conversation,) = self.conversations()
        self.assertEqual(
            [m["html_body"] for m in conversation["messages"][1:]],
            ["asking the supplier", "mail delivery failed"],
        )
        self.assertEqual(conversation["notes"], [])

    def test_messages_are_sorted_chronologically(self):
        self.convert(
            [
                _ticket(
                    actions=[
                        _message_action(
                            id="2", created_at="2024-01-15T12:00:00Z", body="second"
                        ),
                        _message_action(
                            id="1", created_at="2024-01-15T10:00:00Z", body="first"
                        ),
                        _message_action(
                            id="3", created_at="2024-01-15T14:00:00Z", body="third"
                        ),
                    ]
                )
            ]
        )

        (conversation,) = self.conversations()
        self.assertEqual(
            [m["html_body"] for m in conversation["messages"]],
            ["first", "second", "third"],
        )

    def test_ticket_with_only_a_note_is_skipped(self):
        summary = self.convert(
            [
                _ticket(
                    actions=[
                        _message_action(
                            actor=AGENT, note=True, conversation_type="internal"
                        ),
                    ]
                )
            ]
        )

        self.assertEqual(self.conversations(), [])
        self.assertEqual(summary.skipped_without_public_message, 1)

    def test_message_with_an_id_seen_before_is_dropped(self):
        summary = self.convert(
            [
                _ticket(
                    id=1,
                    actions=[
                        _message_action(id="1", created_at="2024-01-15T10:00:00Z"),
                        _message_action(id="1", created_at="2024-01-15T11:00:00Z"),
                    ],
                ),
                _ticket(
                    id=2,
                    actions=[
                        _message_action(id="1", created_at="2024-01-15T10:00:00Z"),
                        _message_action(id="2", created_at="2024-01-15T11:00:00Z"),
                    ],
                ),
                _ticket(id=3, actions=[_message_action(id="2")]),
            ]
        )

        self.assertEqual(
            {
                c["external_id"]: [m["id"] for m in c["messages"]]
                for c in self.conversations()
            },
            {"1": ["1"], "2": ["2"]},
        )
        self.assertEqual(summary.dropped_duplicate_messages, 3)
        self.assertEqual(summary.skipped_without_public_message, 1)

    def test_empty_title_uses_placeholder(self):
        self.convert([_ticket(title="")])

        (conversation,) = self.conversations()
        self.assertEqual(conversation["title"], "(No subject)")


class AttachmentsTest(ConverterTestCase):
    def test_uses_download_url_not_url(self):
        self.convert([_ticket(actions=[_message_action(attachments=[_attachment()])])])

        (conversation,) = self.conversations()
        (attachment,) = conversation["messages"][0]["attachments"]
        self.assertEqual(
            attachment["url"], "https://files.example.com/f/download/stable"
        )

    def test_inline_ref_set_only_when_cid_referenced_in_body(self):
        self.convert(
            [
                _ticket(
                    id=1,
                    actions=[
                        _message_action(
                            id="1",
                            body='<img src="cid:abc@x">',
                            attachments=[_attachment(id="inline", content_id="abc@x")],
                        )
                    ],
                ),
                _ticket(
                    id=2,
                    actions=[
                        _message_action(
                            id="2",
                            body="no image reference here",
                            attachments=[
                                _attachment(id="not-inline", content_id="xyz@y")
                            ],
                        )
                    ],
                ),
            ]
        )

        inline_refs = {
            c["external_id"]: c["messages"][0]["attachments"][0]["inline_ref"]
            for c in self.conversations()
        }
        self.assertEqual(inline_refs, {"1": "cid:abc@x", "2": None})

    def test_attachment_without_an_https_download_url_is_dropped(self):
        summary = self.convert(
            [
                _ticket(
                    actions=[
                        _message_action(
                            attachments=[
                                _attachment(download_url="http://files.example.com/a"),
                                _attachment(download_url="https://"),
                                _attachment(download_url=None),
                                _attachment(file_name="kept.pdf"),
                            ]
                        )
                    ]
                )
            ]
        )

        (conversation,) = self.conversations()
        self.assertEqual(
            [a["filename"] for a in conversation["messages"][0]["attachments"]],
            ["kept.pdf"],
        )
        self.assertEqual(summary.dropped_attachments_without_https, 3)

    def test_attachments_on_notes_are_dropped_and_counted(self):
        summary = self.convert(
            [
                _ticket(
                    actions=[
                        _message_action(id="1"),
                        _message_action(
                            id="2",
                            actor=AGENT,
                            note=True,
                            conversation_type="internal",
                            attachments=[_attachment()],
                        ),
                    ]
                )
            ]
        )

        (conversation,) = self.conversations()
        self.assertNotIn("attachments", conversation["notes"][0])
        self.assertEqual(summary.dropped_note_attachments, 1)


class MailboxesTest(ConverterTestCase):
    def test_splits_into_one_file_per_mailbox(self):
        self.convert(
            [
                _ticket(id=1, mailbox="Support", mailbox_id="1001"),
                _ticket(id=2, mailbox="Sales", mailbox_id="1002"),
            ]
        )

        self.assertEqual(
            {row["mailbox"]: row["filename"] for row in self.manifest()},
            {"Support": FILENAME, "Sales": "sales-1002.ndjson"},
        )
        self.assertEqual(len(self.conversations(FILENAME)), 1)
        self.assertEqual(len(self.conversations("sales-1002.ndjson")), 1)

    def test_same_mailbox_name_different_ids_do_not_collide(self):
        self.convert(
            [
                _ticket(id=1, mailbox="Support", mailbox_id="1001"),
                _ticket(id=2, mailbox="Support", mailbox_id="1003"),
            ]
        )

        self.assertEqual(len(self.conversations(FILENAME)), 1)
        self.assertEqual(len(self.conversations("support-1003.ndjson")), 1)

    def test_non_ascii_mailbox_name_is_slugified(self):
        self.convert([_ticket(mailbox="Café Example.com", mailbox_id="1004")])

        self.assertEqual(self.manifest()[0]["filename"], "cafe-example-com-1004.ndjson")


class ConversationFieldsTest(ConverterTestCase):
    def test_customer_without_email_gets_synthetic_address(self):
        self.convert([_ticket(customer={"id": "c404", "email": None, "name": "N"})])

        (conversation,) = self.conversations()
        self.assertEqual(
            conversation["requester"]["email"], "groove-user-c404@groove.invalid"
        )

    def test_tags_with_a_comma_are_split_and_long_tags_are_cut(self):
        summary = self.convert([_ticket(tags=["vip", "a, b", "x" * 60, 5, "b"])])

        (conversation,) = self.conversations()
        self.assertEqual(conversation["tags"], ["vip", "a", "b", "x" * 50])
        self.assertEqual(summary.changed_tags, 2)

    def test_open_state_is_not_closed(self):
        self.convert([_ticket(state="unread")])

        (conversation,) = self.conversations()
        self.assertIs(conversation["is_closed"], False)

    def test_force_closed_overrides_open_state(self):
        self.convert([_ticket(state="unread")], force_closed=True)

        (conversation,) = self.conversations()
        self.assertIs(conversation["is_closed"], True)


class LimitsTest(ConverterTestCase):
    def test_output_stays_within_the_import_limits_by_default(self):
        self.convert(
            [
                _ticket(
                    actions=[
                        _message_action(
                            body="<p>" + "x" * 150_000 + "</p>",
                            attachments=[_attachment(id=str(i)) for i in range(25)],
                        )
                    ]
                )
            ]
        )

        (conversation,) = self.conversations()
        (message,) = conversation["messages"]
        self.assertLessEqual(
            len(message["html_body"]), IMPORT_LIMITS.max_html_body_length
        )
        self.assertEqual(
            len(message["attachments"]), IMPORT_LIMITS.max_attachments_per_message
        )

    def test_mailbox_over_the_file_limit_continues_in_part_files(self):
        summary = self.convert(
            [_ticket(id=i) for i in range(1, 6)],
            limits=Limits(max_conversations_per_file=2),
        )

        self.assertEqual(
            [(row["filename"], row["conversations"]) for row in summary.files],
            [
                (FILENAME, 2),
                ("support-1001-part2.ndjson", 2),
                ("support-1001-part3.ndjson", 1),
            ],
        )
        self.assertEqual(self.manifest(), summary.files)
        self.assertEqual(
            [
                c["external_id"]
                for row in summary.files
                for c in self.conversations(row["filename"])
            ],
            ["1", "2", "3", "4", "5"],
        )

    def test_too_long_message_is_cut_at_a_tag_boundary(self):
        body = "<p>" + "a" * 40 + '</p><img src="cid:late@x">' + "b" * 100
        max_length = 50 + len(TRUNCATION_NOTICE)

        summary = self.convert(
            [
                _ticket(
                    actions=[
                        _message_action(
                            body=body, attachments=[_attachment(content_id="late@x")]
                        )
                    ]
                )
            ],
            limits=Limits(max_html_body_length=max_length),
        )

        (conversation,) = self.conversations()
        (message,) = conversation["messages"]
        self.assertEqual(
            message["html_body"], "<p>" + "a" * 40 + "</p>" + TRUNCATION_NOTICE
        )
        self.assertIsNone(message["attachments"][0]["inline_ref"])
        self.assertEqual(summary.truncated_messages, 1)

    def test_message_within_the_length_limit_is_kept_as_it_is(self):
        summary = self.convert(
            [_ticket(actions=[_message_action(body="<p>ok</p>")])],
            limits=Limits(max_html_body_length=100),
        )

        (conversation,) = self.conversations()
        self.assertEqual(conversation["messages"][0]["html_body"], "<p>ok</p>")
        self.assertEqual(summary.truncated_messages, 0)

    def test_too_many_attachments_keeps_inline_attachments_first(self):
        attachments = [
            _attachment(id=f"file-{i}", file_name=f"file-{i}.pdf") for i in range(3)
        ]
        attachments.append(
            _attachment(id="image", file_name="image.png", content_id="img@x")
        )

        summary = self.convert(
            [
                _ticket(
                    actions=[
                        _message_action(
                            body='<img src="cid:img@x">', attachments=attachments
                        )
                    ]
                )
            ],
            limits=Limits(max_attachments_per_message=2),
        )

        (conversation,) = self.conversations()
        self.assertEqual(
            [a["filename"] for a in conversation["messages"][0]["attachments"]],
            ["file-0.pdf", "image.png"],
        )
        self.assertEqual(summary.dropped_attachments, 2)


class SummaryTest(unittest.TestCase):
    def test_reports_what_was_changed_or_dropped(self):
        text = format_summary(
            Summary(
                truncated_messages=3,
                dropped_attachments=4,
                dropped_attachments_without_https=5,
                dropped_duplicate_messages=6,
                changed_tags=7,
            ),
            Path("out"),
        )

        self.assertIn("Shortened 3 message(s)", text)
        self.assertIn("Dropped 4 attachment(s) from messages", text)
        self.assertIn("Dropped 5 attachment(s) without an HTTPS", text)
        self.assertIn("Dropped 6 message(s) with the same id", text)
        self.assertIn("Changed 7 tag(s)", text)

    def test_leaves_out_the_lines_for_what_did_not_happen(self):
        text = format_summary(Summary(), Path("out"))

        self.assertEqual(len(text.splitlines()), 3)


class CommandLineTest(ConverterTestCase):
    def test_main_converts_and_prints_the_summary(self):
        _write_pretty_printed(self.export, [_ticket(state="unread")])
        out = io.StringIO()

        with redirect_stdout(out):
            exit_code = main(
                [
                    "--file",
                    str(self.export),
                    "--output-dir",
                    str(self.output_dir),
                    "--force-closed",
                ]
            )

        (conversation,) = self.conversations()
        self.assertEqual(exit_code, 0)
        self.assertIs(conversation["is_closed"], True)
        self.assertEqual(conversation["account"], "default")
        self.assertIn("Wrote 1 file(s)", out.getvalue())

    def test_main_rejects_a_missing_file(self):
        err = io.StringIO()

        with redirect_stderr(err), self.assertRaises(SystemExit) as exit_info:
            main(
                [
                    "--file",
                    str(self.tmp_path / "nope.json"),
                    "--output-dir",
                    str(self.output_dir),
                ]
            )

        self.assertEqual(exit_info.exception.code, 2)
        self.assertIn("no such file", err.getvalue())

    def test_main_rejects_an_account_that_the_import_refuses(self):
        _write_pretty_printed(self.export, [_ticket()])
        err = io.StringIO()

        with redirect_stderr(err), self.assertRaises(SystemExit) as exit_info:
            main(
                [
                    "--file",
                    str(self.export),
                    "--output-dir",
                    str(self.output_dir),
                    "--account",
                    "my account",
                ]
            )

        self.assertEqual(exit_info.exception.code, 2)
        self.assertIn("--account", err.getvalue())
        self.assertFalse(self.output_dir.exists())

    def test_runs_as_a_script(self):
        _write_pretty_printed(self.export, [_ticket()])
        argv = [
            "groove_to_kundo.py",
            "--file",
            str(self.export),
            "--output-dir",
            str(self.output_dir),
        ]

        with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_info:
                runpy.run_path(groove_to_kundo.__file__, run_name="__main__")

        self.assertEqual(exit_info.exception.code, 0)
        self.assertTrue((self.output_dir / FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
