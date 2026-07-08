import shutil
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file

load_dotenv()

from extraction.pipeline import process_files
from spreadsheet import load_master, merge_records, write_xlsx

BASE_DIR = Path(__file__).parent
MASTER_PATH = BASE_DIR / "master.xlsx"
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".xls", ".csv", ".eml", ".html", ".htm"}

app = Flask(__name__, static_folder="static", static_url_path="/static")


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/status")
def status():
    return jsonify({"master_exists": MASTER_PATH.exists()})


@app.route("/api/process", methods=["POST"])
def process():
    files = request.files.getlist("files")
    mode = request.form.get("mode", "append")  # "append" or "fresh"

    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    tmpdir = Path(tempfile.mkdtemp(prefix="office-avail-"))
    try:
        saved_paths = []
        skipped = []
        for f in files:
            ext = Path(f.filename).suffix.lower()
            if ext not in ALLOWED_EXTENSIONS:
                skipped.append({"filename": f.filename, "status": "error", "method": None, "record_count": 0, "error": f"Unsupported file type '{ext}'"})
                continue
            dest = tmpdir / f.filename
            f.save(dest)
            saved_paths.append(dest)

        all_records, file_results = process_files(saved_paths)
        file_results = skipped + file_results

        existing = [] if mode == "fresh" else load_master(MASTER_PATH)
        merged = merge_records(existing, all_records)
        write_xlsx(MASTER_PATH, merged)

        return jsonify(
            {
                "files": file_results,
                "new_records": len(all_records),
                "total_records": len(merged),
                "mode": mode,
            }
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.route("/api/download")
def download():
    if not MASTER_PATH.exists():
        return jsonify({"error": "No master spreadsheet yet — process some files first"}), 404
    return send_file(MASTER_PATH, as_attachment=True, download_name="master.xlsx")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
