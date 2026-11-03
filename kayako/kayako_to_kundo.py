"""
Convert a Kayako ticket export into the Kundo import format: NDJSON, one
conversation per line.

Usage (Python 3.9 or later, no third-party packages):

    python3 kayako_to_kundo.py --file kayako-export.ndjson --output-dir out/

See README.md for the export file that this script reads and where to upload
the converted files.

People download and run this file as it is, so it must only use the Python
standard library and syntax that Python 3.9 accepts. The CI runs the tests on
Python 3.9 with no packages installed to make sure of both.

Kundo finds the conversations and messages that it already has by `source`
"kayako", `account` (by default the Kayako subdomain), the Kayako case id (the
conversation `external_id`) and the Kayako post id (each message `id`). A new
import of the same cases into the same inbox thus only adds the messages that
are not already there.

Kundo refuses a file that is over the import limits or breaks a rule of the
import format, so the output stays in them: a file with more conversations
than one file can hold continues in `<name>-part2.ndjson` and so on, a
message body that is too long is cut, a message with too many attachments
keeps as many as the limit allows, and what the import refuses (duplicate
message ids, attachments without an HTTPS address) is left out.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO
from urllib.parse import urlsplit

FORMAT_VERSION = 1
SOURCE = "kayako"
DEFAULT_ACCOUNT = "default"
ACCOUNT_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
AGENT_ROLES = {"agent", "admin", "owner", "collaborator"}

TRUNCATION_NOTICE = (
    "<p>[Meddelandet kortades vid importen eftersom det var för långt.]</p>"
)

_SUBDOMAIN_URL_RE = re.compile(r"https://([^./\"']+)\.kayako\.com/")
_ID_RE = re.compile(r"\d+")

# The same formats that Kundo accepts, so that a timestamp that Kundo can read
# is not dropped here.
_DATETIME_RE = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})"
    r"[T ](?P<hour>\d{1,2}):(?P<minute>\d{1,2})"
    r"(?::(?P<second>\d{1,2})(?:[.,](?P<microsecond>\d{1,6})\d{0,6})?)?"
    r"\s*(?P<tzinfo>Z|[+-]\d{2}(?::?\d{2})?)?$"
)


@dataclass(frozen=True)
class Limits:
    """None means no limit."""

    max_conversations_per_file: int | None = None
    max_html_body_length: int | None = None
    max_attachments_per_message: int | None = None


#: Must be equal to the limits of the import in Kundo.
IMPORT_LIMITS = Limits(
    max_conversations_per_file=20_000,
    max_html_body_length=100_000,
    max_attachments_per_message=20,
)


@dataclass(frozen=True)
class Summary:
    """`files` holds one row per file written."""

    files: list[dict] = field(default_factory=list)
    skipped_without_posts: int = 0
    skipped_without_public_post: int = 0
    skipped_with_unreadable_created_at: int = 0
    dropped_posts_with_unreadable_created_at: int = 0
    normalized_timestamps: int = 0
    dropped_note_attachments: int = 0
    truncated_messages: int = 0
    dropped_attachments: int = 0
    dropped_attachments_without_https: int = 0
    dropped_duplicate_messages: int = 0


class ExportError(Exception):
    """The export file cannot be read."""


# ---------------------------------------------------------------------------
# Reading the export file
# ---------------------------------------------------------------------------


def _id(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) or (isinstance(value, str) and _ID_RE.fullmatch(value)):
        return str(value)
    return None


def _iter_raw_conversations(path: str) -> Iterator[dict]:
    with open(path, encoding="utf-8") as fh:
        for line_number, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                conversation = json.loads(line)
            except json.JSONDecodeError as e:
                raise ExportError(f"Invalid JSON on line {line_number}: {e}") from e
            error = _conversation_error(conversation)
            if error:
                raise ExportError(
                    f"Invalid conversation on line {line_number}: {error}"
                )
            yield conversation


def _conversation_error(conversation: object) -> str | None:
    if not isinstance(conversation, dict):
        return "the line is not a JSON object"
    if _id(conversation.get("id")) is None:
        return "`id` must be a number"
    if not isinstance(conversation.get("created_at"), str):
        return "`created_at` is missing"
    posts = conversation.get("posts", [])
    if not isinstance(posts, list):
        return "`posts` must be a list"
    for post in posts:
        if not isinstance(post, dict) or _id(post.get("id")) is None:
            return "each post must have a number as `id`"
    return None


def detect_subdomain(path: str) -> str | None:
    """The attachment URLs in an export contain the account's own subdomain."""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            match = _SUBDOMAIN_URL_RE.search(line)
            if match:
                return match.group(1)
    return None


def _user_id(value: object) -> str:
    """Some exports give an empty string as the id of a system user, for
    example Kayako's own AI assistant. Such a user gets id 0."""
    return _id(value) or "0"


