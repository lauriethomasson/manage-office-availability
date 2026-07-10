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

5. **Link to File** — every extracted row's `Link to File` column
   is set to an absolute URL (`app.py`, `_download_url`) back to a
   source artifact saved alongside the generated spreadsheet. All rows
   from the same source file share the exact same URL, since it
   identifies the source document, not a listing. The link carries the
   access token as a query param (`?token=...`) rather than relying on
   the page's own JS header, since clicking a hyperlink in Excel opens a
   plain browser navigation with no custom headers.

   Opens directly in-browser rather than downloading, for PDF and
   `.eml` sources:
   - A PDF source is used as-is, served with `Content-Disposition: inline`
     — the browser's own PDF viewer renders it.
   - An `.eml` with an HTML body links to that HTML directly (the email's
     own HTML MIME part, already parsed by `file_readers.py`, unmodified
     — not a re-rendered conversion), served as `text/html` + `inline`.
     Opens like the original email, images included, since the markup
     already points at the sender's hosted image URLs. A plain-text-only
     `.eml` (no HTML part) falls back to linking the raw `.eml` as a
     normal attachment download.
   - `.docx`/`.xlsx`/`.xls`/`.csv` have no reliable native in-browser
     renderer, so they're left as normal attachment downloads of the
     original file.

   **Floor Plan / High Res Images** — Kitt's-style sources get these from
   their own table columns (`extraction/rules/grid.py`) and Knotel gets
   Floor Plan from its email's own "Download Floorplan" link
   (`extraction/rules/knotel.py`). For a PDF source with no rule-based
   parser (LLM fallback — e.g. BC, Crown Estate), `extraction/pdf_images.py`
   extracts the source PDF's own embedded images (excluding
   logos/banners repeated across many pages), matches a listing's
   Building name to the PDF page it came from, and links High Res Images
   to that real, extracted photo — served `inline` like a PDF, same
   access-token/fallback-storage behavior as everything else here. Left
   blank whenever a source has no embedded images at all, or a listing's
   page can't be matched — never fabricated. Floor Plan is left alone for
   these LLM-fallback PDFs (no validated way to tell a floor-plan diagram
   apart from a building photo purely from image data).

   `/api/download` always tries local disk first (fast path — the batch
   that just ran). If the local copy is gone — Render's free-tier disk is
   wiped on every redeploy/restart, and our own hourly cleanup deletes
   local batch folders regardless (see [Multi-user
   behavior](#multi-user-behavior)) — it falls back to object storage
   (`storage.py`) if configured (see [Persistent storage
   (optional)](#persistent-storage-optional)), which isn't tied to the
   instance's disk at all. Without object storage configured, the link
   really does only last for that hour/until the next redeploy —
   "where did this come from" during/soon after a processing run, not
   long-term archival. `Floor Plan`/`High Res Images` are left as plain,
   non-clickable text for now (whatever URL, if any, a source document
   itself provided) — not reliable enough yet to treat as real links.

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
`normalize_record` in `extraction/schema.py` and `process_files` in
`extraction/pipeline.py`:
- `External Ref` is `<ProviderName>_<YYYY-MM-DD>` — the same value for
  every row in a spreadsheet, since it identifies the source batch, not
  an individual listing. The date prefers the source document's own date
  (an email's `Date` header, then PDF/DOCX metadata) over processing
  time, falling back to the processing date only when neither is
  available (`extraction/naming.resolve_source_date`).
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
  1/second usage policy with a descriptive `User-Agent`.

  `Property Postcode`/`Lat`/`Lng` are required fields, so a failed direct
  lookup doesn't just give up — `extraction/pipeline._geocode_records`
  works through several fallback tiers, in order, each logged distinctly
  (`[geocode] ...`) so which tier produced a result is always traceable:
  1. A spelled-out leading building number converted to digits (e.g.
     "Thirty One Alfred Place" → "31 Alfred Place") — counts as a
     confident match (a full street address, just spelled out in words),
     not the bare-name case below.
  2. For a genuinely bare building name (no house number/street at all,
     spelled out or otherwise) — an actual web search for the building's
     real address, Gemini with Google Search grounding
     (`extraction/address_lookup.py`), with the source/provider name
     (e.g. "GPE", "MetSpace") included as disambiguating context (e.g.
     "Elsley GPE Fully Managed" instead of just "Elsley"). Not another
     Nominatim query — Nominatim can only match an address it's given,
     and this is tried *before* a bare Nominatim search specifically
     because a bare name alone isn't trustworthy (see below).
  3. Only if that finds nothing: Nominatim on just the bare building name
     + "London, UK", as a last resort — same risk as tier 2's justification.

  A bare building name (no house number/street) is inherently a weaker
  signal than a full address — not guaranteed unique, and confirmed
  empirically *twice*: BC's "Porters Place" alone (no city qualifier) once
  matched a street in Barbados, and GPE's "Elsley" alone matched a
  building in Battersea (SW11 5LL) when the real GPE-managed "Elsley" is
  in Fitzrovia (W1W 8BF) — adding "GPE Fully Managed" as search context
  fixed it. So a value from tier 2 or 3 gets `" (Not in source text)"`
  appended to `Lat`/`Lng` (and `Property Postcode`, if that was also
  backfilled from the same lookup) — visible in the spreadsheet itself,
  not just the console log. The label describes *why* it's flagged (the
  source document never stated this address at all, so it had to be
  derived some other way), not a claim that the value is wrong — most
  derived values are correct, they just couldn't be read directly from
  the source. If every tier still finds nothing, the cell shows literal
  text `"Needs manual lookup"` instead of being left blank. None of this
  ever blocks the rest of the batch.

  Note: as of testing this (2026-07), Google Search grounding returned
  429 RESOURCE_EXHAUSTED on this project's free tier for
  `gemini-3.1-flash-lite` and `gemini-2.0-flash` — only `gemini-2.5-flash`
  had free grounding quota available, so `address_lookup.py` uses that
  model specifically (the rest of this app's Gemini calls, which don't
  use grounding, use `gemini-3.1-flash-lite` and are unaffected). Worth
  re-checking if this starts failing later — Google's model/quota lineup
  for grounding shifts over time.

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
- **No persistent disk on the free tier**, and the disk resets on every
  deploy/restart even on paid tiers — this is why `Link to File`
  needs [object storage](#persistent-storage-optional) to keep working
  past that; see there if you want those links to actually last.
- **Outbound bandwidth and build minutes** are generously capped on the
  free tier; a handful of users uploading office-listing documents won't
  come close.
- **When to upgrade**: if the 30–60s wake-up delay becomes annoying (a
  paid instance stays running), or if you need more than 750 hours/month
  across free services (unlikely for this use case). Render's cheapest
  paid tier is a few dollars/month.

### Persistent storage (optional)

Without this, `Link to File` (and the generated spreadsheet's own
download link) only work for about an hour, or until the next
redeploy/restart, whichever comes first — Render's disk (free tier or
paid) doesn't survive either. `storage.py` adds an optional mirror to any
S3-compatible object store, which isn't tied to the instance's disk at
all — `/api/download` falls back to it automatically whenever the local
copy is gone.

It's entirely inert until configured: no env vars set means every
`storage` call is a no-op, and the app behaves exactly as if this feature
didn't exist (local disk only, same lifetime as before).

This app's Render deployment currently uses **Backblaze B2** (private
bucket) — free tier covers 10GB storage with no egress fees, similar in
spirit to Cloudflare R2 below. Any S3-compatible provider works the same
way through the same five env vars; pick whichever's most convenient.

**Backblaze B2** (currently in use):
1. Create a private B2 bucket, then an "Application Key" scoped to it
   (read+write) in the B2 dashboard.
2. Set these environment variables (Render dashboard → Environment, or
   your local `.env`):
   ```
   S3_BUCKET=<your-bucket-name>
   S3_ENDPOINT_URL=https://s3.<region>.backblazeb2.com
   S3_ACCESS_KEY_ID=<the Application Key's keyID>
   S3_SECRET_ACCESS_KEY=<the Application Key's own secret>
   S3_REGION=<region, e.g. us-west-002>
   ```
   B2's S3-compatible API doesn't support recent botocore versions'
   default request-checksum headers — `storage.py`'s client `Config`
   already disables those (`request_checksum_calculation` /
   `response_checksum_validation` set to `"when_required"`), which is
   required for B2 specifically (AWS S3 and R2 don't need it, but it's
   harmless for them too).

**Cloudflare R2** (alternative — also free, no egress fees):
   ```
   S3_BUCKET=<your-bucket-name>
   S3_ENDPOINT_URL=https://<account_id>.r2.cloudflarestorage.com
   S3_ACCESS_KEY_ID=<from an R2 API token scoped to that bucket>
   S3_SECRET_ACCESS_KEY=<from the same token>
   S3_REGION=auto
   ```

**AWS S3** (alternative — small per-GB cost) — same env vars, but omit
`S3_ENDPOINT_URL` (boto3 uses AWS's own regional endpoint) and set
`S3_REGION` to a real AWS region (e.g. `eu-west-2`).

Whichever you pick, nothing else in the app changes — same `Link to
Brochure` URLs, same access-token gating (the bucket itself stays
private; only this app's own credentials can read/write it).

Verified end-to-end against the real B2 bucket in production: uploaded a
file, restarted the service (wiping its local disk), and the exact same
download link still returned the original file afterward — byte-for-byte
identical, correct `Content-Type`/`Content-Disposition` — served from B2.

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
