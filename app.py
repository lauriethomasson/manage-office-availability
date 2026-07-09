import os
import secrets
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, render_template, request, send_file

load_dotenv()

from extraction.naming import make_unique_names
from extraction.pipeline import process_files
from spreadsheet import write_xlsx

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".xls", ".csv", ".eml", ".html", ".htm"}
BATCH_MAX_AGE_SECONDS = 60 * 60  # clean up old batch output dirs after an hour

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

            # Persist a copy of the original source file alongside the
            # generated spreadsheet (as-is — an .eml stays a raw .eml, no
            # conversion), and point every extracted row's "Link to
            # Brochure" at it so the spreadsheet is traceable back to
            # where the data came from. Reuses the same collision-free
            # `name` the spreadsheet got, so it can't collide with another
            # source file in this batch.
            source_path = r["_source_path"]
            source_filename = f"{name}{source_path.suffix.lower()}"
            shutil.copy2(source_path, batch_dir / source_filename)
            r["source_file"] = source_filename
            source_url = _download_url(batch_id, source_filename)
            for record in r["records"]:
                record["Link to Brochure"] = source_url

            write_xlsx(batch_dir / r["output_file"], r["records"], sheet_title=name)

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
    if OUTPUT_DIR.resolve() not in file_path.parents or not file_path.exists():
        return jsonify({"error": "File not found"}), 404
    return send_file(file_path, as_attachment=True, download_name=safe_name)


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
