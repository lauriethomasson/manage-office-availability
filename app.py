import hashlib
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, render_template, request, send_file

load_dotenv()

import storage
from extraction import pdf_images
from extraction.naming import make_unique_names
from extraction.pipeline import process_files
from spreadsheet import write_xlsx

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".xls", ".csv", ".eml", ".html", ".htm"}
BATCH_MAX_AGE_SECONDS = 60 * 60  # clean up old batch output dirs after an hour

# Explicit Content-Type per extension for /api/download, rather than
# relying on send_file's default (Python's mimetypes module, which is
# backed by the OS's own registry/mime.types and is NOT consistent across
# platforms — e.g. .eml resolves to message/rfc822 via the Windows registry
# on a dev machine, but a bare Linux container like Render's often has no
# entry for it at all and falls back to application/octet-stream). A
# browser treating a download as unrecognized/unconfirmed rather than a
# normal, openable file is exactly the kind of symptom that mismatch causes.
CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
    ".eml": "message/rfc822",
    ".html": "text/html",
    ".htm": "text/html",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}
# Extensions a browser can render natively — served as `inline` so
# clicking Link to File opens it directly in-browser instead of
# downloading. PDFs use the browser's built-in PDF viewer. .html/.htm
# covers the HTML brochure saved for .eml sources below (the email's own
# HTML body, not a raw .eml) — it opens like the original email,
# including images, since that markup already points at the sender's
# hosted image URLs. Images (Floor Plan/High Res Images, extracted from a
# source PDF by extraction.pdf_images) should open directly too, same as
# a PDF, rather than force a download. DOCX/XLSX/CSV have no reliable
# native in-browser renderer, so they're deliberately left out — normal
# downloads for those.
INLINE_EXTENSIONS = {".pdf", ".html", ".htm", ".jpg", ".jpeg", ".png"}

# Set in the hosting platform's environment variables (never committed). If
# unset, the app runs "open" with no path/token gating — fine for local dev,
# but you MUST set this before deploying anywhere reachable by other people.
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB per request


def _token_ok(supplied):
    if not ACCESS_TOKEN:
        return True
    return bool(supplied) and secrets.compare_digest(str(supplied), ACCESS_TOKEN)


@app.before_request
def _guard():
    # The landing page lives at /<token>, not /, so root and any wrong
    # guess 404 identically — nothing here confirms whether a token is
    # "close" to correct. Static assets (JS/CSS, no user data) stay open,
    # since the page can't even load them before it has the token otherwise.
    if request.path.startswith("/static/"):
        return
    if request.path.startswith("/api/"):
        supplied = request.headers.get("X-Access-Token") or request.args.get("token")
        if not _token_ok(supplied):
            abort(404)
        return
    if request.path == "/":
        abort(404)
    # else: the /<token> route itself checks the token and 404s there


@app.route("/<token>")
def index(token):
    if not _token_ok(token):
        abort(404)
    return render_template("index.html", access_token=ACCESS_TOKEN)


@app.route("/api/version")
def version():
    """So "is the fix actually deployed" can be answered directly instead
    of inferred from push timing — Render sets RENDER_GIT_COMMIT
    automatically on deployed services; falls back to asking git directly
    for local dev, where that env var isn't set."""
    commit = os.environ.get("RENDER_GIT_COMMIT")
    if not commit:
        try:
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=BASE_DIR, text=True, timeout=5).strip()
        except Exception:
            commit = "unknown"
    return jsonify({"commit": commit, "commit_short": commit[:7]})


