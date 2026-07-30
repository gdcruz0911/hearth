const rootPath = new URL(".", window.location.href).pathname;
const state = { documents: [], selectedDocumentId: null };

const elements = {
  answer: document.querySelector("#answer-panel"),
  attention: document.querySelector("#attention-panel"),
  details: document.querySelector("#detail-panel"),
  documentCount: document.querySelector("#document-count"),
  documentList: document.querySelector("#document-list"),
  health: document.querySelector("#health-summary"),
  importButton: document.querySelector("#import-button"),
  question: document.querySelector("#question"),
  search: document.querySelector("#search-form"),
  status: document.querySelector("#status-message"),
};

function api(path, options = {}) {
  return fetch(`${rootPath}api/${path}`, {
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
    ...options,
  }).then(async (response) => {
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Hearth could not complete that local action.");
    return payload;
  });
}

function setStatus(message, kind = "info") {
  elements.status.textContent = message;
  elements.status.dataset.kind = kind;
}

function make(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function button(label, className, onClick) {
  const element = make("button", `button ${className || ""}`, label);
  element.type = "button";
  element.addEventListener("click", onClick);
  return element;
}

function renderHealth(health) {
  elements.health.replaceChildren();
  const stats = [
    ["Documents", health.document_count],
    ["Pages", health.page_count],
    ["Evidence chunks", health.chunk_count],
    ["OCR review", health.ocr_page_count],
  ];
  for (const [label, value] of stats) {
    const row = document.createElement("div");
    row.append(make("dt", "", label), make("dd", "", String(value)));
    elements.health.append(row);
  }
  elements.attention.replaceChildren();
  if (health.source_attention.length) {
    elements.attention.hidden = false;
    elements.attention.append(make("h3", "", "Needs attention"));
    const list = document.createElement("ul");
    for (const item of health.source_attention) {
      list.append(make("li", "", `${item.document_name}: ${item.status}`));
    }
    elements.attention.append(list);
  } else {
    elements.attention.hidden = true;
  }
}

function renderDocuments() {
  elements.documentList.replaceChildren();
  elements.documentCount.textContent = `${state.documents.length} total`;
  if (!state.documents.length) {
    elements.documentList.append(make("p", "empty-state", "No documents yet. Import one local note or PDF to begin."));
    return;
  }
  for (const item of state.documents) {
    const row = button("", "document-row", () => showDocument(item.id));
    row.setAttribute("aria-current", String(item.id === state.selectedDocumentId));
    const copy = document.createElement("span");
    copy.append(make("span", "document-name", item.name));
    copy.append(make("span", "document-meta", `${item.page_count} pages · ${item.chunk_count} chunks · ${item.ocr_page_count} OCR pages`));
    row.append(copy, make("span", "subtle", `#${item.id}`));
    elements.documentList.append(row);
  }
}

function renderAnswer(answer) {
  elements.answer.hidden = false;
  elements.answer.replaceChildren(make("p", "answer-text", answer.text));
  if (!answer.citations.length) {
    elements.answer.append(make("p", "subtle", "No evidence-bound answer was available from the imported documents."));
    return;
  }
  const list = make("div", "citation-list");
  for (const citation of answer.citations) {
    const card = make("article", "citation");
    const heading = make("div", "citation-heading");
    const source = [citation.document_name, `page ${citation.page_number}`, citation.section].filter(Boolean).join(" · ");
    heading.append(make("span", "", source), make("span", "", `chunk ${citation.chunk_id}`));
    card.append(heading, make("p", "", citation.quote));
    if (citation.extraction_method === "ocr") {
      const confidence = citation.ocr_confidence == null ? "unavailable" : citation.ocr_confidence.toFixed(2);
      card.append(make("p", "ocr-note", `OCR evidence - verify against the original document (confidence: ${confidence}).`));
    }
    list.append(card);
  }
  elements.answer.append(list);
}

function pageCard(page) {
  const card = make("article", "page-card");
  const metadata = [page.section || "No section", page.extraction_method, `${page.chunks.length} chunks`].join(" · ");
  card.append(make("h3", "", `Page ${page.page_number}`), make("p", "", metadata));
  if (page.extraction_method === "ocr") {
    const confidence = page.ocr_confidence == null ? "unavailable" : page.ocr_confidence.toFixed(2);
    card.append(make("p", "ocr-note", `OCR warning - verify against the original document (confidence: ${confidence}).`));
  }
  return card;
}

function renderPreview(preview) {
  const card = make("section", "preview-card");
  card.append(make("h3", "", `Preview ${preview.action}`), make("p", "", preview.message));
  if (preview.source_path) {
    card.append(make("p", "preview-path", `Source: ${preview.source_path}`));
    card.append(make("p", "preview-path", `Target: ${preview.target_path}`));
  }
  if (preview.previous_source_path) {
    card.append(make("p", "preview-path", `Unavailable source: ${preview.previous_source_path}`));
    card.append(make("p", "preview-path", `Replacement: ${preview.replacement_source_path}`));
  }
  const actions = make("div", "preview-actions");
  actions.append(button("Apply this preview", "button-primary", async () => {
    try {
      setStatus("Applying the reviewed local action.");
      const result = await api(`previews/${preview.id}/apply`, { method: "POST" });
      setStatus(result.applied.message || "The local action was applied.");
      await refresh();
      if (state.selectedDocumentId && result.applied.action !== "remove") await showDocument(state.selectedDocumentId);
      if (result.applied.action === "remove") elements.details.replaceChildren(make("p", "empty-state", "The local collection record was removed. The source file remains in place."));
    } catch (error) {
      setStatus(error.message, "error");
    }
  }));
  actions.append(button("Keep collection unchanged", "", () => card.remove()));
  card.append(actions);
  return card;
}

function addAction(detail, label, note, callback, className = "") {
  const row = make("div", "action-row");
  const copy = document.createElement("div");
  copy.append(make("h3", "", label), make("p", "action-note", note));
  row.append(copy, button("Preview", className, callback));
  detail.append(row);
}

function actionPreview(documentId, action, payload = {}) {
  return api(`documents/${documentId}/actions/${action}/preview`, {
    method: "POST",
    body: JSON.stringify(payload),
  }).then((result) => result.preview);
}

function renderDetail(inspection) {
  const detail = elements.details;
  detail.replaceChildren();
  const header = make("div", "detail-header");
  const copy = document.createElement("div");
  copy.append(make("p", "eyebrow", "Document provenance"), make("h2", "", inspection.document.name));
  header.append(copy, make("span", "count", `#${inspection.document.id}`));
  detail.append(header, make("p", "subtle", `${inspection.document.page_count} pages · ${inspection.document.chunk_count} chunks · ${inspection.document.ocr_page_count} OCR pages`));
  const pages = make("div", "page-list");
  inspection.pages.forEach((page) => pages.append(pageCard(page)));
  detail.append(pages);
  const actions = make("section", "actions");
  actions.append(make("p", "eyebrow", "Explicit local actions"));

  const renameRow = make("div", "action-row");
  const renameLabel = make("label", "", "Rename this original file");
  const renameInput = document.createElement("input");
  renameInput.placeholder = "New name with the same extension";
  renameLabel.append(renameInput);
  renameRow.append(renameLabel, button("Preview rename", "", async () => {
    try {
      const preview = await actionPreview(inspection.document.id, "organize", { operation: "rename", rename: renameInput.value });
      detail.append(renderPreview(preview));
    } catch (error) { setStatus(error.message, "error"); }
  }));
  actions.append(renameRow);

  addAction(actions, "Move this original file", "Choose one existing local folder, then review the exact source and target before moving it.", async () => {
    try { detail.append(renderPreview(await actionPreview(inspection.document.id, "organize", { operation: "move" }))); }
    catch (error) { setStatus(error.message, "error"); }
  });
  addAction(actions, "Reindex from the current source", "Refresh derived evidence from the source file after you have reviewed a change.", async () => {
    try { detail.append(renderPreview(await actionPreview(inspection.document.id, "reindex"))); }
    catch (error) { setStatus(error.message, "error"); }
  });
  addAction(actions, "Relink an unavailable source", "Choose an exact local replacement. Hearth accepts it only when its imported fingerprint matches.", async () => {
    try { detail.append(renderPreview(await actionPreview(inspection.document.id, "relink"))); }
    catch (error) { setStatus(error.message, "error"); }
  });
  addAction(actions, "Remove this collection record", "This removes Hearth's local metadata and derived index data. It never deletes the original file.", async () => {
    try { detail.append(renderPreview(await actionPreview(inspection.document.id, "remove"))); }
    catch (error) { setStatus(error.message, "error"); }
  }, "button-danger");
  detail.append(actions);
}

async function showDocument(documentId) {
  try {
    const inspection = await api(`documents/${documentId}`);
    state.selectedDocumentId = documentId;
    renderDocuments();
    renderDetail(inspection);
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function refresh() {
  try {
    const [health, documents] = await Promise.all([api("health"), api("documents")]);
    state.documents = documents.documents;
    renderHealth(health);
    renderDocuments();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

elements.importButton.addEventListener("click", async () => {
  try {
    setStatus("Choose one local file in the macOS dialog.");
    const result = await api("import", { method: "POST" });
    setStatus(`Imported ${result.import.document.name}.`);
    await refresh();
    await showDocument(result.import.document.id);
  } catch (error) {
    setStatus(error.message, "error");
  }
});

elements.search.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    setStatus("Searching only the local collection.");
    const result = await api("search", { method: "POST", body: JSON.stringify({ question: elements.question.value }) });
    renderAnswer(result.answer);
    setStatus(result.answer.status === "supported" ? "Evidence found in the local collection." : "Hearth abstained because no evidence-bound answer was available.");
  } catch (error) {
    setStatus(error.message, "error");
  }
});

refresh().then(() => setStatus("Your local collection is ready."));
