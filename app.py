import shutil
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file

load_dotenv()

from extraction.naming import make_unique_names
from extraction.pipeline import process_files
from spreadsheet import write_xlsx

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".xls", ".csv", ".eml", ".html", ".htm"}

app = Flask(__name__, static_folder="static", static_url_path="/static")


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/process", methods=["POST"])
def process():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    tmpdir = Path(tempfile.mkdtemp(prefix="office-avail-"))
    try:
        saved_paths = []
        results = []
        for f in files:
            ext = Path(f.filename).suffix.lower()
            if ext not in ALLOWED_EXTENSIONS:
                results.append(
                    {"filename": f.filename, "status": "error", "method": None, "record_count": 0, "error": f"Unsupported file type '{ext}'"}
                )
                continue
            dest = tmpdir / f.filename
            f.save(dest)
            saved_paths.append(dest)

        results = process_files(saved_paths) + results

        # Each processing run's outputs are independent — clear anything
        # left over from a previous run so download links always match
        # what's shown in the current results.
        if OUTPUT_DIR.exists():
            shutil.rmtree(OUTPUT_DIR)
        OUTPUT_DIR.mkdir(parents=True)

        ok_results = [r for r in results if r["status"] == "ok"]
        unique_names = make_unique_names([(r["provider_name"], r["date"]) for r in ok_results])
        for r, name in zip(ok_results, unique_names):
            r["output_file"] = f"{name}.xlsx"
            write_xlsx(OUTPUT_DIR / r["output_file"], r["records"], sheet_title=name)

        response_files = [
            {
                "filename": r["filename"],
                "status": r["status"],
                "method": r["method"],
                "record_count": r["record_count"],
                "error": r["error"],
                "output_file": r.get("output_file"),
            }
            for r in results
        ]
        return jsonify({"files": response_files})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.route("/api/download/<path:filename>")
def download(filename):
    safe_name = Path(filename).name  # strip any path components
    file_path = (OUTPUT_DIR / safe_name).resolve()
    if OUTPUT_DIR.resolve() not in file_path.parents or not file_path.exists():
        return jsonify({"error": "File not found"}), 404
    return send_file(file_path, as_attachment=True, download_name=safe_name)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