@app.route("/api/cache/invalidate", methods=["GET", "POST"])
def cache_invalidate():
    """Removes cached geocode/address-lookup entries by building-name
    substring — the fix for the exact pain this app has hit repeatedly: a
    fix to geocoding/address-lookup logic can be completely correct and
    still keep producing the old wrong output, because the stale answer
    is served from cache before the new code ever runs. Before this
    existed, clearing a poisoned entry meant hand-editing the on-disk
    cache file directly, then separately editing the B2/S3-mirrored copy
    (via its own dashboard) so a Render redeploy didn't just pull the
    stale copy right back down, and finally restarting the Render service
    so the currently-running worker's own in-memory copy (loaded once,
    on first use, and never re-read from disk/S3 afterward) picked up
    the change at all. Calling this endpoint on the live app does all
    three at once, including the in-memory piece specifically *because*
    it runs inside that same worker process — no redeploy/restart
    needed. (That last part relies on this app running as a single
    gunicorn worker, per Procfile/render.yaml; with multiple workers a
    request here would only clear the one worker that happened to handle
    it.)

    GET or POST, query string or form field: ?building=<substring>
    (case-insensitive, matched against both caches' keys — geocode's
    "<address>, london, uk" and address_lookup's "<building>|<provider>").
    Add &dry_run=1 to preview what would be removed without changing
    anything, e.g. to sanity-check a substring isn't broader than
    intended before actually deleting."""
    building = (request.values.get("building") or "").strip()
    if not building:
        return jsonify({"error": "missing required 'building' parameter"}), 400
    dry_run = request.values.get("dry_run", "").lower() in ("1", "true", "yes")

    from extraction import address_lookup, geocode as geocode_module

    if dry_run:
        needle = building.lower()
        geo_cache = geocode_module._load_cache()
        addr_cache = address_lookup._load_cache()
        geo_matches = [k for k in geo_cache if needle in k]
        addr_matches = [k for k in addr_cache if needle in k]
    else:
        geo_matches = geocode_module.invalidate(building)
        addr_matches = address_lookup.invalidate(building)

    return jsonify(
        {
            "building": building,
            "dry_run": dry_run,
            "geocode_cache": geo_matches,
            "address_lookup_cache": addr_matches,
            "total_matched": len(geo_matches) + len(addr_matches),
        }
    )


@app.route("/api/process", methods=["POST"])
def process():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    _cleanup_old_batches()
    batch_id = uuid.uuid4().hex
    batch_dir = OUTPUT_DIR / batch_id

    tmpdir = Path(tempfile.mkdtemp(prefix="office-avail-"))
    try:
        saved_paths = []
        unsupported_results = []
        for f in files:
            ext = Path(f.filename).suffix.lower()
            if ext not in ALLOWED_EXTENSIONS:
                unsupported_results.append(
                    {"filename": f.filename, "status": "error", "method": None, "record_count": 0, "error": f"Unsupported file type '{ext}'"}
                )
                continue
            dest = tmpdir / f.filename
            f.save(dest)
            saved_paths.append(dest)

        processed_results = process_files(saved_paths)
        # process_files() returns exactly one result per input path, in the
        # same order — pair each back up with its saved original so the
        # "ok" ones below can copy it into the persistent batch dir (the
        # tmpdir it currently lives in is wiped in `finally`, below).
        for r, path in zip(processed_results, saved_paths):
            r["_source_path"] = path
        results = processed_results + unsupported_results

        # (storage_key, local_path) pairs, uploaded together in one
        # background thread after the response is built rather than
        # inline here — confirmed empirically that doing each upload
        # synchronously (a real network round-trip per file) adds up fast
        # once a batch needs more than a couple of them, e.g. a PDF with
        # several distinct per-listing images (see _attach_pdf_images
        # below): real 500s from gunicorn's default 30s worker timeout on
        # a 20-page/43-listing source with ~15 unique images to mirror.
        upload_jobs = []

        ok_results = [r for r in results if r["status"] == "ok"]
        if ok_results:
            batch_dir.mkdir(parents=True)
        unique_names = make_unique_names([(r["provider_name"], r["date"]) for r in ok_results])
        for r, name in zip(ok_results, unique_names):
            r["output_file"] = f"{name}.xlsx"

            # Persist the source artifact alongside the generated
            # spreadsheet, and point every extracted row's "Link to File"
            # at it so the spreadsheet is traceable back to where the data
            # came from. Reuses the same collision-free `name` the
            # spreadsheet got, so it can't collide with another source
            # file in this batch.
            #
            # An .eml with an HTML body links to that HTML directly
            # (extraction.pipeline already parsed it out, unmodified) —
            # opens in-browser like the original email, images included,
            # since the markup already points at the sender's hosted image
            # URLs. There's nothing to render or convert. Everything else
            # (PDF, DOCX, XLSX, CSV, a plain-text-only .eml) links to the
            # original uploaded file as-is.
            source_path = r["_source_path"]
            email_html = r.get("email_html")
            if email_html:
                source_filename = f"{name}.html"
                (batch_dir / source_filename).write_text(email_html, encoding="utf-8")
            else:
                source_filename = f"{name}{source_path.suffix.lower()}"
                shutil.copy2(source_path, batch_dir / source_filename)
            r["source_file"] = source_filename
            source_url = _download_url(batch_id, source_filename)
            for record in r["records"]:
                record["Link to File"] = source_url

            # Floor Plan/High Res Images for a PDF source with no
            # rule-based parser (LLM fallback) — Kitt's already gets these
            # from its own table columns (extraction.rules.grid) and
            # Knotel already gets Floor Plan from its email's own "Download
            # Floorplan" link (extraction.rules.knotel); neither goes
            # through this. Real embedded images only — genuinely blank
            # when a source PDF has none (e.g. BC) or a listing's building
            # can't be matched to a page.
            if r["method"] == "llm" and source_path.suffix.lower() == ".pdf" and r.get("pages_text"):
                upload_jobs.extend(_attach_pdf_images(r["records"], source_path, r["pages_text"], batch_dir, batch_id, name))

            # Generic, source-agnostic finishing step: any rule (not just
            # PDF ones) can stash a list of real candidate photo URLs on a
            # record as "_high_res_candidates" instead of setting High Res
            # Images directly, when it can't tell in advance whether a
            # listing has one photo or several (extraction.rules.gpe does
            # this — a building can genuinely have two distinct real
            # photos, one from a promotional blurb and one from its own
            # listing card). Turns 2+ into a small gallery page, 1 into a
            # direct link, same as the PDF path above.
            upload_jobs.extend(_finalize_high_res_images(r["records"], batch_dir, batch_id, name))

            write_xlsx(batch_dir / r["output_file"], r["records"], sheet_title=name)

            # Queued for the background thread below (storage.upload is a
            # no-op returning False if S3_BUCKET etc. aren't configured) so
            # these download links keep working past Render's ephemeral
            # disk being wiped on redeploy/restart, and past our own
            # hourly local cleanup below — local disk stays the fast path,
            # this is just the durable fallback /api/download reaches for
            # when the local copy is already gone.
            upload_jobs.append((f"{batch_id}/{source_filename}", batch_dir / source_filename))
            upload_jobs.append((f"{batch_id}/{r['output_file']}", batch_dir / r["output_file"]))

        if upload_jobs:
            threading.Thread(target=_upload_all, args=(upload_jobs,), daemon=True).start()

        response_files = [
            {
                "filename": r["filename"],
                "status": r["status"],
                "method": r["method"],
                "record_count": r["record_count"],
                "error": r["error"],
                "output_file": r.get("output_file"),
                "source_file": r.get("source_file"),
            }
            for r in results
        ]
        return jsonify({"batch_id": batch_id, "files": response_files})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _download_url(batch_id, filename):
    """Absolute URL for /api/download/<batch_id>/<filename>, usable outside
    the app's own JS fetch (e.g. an Excel hyperlink click) — so it carries
    the access token as a query param, since a browser navigating there
    directly can't send the X-Access-Token header the page's own JS uses.
    Uses X-Forwarded-Proto over request.scheme so this comes out as https
    on Render, which terminates TLS at its edge and forwards plain http."""
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    token_part = f"?token={quote(ACCESS_TOKEN)}" if ACCESS_TOKEN else ""
    return f"{scheme}://{request.host}/api/download/{quote(batch_id)}/{quote(filename)}{token_part}"


