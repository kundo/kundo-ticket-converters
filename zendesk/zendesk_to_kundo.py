"""
Convert a Zendesk ticket export into the Kundo import format: NDJSON, one
conversation per line.

Usage (Python 3.9 or later, no third-party packages):

    python3 zendesk_to_kundo.py --file zendesk-export.ndjson --output-dir out/

See README.md for the export file that this script reads and where to upload
the converted files.

People download and run this file as it is, so it must only use the Python
standard library and syntax that Python 3.9 accepts. The CI runs the tests on
Python 3.9 with no packages installed to make sure of both.

Kundo finds the conversations and messages that it already has by `source`
"zendesk", `account` (the Zendesk subdomain), the Zendesk ticket id (the
conversation `external_id`) and the Zendesk comment id (each message `id`). A
new import of the same tickets into the same inbox thus only adds the
messages that are not already there.

Kundo refuses a file that is over the import limits or breaks a rule of the
import format, so the output stays in them: a file with more conversations
than one file can hold continues in `<name>-part2.ndjson` and so on, a
message body that is too long is cut, a message with too many attachments
keeps as many as the limit allows, and what the import refuses (tags with a
comma, duplicate message ids, attachments without an HTTPS address) is
changed or left out.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
from collections import Counter
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO
from urllib.parse import parse_qs, urlsplit

FORMAT_VERSION = 1
SOURCE = "zendesk"
ACCOUNT_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
MAX_TAG_LENGTH = 50
CLOSED_STATUSES = {"solved", "closed"}

TRUNCATION_NOTICE = (
    "<p>[Meddelandet kortades vid importen eftersom det var för långt.]</p>"
)

_SUBDOMAIN_URL_RE = re.compile(r"https://([^./\"']+)\.zendesk\.com/")

# Zendesk's export sometimes leaves an inline image out of the attachments
# list, so the image is found from its tokenized URL in the HTML body.
_INLINE_URL_RE = re.compile(
    r"""src=["'](https://[^"']+/attachments/token/[^"']+)["']"""
)

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
    skipped_without_comments: int = 0
    skipped_without_public_comment: int = 0
    skipped_with_unreadable_created_at: int = 0
    dropped_comments_with_unreadable_created_at: int = 0
    normalized_timestamps: int = 0
    dropped_note_attachments: int = 0
    truncated_messages: int = 0
    dropped_attachments: int = 0
    dropped_attachments_without_https: int = 0
    dropped_duplicate_messages: int = 0
    changed_tags: int = 0


class ExportError(Exception):
    """The export file cannot be read."""


# ---------------------------------------------------------------------------
# Reading the export file
# ---------------------------------------------------------------------------


def _iter_raw_tickets(path: str) -> Iterator[dict]:
    with open(path, encoding="utf-8") as fh:
        for line_number, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                ticket = json.loads(line)
            except json.JSONDecodeError as e:
                raise ExportError(f"Invalid JSON on line {line_number}: {e}") from e
            error = _ticket_error(ticket)
            if error:
                raise ExportError(f"Invalid ticket on line {line_number}: {error}")
            # Zendesk sometimes puts pagination metadata into the comments.
            ticket["comments"] = [
                c
                for c in ticket.get("comments") or []
                if isinstance(c, dict) and "id" in c and "author_id" in c
            ]
            yield ticket


def _ticket_error(ticket: object) -> str | None:
    if not isinstance(ticket, dict):
        return "the line is not a JSON object"
    if not isinstance(ticket.get("id"), int):
        return "`id` must be a number"
    if not isinstance(ticket.get("created_at"), str):
        return "`created_at` is missing"
    if not isinstance(ticket.get("comments", []), list):
        return "`comments` must be a list"
    return None


def detect_subdomain(path: str) -> str | None:
    """The attachment URLs in an export contain the account's own subdomain."""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            match = _SUBDOMAIN_URL_RE.search(line)
            if match:
                return match.group(1)
    return None


class _Users:
    """Users that the export embeds anywhere in the file, by id. A comment
    can refer to its author only by id, also when the author is embedded on
    an earlier line or is not the requester, submitter or assignee."""

    def __init__(self) -> None:
        self._users: dict[str, dict] = {}

    def add_from(self, ticket: dict) -> None:
        for role in ("requester", "submitter", "assignee"):
            user = ticket.get(role)
            if isinstance(user, dict) and "id" in user:
                self._users.setdefault(str(user["id"]), user)
        for comment in ticket["comments"]:
            author = comment.get("author")
            if isinstance(author, dict):
                self._users.setdefault(str(comment["author_id"]), author)

    def get(self, user_id: object) -> dict:
        return self._users.get(str(user_id)) or {"id": user_id or 0}


# ---------------------------------------------------------------------------
# Ticket -> conversation
# ---------------------------------------------------------------------------


def _parse_timestamp(value: object) -> tuple[str | None, bool]:
    """Returns the ISO-8601 timestamp with a UTC offset and whether the
    offset was added, or None if the value is not a timestamp. The import
    refuses a timestamp without an offset; Zendesk gives UTC."""
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


