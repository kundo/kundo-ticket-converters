"""
Convert a Groove Cloud ticket export into the Kundo import format: NDJSON,
one conversation per line, one file per Groove mailbox, plus a manifest.json.

Usage (Python 3.9 or later, no third-party packages):

    python3 groove_to_kundo.py --file groove-ticket-export.json --output-dir out/

See README.md for how to get the export and where to upload the files.

People download and run this file as it is, so it must only use the Python
standard library and syntax that Python 3.9 accepts. The CI runs the tests on
Python 3.9 with no packages installed to make sure of both.

Kundo finds the conversations and messages that it already has by `source`
"groove", `account`, the Groove ticket id (the conversation `external_id`) and
the Groove message id (`change.id`, each message `id`). A new import of the
same tickets into the same inbox thus only adds the messages that are not
already there.

Kundo refuses a file that is over the import limits, so the output stays in
them: a mailbox with more conversations than one file can hold continues in
`<name>-part2.ndjson` and so on, a message body that is too long is cut, and
a message with too many attachments keeps as many as the limit allows.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
SOURCE = "groove"
DEFAULT_ACCOUNT = "default"
ACCOUNT_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
MAX_TAG_LENGTH = 50

TRUNCATION_NOTICE = (
    "<p>[Meddelandet kortades vid importen eftersom det var för långt.]</p>"
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
    """`files` holds one manifest.json row per file written."""

    files: list[dict] = field(default_factory=list)
    skipped_without_public_message: int = 0
    dropped_note_attachments: int = 0
    truncated_messages: int = 0
    dropped_attachments: int = 0
    dropped_attachments_without_https: int = 0
    dropped_duplicate_messages: int = 0
    changed_tags: int = 0


# ---------------------------------------------------------------------------
# Reading the export file
# ---------------------------------------------------------------------------


def _open_text(path: str) -> IO[str]:
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, encoding="utf-8")


def _is_line_delimited(path: str) -> bool:
    """
    Groove's export is a single JSON array, but formatted with one ticket per
    line (each line after the first is `,{...}`). Probe the first ticket line
    to decide whether that holds for this file, without risking a partial
    read: if the probe fails — including a single line holding the *entire*
    array, which parses fine but yields a list, not a ticket — the whole file
    is loaded as one JSON document instead (~10x more memory, but correct
    regardless of formatting).
    """
    with _open_text(path) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line in ("[", "]"):
                continue
            try:
                return isinstance(json.loads(line.strip(",")), dict)
            except json.JSONDecodeError:
                return False
    return True  # empty array; doesn't matter which path is taken


def _iter_raw_tickets(path: str) -> Iterator[dict]:
    if _is_line_delimited(path):
        with _open_text(path) as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line in ("[", "]"):
                    continue
                yield json.loads(line.strip(","))
    else:
        logger.warning(
            "%s is not one-ticket-per-line; loading it as a single JSON document",
            path,
        )
        with _open_text(path) as fh:
            yield from json.load(fh)


# ---------------------------------------------------------------------------
# Ticket -> conversation
# ---------------------------------------------------------------------------


def _slugify(value: str) -> str:
    normalized = (
        unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^A-Za-z0-9]+", "-", normalized).strip("-").lower()
    return slug or "mailbox"


def _convert_actor(actor: dict | None) -> dict:
    actor = actor or {}
    return {
        "external_id": str(actor.get("id") or "0"),
        "email": actor.get("email"),
        "name": actor.get("name"),
        "is_agent": actor.get("type") == "Agent",
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


def _convert_attachments(
    attachments: list[dict], body_html: str
) -> tuple[list[dict], int]:
    """
    Returns the attachments and how many were dropped because they have no
    HTTPS `download_url`, which the import refuses.
    Use `download_url` — Groove's `url` field redirects to a presigned S3
    URL that expires after 60 seconds and is unusable for a later import run.
    An attachment is treated as inline only when its `content_id` is actually
    referenced as `cid:{content_id}` in the body; Groove sets `content_id` on
    more attachments than are truly inline.
    """
    downloadable = [
        att for att in attachments if _is_https_url(att.get("download_url"))
    ]
    result = []
    for att in downloadable:
        content_id = att.get("content_id")
        cid_ref = f"cid:{content_id}" if content_id else None
        result.append(
            {
                "url": att["download_url"],
                "filename": att.get("file_name") or "attachment",
                "content_type": att.get("file_type") or "application/octet-stream",
                "inline_ref": cid_ref if cid_ref and cid_ref in body_html else None,
            }
        )
    return result, len(attachments) - len(downloadable)


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


def _convert_message(change: dict, limits: Limits) -> tuple[dict, Counter]:
    html_body, truncated = _fit_html_body(
        change.get("body") or "", limits.max_html_body_length
    )
    downloadable, without_https = _convert_attachments(
        change.get("attachments") or [], html_body
    )
    attachments, dropped = _cap_attachments(
        downloadable, limits.max_attachments_per_message
    )
    stats = Counter(
        truncated_messages=int(truncated),
        dropped_attachments=dropped,
        dropped_attachments_without_https=without_https,
    )
    return {"html_body": html_body, "attachments": attachments}, stats


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


def _convert_ticket(
    ticket: dict,
    account: str,
    force_closed: bool,
    limits: Limits,
    seen_message_ids: set[str],
) -> tuple[dict | None, Counter]:
    """
    Returns the conversation plus counts of what was changed or left out. The
    conversation is None if the ticket has no public message to build a
    dialog from.
    Attachments on internal notes are left out because notes in Kundo cannot
    have attachments.

    The import refuses a file where two messages have the same id, also in
    different conversations, so a message with an id in `seen_message_ids` is
    left out. The ids of the messages that are kept are added to it.
    """
    messages: list[dict] = []
    notes: list[dict] = []
    stats: Counter = Counter()

    for action in ticket.get("actions") or []:
        change = action.get("change")
        if not isinstance(change, dict) or change.get("type") != "Message":
            continue
        if change.get("deleted_at"):
            continue

        common = {
            "id": str(change["id"]),
            "created_at": action.get("created_at") or change.get("created_at"),
            "author": _convert_actor(action.get("actor")),
        }
        if change.get("note"):
            stats["dropped_note_attachments"] += len(change.get("attachments") or [])
            notes.append({**common, "html_body": change.get("body") or ""})
        elif common["id"] in seen_message_ids:
            stats["dropped_duplicate_messages"] += 1
        else:
            seen_message_ids.add(common["id"])
            content, message_stats = _convert_message(change, limits)
            stats += message_stats
            messages.append({**common, **content})

    if not messages:
        return None, stats

    # Timestamps are ISO-8601 with a fixed "Z" suffix throughout the export,
    # so lexicographic sort matches chronological order.
    messages.sort(key=lambda m: m["created_at"])
    notes.sort(key=lambda n: n["created_at"])

    tags, stats["changed_tags"] = _fit_tags(ticket.get("tags") or [])
    customer = (ticket.get("customer") or {}).get("customer") or {}
    customer_id = customer.get("id") or "unknown"
    conversation = {
        "version": FORMAT_VERSION,
        "source": SOURCE,
        "account": account,
        "external_id": str(ticket["id"]),
        "title": (ticket.get("title") or "").strip() or "(No subject)",
        "created_at": ticket["created_at"],
        "requester": {
            "external_id": str(customer_id),
            "email": customer.get("email")
            or f"groove-user-{customer_id}@groove.invalid",
            "name": customer.get("name"),
            "is_agent": False,
        },
        "is_closed": force_closed or ticket.get("state") == "closed",
        "tags": tags,
        "messages": messages,
        "notes": notes,
    }
    return conversation, stats


# ---------------------------------------------------------------------------
# Writing the output files
# ---------------------------------------------------------------------------


class _MailboxFiles:
    """Writes the conversations of one mailbox, and starts a new part file
    when the current one holds `max_per_file` conversations."""

    def __init__(
        self,
        stack: ExitStack,
        output_dir: Path,
        mailbox: str,
        mailbox_id: str,
        max_per_file: int | None,
    ) -> None:
        self._stack = stack
        self._output_dir = output_dir
        self._mailbox = mailbox
        self._mailbox_id = mailbox_id
        self._max_per_file = max_per_file
        self.rows: list[dict] = []
        self._fh = self._open_next_part()

    def _open_next_part(self) -> IO[str]:
        part = len(self.rows) + 1
        suffix = "" if part == 1 else f"-part{part}"
        filename = f"{_slugify(self._mailbox)}-{self._mailbox_id}{suffix}.ndjson"
        self.rows.append(
            {
                "mailbox": self._mailbox,
                "mailbox_id": self._mailbox_id,
                "filename": filename,
                "conversations": 0,
                "messages": 0,
                "notes": 0,
            }
        )
        return self._stack.enter_context(
            open(self._output_dir / filename, "w", encoding="utf-8")
        )

    def write(self, line: str, conversation: dict) -> None:
        if (
            self._max_per_file is not None
            and self.rows[-1]["conversations"] >= self._max_per_file
        ):
            self._fh.close()
            self._fh = self._open_next_part()
        self._fh.write(line + "\n")
        row = self.rows[-1]
        row["conversations"] += 1
        row["messages"] += len(conversation["messages"])
        row["notes"] += len(conversation["notes"])


def convert_export(
    file_path: str,
    output_dir: Path,
    *,
    account: str = DEFAULT_ACCOUNT,
    force_closed: bool = False,
    limits: Limits = IMPORT_LIMITS,
) -> Summary:
    """Writes one NDJSON file (or more parts) per mailbox into `output_dir`,
    plus manifest.json."""
    output_dir.mkdir(parents=True, exist_ok=True)
    mailboxes: dict[str, _MailboxFiles] = {}
    stats: Counter = Counter()
    seen_message_ids: set[str] = set()

    with ExitStack() as stack:
        for ticket in _iter_raw_tickets(file_path):
            mailbox_id = str(ticket.get("mailbox_id") or "unknown")
            if mailbox_id not in mailboxes:
                mailboxes[mailbox_id] = _MailboxFiles(
                    stack,
                    output_dir,
                    ticket.get("mailbox") or "unknown",
                    mailbox_id,
                    limits.max_conversations_per_file,
                )

            conversation, ticket_stats = _convert_ticket(
                ticket, account, force_closed, limits, seen_message_ids
            )
            stats += ticket_stats
            if conversation is None:
                stats["skipped_without_public_message"] += 1
                continue
            mailboxes[mailbox_id].write(
                json.dumps(conversation, ensure_ascii=False), conversation
            )

    rows = [row for files in mailboxes.values() for row in files.rows]
    with open(output_dir / "manifest.json", "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)
    return Summary(files=rows, **stats)


def format_summary(summary: Summary, output_dir: Path) -> str:
    lines = [
        f"Wrote {len(summary.files)} file(s) to {output_dir}/ (see manifest.json).",
        f"Skipped {summary.skipped_without_public_message} ticket(s) with no public message.",
        f"Dropped {summary.dropped_note_attachments} attachment(s) attached to internal "
        "notes (Kundo notes cannot have attachments).",
    ]
    if summary.truncated_messages:
        lines.append(
            f"Shortened {summary.truncated_messages} message(s) that were longer than "
            "the import allows."
        )
    if summary.dropped_attachments:
        lines.append(
            f"Dropped {summary.dropped_attachments} attachment(s) from messages that "
            "had more attachments than the import allows."
        )
    if summary.dropped_attachments_without_https:
        lines.append(
            f"Dropped {summary.dropped_attachments_without_https} attachment(s) "
            "without an HTTPS download address, which the import cannot download."
        )
    if summary.dropped_duplicate_messages:
        lines.append(
            f"Dropped {summary.dropped_duplicate_messages} message(s) with the same "
            "id as an earlier message."
        )
    if summary.changed_tags:
        lines.append(
            f"Changed {summary.changed_tags} tag(s): a tag with a comma became one "
            f"tag for each part, and a tag longer than {MAX_TAG_LENGTH} characters "
            "was cut."
        )
    return "\n".join(lines)


def _account(value: str) -> str:
    if not ACCOUNT_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "use 1-64 characters from A-Z, a-z, 0-9, '.', '_' and '-'"
        )
    return value


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Convert a Groove ticket export (.json or .json.gz) into Kundo "
            "import files, one per Groove mailbox."
        )
    )
    parser.add_argument(
        "--file", required=True, help="Path to the Groove ticket export."
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory to write the NDJSON files and manifest.json into.",
    )
    parser.add_argument(
        "--account",
        default=DEFAULT_ACCOUNT,
        type=_account,
        help=(
            "Namespace for the duplicate check. Keep the default to match "
            "earlier imports of the same Groove account."
        ),
    )
    parser.add_argument(
        "--force-closed",
        action="store_true",
        help="Import every conversation as closed, whatever its Groove state.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not Path(args.file).exists():
        parser.error(f"no such file: {args.file!r}")

    output_dir = Path(args.output_dir)
    summary = convert_export(
        args.file,
        output_dir,
        account=args.account,
        force_closed=args.force_closed,
    )
    print(format_summary(summary, output_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
