import os
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, render_template, request, send_file

load_dotenv()

import storage
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
}
# Extensions a browser can render natively — served as `inline` so
# clicking Link to Brochure opens it directly in-browser instead of
# downloading. PDFs use the browser's built-in PDF viewer. .html/.htm
# covers the HTML brochure saved for .eml sources below (the email's own
# HTML body, not a raw .eml) — it opens like the original email,
# including images, since that markup already points at the sender's
# hosted image URLs. DOCX/XLSX/CSV have no reliable native in-browser
# renderer, so they're deliberately left out — normal downloads for those.
INLINE_EXTENSIONS = {".pdf", ".html", ".htm"}

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

        ok_results = [r for r in results if r["status"] == "ok"]
        if ok_results:
            batch_dir.mkdir(parents=True)
        unique_names = make_unique_names([(r["provider_name"], r["date"]) for r in ok_results])
        for r, name in zip(ok_results, unique_names):
            r["output_file"] = f"{name}.xlsx"

            # Persist the brochure artifact alongside the generated
            # spreadsheet, and point every extracted row's "Link to
            # Brochure" at it so the spreadsheet is traceable back to
            # where the data came from. Reuses the same collision-free
            # `name` the spreadsheet got, so it can't collide with another
            # source file in this batch.
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
                record["Link to Brochure"] = source_url

            write_xlsx(batch_dir / r["output_file"], r["records"], sheet_title=name)

            # Best-effort mirror to object storage (storage.upload is a
            # no-op returning False if S3_BUCKET etc. aren't configured) so
            # these download links keep working past Render's ephemeral
            # disk being wiped on redeploy/restart, and past our own
            # hourly local cleanup below — local disk stays the fast path,
            # this is just the durable fallback /api/download reaches for
            # when the local copy is already gone.
            storage.upload(f"{batch_id}/{source_filename}", batch_dir / source_filename)
            storage.upload(f"{batch_id}/{r['output_file']}", batch_dir / r["output_file"])

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
