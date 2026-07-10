const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const fileListEl = document.getElementById("fileList");
const processBtn = document.getElementById("processBtn");
const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");

let selectedFiles = [];

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropzone.classList.add("dragover");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  addFiles(e.dataTransfer.files);
});
fileInput.addEventListener("change", () => {
  addFiles(fileInput.files);
  fileInput.value = "";
});

function addFiles(fileListLike) {
  for (const f of fileListLike) {
    if (!selectedFiles.some((existing) => existing.name === f.name && existing.size === f.size)) {
      selectedFiles.push(f);
    }
  }
  renderFileList();
}

function removeFile(index) {
  selectedFiles.splice(index, 1);
  renderFileList();
}

function renderFileList() {
  fileListEl.innerHTML = "";
  selectedFiles.forEach((f, i) => {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = `${f.name} (${(f.size / 1024).toFixed(1)} KB)`;
    const removeBtn = document.createElement("button");
    removeBtn.textContent = "×";
    removeBtn.title = "Remove";
    removeBtn.addEventListener("click", () => removeFile(i));
    li.appendChild(name);
    li.appendChild(removeBtn);
    fileListEl.appendChild(li);
  });
  processBtn.disabled = selectedFiles.length === 0;
}

processBtn.addEventListener("click", async () => {
  if (!selectedFiles.length) return;

  const formData = new FormData();
  selectedFiles.forEach((f) => formData.append("files", f));

  processBtn.disabled = true;
  statusEl.textContent = `Processing ${selectedFiles.length} file(s)…`;
  resultsEl.innerHTML = "";

  try {
    const res = await fetch("/api/process", {
      method: "POST",
      body: formData,
      headers: { "X-Access-Token": window.ACCESS_TOKEN || "" },
    });
    const data = await res.json();
    if (!res.ok) {
      statusEl.textContent = `Error: ${data.error || "processing failed"}`;
      return;
    }
    statusEl.textContent = "Done.";
    renderResults(data);
    selectedFiles = [];
    renderFileList();
  } catch (err) {
    statusEl.textContent = `Error: ${err.message}`;
  } finally {
    processBtn.disabled = selectedFiles.length === 0;
  }
});

function renderResults(data) {
  const okCount = data.files.filter((f) => f.status === "ok").length;
  const summary = document.createElement("div");
  summary.className = "summary-line";
  summary.textContent = `Generated ${okCount} spreadsheet(s) from ${data.files.length} file(s).`;
  resultsEl.appendChild(summary);

  const table = document.createElement("table");
  table.innerHTML = `
    <thead><tr><th>File</th><th>Status</th><th>Method</th><th>Records</th><th>Output</th><th>Notes</th></tr></thead>
    <tbody></tbody>`;
  const tbody = table.querySelector("tbody");

  data.files.forEach((f) => {
    const tr = document.createElement("tr");
    const badgeClass = f.status === "ok" ? "ok" : "error";
    const methodLabel = f.method
      ? f.method.startsWith("rule:")
        ? `Rule (${f.method.slice(5)})`
        : "LLM fallback"
      : "—";
    const downloadUrl =
      `/api/download/${encodeURIComponent(data.batch_id)}/${encodeURIComponent(f.output_file)}` +
      `?token=${encodeURIComponent(window.ACCESS_TOKEN || "")}`;
    const outputCell = f.output_file
      ? `<a class="download-link" href="${downloadUrl}">${escapeHtml(f.output_file)}</a>`
      : "—";
    // A file can be "ok" and still carry warnings (extraction.sanity_checks —
    // a field that came back blank for every row in this file, a likely
    // missed extraction path rather than a genuine per-row gap) — shown
    // alongside any hard error, styled distinctly so it doesn't read as a
    // failure.
    const notesParts = [];
    if (f.error) notesParts.push(`<span class="error-text">${escapeHtml(f.error)}</span>`);
    if (f.warnings && f.warnings.length) {
      notesParts.push(
        f.warnings.map((w) => `<div class="warning-text">${escapeHtml(w)}</div>`).join("")
      );
    }
    tr.innerHTML = `
      <td>${escapeHtml(f.filename)}</td>
      <td><span class="badge ${badgeClass}">${f.status}</span></td>
      <td class="method">${methodLabel}</td>
      <td>${f.record_count}</td>
      <td>${outputCell}</td>
      <td>${notesParts.join("")}</td>`;
    tbody.appendChild(tr);
  });

  resultsEl.appendChild(table);
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}
