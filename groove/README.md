# Groove to Kundo

`groove_to_kundo.py` converts a Groove ticket export into files for the import
in Kundo. It needs Python 3.9 or later and no other packages.

## 1. Get the export from Groove

In Groove, go to **Settings → Company → More → Exports** and request a new
export. Groove sends you an email with a download link when the export is
ready. For a large account this can take some time.

The export is a `.json` or `.json.gz` file. You do not have to unpack a
`.json.gz` file.

## 2. Run the converter

```bash
python3 groove_to_kundo.py --file groove-ticket-export-....json.gz --output-dir converted
```

The script writes one `.ndjson` file for each Groove mailbox into the
`converted` folder. The file name is the name and the id of the mailbox, for
example `support-1001.ndjson`. The script also writes `manifest.json`, which
shows the mailbox, the number of conversations, messages and notes in each
file.

| Option | What it does |
|---|---|
| `--file` | The Groove export. Required. |
| `--output-dir` | The folder for the converted files. Required. |
| `--force-closed` | Import all conversations as closed, whatever their state in Groove. |
| `--account` | A name that keeps conversations from different Groove accounts apart. Use the default (`default`) unless you import more than one Groove account into the same Kundo organization. |

## 3. Upload the files to Kundo

In Kundo, open the settings of the inbox that a mailbox goes to and select
**Import Tickets**. Upload the file of that mailbox there. Upload each mailbox
to its own inbox.

If you import the same tickets again into the same inbox, Kundo adds only the
messages that it does not already have. If you import them into a different
inbox, you get duplicates.

## How the converter reads the export

| In Groove | In Kundo |
|---|---|
| A ticket with at least one public message | A conversation |
| A ticket with only internal notes | Not imported |
| A message | A message in the conversation |
| A message to a third party, or a bounce | A message in the conversation |
| An internal note | An internal note |
| A deleted message | Not imported |
| State "closed" | A closed conversation |
| Tags | Tags. Kundo does not allow commas in a tag, so a tag with commas becomes one tag for each part. |
| A customer without an email address | A placeholder address, `groove-user-<id>@groove.invalid` |
| A message with the same id as an earlier message | Not imported. Kundo uses the id to find messages that it already has. |

## Limits

Kundo does not accept a file that is over the import limits, so the converter
keeps the files inside them:

| Limit | What the converter does |
|---|---|
| 20 000 conversations in each file | The mailbox continues in `<name>-<id>-part2.ndjson`, `-part3` and so on. Upload all parts to the same inbox. |
| 100 000 characters in each message | The converter cuts the message and adds a note that it was shortened. |
| 20 attachments on each message | The converter keeps 20. It keeps images that show in the message text first. |
| 50 characters in each tag | The converter cuts the tag. |

Internal notes in Kundo cannot have attachments, so the converter does not
include the attachments of internal notes.

Kundo can only download an attachment from an HTTPS address, so the converter
does not include an attachment that has no HTTPS download address in the
export.

At the end, the script tells you how many tickets, messages, attachments and
tags this applied to.