def _attach_pdf_images(records, source_path, pages_text, batch_dir, batch_id, name):
    """Fills High Res Images (and, where a real one is found, Floor Plan)
    for records whose Building can be matched to page(s) of the source PDF
    with genuine embedded images (extraction.pdf_images) — never
    fabricated, left blank when no match/no image exists. A listing can
    span several pages with several real photos each (confirmed
    empirically: BC's own single-listing brochures run up to 10 pages with
    as many as 6 images on one page) — since a spreadsheet cell can only
    hold one hyperlink, 2+ photos get a small, self-contained HTML gallery
    page instead of just the first one found; exactly 1 links directly, no
    gallery indirection needed.

    Every matched image is classified individually as a floor-plan diagram
    or a real photo — deliberately per-image, not per-page: confirmed on
    Breezblok's John Stow House brochure that a floor-plan diagram and a
    real desk photo can share the same PDF page, so an earlier per-page-
    only classification (excluding a whole "floor plan" page from the
    photo gallery, based only on that page's own text) missed this case
    entirely — the page's text never mentioned "floor plan" at all, so
    neither the page nor the diagram on it was ever excluded, and it was
    silently swept into the photo gallery instead of populating Floor
    Plan. See extraction.pdf_images.is_floorplan_page (source-labeled
    text, e.g. BC's own "Example Floorplan" heading) and
    is_floorplan_image (a pixel-content fallback for sources with no such
    label) — either signal marks an image as the floor plan rather than a
    photo. A listing with more than one floor-plan-classified image (not
    seen in any source tested) just uses the first found; there's no
    established gallery convention for Floor Plan the way there is for
    High Res Images.

    Returns the (storage_key, local_path) pairs for the caller to upload —
    doesn't upload them itself, so a source with many distinct images
    (e.g. Crown Estate's ~15) doesn't add that many synchronous network
    round-trips to this request; see the background-thread upload in
    process() above.

    Deliberately uses pdf_images.scan_pages (a cheap, hash-only pass) plus
    load_page_images (decodes one page's images at a time, on demand)
    rather than extract_page_images (which materializes every real image
    for the whole document at once) — confirmed via Render's own logs
    that processing a large PDF (Crown Estate, 4.3MB) got the worker
    SIGKILLed for exceeding the free tier's 512MB RAM limit. Bounding this
    to one page's images at a time caps how much of a large, photo-heavy
    document this function can ever hold in memory at once, regardless of
    how many pages/records it has."""
    page_hashes = pdf_images.scan_pages(source_path)
    if not page_hashes:
        return []

    jobs = []
    saved_image_urls = {}  # image content hash -> already-saved download
    # URL, so the same real image isn't re-saved/re-uploaded twice across
    # different listings/galleries/floor-plan-links that happen to include it.
    gallery_url_by_photos = {}  # tuple(photo URLs) -> gallery (or single-
    # image) URL, so 2+ listings sharing the same real photos (e.g. two
    # floors of one building) share one file instead of a duplicate.
    gallery_count = 0

    def _save(page_num, image_bytes, ext):
        h = hashlib.sha256(image_bytes).hexdigest()
        if h not in saved_image_urls:
            image_filename = f"{name}_p{page_num + 1}_{h[:8]}.{ext}"
            (batch_dir / image_filename).write_bytes(image_bytes)
            jobs.append((f"{batch_id}/{image_filename}", batch_dir / image_filename))
            saved_image_urls[h] = _download_url(batch_id, image_filename)
        return saved_image_urls[h]

    for record in records:
        building = record.get("Building")
        if len(records) == 1:
            # A single-listing brochure spanning the whole document (e.g.
            # BC's own "2-7 Clerkenwell Green" brochure) — confirmed
            # empirically the building name only recurs on some pages
            # (a generic "Location"/"Key features"/pricing page doesn't
            # repeat it), so name-searching page-by-page would undercount.
            # The entire document is unambiguously about this one listing
            # regardless of which pages happen to repeat its name, so
            # every real image in it belongs to this record — skip the
            # name search entirely rather than only falling back when it
            # finds literally nothing.
            matching_pages = sorted(page_hashes.keys())
        else:
            matching_pages = pdf_images.find_matching_pages(building, pages_text)

        photo_urls = []
        floorplan_url = None
        for p in matching_pages:
            if p not in page_hashes:
                continue
            page_is_labeled_floorplan = pdf_images.is_floorplan_page(pages_text[p] if p < len(pages_text) else "")
            # Decoded on demand, one page at a time — never more than one
            # page's own images resident at once (see the docstring above).
            for image_bytes, ext in pdf_images.load_page_images(source_path, p, page_hashes[p]):
                is_floorplan = page_is_labeled_floorplan or pdf_images.is_floorplan_image(image_bytes)
                url = _save(p, image_bytes, ext)
                if is_floorplan:
                    if floorplan_url is None:
                        floorplan_url = url
                elif url not in photo_urls:
                    photo_urls.append(url)

        if floorplan_url:
            record["Floor Plan"] = floorplan_url

        if not photo_urls:
            continue

        photos_key = tuple(photo_urls)
        if photos_key not in gallery_url_by_photos:
            if len(photo_urls) == 1:
                gallery_url_by_photos[photos_key] = photo_urls[0]
            else:
                gallery_count += 1
                gallery_filename = f"{name}_gallery{gallery_count}.html"
                gallery_html = pdf_images.build_gallery_html(building or name, photo_urls)
                (batch_dir / gallery_filename).write_text(gallery_html, encoding="utf-8")
                jobs.append((f"{batch_id}/{gallery_filename}", batch_dir / gallery_filename))
                gallery_url_by_photos[photos_key] = _download_url(batch_id, gallery_filename)

        record["High Res Images"] = gallery_url_by_photos[photos_key]

    return jobs


