# Office Availability Consolidator

A local tool for extracting office-space listings from broker emails,
PDFs, spreadsheets, and Word docs — one clean, formatted `.xlsx` per
source file.

Drag files onto the page, click **Process Files**, and download each
generated spreadsheet, named after the source it came from (e.g.
`MetSpace.xlsx`, `Knotel.xlsx`).

## Setup

```
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env         # then edit .env and add your GEMINI_API_KEY
```

The API key is only used as a fallback, when a file's layout doesn't match
one of the known rule-based parsers. Get a key from
[Google AI Studio](https://aistudio.google.com/apikey).

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
   to Gemini, which is asked to return structured JSON matching the target
   schema, plus its best guess at the sender/broker name. The response is
   validated before anything is written to a spreadsheet; a malformed or
   empty response is reported as a per-file error, not a crash.
4. **One spreadsheet per file** — each source file gets its own `.xlsx`,
   named after the identified provider:
   - Knotel/MetSpace/GPE emails → `Knotel.xlsx`, `MetSpace.xlsx`, `GPE.xlsx`
   - Anything else → the LLM's identified sender name if it's confident,
     otherwise a cleaned-up version of the original filename (e.g.
     `Kitt's Availability (External)....pdf` → `Kitts.xlsx`)
   - If two files in the same batch resolve to the same name (e.g. two
     MetSpace emails from different weeks), the second one gets a
     distinguishing suffix — the email's date if available (`MetSpace
     (2026-06-30).xlsx`), otherwise a counter (`MetSpace (2).xlsx`).

   Each processing run's outputs are independent — there's no persistent
   master file merging data across runs. Re-processing a batch overwrites
   the previous batch's generated spreadsheets.

The results summary after processing shows, per file: how many records were
extracted, which method was used (rule-based vs LLM), a download link for
its generated spreadsheet, and a clear error message for anything that
failed — so you can tell at a glance when a new source needs a proper
parser added to `extraction/rules/`.

## Target schema

Each spreadsheet's columns were derived from the example output (`Kitt's
Availability... .pdf`) — see `extraction/schema.py`. The two "Contact"
columns are a naming assumption (the source PDF had a single merged
header, "Please reach out to the team assigned to this space", over two
name columns with no sub-header text of their own).

## Adding a new source

If a new sender's emails consistently fail to a rule and always land on
the LLM fallback, it's worth adding a dedicated parser: drop a new module
in `extraction/rules/` following the pattern in `knotel.py`/`metspace.py`/
`gpe.py` (a `detect(content)` and `parse(content)` function), then register
it in `extraction/rules/__init__.py`. Doing so also means its output
spreadsheet gets a proper provider-name (e.g. `Foo.xlsx`) instead of
falling back to a filename guess.

## Tests

```
python tests\test_examples.py
```

Runs the three example broker emails and the example PDF through the
rule-based parsers and checks the record counts — a quick regression check
that nothing has silently broken.

## Notes

- Runs entirely on localhost. The only outbound network call is to the
  Gemini API, and only for files that need the LLM fallback.
- Generated spreadsheets live in `output/`, which — like `.env` — is
  gitignored: it's local, working-copy data, not something to version.
