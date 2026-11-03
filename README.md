# Kundo ticket converters

Scripts that convert a ticket export from another helpdesk into files that you
can import into [Kundo](https://www.kundo.se).

| Helpdesk | Converter |
|---|---|
| Groove | [`groove/`](groove/) |
| Kayako | [`kayako/`](kayako/) |
| Zendesk | [`zendesk/`](zendesk/) |

## What you need

- Python 3.9 or later. The scripts use only the Python standard library, so
  you do not have to install any packages.
- An export of your tickets from the other helpdesk. The README of each
  converter tells you how to get it.

## How to use a converter

1. Download the converter script, for example `groove/groove_to_kundo.py`.
   You can also clone this repository or download it as a ZIP file.
2. Run the script on your export. The README of each converter shows the
   command.
3. In Kundo, open the settings of the inbox that the tickets go to and select
   **Import Tickets**. Upload the files that the script wrote.

Kundo checks each file before it imports it. If a file has errors, Kundo shows
them and imports nothing from that file.

## Attachments

Kundo downloads each attachment from the address in the file. The address
must use HTTPS and must not need a login. The converters use the attachment
addresses from the export, so the attachments must still be available from
the other helpdesk when you import the files.

## Run the tests

```bash
python3 -m unittest discover -s groove
python3 -m unittest discover -s kayako
python3 -m unittest discover -s zendesk
```

Each converter is one file that people download alone, so the converters do
not share code. A change to a rule of the import, for example a limit, must
be made in each converter.

## License

[MIT](LICENSE)
