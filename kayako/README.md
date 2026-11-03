# Kayako to Kundo

`kayako_to_kundo.py` converts a Kayako ticket export into files for the import
in Kundo. It needs Python 3.9 or later and no other packages.

## 1. Make the export file

Write one JSON object for each Kayako conversation (case) on its own line in
a `.ndjson` file. See [The export file](#the-export-file) for the fields. There
are two ways to get the data:

- **A full backup from Kayako.** Ask Kayako for a full data dump of your
  instance. Kayako asks for a paid invoice, your billing and mailing address
  and the credentials of the account owner. Then change the dump into the
  export file.
- **The Kayako REST API.** Write a script that reads the conversations from
  the [Kayako API](https://developer.kayako.com/) and writes the export file.

| Item | Value |
|---|---|
| Base URL | `https://{subdomain}.kayako.com/api/v1` |
| Authentication | HTTP Basic: email and password, or email and API token |
| Pagination | `?limit=100&offset=N`, until `offset` is `total_count` or more |

Basic authentication does not work for an agent with two-factor
authentication. Use an agent account without two-factor authentication for
the export.

1. Get all cases: `GET /api/v1/cases.json?limit=100&offset=0`. Get the
   archived cases in a second pass with `&archived=1`.
2. Get the posts of each case:
   `GET /api/v1/cases/{case_id}/posts.json?filters=ALL`. `filters=ALL` gives
   both the public posts and the internal notes.
3. Get the users: `GET /api/v1/users/{user_id}.json`. Add the `requester` and
   `assigned_agent` of the case, and the `creator` of each post, as objects in
   the conversation.

The API puts its answers in a `data` object with linked `resources`. Change
them into the plain objects that [The export file](#the-export-file) shows.

## 2. Run the converter

```bash
python3 kayako_to_kundo.py --file kayako-export.ndjson --output-dir converted
```

The script writes `kayako-<account>.ndjson` into the `converted` folder.

| Option | What it does |
|---|---|
| `--file` | The export file. Required. |
| `--output-dir` | The folder for the converted files. Required. |
| `--account` | A name that keeps conversations from different Kayako accounts apart. The default is your Kayako subdomain, which the script finds in the attachment URLs of the export, or `default` if there are none. Use the same value each time you convert the same account. |

## 3. Upload the file to Kundo

In Kundo, open the settings of the inbox that the conversations go to and
select **Import Tickets**. Upload the file there.

If you import the same conversations again into the same inbox, Kundo adds
only the messages that it does not already have. If you import them into a
different inbox, you get duplicates.

Each conversation keeps the Kayako case id as its ticket number in Kundo, so
agents can find a case by its Kayako id. Do not change the ids in the export:
Kundo uses them to find the conversations that it already has, and changed
ids give duplicates.

## Attachments

Kundo downloads the attachments when it imports the file, from an HTTPS
address that does not need a login. The download URLs of the Kayako API need
a login, so Kundo cannot download attachments from them. If your export has
such URLs, the conversations are imported without those attachments. To
import the attachments, put them on HTTPS addresses that do not need a login,
and use those addresses in the export.

The converter does not include an attachment that has no HTTPS URL in the
export.

## The export file

One conversation on each line, as a JSON object. No list around the lines and
no commas between them. The script skips empty lines.

### Conversation

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | number | Yes | The Kayako case id. |
| `created_at` | ISO-8601 timestamp | Yes | For example `"2024-01-15T10:00:00Z"`. A timestamp without a UTC offset is read as UTC. |
| `posts` | list of posts | Yes | The conversation needs at least one public post. |
| `subject` | text | No | `"(No subject)"` if it is empty. |
| `requester` | user | No* | |
| `requester_id` | number | No* | *Without `requester` and `requester_id`, the creator of the first public post becomes the requester. |
| `assigned_agent` | user | No | Used to find the user of a post. |

The export has no status or tags, so all conversations are closed and have no
tags in Kundo.

### User

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | number | Yes | The Kayako user id. An empty string, which Kayako gives for some system users, becomes id 0. |
| `full_name` | text | No | Also accepted: `name`. |
| `emails` | list of text | No | Kundo uses the first address. |
| `role` | text | No | `agent`, `admin`, `owner` and `collaborator` are agents. Other roles, for example `user`, are customers. |

### Post

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | number | Yes | The Kayako post id. |
| `created_at` | ISO-8601 timestamp | Yes | A post without a timestamp is not imported. |
| `creator` | user | Recommended | The author of the post. |
| `creator_id` | number | No | Used when the post has no `creator`. |
| `source_channel` | text | No | `NOTE` for an internal note. Other values, for example `MAIL`, are public posts. |
| `content_html` | text | No | The HTML of the post. Also accepted: `html_body`. |
| `content_text` | text | No | Used when there is no HTML. Also accepted: `body`. |
| `attachments` | list of attachments | No | |

### Attachment

| Field | Type | Required | Notes |
|---|---|---|---|
| `url` | text | Yes | The download URL. Also accepted: `url_download`, `content_url`. |
| `name` | text | No | Also accepted: `filename`, `file_name`. |
| `type` | text | No | For example `"application/pdf"`. Also accepted: `content_type`, `mime_type`. |
| `inline` | true or false | No | `true` if the file shows in the HTML of the post. |

### Example

```json
{"id":12345,"subject":"Billing question","created_at":"2024-01-15T09:00:00Z","requester":{"id":100,"full_name":"John Customer","emails":["john@example.com"],"role":"user"},"posts":[{"id":5001,"created_at":"2024-01-15T09:00:00Z","creator":{"id":100,"full_name":"John Customer","emails":["john@example.com"],"role":"user"},"source_channel":"MAIL","content_html":"<p>Hi, I have a billing issue.</p>","attachments":[{"url":"https://files.example.com/invoice.pdf","name":"invoice.pdf","type":"application/pdf"}]},{"id":5002,"created_at":"2024-01-15T10:00:00Z","creator":{"id":200,"full_name":"Agent Alice","emails":["alice@example.com"],"role":"agent"},"source_channel":"MAIL","content_html":"<p>Thanks, I will look into this now.</p>"},{"id":5003,"created_at":"2024-01-15T10:05:00Z","creator":{"id":200,"full_name":"Agent Alice","emails":["alice@example.com"],"role":"agent"},"source_channel":"NOTE","content_html":"<p>Check the billing system for a duplicate charge.</p>"}]}
```

Post 5001 opens the conversation with a PDF attachment, 5002 is a reply from
the agent, and 5003 is an internal note.

## How the converter reads the export

| In Kayako | In Kundo |
|---|---|
| A conversation with at least one public post | A conversation |
| A conversation without public posts | Not imported |
| The first public post, by time | The first message of the conversation |
| Other public posts | Messages in the conversation, sorted by time |
| A post with `source_channel` `NOTE` | An internal note |
| A post with a `created_at` that is not a timestamp | Not imported |
| A message with the same id as an earlier message | Not imported. Kundo uses the id to find messages that it already has. |
| An attachment that is `inline` and that the HTML refers to | An attachment that shows in the message |

## Limits

Kundo does not accept a file that is over the import limits, so the converter
keeps the files inside them:

| Limit | What the converter does |
|---|---|
| 20 000 conversations in each file | The conversations continue in `kayako-<account>-part2.ndjson`, `-part3` and so on. Upload all parts to the same inbox. |
| 100 000 characters in each message | The converter cuts the message and adds a note that it was shortened. |
| 20 attachments on each message | The converter keeps 20. It keeps images that show in the message text first. |

Internal notes in Kundo cannot have attachments, so the converter does not
include the attachments of internal notes.

At the end, the script tells you how many conversations, posts and
attachments this applied to.
