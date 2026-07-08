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
    const res = await fetch("/api/process", { method: "POST", body: formData });
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
    const outputCell = f.output_file
      ? `<a class="download-link" href="/api/download/${encodeURIComponent(f.output_file)}">${escapeHtml(f.output_file)}</a>`
      : "—";
    tr.innerHTML = `
      <td>${escapeHtml(f.filename)}</td>
      <td><span class="badge ${badgeClass}">${f.status}</span></td>
      <td class="method">${methodLabel}</td>
      <td>${f.record_count}</td>
      <td>${outputCell}</td>
      <td class="error-text">${f.error ? escapeHtml(f.error) : ""}</td>`;
    tbody.appendChild(tr);
  });

  resultsEl.appendChild(table);
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}
