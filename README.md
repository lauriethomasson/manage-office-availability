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

Then open **http://127.0.0.1:5000** in your browser — local dev runs
"open" (no access token needed) unless you've set `ACCESS_TOKEN` in your
`.env`, in which case the app lives at `http://127.0.0.1:5000/<token>`
instead (see [Deployment](#deployment-render) for why).

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
Availability... .pdf`) — see `extraction/schema.py`. The "Contacts"
column holds every identified contact for that listing/source,
comma-separated, however many there are (not capped at a fixed number) —
the source PDF had a single merged header, "Please reach out to the team
assigned to this space", over what happened to be two name columns with
no sub-header text of their own, but other sources (e.g. MetSpace) list
three.

Every spreadsheet also carries eight extra columns for Kato bulk-upload
compatibility (matching the "Loader" sheet of
`kato-disposals-loader-example-LATEST VERSION (3).xlsx`): `External Ref`,
`Assigned Agents`, `Property Address 1`, `Property Postcode`, `Lat`,
`Lng`, `For Sale`, `To Let`. These are derived, not extracted — see
`normalize_record` in `extraction/schema.py`:
- `External Ref` is always blank (assigned on Kato's side).
- `Assigned Agents` mirrors `Contacts`.
- `Property Address 1` mirrors `Building`, and `Property Postcode` is
  parsed out of it with a UK postcode regex (`extraction/address.py`) —
  left blank rather than guessed when no postcode is confidently present
  (true for all current sources, whose `Building` text is usually just a
  building/street name with no postcode).
- `For Sale`/`To Let` are hardcoded `"No"`/`"Yes"` — every current source
  is lettings, not sales.
- `Lat`/`Lng` come from geocoding `Property Address 1` (+ postcode, +
  ", London, UK" if not already implied) via OpenStreetMap Nominatim
  (`extraction/geocode.py`), called once per unique address per
  `process_files` batch. Results are cached on disk in
  `.geocode_cache.json` (gitignored) so the same building is never
  re-geocoded across runs, and requests are throttled to Nominatim's
  1/second usage policy with a descriptive `User-Agent`. A failed lookup
  (no match, or a network error) leaves `Lat`/`Lng` blank for that row and
  prints a `[geocode] ...` note to the console — it never blocks the rest
  of the batch or guesses coordinates.

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

## Deployment (Render)

The app can also run as a small hosted service for a handful of known
people, instead of only on localhost. It's set up for
[Render](https://render.com)'s free tier — a good fit here because it
deploys straight from a GitHub repo with no Docker/config authoring
required, environment variables are first-class (no secrets in the repo),
and it doesn't restrict outbound network calls (some free hosts do, which
would silently break the Gemini API fallback).

### Deploy steps

1. Push this repo to GitHub if it isn't already.
2. Create a free account at [render.com](https://render.com) (GitHub
   login is easiest).
3. **New → Web Service**, connect your GitHub account, and select this
   repo. Render will detect `render.yaml` automatically; if it doesn't,
   set these manually:
   - **Environment**: Python
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --bind 0.0.0.0:$PORT`
   - **Plan**: Free
4. Under **Environment**, add two environment variables:
   - `GEMINI_API_KEY` — your key from
     [Google AI Studio](https://aistudio.google.com/apikey)
   - `ACCESS_TOKEN` — a long random string (see below); generate one with:
     ```
     python -c "import secrets; print(secrets.token_urlsafe(16))"
     ```
5. Click **Create Web Service**. The first build takes a few minutes;
   Render gives you a URL like `https://office-availability-consolidator.onrender.com`.

### The access token

`ACCESS_TOKEN` gates the app behind an unguessable URL: the real page
lives at `https://<your-app>.onrender.com/<ACCESS_TOKEN>`, not at the
root. The root path and any wrong/missing token both return a plain 404,
so there's nothing to distinguish "wrong guess" from "page doesn't
exist." The API endpoints (`/api/process`, `/api/download/...`) require
the same token too (sent automatically by the page's own JS), so knowing
those paths without the token doesn't help.

**What this protects against:** casual discovery — search engine
crawling, opportunistic URL scanning, someone finding the Render app
name and guessing at paths.

**What this does NOT protect against:** anyone who already has the full
URL can use the app fully, indistinguishable from an authorized user —
there's no per-person login, so you can't tell people apart or revoke
just one person's access without rotating the token for everyone. The
token also isn't encrypted-at-rest anywhere special — it's a shared
secret, similar in spirit to an unlisted Google Doc link. If it leaks
(e.g. forwarded somewhere public), rotate it: change `ACCESS_TOKEN` in
Render's dashboard and share the new URL — no code change or redeploy
needed for the value itself, though Render does restart the service to
pick up the new env var.

If you outgrow this later, the natural upgrade is Flask-Login or
HTTP Basic Auth with per-person credentials, or Render's own paid
"Preview/Access Control" features.

### Multi-user behavior

Each **Process Files** click gets its own isolated batch folder
(`output/<random-batch-id>/`) and its own download links — two people
using the app at the same time (or one person running two batches back
to back) never overwrite each other's generated spreadsheets. Batch
folders older than an hour are cleaned up automatically on the next
request; Render's disk is also ephemeral and resets on every deploy/restart
regardless, so nothing here is meant to persist long-term.

### Free tier limits & costs

- **Spins down after 15 minutes of inactivity.** The next request after
  that wakes it back up, which takes roughly 30–60 seconds (the request
  will just hang/load slowly during that time — no action needed, it
  resolves itself). Fine for occasional use by a small group; annoying if
  someone's waiting on a fast response for a demo.
- **750 free instance-hours/month** across all your free services —
  effectively unlimited for a low-traffic internal tool used by a few
  people, since a spun-down service doesn't consume hours.
- **No persistent disk on the free tier** — not an issue here, since
  nothing needs to survive a restart (see above).
- **Outbound bandwidth and build minutes** are generously capped on the
  free tier; a handful of users uploading office-listing documents won't
  come close.
- **When to upgrade**: if the 30–60s wake-up delay becomes annoying (a
  paid instance stays running), or if you need more than 750 hours/month
  across free services (unlikely for this use case). Render's cheapest
  paid tier is a few dollars/month.

## Notes

- Outbound network calls: the Gemini API (only for files that need the
  LLM fallback), and OpenStreetMap Nominatim (for geocoding every
  extracted listing's address into `Lat`/`Lng` — see [Target
  schema](#target-schema)).
- Generated spreadsheets live in `output/`, which — like `.env` — is
  gitignored: it's local, working-copy data, not something to version.
- `.geocode_cache.json` (repo root, gitignored) persists geocoding
  results across runs/restarts on a local machine. On Render's free tier
  the disk is ephemeral (see [Deployment](#deployment-render)), so this
  cache resets on every deploy/restart there — it still avoids
  re-geocoding within a single running instance's lifetime.