def _finalize_high_res_images(records, batch_dir, batch_id, name):
    """Generic counterpart to _attach_pdf_images for rules (e.g.
    extraction.rules.gpe) that can't tell in advance whether a listing has
    one real photo or several, so they stash candidate URLs on
    "_high_res_candidates" instead of setting High Res Images directly.
    Unlike the PDF case, these URLs are already externally hosted (e.g.
    GPE's own assets-gbr.mkt.dynamics.com images) — nothing to download or
    re-host, only a gallery page to build when there's more than one.

    Same page-cell constraint as the PDF path: 2+ candidates get a small
    self-contained gallery (reusing pdf_images.build_gallery_html, which is
    agnostic to whether the image URLs it embeds are ours or external),
    exactly 1 links directly. Records that share the exact same candidate
    list (e.g. every floor of one building) share one gallery file instead
    of a duplicate.

    Returns the (storage_key, local_path) pairs for the caller to upload,
    same convention as _attach_pdf_images — only the generated gallery HTML
    needs uploading here, never the candidate images themselves."""
    jobs = []
    gallery_url_by_candidates = {}
    gallery_count = 0

    for record in records:
        candidates = record.pop("_high_res_candidates", None)
        if not candidates:
            continue

        key = tuple(candidates)
        if key not in gallery_url_by_candidates:
            if len(candidates) == 1:
                gallery_url_by_candidates[key] = candidates[0]
            else:
                gallery_count += 1
                gallery_filename = f"{name}_photos{gallery_count}.html"
                gallery_html = pdf_images.build_gallery_html(record.get("Building") or name, candidates)
                (batch_dir / gallery_filename).write_text(gallery_html, encoding="utf-8")
                jobs.append((f"{batch_id}/{gallery_filename}", batch_dir / gallery_filename))
                gallery_url_by_candidates[key] = _download_url(batch_id, gallery_filename)

        record["High Res Images"] = gallery_url_by_candidates[key]

    return jobs


