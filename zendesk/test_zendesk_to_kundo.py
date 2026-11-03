import io
import json
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import zendesk_to_kundo
from zendesk_to_kundo import (
    IMPORT_LIMITS,
    TRUNCATION_NOTICE,
    ExportError,
    Limits,
    Summary,
    convert_export,
    format_summary,
    main,
)

REQUESTER = {
    "id": 201,
    "name": "Customer Joe",
    "email": "joe@example.com",
    "role": "end-user",
}
AGENT = {
    "id": 202,
    "name": "Agent Alice",
    "email": "alice@example.com",
    "role": "agent",
}
FILENAME = "zendesk-acme.ndjson"
TOKEN_URL = "https://acme.zendesk.com/attachments/token/xyz/?name=photo.png"
BRANDED_TOKEN_URL = "https://support.example.com/attachments/token/xyz/?name=photo.png"


def _comment(**overrides):
    defaults = dict(
        id=3001,
        author_id=REQUESTER["id"],
        html_body="<p>I can't log in.</p>",
        public=True,
        created_at="2024-01-15T10:00:00Z",
        attachments=[],
    )
    return {**defaults, **overrides}


def _ticket(**overrides):
    id = overrides.get("id", 1001)
    defaults = dict(
        id=id,
        subject="Help with login",
        created_at="2024-01-15T10:00:00Z",
        requester_id=REQUESTER["id"],
        requester=REQUESTER,
        comments=[_comment(id=id * 10)],
    )
    return {**defaults, **overrides}


class ConverterTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.export = self.tmp_path / "export.ndjson"
        self.output_dir = self.tmp_path / "out"

    def write_export(self, tickets):
        self.export.write_text(
            "\n".join(json.dumps(t) for t in tickets) + "\n", encoding="utf-8"
        )

    def convert(self, tickets, **kwargs):
        self.write_export(tickets)
        kwargs.setdefault("subdomain", "acme")
        return convert_export(str(self.export), self.output_dir, **kwargs)

    def conversations(self, filename=FILENAME):
        path = self.output_dir / filename
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def conversation(self, ticket, **kwargs):
        self.convert([ticket], **kwargs)
        (conversation,) = self.conversations()
        return conversation

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(
                    ["--file", str(self.export), "--output-dir", str(self.output_dir)]
                    + list(args)
                )
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()