class _Users:
    """Users that the export embeds anywhere in the file, by id. A post can
    refer to its creator only by id, also when the creator is embedded on an
    earlier line.

    Id 0 is not kept: different system users can all have it, and a post
    without a known creator must not get the name of one of them."""

    def __init__(self) -> None:
        self._users: dict[str, dict] = {}

    def add_from(self, conversation: dict) -> None:
        candidates = [conversation.get("requester"), conversation.get("assigned_agent")]
        candidates += [post.get("creator") for post in conversation.get("posts") or []]
        for user in candidates:
            if isinstance(user, dict) and _user_id(user.get("id")) != "0":
                self._users.setdefault(_user_id(user.get("id")), user)

    def get(self, user_id: object) -> dict:
        return self._users.get(_user_id(user_id)) or {"id": _user_id(user_id)}


# ---------------------------------------------------------------------------
# Conversation -> conversation
# ---------------------------------------------------------------------------


def _parse_timestamp(value: object) -> tuple[str | None, bool]:
    """Returns the ISO-8601 timestamp with a UTC offset and whether the
    offset was added, or None if the value is not a timestamp. The import
    refuses a timestamp without an offset; Kayako gives UTC."""
    match = _DATETIME_RE.match(value) if isinstance(value, str) else None
    if match is None:
        return None, False
    parts = match.groupdict()
    offset = parts.pop("tzinfo")
    parts["microsecond"] = (parts["microsecond"] or "0").ljust(6, "0")
    try:
        dt = datetime(**{k: int(v) for k, v in parts.items() if v is not None})
    except ValueError:
        return None, False
    if offset is None:
        return dt.replace(tzinfo=timezone.utc).isoformat(), True
    if offset == "Z":
        return dt.replace(tzinfo=timezone.utc).isoformat(), False
    minutes = 60 * int(offset[1:3]) + (int(offset[-2:]) if len(offset) > 3 else 0)
    sign = -1 if offset[0] == "-" else 1
    tz = timezone(timedelta(minutes=sign * minutes))
    return dt.replace(tzinfo=tz).isoformat(), False


def _first(values: dict, *keys: str) -> object:
    """The export may use any of `keys` for the same field."""
    return next((values[key] for key in keys if values.get(key)), None)


def _convert_user(user: dict) -> dict:
    emails = user.get("emails") or []
    return {
        "external_id": _user_id(user.get("id")),
        "email": emails[0] if isinstance(emails, list) and emails else None,
        "name": _first(user, "full_name", "name"),
        "is_agent": user.get("role") in AGENT_ROLES,
    }


def _fit_html_body(html: str, max_length: int | None) -> tuple[str, bool]:
    """Returns the body and whether it was cut. The cut does not stop inside
    a tag: the importer sanitizes the HTML and would show the rest of a
    broken tag as text."""
    if max_length is None or len(html) <= max_length:
        return html, False
    cut = html[: max_length - len(TRUNCATION_NOTICE)]
    last_tag_start = cut.rfind("<")
    if last_tag_start > cut.rfind(">"):
        cut = cut[:last_tag_start]
    return cut + TRUNCATION_NOTICE, True


def _is_https_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        return parts.scheme == "https" and bool(parts.hostname)
    except ValueError:
        return False


def _convert_attachments(attachments: list, html_body: str) -> tuple[list[dict], int]:
    """
    Returns the attachments and how many were dropped because they have no
    HTTPS URL, which the import refuses.

    An attachment is inline only when the body refers to its URL, because
    Kayako marks more attachments as inline than the body shows.
    """
    result: list[dict] = []
    without_https = 0
    for att in attachments:
        att = att if isinstance(att, dict) else {}
        url = _first(att, "url", "url_download", "content_url")
        if not _is_https_url(url):
            without_https += 1
            continue
        result.append(
            {
                "url": url,
                "filename": _first(att, "name", "filename", "file_name")
                or "attachment",
                "content_type": _first(att, "type", "content_type", "mime_type")
                or "application/octet-stream",
                "inline_ref": url if att.get("inline") and url in html_body else None,
            }
        )
    return result, without_https


def _cap_attachments(
    attachments: list[dict], max_count: int | None
) -> tuple[list[dict], int]:
    """Returns the attachments to keep and how many were dropped. Inline
    attachments are kept first, because without them the body shows broken
    images."""
    if max_count is None or len(attachments) <= max_count:
        return attachments, 0
    by_priority = sorted(
        range(len(attachments)), key=lambda i: attachments[i]["inline_ref"] is None
    )
    keep = set(by_priority[:max_count])
    kept = [att for i, att in enumerate(attachments) if i in keep]
    return kept, len(attachments) - max_count