def _upload_all(jobs):
    """Runs in a background thread (see process() above) so /api/process
    can return as soon as local disk is ready, without waiting on however
    many real network round-trips a batch's storage.upload calls add up
    to. Best-effort per job — one failing upload (logged inside
    storage.upload itself) doesn't stop the rest."""
    for key, path in jobs:
        storage.upload(key, path)


@app.route("/api/download/<batch_id>/<path:filename>")
def download(batch_id, filename):
    safe_batch = Path(batch_id).name
    safe_name = Path(filename).name  # strip any path components
    file_path = (OUTPUT_DIR / safe_batch / safe_name).resolve()
    ext = Path(safe_name).suffix.lower()
    mimetype = CONTENT_TYPES.get(ext, "application/octet-stream")
    disposition = "inline" if ext in INLINE_EXTENSIONS else "attachment"

    if OUTPUT_DIR.resolve() in file_path.parents and file_path.exists():
        # Fast path: still on local disk (recent batch, same instance
        # that generated it).
        response = send_file(file_path, mimetype=mimetype, as_attachment=(disposition == "attachment"), download_name=safe_name)
    else:
        # Local copy is gone — either Render redeployed/restarted since
        # (wiping its ephemeral disk) or our own hourly cleanup ran.
        # Fall back to object storage, which isn't tied to this instance's
        # disk at all (storage.fetch returns None if unconfigured or the
        # object genuinely doesn't exist there either).
        data = storage.fetch(f"{safe_batch}/{safe_name}")
        if data is None:
            return jsonify({"error": "File not found"}), 404
        response = Response(data, mimetype=mimetype)

    # Set this explicitly (quoted) rather than trusting send_file's default
    # formatting alone, so the header is deterministic regardless of
    # Werkzeug version quirks — this is the header a browser actually reads
    # to recognize a completed download's real filename/extension, and
    # inline vs. attachment decides whether it opens in-browser or downloads.
    response.headers["Content-Disposition"] = f'{disposition}; filename="{safe_name}"'
    return response


def _cleanup_old_batches():
    """Each batch gets its own output subfolder so concurrent users never
    clobber each other's files (the previous version wiped one shared
    output/ dir on every run). This just prevents unbounded buildup on
    long-running instances — Render's disk is ephemeral anyway and resets
    on every deploy/restart."""
    if not OUTPUT_DIR.exists():
        return
    cutoff = time.time() - BATCH_MAX_AGE_SECONDS
    for child in OUTPUT_DIR.iterdir():
        if child.is_dir() and child.stat().st_mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_DEBUG") == "1")