def _convert_user(user: dict, subdomain: str) -> dict:
    # Earlier Zendesk imports into Kundo gave a user without an email address
    # this address. Kundo finds the user again by it.
    user_id = user.get("id") or 0
    return {
        "external_id": str(user_id),
        "email": user.get("email") or f"zendesk-user-{user_id}@{subdomain}.zendesk.com",
        "name": user.get("name") or None,
        "is_agent": user.get("role") in ("admin", "agent"),
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


def _canonical_url(url: str, subdomain: str) -> str:
    """A branded help center host answers 404 for an attachment token, but
    `<subdomain>.zendesk.com` serves it."""
    return urlsplit(url.strip())._replace(netloc=f"{subdomain}.zendesk.com").geturl()


def _is_https_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return parts.scheme == "https" and bool(parts.hostname)
    except ValueError:
        return False


def _convert_attachments(
    attachments: list, html_body: str, subdomain: str
) -> tuple[list[dict], int]:
    """
    Returns the attachments and how many were dropped because they have no
    HTTPS URL, which the import refuses.

    The list holds the attachments that Zendesk lists, and then the inline
    images that are only in the HTML body. A listed attachment is inline only
    when the body refers to its URL, because Zendesk's own `inline` flag is
    not reliable. The body and the list can refer to the same file on
    different hosts, so they are compared by canonical URL. `inline_ref` is
    the URL as it is in the body, because Kundo replaces that text with the
    URL of the imported file.
    """
    body_srcs = {
        _canonical_url(src, subdomain): src
        for src in dict.fromkeys(_INLINE_URL_RE.findall(html_body))
    }
    result: list[dict] = []
    without_https = 0
    for att in attachments:
        content_url = att.get("content_url") if isinstance(att, dict) else None
        if not isinstance(content_url, str):
            without_https += 1
            continue
        url = _canonical_url(content_url, subdomain)
        if not _is_https_url(url):
            without_https += 1
            continue
        inline_ref = None
        if att.get("inline"):
            inline_ref = content_url if content_url in html_body else body_srcs.get(url)
        result.append(
            {
                "url": url,
                "filename": att.get("file_name") or att.get("filename") or "attachment",
                "content_type": att.get("content_type") or "application/octet-stream",
                "inline_ref": inline_ref,
            }
        )
    listed = {att["url"] for att in result}
    for url, src in body_srcs.items():
        if url in listed:
            continue
        filename = parse_qs(urlsplit(url).query).get("name", ["attachment"])[0]
        result.append(
            {
                "url": url,
                "filename": filename,
                "content_type": mimetypes.guess_type(filename)[0]
                or "application/octet-stream",
                "inline_ref": src,
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


def _fit_tags(tags: list) -> tuple[list[str], int]:
    """Returns the tags and how many of them were changed. Kundo splits tags
    on commas, so the import refuses a tag with a comma; this splits it the
    same way. A tag longer than the import allows is cut."""
    fitted: list[str] = []
    changed = 0
    for tag in tags:
        if not isinstance(tag, str):
            continue
        parts = [part.strip()[:MAX_TAG_LENGTH] for part in tag.split(",")]
        parts = [part for part in parts if part]
        if parts != [tag]:
            changed += 1
        fitted.extend(part for part in parts if part not in fitted)
    return fitted, changed


def _convert_message(
    comment: dict, created_at: str, author: dict, subdomain: str, limits: Limits
) -> tuple[dict, Counter]:
    html_body, truncated = _fit_html_body(
        comment.get("html_body") or comment.get("body") or "",
        limits.max_html_body_length,
    )
    downloadable, without_https = _convert_attachments(
        comment.get("attachments") or [], html_body, subdomain
    )
    attachments, dropped = _cap_attachments(
        downloadable, limits.max_attachments_per_message
    )
    message = {
        "id": str(comment["id"]),
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


def _convert_ticket(
    ticket: dict,
    users: _Users,
    subdomain: str,
    limits: Limits,
    seen_message_ids: set[str],
) -> tuple[dict | None, Counter]:
    """
    Returns the conversation plus counts of what was changed or left out. The
    conversation is None if the ticket cannot become one; the counts tell
    why.

    The import refuses a file where two messages have the same id, also in
    different conversations, so a message with an id in `seen_message_ids` is
    left out. The ids of the messages that are kept are added to it.
    """
    stats: Counter = Counter()
    if not ticket["comments"]:
        stats["skipped_without_comments"] += 1
        return None, stats
    created_at, normalized = _parse_timestamp(ticket["created_at"])
    if created_at is None:
        stats["skipped_with_unreadable_created_at"] += 1
        return None, stats
    stats["normalized_timestamps"] += normalized

    # A comment without a timestamp gets the ticket's. Sorted, because the
    # first public comment becomes the conversation's first message.
    dated_comments = []
    for comment in ticket["comments"]:
        comment_created_at, normalized = _parse_timestamp(
            comment.get("created_at") or ticket["created_at"]
        )
        if comment_created_at is None:
            stats["dropped_comments_with_unreadable_created_at"] += 1
            continue
        stats["normalized_timestamps"] += normalized
        dated_comments.append((comment_created_at, comment))
    dated_comments.sort(key=lambda pair: datetime.fromisoformat(pair[0]))

    messages: list[dict] = []
    notes: list[dict] = []
    for comment_created_at, comment in dated_comments:
        author = _convert_user(users.get(comment["author_id"]), subdomain)
        if comment.get("public", True) is False:
            stats["dropped_note_attachments"] += len(comment.get("attachments") or [])
            notes.append(
                {
                    "id": str(comment["id"]),
                    "created_at": comment_created_at,
                    "author": author,
                    "html_body": comment.get("html_body") or comment.get("body") or "",
                }
            )
        elif str(comment["id"]) in seen_message_ids:
            stats["dropped_duplicate_messages"] += 1
        else:
            seen_message_ids.add(str(comment["id"]))
            message, message_stats = _convert_message(
                comment, comment_created_at, author, subdomain, limits
            )
            stats += message_stats
            messages.append(message)

    if not messages:
        stats["skipped_without_public_comment"] += 1
        return None, stats

    requester = ticket.get("requester")
    if not isinstance(requester, dict) or "id" not in requester:
        requester = users.get(
            ticket.get("requester_id") or messages[0]["author"]["external_id"]
        )
    tags, stats["changed_tags"] = _fit_tags(ticket.get("tags") or [])
    status = ticket.get("status")
    conversation = {
        "version": FORMAT_VERSION,
        "source": SOURCE,
        "account": subdomain,
        "external_id": str(ticket["id"]),
        "title": (ticket.get("subject") or "").strip() or "(No subject)",
        "created_at": created_at,
        "requester": _convert_user(requester, subdomain),
        # Some exports have no status. Earlier Zendesk imports into Kundo
        # closed every ticket.
        "is_closed": status is None or str(status).lower() in CLOSED_STATUSES,
        "tags": tags,
        "messages": messages,
        "notes": notes,
    }
    return conversation, stats


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
    subdomain: str,
    limits: Limits = IMPORT_LIMITS,
) -> Summary:
    """Writes `zendesk-<subdomain>.ndjson` (and part files when needed) into
    `output_dir`."""
    output_dir.mkdir(parents=True, exist_ok=True)
    users = _Users()
    stats: Counter = Counter()
    seen_message_ids: set[str] = set()

    with ExitStack() as stack:
        output = _OutputFiles(
            stack,
            output_dir,
            f"{SOURCE}-{subdomain}",
            limits.max_conversations_per_file,
            Path(file_path),
        )
        for ticket in _iter_raw_tickets(file_path):
            users.add_from(ticket)
            conversation, ticket_stats = _convert_ticket(
                ticket, users, subdomain, limits, seen_message_ids
            )
            stats += ticket_stats
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
            summary.skipped_without_comments,
            "Skipped {} ticket(s) with no comments.",
        ),
        (
            summary.skipped_without_public_comment,
            "Skipped {} ticket(s) with no public comment.",
        ),
        (
            summary.skipped_with_unreadable_created_at,
            "Skipped {} ticket(s) with a created_at that is not a timestamp.",
        ),
        (
            summary.dropped_comments_with_unreadable_created_at,
            "Dropped {} comment(s) with a created_at that is not a timestamp.",
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
        (
            summary.changed_tags,
            "Changed {} tag(s): a tag with a comma became one tag for each part, "
            f"and a tag longer than {MAX_TAG_LENGTH} characters was cut.",
        ),
    ]
    lines += [text.format(count) for count, text in optional_lines if count]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _subdomain(value: str) -> str:
    if not ACCOUNT_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "use 1-64 characters from A-Z, a-z, 0-9, '.', '_' and '-'"
        )
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a Zendesk ticket export (NDJSON) into Kundo import files."
    )
    parser.add_argument("--file", required=True, help="Path to the Zendesk export.")
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory to write the NDJSON files into.",
    )
    parser.add_argument(
        "--subdomain",
        type=_subdomain,
        help=(
            "Your Zendesk subdomain, for example 'acme' for acme.zendesk.com. "
            "Found from the attachment URLs in the export when not given. Use "
            "the same value each time you convert the same account."
        ),
    )
    args = parser.parse_args(argv)

    if not Path(args.file).exists():
        parser.error(f"no such file: {args.file!r}")
    subdomain = args.subdomain or detect_subdomain(args.file)
    if not subdomain:
        parser.error("no Zendesk subdomain found in the export; give --subdomain")
    try:
        subdomain = _subdomain(subdomain)
    except argparse.ArgumentTypeError as e:
        parser.error(f"subdomain {subdomain!r} found in the export: {e}")

    output_dir = Path(args.output_dir)
    try:
        summary = convert_export(args.file, output_dir, subdomain=subdomain)
    except ExportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(format_summary(summary, output_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