def _html_body(post: dict) -> str:
    return (
        _first(post, "content_html", "html_body")
        or _first(post, "content_text", "body")
    ) or ""


def _convert_message(
    post: dict, created_at: str, author: dict, limits: Limits
) -> tuple[dict, Counter]:
    html_body, truncated = _fit_html_body(_html_body(post), limits.max_html_body_length)
    downloadable, without_https = _convert_attachments(
        post.get("attachments") or [], html_body
    )
    attachments, dropped = _cap_attachments(
        downloadable, limits.max_attachments_per_message
    )
    message = {
        "id": _id(post["id"]),
        "created_at": created_at,
        "author": author,
        "html_body": html_body,
        "attachments": attachments,
    }
    stats = Counter(
        truncated_messages=int(truncated),
        dropped_attachments=dropped,
        dropped_attachments_without_https=without_https,
    )
    return message, stats


def _convert_conversation(
    conversation: dict,
    users: _Users,
    account: str,
    limits: Limits,
    seen_message_ids: set[str],
) -> tuple[dict | None, Counter]:
    """
    Returns the conversation plus counts of what was changed or left out. The
    conversation is None if the Kayako conversation cannot become one; the
    counts tell why.

    The import refuses a file where two messages have the same id, also in
    different conversations, so a message with an id in `seen_message_ids` is
    left out. The ids of the messages that are kept are added to it.
    """
    stats: Counter = Counter()
    posts = conversation.get("posts") or []
    if not posts:
        stats["skipped_without_posts"] += 1
        return None, stats
    created_at, normalized = _parse_timestamp(conversation["created_at"])
    if created_at is None:
        stats["skipped_with_unreadable_created_at"] += 1
        return None, stats
    stats["normalized_timestamps"] += normalized

    # Sorted, because the first public post becomes the conversation's first
    # message and the export does not promise an order.
    dated_posts = []
    for post in posts:
        post_created_at, normalized = _parse_timestamp(post.get("created_at"))
        if post_created_at is None:
            stats["dropped_posts_with_unreadable_created_at"] += 1
            continue
        stats["normalized_timestamps"] += normalized
        dated_posts.append((post_created_at, post))
    dated_posts.sort(key=lambda pair: datetime.fromisoformat(pair[0]))

    messages: list[dict] = []
    notes: list[dict] = []
    for post_created_at, post in dated_posts:
        # The post's own creator first: two system users with id 0 must each
        # keep their own name.
        creator = post.get("creator")
        if not isinstance(creator, dict):
            creator = users.get(post.get("creator_id"))
        author = _convert_user(creator)
        post_id = _id(post["id"])
        if str(post.get("source_channel") or "").upper() == "NOTE":
            stats["dropped_note_attachments"] += len(post.get("attachments") or [])
            notes.append(
                {
                    "id": post_id,
                    "created_at": post_created_at,
                    "author": author,
                    "html_body": _html_body(post),
                }
            )
        elif post_id in seen_message_ids:
            stats["dropped_duplicate_messages"] += 1
        else:
            seen_message_ids.add(post_id)
            message, message_stats = _convert_message(
                post, post_created_at, author, limits
            )
            stats += message_stats
            messages.append(message)

    if not messages:
        stats["skipped_without_public_post"] += 1
        return None, stats

    requester = conversation.get("requester")
    if not isinstance(requester, dict):
        requester = users.get(
            conversation.get("requester_id") or messages[0]["author"]["external_id"]
        )
    case_id = _id(conversation["id"])
    converted = {
        "version": FORMAT_VERSION,
        "source": SOURCE,
        "account": account,
        "external_id": case_id,
        # Kundo shows it as the ticket number, so agents can find a case by
        # its Kayako id.
        "case_number": case_id,
        "title": (conversation.get("subject") or "").strip() or "(No subject)",
        "created_at": created_at,
        "requester": _convert_user(requester),
        # The export has no status and no tags.
        "is_closed": True,
        "tags": [],
        "messages": messages,
        "notes": notes,
    }
    return converted, stats


# ---------------------------------------------------------------------------
# Writing the output files
# ---------------------------------------------------------------------------


