# Office Availability Consolidator

A local tool for consolidating office-space listings from broker emails,
PDFs, spreadsheets, and Word docs into one clean master spreadsheet.

Drag files onto the page, click **Process Files**, and download a
formatted `.xlsx` with everything merged and de-duplicated.

## Setup

```
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env         # then edit .env and add your ANTHROPIC_API_KEY
```

The API key is only used as a fallback, when a file's layout doesn't match
one of the known rule-based parsers. Get a key at
[console.anthropic.com](https://console.anthropic.com/).

## Run

```
python app.py
```

Then open **http://127.0.0.1:5000** in your browser.

## How it works

1. **Read** — each uploaded file's raw text/tables are extracted based on
   its type (PDF, DOCX, XLSX/CSV, EML, HTML).
2. **Rule-based extraction first** — a handful of parsers recognize known
   layouts (currently: Knotel, MetSpace, and GPE broker emails, plus a
   generic "grid" parser for any spreadsheet/PDF whose columns already
   resemble the target schema). This is fast and free.
3. **LLM fallback** — if nothing recognizes the file, its raw text is sent
   to Claude, which is asked to return structured JSON matching the target
   schema. The response is validated before anything is written to the
   spreadsheet; a malformed or empty response is reported as a per-file
   error, not a crash.
4. **Merge** — new records are upserted into the existing `master.xlsx` (if
   one exists in this folder) by a `Building + Floor/Unit` key, so
   re-uploading an updated availability list overwrites the old row instead
   of duplicating it. Choose "Start fresh" in the UI to overwrite the whole
   sheet instead.

The results summary after processing shows, per file: how many records were
extracted, which method was used (rule-based vs LLM), and a clear error
message for anything that failed — so you can tell at a glance when a new
source needs a proper parser added to `extraction/rules/`.

## Target schema

The master spreadsheet's columns were derived from the example output
(`Kitt's Availability... .pdf`) — see `extraction/schema.py`. The two
"Contact" columns are a naming assumption (the source PDF had a single
merged header, "Please reach out to the team assigned to this space", over
two name columns with no sub-header text of their own).

## Adding a new source

If a new sender's emails consistently fail to a rule and always land on
the LLM fallback, it's worth adding a dedicated parser: drop a new module
in `extraction/rules/` following the pattern in `knotel.py`/`metspace.py`/
`gpe.py` (a `detect(content)` and `parse(content)` function), then register
it in `extraction/rules/__init__.py`.

## Tests

```
python tests\test_examples.py
```

Runs the three example broker emails and the example PDF through the
rule-based parsers and checks the record counts — a quick regression check
that nothing has silently broken.

## Notes

- Runs entirely on localhost. The only outbound network call is to the
  Anthropic API, and only for files that need the LLM fallback.
- `master.xlsx` and `.env` are gitignored — they're local, working-copy
  data/secrets, not something to version.
