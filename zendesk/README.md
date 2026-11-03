# Zendesk to Kundo

`zendesk_to_kundo.py` converts a Zendesk ticket export into files for the
import in Kundo. It needs Python 3.9 or later and no other packages.

## 1. Make the export file

Zendesk has no export that contains the tickets with all their comments, so
you make the export file with the
[Zendesk REST API](https://developer.zendesk.com/api-reference/). Write one
JSON object for each ticket on its own line in a `.ndjson` file. See
[The export file](#the-export-file) for the fields.

| Item | Value |
|---|---|
| Base URL | `https://{subdomain}.zendesk.com/api/v2` |
| Authentication | HTTP Basic: `{email}/token` as the user name and an API token as the password |
| Pagination | Cursor pagination: `page[size]=100`, follow `links.next` until `meta.has_more` is `false` |

Use cursor pagination. Offset pagination (`page` and `per_page`) stops after
10 000 tickets, so a larger account gets an incomplete export.

1. Get all tickets:
   `GET /api/v2/tickets?page[size]=100&sort=created_at`
2. Get the comments of each ticket:
   `GET /api/v2/tickets/{ticket_id}/comments?page[size]=100`.
   Follow the pages also here, because a ticket can have more than 100
   comments.
3. Get the users: `GET /api/v2/users/{user_id}.json`. Add the `requester`,
   `submitter` and `assignee` of the ticket, and the `author` of each comment,
   as objects in the ticket.

Add the `author` object to each comment. If you do not, an agent who is not
the requester, submitter or assignee of the ticket gets a placeholder user,
and Kundo shows the replies of that agent as replies from the customer.

## 2. Run the converter

```bash
python3 zendesk_to_kundo.py --file zendesk-export.ndjson --output-dir converted
```

The script writes `zendesk-<subdomain>.ndjson` into the `converted` folder.

| Option | What it does |
|---|---|
| `--file` | The export file. Required. |
| `--output-dir` | The folder for the converted files. Required. |
| `--subdomain` | Your Zendesk subdomain, for example `acme` for `acme.zendesk.com`. The script finds it in the attachment URLs of the export if you do not give it. Use the same value each time you convert the same account. |

## 3. Upload the file to Kundo

In Kundo, open the settings of the inbox that the tickets go to and select
**Import Tickets**. Upload the file there.

If you import the same tickets again into the same inbox, Kundo adds only the
messages that it does not already have. If you import them into a different
inbox, you get duplicates.

## Attachments

Kundo downloads the attachments when it imports the file. Zendesk attachment
URLs contain a token and do not need a login, but:

- If your Zendesk account is set to require a sign-in to download
  attachments, Kundo cannot download them. The conversations are imported
  without those attachments.
- The tokens can expire. Import the file soon after you make the export.

Kundo can only download an attachment from an HTTPS address, so the converter
does not include an attachment that has no HTTPS URL in the export.

A help center on your own domain answers 404 for an attachment token, so the
converter changes the host of each attachment URL to
`<subdomain>.zendesk.com`.

## The export file

One ticket on each line, as a JSON object. No list around the lines and no
commas between them. The script skips empty lines.

### Ticket

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | number | Yes | The Zendesk ticket id. |
| `created_at` | ISO-8601 timestamp | Yes | For example `"2024-01-15T10:00:00Z"`. A timestamp without a UTC offset is read as UTC. |
| `comments` | list of comments | Yes | The ticket needs at least one public comment. |
| `subject` | text | No | `"(No subject)"` if it is empty. |
| `status` | text | No | `solved` and `closed` become a closed conversation. A ticket without a status is closed too. |
| `tags` | list of text | No | |
| `requester_id` | number | No* | The id of the requester. |
| `requester` | user | No* | *Give `requester` or `requester_id`. Without them, the author of the first public comment becomes the requester. |
| `submitter` | user | No | Used to find the user of a comment. |
| `assignee` | user | No | Used to find the user of a comment. |

### User

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | number | Yes | The Zendesk user id. |
| `email` | text | No | Without it, the user gets the placeholder address `zendesk-user-<id>@<subdomain>.zendesk.com`. |
| `name` | text | No | |
| `role` | text | No | `end-user`, `agent` or `admin`. |

### Comment

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | number | Yes | The Zendesk comment id. |
| `author_id` | number | Yes | The user id of the author. |
| `author` | user | Recommended | See step 1. |
| `created_at` | ISO-8601 timestamp | No | Without it, the comment gets the `created_at` of the ticket. |
| `public` | true or false | No | `false` for an internal note. The default is `true`. |
| `html_body` | text | No | The HTML of the comment. Without it, the script uses `body`. |
| `attachments` | list of attachments | No | |

### Attachment

| Field | Type | Required | Notes |
|---|---|---|---|
| `content_url` | text | Yes | The download URL. |
| `file_name` | text | No | Also accepted: `filename`. |
| `content_type` | text | No | For example `"application/pdf"`. |
| `inline` | true or false | No | `true` if the file shows in the HTML of the comment. |

### Example

```json
{"id":1001,"subject":"Help with login","created_at":"2024-01-15T10:00:00Z","status":"solved","tags":["billing"],"requester":{"id":201,"name":"Customer Joe","email":"joe@example.com","role":"end-user"},"comments":[{"id":3001,"author_id":201,"html_body":"<p>I can't log in.</p>","public":true,"created_at":"2024-01-15T10:00:00Z"},{"id":3002,"author_id":202,"author":{"id":202,"name":"Agent Alice","email":"alice@example.com","role":"agent"},"html_body":"<p>Have you tried to reset your password?</p>","public":true,"created_at":"2024-01-15T11:00:00Z"},{"id":3003,"author_id":202,"author":{"id":202,"name":"Agent Alice","email":"alice@example.com","role":"agent"},"html_body":"<p>Escalate to tier 2 if it is not solved.</p>","public":false,"created_at":"2024-01-15T13:00:00Z"}]}
```

Comment 3001 opens the conversation, 3002 is a reply from the agent, and 3003
is an internal note.

## How the converter reads the export

| In Zendesk | In Kundo |
|---|---|
| A ticket with at least one public comment | A conversation |
| A ticket without public comments | Not imported |
| The first public comment, by time | The first message of the conversation |
| Other public comments | Messages in the conversation, sorted by time |
| A private comment | An internal note |
| A comment with a `created_at` that is not a timestamp | Not imported |
| An inline image that is in the HTML but not in `attachments` | An attachment that shows in the message |
| A message with the same id as an earlier message | Not imported. Kundo uses the id to find messages that it already has. |
| Tags | Tags. Kundo does not allow commas in a tag, so a tag with commas becomes one tag for each part. |

## Limits

Kundo does not accept a file that is over the import limits, so the converter
keeps the files inside them:

| Limit | What the converter does |
|---|---|
| 20 000 conversations in each file | The conversations continue in `zendesk-<subdomain>-part2.ndjson`, `-part3` and so on. Upload all parts to the same inbox. |
| 100 000 characters in each message | The converter cuts the message and adds a note that it was shortened. |
| 20 attachments on each message | The converter keeps 20. It keeps images that show in the message text first. |
| 50 characters in each tag | The converter cuts the tag. |

Internal notes in Kundo cannot have attachments, so the converter does not
include the attachments of internal notes.

At the end, the script tells you how many tickets, comments, attachments and
tags this applied to.
