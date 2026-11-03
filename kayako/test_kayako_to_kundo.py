import io
import json
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import kayako_to_kundo
from kayako_to_kundo import (
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
    "id": 100,
    "full_name": "John Customer",
    "emails": ["john@example.com"],
    "role": "user",
}
AGENT = {
    "id": 200,
    "full_name": "Agent Alice",
    "emails": ["alice@example.com"],
    "role": "agent",
}
ATLAS = {"id": "", "full_name": "Atlas AI", "emails": [], "role": "agent"}
FILENAME = "kayako-acme.ndjson"
ATTACHMENT_URL = "https://acme.kayako.com/api/v1/cases/1/attachments/download/1"


def _post(**overrides):
    defaults = dict(
        id=5001,
        created_at="2024-01-15T09:00:00Z",
        creator=REQUESTER,
        creator_id=REQUESTER["id"],
        source_channel="MAIL",
        content_html="<p>Hi, I have a billing issue.</p>",
        attachments=[],
    )
    return {**defaults, **overrides}


def _conversation(**overrides):
    id = overrides.get("id", 12345)
    defaults = dict(
        id=id,
        subject="Billing question",
        created_at="2024-01-15T09:00:00Z",
        requester_id=REQUESTER["id"],
        requester=REQUESTER,
        assigned_agent=AGENT,
        posts=[_post(id=id * 10)],
    )
    return {**defaults, **overrides}


class ConverterTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.export = self.tmp_path / "export.ndjson"
        self.output_dir = self.tmp_path / "out"

    def write_export(self, conversations):
        self.export.write_text(
            "\n".join(json.dumps(c) for c in conversations) + "\n", encoding="utf-8"
        )

    def convert(self, conversations, **kwargs):
        self.write_export(conversations)
        kwargs.setdefault("account", "acme")
        return convert_export(str(self.export), self.output_dir, **kwargs)

    def conversations(self, filename=FILENAME):
        path = self.output_dir / filename
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def conversation(self, raw, **kwargs):
        self.convert([raw], **kwargs)
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
    def test_first_public_post_opens_the_conversation(self):
        conversation = self.conversation(
            _conversation(
                posts=[
                    _post(id=5001, created_at="2024-01-15T09:00:00Z"),
                    _post(
                        id=5002,
                        created_at="2024-01-15T10:00:00Z",
                        creator=AGENT,
                        creator_id=AGENT["id"],
                    ),
                ]
            )
        )

        self.assertEqual([m["id"] for m in conversation["messages"]], ["5001", "5002"])

    def test_note_post_becomes_a_note(self):
        conversation = self.conversation(
            _conversation(
                posts=[
                    _post(id=5001),
                    _post(
                        id=5003,
                        created_at="2024-01-15T10:05:00Z",
                        creator=AGENT,
                        source_channel="note",
                        content_html="<p>Internal note.</p>",
                    ),
                ]
            )
        )

        self.assertEqual([m["id"] for m in conversation["messages"]], ["5001"])
        self.assertEqual(conversation["notes"][0]["html_body"], "<p>Internal note.</p>")
        self.assertNotIn("attachments", conversation["notes"][0])

    def test_leading_note_does_not_open_the_conversation(self):
        conversation = self.conversation(
            _conversation(
                posts=[
                    _post(
                        id=5000,
                        created_at="2024-01-15T08:00:00Z",
                        source_channel="NOTE",
                    ),
                    _post(id=5001),
                ]
            )
        )

        self.assertEqual([m["id"] for m in conversation["messages"]], ["5001"])
        self.assertEqual([n["id"] for n in conversation["notes"]], ["5000"])

    def test_conversation_with_only_notes_is_skipped(self):
        summary = self.convert([_conversation(posts=[_post(source_channel="NOTE")])])

        self.assertEqual(self.conversations(), [])
        self.assertEqual(summary.skipped_without_public_post, 1)

    def test_conversation_with_no_posts_is_skipped(self):
        summary = self.convert([_conversation(posts=[])])

        self.assertEqual(self.conversations(), [])
        self.assertEqual(summary.skipped_without_posts, 1)

    def test_posts_are_sorted_chronologically_across_utc_offsets(self):
        conversation = self.conversation(
            _conversation(
                posts=[
                    _post(id=5002, created_at="2024-01-15T08:00:00+00:00"),
                    _post(id=5001, created_at="2024-01-15T09:00:00+02:00"),
                ]
            )
        )

        self.assertEqual([m["id"] for m in conversation["messages"]], ["5001", "5002"])

    def test_body_falls_back_to_text_and_then_to_empty(self):
        for post, expected in [
            (_post(content_html=None, content_text="plain text"), "plain text"),
            (_post(content_html=None, body="plain text"), "plain text"),
            (_post(content_html=None, html_body="<p>x</p>"), "<p>x</p>"),
            (_post(content_html=None), ""),
        ]:
            with self.subTest(post=post):
                conversation = self.conversation(_conversation(posts=[post]))

                self.assertEqual(conversation["messages"][0]["html_body"], expected)

    def test_message_with_an_id_seen_before_is_dropped(self):
        summary = self.convert(
            [
                _conversation(
                    id=1,
                    posts=[
                        _post(id=1, created_at="2024-01-15T09:00:00Z"),
                        _post(id=1, created_at="2024-01-15T10:00:00Z"),
                    ],
                ),
                _conversation(
                    id=2,
                    posts=[
                        _post(id=1, created_at="2024-01-15T09:00:00Z"),
                        _post(id=2, created_at="2024-01-15T10:00:00Z"),
                    ],
                ),
                _conversation(id=3, posts=[_post(id=2)]),
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
        self.assertEqual(summary.skipped_without_public_post, 1)


class TimestampsTest(ConverterTestCase):
    def test_timestamps_get_an_explicit_utc_offset(self):
        conversation = self.conversation(_conversation())

        self.assertEqual(conversation["created_at"], "2024-01-15T09:00:00+00:00")

    def test_timestamp_without_an_offset_is_read_as_utc(self):
        summary = self.convert(
            [
                _conversation(
                    created_at="2024-01-15T09:00:00",
                    posts=[_post(created_at="2024-01-15T09:00:00")],
                )
            ]
        )

        (conversation,) = self.conversations()
        self.assertEqual(conversation["created_at"], "2024-01-15T09:00:00+00:00")
        self.assertEqual(
            conversation["messages"][0]["created_at"], "2024-01-15T09:00:00+00:00"
        )
        self.assertEqual(summary.normalized_timestamps, 2)

    def test_conversation_with_an_unreadable_created_at_is_skipped(self):
        summary = self.convert([_conversation(created_at="not-a-date")])

        self.assertEqual(self.conversations(), [])
        self.assertEqual(summary.skipped_with_unreadable_created_at, 1)

    def test_post_with_an_unreadable_or_missing_created_at_is_dropped(self):
        summary = self.convert(
            [
                _conversation(
                    posts=[
                        _post(id=5001),
                        _post(id=5002, created_at="not-a-date"),
                        _post(id=5003, created_at=None),
                    ]
                )
            ]
        )

        (conversation,) = self.conversations()
        self.assertEqual([m["id"] for m in conversation["messages"]], ["5001"])
        self.assertEqual(summary.dropped_posts_with_unreadable_created_at, 2)


class UsersTest(ConverterTestCase):
    def test_role_maps_to_is_agent(self):
        for role, expected in [
            ("agent", True),
            ("admin", True),
            ("owner", True),
            ("collaborator", True),
            ("user", False),
            (None, False),
        ]:
            with self.subTest(role=role):
                creator = {"id": 900, "full_name": "X", "emails": [], "role": role}
                conversation = self.conversation(
                    _conversation(posts=[_post(creator=creator, creator_id=900)])
                )

                self.assertIs(
                    conversation["messages"][0]["author"]["is_agent"], expected
                )

    def test_user_fields(self):
        creator = {
            "id": 900,
            "name": "Renamed User",
            "emails": ["first@example.com", "second@example.com"],
        }
        conversation = self.conversation(
            _conversation(posts=[_post(creator=creator, creator_id=900)])
        )

        self.assertEqual(
            conversation["messages"][0]["author"],
            {
                "external_id": "900",
                "email": "first@example.com",
                "name": "Renamed User",
                "is_agent": False,
            },
        )

    def test_unknown_creator_has_no_email(self):
        conversation = self.conversation(
            _conversation(posts=[_post(creator=None, creator_id=9999)])
        )

        author = conversation["messages"][0]["author"]
        self.assertEqual(author["external_id"], "9999")
        self.assertIsNone(author["email"])

    def test_user_embedded_on_an_earlier_line_is_used_on_a_later_line(self):
        self.convert(
            [
                _conversation(id=1, posts=[_post(id=5001, creator=AGENT)]),
                _conversation(
                    id=2, posts=[_post(id=5002, creator=None, creator_id=200)]
                ),
            ]
        )

        _first, second = self.conversations()
        self.assertEqual(second["messages"][0]["author"]["name"], "Agent Alice")

    def test_system_user_with_an_empty_id_gets_id_0_and_keeps_its_name(self):
        conversation = self.conversation(
            _conversation(posts=[_post(creator=ATLAS, creator_id="")])
        )

        author = conversation["messages"][0]["author"]
        self.assertEqual(author["external_id"], "0")
        self.assertEqual(author["name"], "Atlas AI")

    def test_system_user_does_not_name_a_post_without_a_known_creator(self):
        self.convert(
            [
                _conversation(
                    id=1, posts=[_post(id=5001, creator=ATLAS, creator_id="")]
                ),
                _conversation(
                    id=2, posts=[_post(id=5002, creator=None, creator_id="")]
                ),
            ]
        )

        _atlas, unknown = self.conversations()
        self.assertEqual(unknown["messages"][0]["author"]["external_id"], "0")
        self.assertIsNone(unknown["messages"][0]["author"]["name"])

    def test_empty_assigned_agent_is_ignored(self):
        conversation = self.conversation(_conversation(assigned_agent=""))

        self.assertEqual(conversation["external_id"], "12345")

    def test_requester_falls_back_to_the_first_post_creator(self):
        conversation = self.conversation(
            _conversation(requester=None, requester_id=None)
        )

        self.assertEqual(conversation["requester"]["external_id"], "100")
        self.assertEqual(conversation["requester"]["email"], "john@example.com")


class ConversationFieldsTest(ConverterTestCase):
    def test_fields_that_the_export_does_not_have(self):
        conversation = self.conversation(_conversation())

        self.assertEqual(conversation["source"], "kayako")
        self.assertEqual(conversation["account"], "acme")
        self.assertIs(conversation["is_closed"], True)
        self.assertEqual(conversation["tags"], [])

    def test_case_id_becomes_the_case_number(self):
        conversation = self.conversation(_conversation(id="98765"))

        self.assertEqual(conversation["external_id"], "98765")
        self.assertEqual(conversation["case_number"], "98765")

    def test_empty_subject_uses_placeholder(self):
        conversation = self.conversation(_conversation(subject=""))

        self.assertEqual(conversation["title"], "(No subject)")


class AttachmentsTest(ConverterTestCase):
    def attachments(self, html_body="<p>x</p>", attachments=()):
        conversation = self.conversation(
            _conversation(
                posts=[_post(content_html=html_body, attachments=list(attachments))]
            )
        )
        return conversation["messages"][0]["attachments"]

    def test_attachment_fields(self):
        (attachment,) = self.attachments(
            attachments=[
                {"url": ATTACHMENT_URL, "name": "a.pdf", "type": "application/pdf"}
            ]
        )

        self.assertEqual(
            attachment,
            {
                "url": ATTACHMENT_URL,
                "filename": "a.pdf",
                "content_type": "application/pdf",
                "inline_ref": None,
            },
        )

    def test_field_aliases(self):
        (attachment,) = self.attachments(
            attachments=[
                {
                    "url_download": ATTACHMENT_URL,
                    "file_name": "a.pdf",
                    "content_type": "application/pdf",
                }
            ]
        )

        self.assertEqual(attachment["url"], ATTACHMENT_URL)
        self.assertEqual(attachment["filename"], "a.pdf")
        self.assertEqual(attachment["content_type"], "application/pdf")

    def test_inline_ref_is_set_only_when_the_body_refers_to_the_url(self):
        listed = {"content_url": ATTACHMENT_URL, "filename": "a.png", "inline": True}

        in_body = self.attachments(f'<img src="{ATTACHMENT_URL}">', [listed])
        not_in_body = self.attachments("<p>No image here.</p>", [listed])

        self.assertEqual(in_body[0]["inline_ref"], ATTACHMENT_URL)
        self.assertIsNone(not_in_body[0]["inline_ref"])

    def test_attachment_without_an_https_url_is_dropped(self):
        summary = self.convert(
            [
                _conversation(
                    posts=[
                        _post(
                            attachments=[
                                {"name": "no-url.pdf"},
                                {"url": "http://acme.kayako.com/a"},
                                {"url": "abfss://container@account/path/a.pdf"},
                                {"url": "https://"},
                                {"url": ATTACHMENT_URL, "name": "kept.pdf"},
                            ]
                        )
                    ]
                )
            ]
        )

        (conversation,) = self.conversations()
        (message,) = conversation["messages"]
        self.assertEqual([a["filename"] for a in message["attachments"]], ["kept.pdf"])
        self.assertEqual(message["html_body"], "<p>Hi, I have a billing issue.</p>")
        self.assertEqual(summary.dropped_attachments_without_https, 4)

    def test_attachments_on_notes_are_dropped_and_counted(self):
        summary = self.convert(
            [
                _conversation(
                    posts=[
                        _post(id=5001),
                        _post(
                            id=5003,
                            source_channel="NOTE",
                            content_html="<p>Note.</p>",
                            attachments=[{"url": ATTACHMENT_URL}],
                        ),
                    ]
                )
            ]
        )

        (conversation,) = self.conversations()
        self.assertEqual(conversation["notes"][0]["html_body"], "<p>Note.</p>")
        self.assertEqual(summary.dropped_note_attachments, 1)


class LimitsTest(ConverterTestCase):
    def test_output_stays_within_the_import_limits_by_default(self):
        conversation = self.conversation(
            _conversation(
                posts=[
                    _post(
                        content_html="<p>" + "x" * 150_000 + "</p>",
                        attachments=[
                            {"url": f"{ATTACHMENT_URL}/{i}"} for i in range(25)
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
            [_conversation(id=i) for i in range(1, 6)],
            limits=Limits(max_conversations_per_file=2),
        )

        self.assertEqual(
            [(row["filename"], row["conversations"]) for row in summary.files],
            [
                (FILENAME, 2),
                ("kayako-acme-part2.ndjson", 2),
                ("kayako-acme-part3.ndjson", 1),
            ],
        )

    def test_too_long_message_is_cut_at_a_tag_boundary(self):
        body = "<p>" + "a" * 40 + f'</p><img src="{ATTACHMENT_URL}">' + "b" * 100

        summary = self.convert(
            [
                _conversation(
                    posts=[
                        _post(
                            content_html=body,
                            attachments=[{"url": ATTACHMENT_URL, "inline": True}],
                        )
                    ]
                )
            ],
            limits=Limits(max_html_body_length=50 + len(TRUNCATION_NOTICE)),
        )

        (conversation,) = self.conversations()
        (message,) = conversation["messages"]
        self.assertEqual(
            message["html_body"], "<p>" + "a" * 40 + "</p>" + TRUNCATION_NOTICE
        )
        self.assertIsNone(message["attachments"][0]["inline_ref"])
        self.assertEqual(summary.truncated_messages, 1)

    def test_too_many_attachments_keeps_inline_attachments_first(self):
        summary = self.convert(
            [
                _conversation(
                    posts=[
                        _post(
                            content_html=f'<img src="{ATTACHMENT_URL}">',
                            attachments=[
                                {"url": f"{ATTACHMENT_URL}/{i}", "name": f"file-{i}"}
                                for i in range(3)
                            ]
                            + [{"url": ATTACHMENT_URL, "name": "img", "inline": True}],
                        )
                    ]
                )
            ],
            limits=Limits(max_attachments_per_message=2),
        )

        (conversation,) = self.conversations()
        self.assertEqual(
            [a["filename"] for a in conversation["messages"][0]["attachments"]],
            ["file-0", "img"],
        )
        self.assertEqual(summary.dropped_attachments, 2)


class ReadingTheExportTest(ConverterTestCase):
    def test_blank_lines_are_skipped(self):
        self.export.write_text(f"\n{json.dumps(_conversation())}\n\n", encoding="utf-8")

        convert_export(str(self.export), self.output_dir, account="acme")

        self.assertEqual(len(self.conversations()), 1)

    def test_invalid_json_names_the_line(self):
        self.export.write_text(json.dumps(_conversation()) + "\n{not json}\n")

        with self.assertRaisesRegex(ExportError, "Invalid JSON on line 2"):
            convert_export(str(self.export), self.output_dir, account="acme")

    def test_invalid_conversation_names_the_line(self):
        for raw in (
            [],
            {"id": "x"},
            {"id": 1},
            _conversation(posts={}),
            _conversation(posts=[{"id": None}]),
        ):
            with self.subTest(raw=raw):
                self.write_export([raw])

                with self.assertRaisesRegex(
                    ExportError, "Invalid conversation on line 1"
                ):
                    convert_export(str(self.export), self.output_dir, account="acme")

    def test_refuses_to_write_over_the_export(self):
        export = self.output_dir / FILENAME
        self.output_dir.mkdir()
        export.write_text(json.dumps(_conversation()) + "\n", encoding="utf-8")
        original = export.read_text(encoding="utf-8")

        with self.assertRaisesRegex(ExportError, "is the export file"):
            convert_export(str(export), self.output_dir, account="acme")

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
    def test_account_is_the_subdomain_in_the_export(self):
        self.write_export(
            [_conversation(posts=[_post(attachments=[{"url": ATTACHMENT_URL}])])]
        )

        code, out, _err = self.run_main()

        self.assertEqual(code, 0)
        self.assertEqual(self.conversations()[0]["account"], "acme")
        self.assertIn("Wrote 1 file(s)", out)

    def test_account_is_default_without_a_subdomain_in_the_export(self):
        self.write_export([_conversation()])

        code, _out, _err = self.run_main()

        self.assertEqual(code, 0)
        self.assertEqual(
            self.conversations("kayako-default.ndjson")[0]["account"], "default"
        )

    def test_given_account_wins_over_the_subdomain(self):
        self.write_export(
            [_conversation(posts=[_post(attachments=[{"url": ATTACHMENT_URL}])])]
        )

        code, _out, _err = self.run_main("--account", "other")

        self.assertEqual(code, 0)
        self.assertEqual(
            self.conversations("kayako-other.ndjson")[0]["account"], "other"
        )

    def test_rejects_an_account_that_the_import_refuses(self):
        self.write_export([_conversation()])

        code, _out, err = self.run_main("--account", "not valid!")

        self.assertEqual(code, 2)
        self.assertIn("--account", err)
        self.assertFalse(self.output_dir.exists())

    def test_rejects_a_missing_file(self):
        code, _out, err = self.run_main()

        self.assertEqual(code, 2)
        self.assertIn("no such file", err)

    def test_reports_an_export_that_cannot_be_read(self):
        self.export.write_text("{not json}\n", encoding="utf-8")

        code, _out, err = self.run_main("--account", "acme")

        self.assertEqual(code, 1)
        self.assertIn("Invalid JSON on line 1", err)

    def test_runs_as_a_script(self):
        self.write_export([_conversation()])
        argv = [
            "kayako_to_kundo.py",
            "--file",
            str(self.export),
            "--output-dir",
            str(self.output_dir),
            "--account",
            "acme",
        ]

        with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_info:
                runpy.run_path(kayako_to_kundo.__file__, run_name="__main__")

        self.assertEqual(exit_info.exception.code, 0)
        self.assertTrue((self.output_dir / FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