class MessagesAndNotesTest(ConverterTestCase):
    def test_first_public_comment_opens_the_conversation(self):
        conversation = self.conversation(
            _ticket(
                comments=[
                    _comment(id=3001, created_at="2024-01-15T10:00:00Z"),
                    _comment(
                        id=3002,
                        created_at="2024-01-15T11:00:00Z",
                        author_id=AGENT["id"],
                    ),
                ]
            )
        )

        self.assertEqual([m["id"] for m in conversation["messages"]], ["3001", "3002"])

    def test_leading_private_comment_does_not_open_the_conversation(self):
        conversation = self.conversation(
            _ticket(
                comments=[
                    _comment(
                        id=2999,
                        created_at="2024-01-15T09:00:00Z",
                        author_id=AGENT["id"],
                        public=False,
                    ),
                    _comment(id=3001, created_at="2024-01-15T10:00:00Z"),
                ]
            )
        )

        self.assertEqual([m["id"] for m in conversation["messages"]], ["3001"])
        self.assertEqual([n["id"] for n in conversation["notes"]], ["2999"])

    def test_private_comment_becomes_a_note(self):
        conversation = self.conversation(
            _ticket(
                comments=[
                    _comment(id=3001),
                    _comment(
                        id=3003,
                        created_at="2024-01-15T13:00:00Z",
                        author_id=AGENT["id"],
                        public=False,
                        html_body="<p>Escalate to tier 2.</p>",
                    ),
                ]
            )
        )

        self.assertEqual([m["id"] for m in conversation["messages"]], ["3001"])
        self.assertEqual(
            [n["html_body"] for n in conversation["notes"]],
            ["<p>Escalate to tier 2.</p>"],
        )
        self.assertNotIn("attachments", conversation["notes"][0])

    def test_ticket_with_only_private_comments_is_skipped(self):
        summary = self.convert([_ticket(comments=[_comment(public=False)])])

        self.assertEqual(self.conversations(), [])
        self.assertEqual(summary.skipped_without_public_comment, 1)

    def test_ticket_with_no_comments_is_skipped(self):
        summary = self.convert([_ticket(comments=[])])

        self.assertEqual(self.conversations(), [])
        self.assertEqual(summary.skipped_without_comments, 1)

    def test_comments_are_sorted_chronologically(self):
        conversation = self.conversation(
            _ticket(
                comments=[
                    _comment(id=3002, created_at="2024-01-15T11:00:00Z"),
                    _comment(id=3001, created_at="2024-01-15T10:00:00+02:00"),
                    _comment(id=3000, created_at="2024-01-15T09:00:00Z"),
                ]
            )
        )

        self.assertEqual(
            [m["id"] for m in conversation["messages"]], ["3001", "3000", "3002"]
        )

    def test_html_body_falls_back_to_plain_body(self):
        conversation = self.conversation(
            _ticket(comments=[_comment(html_body="", body="plain text")])
        )

        self.assertEqual(conversation["messages"][0]["html_body"], "plain text")

    def test_junk_comment_entries_are_left_out(self):
        conversation = self.conversation(
            _ticket(comments=[_comment(id=3001), {"meta": {"has_more": False}}])
        )

        self.assertEqual([m["id"] for m in conversation["messages"]], ["3001"])

    def test_message_with_an_id_seen_before_is_dropped(self):
        summary = self.convert(
            [
                _ticket(
                    id=1,
                    comments=[
                        _comment(id=1, created_at="2024-01-15T10:00:00Z"),
                        _comment(id=1, created_at="2024-01-15T11:00:00Z"),
                    ],
                ),
                _ticket(
                    id=2,
                    comments=[
                        _comment(id=1, created_at="2024-01-15T10:00:00Z"),
                        _comment(id=2, created_at="2024-01-15T11:00:00Z"),
                    ],
                ),
                _ticket(id=3, comments=[_comment(id=2)]),
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
        self.assertEqual(summary.skipped_without_public_comment, 1)


class TimestampsTest(ConverterTestCase):
    def test_timestamps_get_an_explicit_utc_offset(self):
        conversation = self.conversation(_ticket())

        self.assertEqual(conversation["created_at"], "2024-01-15T10:00:00+00:00")
        self.assertEqual(
            conversation["messages"][0]["created_at"], "2024-01-15T10:00:00+00:00"
        )

    def test_an_offset_is_kept(self):
        conversation = self.conversation(
            _ticket(created_at="2024-01-15 10:00:00.5+0130")
        )

        self.assertEqual(conversation["created_at"], "2024-01-15T10:00:00.500000+01:30")

    def test_timestamp_without_an_offset_is_read_as_utc(self):
        summary = self.convert(
            [
                _ticket(
                    created_at="2024-01-15T09:00:00",
                    comments=[_comment(created_at="2024-01-15T09:00:00")],
                )
            ]
        )

        (conversation,) = self.conversations()
        self.assertEqual(conversation["created_at"], "2024-01-15T09:00:00+00:00")
        self.assertEqual(
            conversation["messages"][0]["created_at"], "2024-01-15T09:00:00+00:00"
        )
        self.assertEqual(summary.normalized_timestamps, 2)

    def test_comment_without_created_at_gets_the_ticket_created_at(self):
        conversation = self.conversation(
            _ticket(
                created_at="2024-01-15T09:00:00Z",
                comments=[_comment(created_at=None)],
            )
        )

        self.assertEqual(
            conversation["messages"][0]["created_at"], "2024-01-15T09:00:00+00:00"
        )

    def test_ticket_with_an_unreadable_created_at_is_skipped(self):
        for created_at in ("not-a-date", "2024-13-01T10:00:00Z"):
            with self.subTest(created_at=created_at):
                summary = self.convert([_ticket(created_at=created_at)])

                self.assertEqual(self.conversations(), [])
                self.assertEqual(summary.skipped_with_unreadable_created_at, 1)

    def test_comment_with_an_unreadable_created_at_is_dropped(self):
        summary = self.convert(
            [
                _ticket(
                    comments=[
                        _comment(id=3001),
                        _comment(id=3002, created_at="not-a-date"),
                    ]
                )
            ]
        )

        (conversation,) = self.conversations()
        self.assertEqual([m["id"] for m in conversation["messages"]], ["3001"])
        self.assertEqual(summary.dropped_comments_with_unreadable_created_at, 1)


class UsersTest(ConverterTestCase):
    def test_requester_id_without_an_embedded_user_gets_a_placeholder(self):
        conversation = self.conversation(_ticket(requester=None))

        self.assertEqual(conversation["requester"]["external_id"], "201")
        self.assertEqual(
            conversation["requester"]["email"], "zendesk-user-201@acme.zendesk.com"
        )

    def test_embedded_requester_without_requester_id(self):
        conversation = self.conversation(_ticket(requester_id=None))

        self.assertEqual(conversation["requester"]["email"], "joe@example.com")

    def test_requester_falls_back_to_the_first_comment_author(self):
        conversation = self.conversation(_ticket(requester=None, requester_id=None))

        self.assertEqual(conversation["requester"]["external_id"], "201")

    def test_role_maps_to_is_agent(self):
        for role, expected in [
            ("admin", True),
            ("agent", True),
            ("end-user", False),
            (None, False),
        ]:
            with self.subTest(role=role):
                user = {"id": 900, "email": "x@example.com", "role": role}
                conversation = self.conversation(
                    _ticket(
                        requester=user,
                        requester_id=900,
                        comments=[_comment(author_id=900)],
                    )
                )

                self.assertIs(
                    conversation["messages"][0]["author"]["is_agent"], expected
                )

    def test_user_embedded_on_an_earlier_line_is_used_on_a_later_line(self):
        self.convert(
            [
                _ticket(id=1, assignee=AGENT),
                _ticket(id=2, comments=[_comment(id=3002, author_id=AGENT["id"])]),
            ]
        )

        _first, second = self.conversations()
        self.assertEqual(second["messages"][0]["author"]["email"], "alice@example.com")

    def test_comment_author_embedded_on_the_comment(self):
        second_agent = {
            "id": 999,
            "name": "Agent Bob",
            "email": "bob@example.com",
            "role": "agent",
        }
        conversation = self.conversation(
            _ticket(
                comments=[
                    _comment(id=3001),
                    _comment(
                        id=3002,
                        created_at="2024-01-15T11:00:00Z",
                        author_id=999,
                        author=second_agent,
                    ),
                ]
            )
        )

        reply_author = conversation["messages"][1]["author"]
        self.assertEqual(reply_author["email"], "bob@example.com")
        self.assertIs(reply_author["is_agent"], True)

    def test_unknown_author_gets_a_placeholder_email(self):
        conversation = self.conversation(_ticket(comments=[_comment(author_id=9999)]))

        self.assertEqual(
            conversation["messages"][0]["author"]["email"],
            "zendesk-user-9999@acme.zendesk.com",
        )


class ConversationFieldsTest(ConverterTestCase):
    def test_status_maps_to_is_closed(self):
        for status, expected in [
            ("solved", True),
            ("closed", True),
            ("open", False),
            ("pending", False),
            ("new", False),
            ("hold", False),
            (None, True),
        ]:
            with self.subTest(status=status):
                conversation = self.conversation(_ticket(status=status))

                self.assertIs(conversation["is_closed"], expected)

    def test_source_and_account(self):
        conversation = self.conversation(_ticket())

        self.assertEqual(conversation["source"], "zendesk")
        self.assertEqual(conversation["account"], "acme")

    def test_empty_subject_uses_placeholder(self):
        conversation = self.conversation(_ticket(subject=""))

        self.assertEqual(conversation["title"], "(No subject)")

    def test_tags_are_kept(self):
        conversation = self.conversation(_ticket(tags=["billing", "vip"]))

        self.assertEqual(conversation["tags"], ["billing", "vip"])

    def test_tags_with_a_comma_are_split_and_long_tags_are_cut(self):
        summary = self.convert([_ticket(tags=["vip", "a, b", "x" * 60, "", "b"])])

        (conversation,) = self.conversations()
        self.assertEqual(conversation["tags"], ["vip", "a", "b", "x" * 50])
        self.assertEqual(summary.changed_tags, 3)


class AttachmentsTest(ConverterTestCase):
    def attachments(self, html_body="<p>x</p>", attachments=()):
        conversation = self.conversation(
            _ticket(
                comments=[_comment(html_body=html_body, attachments=list(attachments))]
            )
        )
        return conversation["messages"][0]["attachments"]

    def test_attachment_without_an_https_url_is_dropped(self):
        summary = self.convert(
            [
                _ticket(
                    comments=[
                        _comment(
                            attachments=[
                                {"file_name": "no-url.pdf"},
                                {"content_url": "http://acme.zendesk.com/a"},
                                {"content_url": TOKEN_URL, "file_name": "kept.png"},
                            ]
                        )
                    ]
                )
            ]
        )

        (conversation,) = self.conversations()
        self.assertEqual(
            [a["filename"] for a in conversation["messages"][0]["attachments"]],
            ["kept.png"],
        )
        self.assertEqual(summary.dropped_attachments_without_https, 2)

    def test_inline_ref_is_set_only_when_the_body_refers_to_the_url(self):
        listed = {"content_url": TOKEN_URL, "file_name": "photo.png", "inline": True}

        in_body = self.attachments(f'<img src="{TOKEN_URL}">', [listed])
        not_in_body = self.attachments("<p>No image here.</p>", [listed])

        self.assertEqual(in_body[0]["inline_ref"], TOKEN_URL)
        self.assertIsNone(not_in_body[0]["inline_ref"])

    def test_url_is_canonicalized_to_the_subdomain(self):
        (attachment,) = self.attachments(
            attachments=[{"content_url": BRANDED_TOKEN_URL, "file_name": "a.png"}]
        )

        self.assertEqual(attachment["url"], TOKEN_URL)

    def test_filename_alias(self):
        (attachment,) = self.attachments(
            attachments=[{"content_url": "https://x/1", "filename": "a.pdf"}]
        )

        self.assertEqual(attachment["filename"], "a.pdf")

    def test_attachments_on_notes_are_dropped_and_counted(self):
        summary = self.convert(
            [
                _ticket(
                    comments=[
                        _comment(id=3001),
                        _comment(
                            id=3003,
                            public=False,
                            attachments=[{"content_url": TOKEN_URL}],
                        ),
                    ]
                )
            ]
        )

        self.assertEqual(summary.dropped_note_attachments, 1)

    def test_image_only_in_the_body_becomes_an_inline_attachment(self):
        (attachment,) = self.attachments(f'<p><img src="{TOKEN_URL}"></p>')

        self.assertEqual(
            attachment,
            {
                "url": TOKEN_URL,
                "filename": "photo.png",
                "content_type": "image/png",
                "inline_ref": TOKEN_URL,
            },
        )

    def test_image_only_in_the_body_on_a_branded_host(self):
        (attachment,) = self.attachments(f'<p><img src="{BRANDED_TOKEN_URL}"></p>')

        self.assertEqual(attachment["url"], TOKEN_URL)
        self.assertEqual(attachment["inline_ref"], BRANDED_TOKEN_URL)

    def test_image_in_the_body_and_in_the_list_is_one_attachment(self):
        for body_url in (TOKEN_URL, BRANDED_TOKEN_URL):
            with self.subTest(body_url=body_url):
                (attachment,) = self.attachments(
                    f'<p><img src="{body_url}"></p>',
                    [
                        {
                            "content_url": TOKEN_URL,
                            "file_name": "photo.png",
                            "inline": True,
                        }
                    ],
                )

                self.assertEqual(attachment["url"], TOKEN_URL)
                self.assertEqual(attachment["inline_ref"], body_url)


class LimitsTest(ConverterTestCase):
    def test_output_stays_within_the_import_limits_by_default(self):
        conversation = self.conversation(
            _ticket(
                comments=[
                    _comment(
                        html_body="<p>" + "x" * 150_000 + "</p>",
                        attachments=[
                            {"content_url": f"https://acme.zendesk.com/{i}"}
                            for i in range(25)
                        ],
                    )
                ]
            )
        )

        (message,) = conversation["messages"]
        self.assertLessEqual(
            len(message["html_body"]), IMPORT_LIMITS.max_html_body_length
        )
        self.assertEqual(
            len(message["attachments"]), IMPORT_LIMITS.max_attachments_per_message
        )

    def test_conversations_over_the_file_limit_continue_in_part_files(self):
        summary = self.convert(
            [_ticket(id=i) for i in range(1, 6)],
            limits=Limits(max_conversations_per_file=2),
        )

        self.assertEqual(
            [(row["filename"], row["conversations"]) for row in summary.files],
            [
                (FILENAME, 2),
                ("zendesk-acme-part2.ndjson", 2),
                ("zendesk-acme-part3.ndjson", 1),
            ],
        )
        self.assertEqual(
            [
                c["external_id"]
                for row in summary.files
                for c in self.conversations(row["filename"])
            ],
            ["1", "2", "3", "4", "5"],
        )

    def test_too_long_message_is_cut_at_a_tag_boundary(self):
        body = "<p>" + "a" * 40 + f'</p><img src="{TOKEN_URL}">' + "b" * 100

        summary = self.convert(
            [_ticket(comments=[_comment(html_body=body)])],
            limits=Limits(max_html_body_length=50 + len(TRUNCATION_NOTICE)),
        )

        (conversation,) = self.conversations()
        (message,) = conversation["messages"]
        self.assertEqual(
            message["html_body"], "<p>" + "a" * 40 + "</p>" + TRUNCATION_NOTICE
        )
        self.assertEqual(message["attachments"], [])
        self.assertEqual(summary.truncated_messages, 1)

    def test_too_many_attachments_keeps_inline_attachments_first(self):
        summary = self.convert(
            [
                _ticket(
                    comments=[
                        _comment(
                            html_body=f'<img src="{TOKEN_URL}">',
                            attachments=[
                                {
                                    "content_url": f"https://acme.zendesk.com/{i}",
                                    "file_name": f"file-{i}.pdf",
                                }
                                for i in range(3)
                            ],
                        )
                    ]
                )
            ],
            limits=Limits(max_attachments_per_message=2),
        )

        (conversation,) = self.conversations()
        self.assertEqual(
            [a["filename"] for a in conversation["messages"][0]["attachments"]],
            ["file-0.pdf", "photo.png"],
        )
        self.assertEqual(summary.dropped_attachments, 2)


class ReadingTheExportTest(ConverterTestCase):
    def test_blank_lines_are_skipped(self):
        self.export.write_text(f"\n{json.dumps(_ticket())}\n\n", encoding="utf-8")

        convert_export(str(self.export), self.output_dir, subdomain="acme")

        self.assertEqual(len(self.conversations()), 1)

    def test_invalid_json_names_the_line(self):
        self.export.write_text(json.dumps(_ticket()) + "\n{not json}\n")

        with self.assertRaisesRegex(ExportError, "Invalid JSON on line 2"):
            convert_export(str(self.export), self.output_dir, subdomain="acme")

    def test_invalid_ticket_names_the_line(self):
        for ticket in ([], {"id": "1001"}, {"id": 1001}, _ticket(comments={})):
            with self.subTest(ticket=ticket):
                self.write_export([ticket])

                with self.assertRaisesRegex(ExportError, "Invalid ticket on line 1"):
                    convert_export(str(self.export), self.output_dir, subdomain="acme")

    def test_refuses_to_write_over_the_export(self):
        export = self.output_dir / FILENAME
        self.output_dir.mkdir()
        export.write_text(json.dumps(_ticket()) + "\n", encoding="utf-8")
        original = export.read_text(encoding="utf-8")

        with self.assertRaisesRegex(ExportError, "is the export file"):
            convert_export(str(export), self.output_dir, subdomain="acme")

        self.assertEqual(export.read_text(encoding="utf-8"), original)


class SummaryTest(unittest.TestCase):
    def test_reports_the_files_and_what_was_changed_or_dropped(self):
        counts = {
            name: i
            for i, name in enumerate(Summary.__dataclass_fields__, start=1)
            if name != "files"
        }
        text = format_summary(
            Summary(files=[{"filename": FILENAME, "conversations": 7}], **counts),
            Path("out"),
        )

        self.assertIn(f"{FILENAME}: 7 conversation(s)", text)
        self.assertEqual(len(text.splitlines()), 2 + len(counts))

    def test_leaves_out_the_lines_for_what_did_not_happen(self):
        text = format_summary(Summary(), Path("out"))

        self.assertEqual(text, "Wrote 0 file(s) to out/:")


class CommandLineTest(ConverterTestCase):
    def test_finds_the_subdomain_in_the_export(self):
        self.write_export(
            [
                _ticket(
                    comments=[
                        _comment(attachments=[{"content_url": TOKEN_URL}]),
                    ]
                )
            ]
        )

        code, out, _err = self.run_main()

        self.assertEqual(code, 0)
        self.assertEqual(self.conversations()[0]["account"], "acme")
        self.assertIn("Wrote 1 file(s)", out)

    def test_given_subdomain_wins_over_the_one_in_the_export(self):
        self.write_export(
            [_ticket(comments=[_comment(attachments=[{"content_url": TOKEN_URL}])])]
        )

        code, _out, _err = self.run_main("--subdomain", "other")

        self.assertEqual(code, 0)
        self.assertEqual(
            self.conversations("zendesk-other.ndjson")[0]["account"], "other"
        )

    def test_rejects_an_export_without_a_subdomain(self):
        self.write_export([_ticket()])

        code, _out, err = self.run_main()

        self.assertEqual(code, 2)
        self.assertIn("give --subdomain", err)

    def test_rejects_a_subdomain_that_the_import_refuses(self):
        self.write_export([_ticket()])

        code, _out, err = self.run_main("--subdomain", "not valid!")

        self.assertEqual(code, 2)
        self.assertIn("--subdomain", err)
        self.assertFalse(self.output_dir.exists())

    def test_rejects_a_missing_file(self):
        code, _out, err = self.run_main()

        self.assertEqual(code, 2)
        self.assertIn("no such file", err)

    def test_reports_an_export_that_cannot_be_read(self):
        self.export.write_text("{not json}\n", encoding="utf-8")

        code, _out, err = self.run_main("--subdomain", "acme")

        self.assertEqual(code, 1)
        self.assertIn("Invalid JSON on line 1", err)

    def test_runs_as_a_script(self):
        self.write_export([_ticket()])
        argv = [
            "zendesk_to_kundo.py",
            "--file",
            str(self.export),
            "--output-dir",
            str(self.output_dir),
            "--subdomain",
            "acme",
        ]

        with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_info:
                runpy.run_path(zendesk_to_kundo.__file__, run_name="__main__")

        self.assertEqual(exit_info.exception.code, 0)
        self.assertTrue((self.output_dir / FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