class _OutputFiles:
    """Writes the conversations, and starts a new part file when the current
    one holds `max_per_file` conversations."""

    def __init__(
        self,
        stack: ExitStack,
        output_dir: Path,
        name: str,
        max_per_file: int | None,
        input_path: Path,
    ) -> None:
        self._stack = stack
        self._output_dir = output_dir
        self._name = name
        self._max_per_file = max_per_file
        self._input_path = input_path.resolve()
        self.rows: list[dict] = []
        self._fh: IO[str] | None = None

    def _open_next_part(self) -> IO[str]:
        part = len(self.rows) + 1
        suffix = "" if part == 1 else f"-part{part}"
        path = self._output_dir / f"{self._name}{suffix}.ndjson"
        if path.resolve() == self._input_path:
            raise ExportError(f"{path} is the export file; use another --output-dir")
        self.rows.append(
            {"filename": path.name, "conversations": 0, "messages": 0, "notes": 0}
        )
        return self._stack.enter_context(open(path, "w", encoding="utf-8"))

    def write(self, conversation: dict) -> None:
        if self._fh is None or (
            self._max_per_file is not None
            and self.rows[-1]["conversations"] >= self._max_per_file
        ):
            if self._fh is not None:
                self._fh.close()
            self._fh = self._open_next_part()
        self._fh.write(json.dumps(conversation, ensure_ascii=False) + "\n")
        row = self.rows[-1]
        row["conversations"] += 1
        row["messages"] += len(conversation["messages"])
        row["notes"] += len(conversation["notes"])


def convert_export(
    file_path: str,
    output_dir: Path,
    *,
    account: str = DEFAULT_ACCOUNT,
    limits: Limits = IMPORT_LIMITS,
) -> Summary:
    """Writes `kayako-<account>.ndjson` (and part files when needed) into
    `output_dir`."""
    output_dir.mkdir(parents=True, exist_ok=True)
    users = _Users()
    stats: Counter = Counter()
    seen_message_ids: set[str] = set()

    with ExitStack() as stack:
        output = _OutputFiles(
            stack,
            output_dir,
            f"{SOURCE}-{account}",
            limits.max_conversations_per_file,
            Path(file_path),
        )
        for raw in _iter_raw_conversations(file_path):
            users.add_from(raw)
            conversation, conversation_stats = _convert_conversation(
                raw, users, account, limits, seen_message_ids
            )
            stats += conversation_stats
            if conversation is not None:
                output.write(conversation)

    return Summary(files=output.rows, **stats)


def format_summary(summary: Summary, output_dir: Path) -> str:
    lines = [f"Wrote {len(summary.files)} file(s) to {output_dir}/:"]
    lines += [
        f"  {row['filename']}: {row['conversations']} conversation(s)"
        for row in summary.files
    ]
    optional_lines = [
        (
            summary.skipped_without_posts,
            "Skipped {} conversation(s) with no posts.",
        ),
        (
            summary.skipped_without_public_post,
            "Skipped {} conversation(s) with no public post.",
        ),
        (
            summary.skipped_with_unreadable_created_at,
            "Skipped {} conversation(s) with a created_at that is not a timestamp.",
        ),
        (
            summary.dropped_posts_with_unreadable_created_at,
            "Dropped {} post(s) with a created_at that is not a timestamp.",
        ),
        (
            summary.normalized_timestamps,
            "Read {} timestamp(s) without a UTC offset as UTC.",
        ),
        (
            summary.dropped_note_attachments,
            "Dropped {} attachment(s) attached to internal notes "
            "(Kundo notes cannot have attachments).",
        ),
        (
            summary.truncated_messages,
            "Shortened {} message(s) that were longer than the import allows.",
        ),
        (
            summary.dropped_attachments,
            "Dropped {} attachment(s) from messages that had more attachments "
            "than the import allows.",
        ),
        (
            summary.dropped_attachments_without_https,
            "Dropped {} attachment(s) without an HTTPS download address, which "
            "the import cannot download.",
        ),
        (
            summary.dropped_duplicate_messages,
            "Dropped {} message(s) with the same id as an earlier message.",
        ),
    ]
    lines += [text.format(count) for count, text in optional_lines if count]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _account(value: str) -> str:
    if not ACCOUNT_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "use 1-64 characters from A-Z, a-z, 0-9, '.', '_' and '-'"
        )
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a Kayako ticket export (NDJSON) into Kundo import files."
    )
    parser.add_argument("--file", required=True, help="Path to the Kayako export.")
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory to write the NDJSON files into.",
    )
    parser.add_argument(
        "--account",
        type=_account,
        help=(
            "A name that keeps conversations from different Kayako accounts "
            "apart. The default is your Kayako subdomain, found from the "
            "attachment URLs in the export, or 'default'. Use the same value "
            "each time you convert the same account."
        ),
    )
    args = parser.parse_args(argv)

    if not Path(args.file).exists():
        parser.error(f"no such file: {args.file!r}")
    account = args.account or detect_subdomain(args.file) or DEFAULT_ACCOUNT
    try:
        account = _account(account)
    except argparse.ArgumentTypeError as e:
        parser.error(f"subdomain {account!r} found in the export: {e}")

    output_dir = Path(args.output_dir)
    try:
        summary = convert_export(args.file, output_dir, account=account)
    except ExportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(format_summary(summary, output_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
