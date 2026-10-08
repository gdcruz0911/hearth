const rootPath = new URL(".", window.location.href).pathname;
// ADR-0037: the dashboard hands this page its session token in the fragment; it stays in memory and goes in a header.
const sessionToken = window.location.hash.startsWith("#session=") ? window.location.hash.slice("#session=".length) : "";
if (sessionToken) history.replaceState(null, "", window.location.pathname);
const svgNamespace = "http://www.w3.org/2000/svg";
const state = {
  documents: [],
  semanticEdges: [],
  semanticIndexStatus: "not configured",
  semanticJob: { status: "idle", completed: 0, total: 0, benchmark: null },
  sourceRoots: [],
  selectedDocumentId: null,
  selectedNeighborhoodId: null,
};

const elements = {
  answer: document.querySelector("#answer-panel"),
  attention: document.querySelector("#attention-panel"),
  details: document.querySelector("#detail-panel"),
  documentCount: document.querySelector("#document-count"),
  documentList: document.querySelector("#document-list"),
  health: document.querySelector("#health-summary"),
  importButton: document.querySelector("#import-button"),
  mapLinks: document.querySelector("#map-links"),
  mapContent: document.querySelector("#map-content"),
  mapNodes: document.querySelector("#map-nodes"),
  mapSummary: document.querySelector("#map-summary"),
  mapSurface: document.querySelector("#map-surface"),
  question: document.querySelector("#question"),
  scanSourcesButton: document.querySelector("#scan-sources-button"),
  scanSourcesPanelButton: document.querySelector("#scan-sources-panel-button"),
  search: document.querySelector("#search-form"),
  sourceRootSummary: document.querySelector("#source-root-summary"),
  status: document.querySelector("#status-message"),
};

function api(path, options = {}) {
  return fetch(`${rootPath}api/${path}`, {
    headers: { "Content-Type": "application/json", "X-Hearth-Session": sessionToken },
    cache: "no-store",
    ...options,
  }).then(async (response) => {
    const payload = await response.json();
    if (response.status === 401) throw new Error("This page's session ended. Run hearth web in your terminal to open a fresh one.");
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
    ["Sources", health.document_count],
    ["Evidence", health.chunk_count],
    ["Needs review", health.source_attention.length],
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

function renderSourceRoots() {
  if (!state.sourceRoots.length) {
    elements.sourceRootSummary.textContent = "No folders are connected. Create or update the runtime profile to choose source roots.";
    elements.scanSourcesButton.disabled = true;
    elements.scanSourcesPanelButton.disabled = true;
    return;
  }
  const ready = state.sourceRoots.filter((item) => item.status === "ready");
  const unavailable = state.sourceRoots.filter((item) => item.status !== "ready");
  elements.sourceRootSummary.textContent = `${ready.map((item) => item.name).join(", ")} connected.${unavailable.length ? ` ${unavailable.map((item) => item.name).join(", ")} unavailable.` : ""}`;
  elements.scanSourcesButton.disabled = false;
  elements.scanSourcesPanelButton.disabled = false;
}

function renderDocuments() {
  elements.documentList.replaceChildren();
  elements.documentCount.textContent = `${state.documents.length} source${state.documents.length === 1 ? "" : "s"}`;
  if (!state.documents.length) {
    elements.documentList.append(make("p", "empty-state", "No sources yet. Import one local note or PDF to kindle the first part of your map."));
    return;
  }
  for (const item of state.documents) {
    const row = button("", "document-row", () => showDocument(item.id));
    row.setAttribute("aria-current", String(item.id === state.selectedDocumentId));
    const copy = document.createElement("span");
    copy.append(make("span", "document-name", item.name));
    copy.append(make("span", "document-meta", `${item.page_count} pages · ${item.chunk_count} evidence units · ${item.ocr_page_count} OCR pages`));
    row.append(copy, make("span", "subtle", `#${item.id}`));
    elements.documentList.append(row);
  }
}

function pointOnRing(index, total, centerX, centerY, radiusX, radiusY, offset = -Math.PI / 2) {
  const angle = offset + (Math.PI * 2 * index) / Math.max(total, 1);
  return { x: centerX + Math.cos(angle) * radiusX, y: centerY + Math.sin(angle) * radiusY };
}

function documentPoint(index, documentCount, center, width, height) {
  return pointOnRing(index, documentCount, center.x, center.y, width * 0.34, height * 0.28, -Math.PI / 2);
}

function addLink(from, to, className = "") {
  const line = document.createElementNS(svgNamespace, "line");
  line.setAttribute("x1", String(from.x));
  line.setAttribute("y1", String(from.y));
  line.setAttribute("x2", String(to.x));
  line.setAttribute("y2", String(to.y));
  if (className) line.setAttribute("class", className);
  elements.mapLinks.append(line);
}

function addMapNode({ label, kind, point, size, selected, onClick, description }) {
  const node = button("", `map-node map-node-${kind}${selected ? " is-selected" : ""}`, onClick);
  node.style.setProperty("--node-size", `${size}px`);
  node.style.left = `${point.x}px`;
  node.style.top = `${point.y}px`;
  node.setAttribute("aria-label", description || label);
  node.append(make("span", "map-node-label", label));
  elements.mapNodes.append(node);
}

function semanticNeighborhoods() {
  const documentIds = new Set(state.documents.map((document) => document.id));
  const edges = state.semanticEdges.filter((edge) => documentIds.has(edge.left_document_id) && documentIds.has(edge.right_document_id));
  const parent = new Map();
  const weight = new Map();
  for (const edge of edges) {
    parent.set(edge.left_document_id, edge.left_document_id);
    parent.set(edge.right_document_id, edge.right_document_id);
    weight.set(edge.left_document_id, (weight.get(edge.left_document_id) || 0) + edge.score);
    weight.set(edge.right_document_id, (weight.get(edge.right_document_id) || 0) + edge.score);
  }
  const find = (documentId) => {
    const root = parent.get(documentId);
    if (root === documentId) return root;
    const resolved = find(root);
    parent.set(documentId, resolved);
    return resolved;
  };
  for (const edge of edges) {
    const left = find(edge.left_document_id);
    const right = find(edge.right_document_id);
    if (left !== right) parent.set(right, left);
  }
  const groups = new Map();
  for (const documentId of parent.keys()) {
    const group = groups.get(find(documentId)) || [];
    group.push(documentId);
    groups.set(find(documentId), group);
  }
  const documentsById = new Map(state.documents.map((document) => [document.id, document]));
  return [...groups.values()].map((ids) => {
    const documentIdsInGroup = ids.sort((left, right) => left - right);
    const groupEdges = edges.filter((edge) => documentIdsInGroup.includes(edge.left_document_id) && documentIdsInGroup.includes(edge.right_document_id));
    const representativeId = [...documentIdsInGroup].sort((left, right) => (weight.get(right) || 0) - (weight.get(left) || 0) || left - right)[0];
    return {
      id: `neighborhood:${documentIdsInGroup.join("-")}`,
      documentIds: documentIdsInGroup,
      edges: groupEdges,
      representative: documentsById.get(representativeId),
      score: groupEdges.reduce((total, edge) => total + edge.score, 0),
    };
  }).sort((left, right) => right.documentIds.length - left.documentIds.length || right.score - left.score || left.id.localeCompare(right.id));
}

function selectedNeighborhood() {
  return semanticNeighborhoods().find((neighborhood) => neighborhood.id === state.selectedNeighborhoodId) || null;
}

function renderSemanticSetup() {
  const detail = elements.details;
  detail.replaceChildren(make("p", "eyebrow", "Semantic map"));
  detail.append(make("h2", "", "Local semantic index"));
  if (["running", "cancelling", "cancelled", "failed"].includes(state.semanticJob.status)) {
    renderSemanticJob(detail, state.semanticJob);
    return;
  }
  if (!state.documents.length) {
    detail.append(make("p", "empty-state", "Import a few sources first. Hearth can then derive an explainable relationship map from their local evidence."));
    return;
  }
  if (state.semanticIndexStatus === "not configured") {
    detail.append(make("p", "empty-state", "Start Hearth with a local embedding model and index directory to turn imported evidence into semantic neighborhoods."));
    return;
  }
  if (state.semanticIndexStatus === "needs reindex") {
    detail.append(make("p", "empty-state", "Your imported evidence changed. Rebuild the local semantic index before using the map."));
    addAction(detail, "Rebuild the semantic map", "This creates derived vectors from the evidence already in Hearth. Original files stay untouched.", previewSemanticIndex, "button-primary");
    return;
  }
  if (state.semanticEdges.length) {
    detail.append(make("p", "empty-state", "Select a semantic neighborhood to inspect the related sources and the evidence behind each link."));
    renderSemanticBenchmark(detail, state.semanticJob);
    return;
  }
  detail.append(make("p", "empty-state", "No semantic neighborhoods cleared the current similarity threshold. Add related sources or lower the profile threshold, then rebuild the local index."));
  renderSemanticBenchmark(detail, state.semanticJob);
}

function renderSemanticJob(detail, job) {
  const completed = Math.min(job.completed || 0, job.total || 0);
  const percent = job.total ? Math.round((completed / job.total) * 100) : 0;
  if (job.status === "cancelled") {
    detail.append(make("p", "empty-state", "The local rebuild was cancelled. No incomplete index was activated, and your original files were not changed."));
    addAction(detail, "Rebuild the semantic map", "Start again when your Mac is available for a local background task.", previewSemanticIndex, "button-primary");
    return;
  }
  if (job.status === "failed") {
    detail.append(make("p", "empty-state", job.error || "The local rebuild stopped before completion. No incomplete index was activated."));
    addAction(detail, "Try the semantic rebuild again", "Hearth keeps the previous active index until a complete replacement is ready.", previewSemanticIndex, "button-primary");
    return;
  }
  const phase = job.phase || "embedding evidence";
  const activity = job.status === "cancelling"
    ? "Stopping after the current small embedding batch."
    : phase === "preparing local model"
      ? "Preparing the local embedding model. The first batch can take longer than later batches."
      : phase === "finalizing derived index"
        ? "Finalizing the derived index before it becomes active."
        : "Embedding evidence locally in the background.";
  detail.append(make("p", "subtle", activity));
  if (job.warning) detail.append(make("p", "empty-state", job.warning));
  const progress = document.createElement("progress");
  progress.className = "semantic-progress";
  progress.max = Math.max(job.total || 1, 1);
  progress.value = completed;
  progress.setAttribute("aria-label", "Semantic-index rebuild progress");
  detail.append(progress, make("p", "subtle", `${completed.toLocaleString()} of ${(job.total || 0).toLocaleString()} evidence units (${percent}%).`));
  const cancel = button(job.status === "cancelling" ? "Stopping" : "Cancel rebuild", "button-danger", cancelSemanticIndex);
  cancel.disabled = job.status === "cancelling";
  detail.append(make("p", "action-note", "Progress counts evidence units, whose processing time can vary with length. You can keep using Hearth while this runs."), cancel);
  renderSemanticBenchmark(detail, job);
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds)) return "Unavailable";
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} sec`;
  return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} sec`;
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 1) return "Unavailable";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function renderSemanticBenchmark(detail, job) {
  const benchmark = job.benchmark;
  if (!benchmark) return;
  const section = make("section", "semantic-benchmark");
  section.append(make("h3", "", "This rebuild"));
  if (job.warning && !["running", "cancelling"].includes(job.status)) section.append(make("p", "empty-state", job.warning));
  const metrics = make("dl", "benchmark-metrics");
  const rows = [
    ["Elapsed", formatDuration(benchmark.elapsed_seconds)],
    ["Hearth CPU", formatDuration(benchmark.cpu_seconds)],
    ["Peak memory", formatBytes(benchmark.peak_resident_memory_bytes)],
    ["Current rate", benchmark.evidence_units_per_minute ? `${Math.round(benchmark.evidence_units_per_minute).toLocaleString()} units/min` : "Preparing"],
  ];
  for (const [label, value] of rows) {
    const row = document.createElement("div");
    row.append(make("dt", "", label), make("dd", "", value));
    metrics.append(row);
  }
  section.append(metrics, make("p", "action-note", "These figures measure the Hearth process, not total Mac power or fan activity."));
  detail.append(section);
}

function renderNeighborhoodDetail(neighborhood) {
  const detail = elements.details;
  detail.replaceChildren();
  detail.append(make("p", "eyebrow", "Semantic neighborhood"));
  detail.append(make("h2", "", neighborhood.representative.name));
  detail.append(make("p", "subtle", `${neighborhood.documentIds.length} sources connected by ${neighborhood.edges.length} explainable local link${neighborhood.edges.length === 1 ? "" : "s"}.`));
  const list = make("div", "relationships");
  for (const documentId of neighborhood.documentIds) {
    const document = state.documents.find((item) => item.id === documentId);
    if (document) list.append(button(document.name, "relationship-button", () => showDocument(document.id)));
  }
  detail.append(list, button("Back to all neighborhoods", "button-quiet", resetMap));
}

function renderMap() {
  const width = Math.max(elements.mapSurface.clientWidth, 320);
  const height = Math.max(elements.mapSurface.clientHeight, 460);
  const center = { x: width / 2, y: height / 2 };
  const neighborhoods = semanticNeighborhoods();
  const neighborhood = selectedNeighborhood();
  if (state.selectedNeighborhoodId && !neighborhood) state.selectedNeighborhoodId = null;
  const visibleNeighborhoods = neighborhoods.slice(0, 14);
  const documentPoints = new Map();
  const neighborhoodPoints = new Map();
  const visibleDocuments = neighborhood
    ? neighborhood.documentIds.map((id) => state.documents.find((document) => document.id === id)).filter(Boolean).slice(0, 24)
    : state.selectedDocumentId === null
      ? []
      : state.documents.filter((document) => document.id === state.selectedDocumentId);
  const maxChunks = Math.max(...visibleDocuments.map((item) => item.chunk_count), 1);

  visibleNeighborhoods.forEach((item, index) => {
    neighborhoodPoints.set(item.id, pointOnRing(index, visibleNeighborhoods.length, center.x, center.y, width * 0.35, height * 0.27));
  });
  visibleDocuments.forEach((document, index) => {
    documentPoints.set(document.id, documentPoint(index, visibleDocuments.length, center, width, height));
  });

  elements.mapLinks.replaceChildren();
  elements.mapNodes.replaceChildren();
  elements.mapLinks.setAttribute("viewBox", `0 0 ${width} ${height}`);
  elements.mapLinks.setAttribute("width", String(width));
  elements.mapLinks.setAttribute("height", String(height));
  elements.mapContent.classList.toggle("is-neighborhood-focus", Boolean(neighborhood));
  elements.mapContent.classList.toggle("is-source-focus", state.selectedDocumentId !== null);

  if (neighborhood) {
    for (const edge of neighborhood.edges) {
      const left = documentPoints.get(edge.left_document_id);
      const right = documentPoints.get(edge.right_document_id);
      if (left && right) addLink(left, right, "semantic-link");
    }
  } else {
    for (const item of visibleNeighborhoods) {
      addLink(center, neighborhoodPoints.get(item.id), "neighborhood-link");
    }
  }

  addMapNode({
    label: "Hearth",
    kind: "hearth",
    point: center,
    size: 86,
    selected: state.selectedDocumentId === null && state.selectedNeighborhoodId === null,
    onClick: resetMap,
    description: "Hearth, reset the current selection",
  });
  if (!neighborhood) {
    for (const item of visibleNeighborhoods) {
      addMapNode({
        label: `${item.representative.name} +${item.documentIds.length - 1}`,
        kind: "neighborhood",
        point: neighborhoodPoints.get(item.id),
        size: Math.min(94, 56 + item.documentIds.length * 4),
        selected: item.id === state.selectedNeighborhoodId,
        onClick: () => selectNeighborhood(item.id),
        description: `${item.representative.name}, semantic neighborhood with ${item.documentIds.length} sources`,
      });
    }
  }
  for (const document of visibleDocuments) {
    const intensity = 38 + Math.round((document.chunk_count / maxChunks) * 22);
    addMapNode({
      label: document.name,
      kind: "document",
      point: documentPoints.get(document.id),
      size: intensity,
      selected: document.id === state.selectedDocumentId,
      onClick: () => showDocument(document.id),
      description: `${document.name}, ${document.chunk_count} indexed chunk${document.chunk_count === 1 ? "" : "s"}`,
    });
  }

  if (!state.documents.length) {
    elements.mapSummary.textContent = "Import a source to begin.";
  } else if (state.semanticIndexStatus !== "ready") {
    elements.mapSummary.textContent = `Semantic map unavailable: local index ${state.semanticIndexStatus}.`;
  } else if (!neighborhoods.length) {
    elements.mapSummary.textContent = "No semantic neighborhoods cleared the current similarity threshold.";
  } else if (neighborhood) {
    elements.mapSummary.textContent = `Showing ${visibleDocuments.length} of ${neighborhood.documentIds.length} sources in this semantic neighborhood.`;
  } else if (state.selectedDocumentId !== null) {
    elements.mapSummary.textContent = "Showing the selected source in map context.";
  } else {
    const connectedDocumentCount = new Set(state.semanticEdges.flatMap((edge) => [edge.left_document_id, edge.right_document_id])).size;
    elements.mapSummary.textContent = `${visibleNeighborhoods.length} semantic neighborhood${visibleNeighborhoods.length === 1 ? "" : "s"}, ${state.semanticEdges.length} explainable link${state.semanticEdges.length === 1 ? "" : "s"}, ${state.documents.length - connectedDocumentCount} sources outside the current threshold.`;
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
  const actionLabel = preview.action === "semantic-index" ? "semantic index" : preview.action;
  card.append(make("h3", "", `Preview ${actionLabel}`), make("p", "", preview.message));
  const actions = make("div", "preview-actions");
  actions.append(button("Apply this preview", "button-primary", async () => {
    try {
      setStatus("Applying the reviewed local action.");
      const result = await api(`previews/${preview.id}/apply`, { method: "POST" });
      setStatus(result.applied.message || "The local action was applied.");
      if (result.applied.action === "semantic-index") {
        state.semanticJob = result.applied.job;
        renderSemanticSetup();
        pollSemanticIndex();
        return;
      }
      await refresh();
      if (state.selectedDocumentId && result.applied.action !== "remove") await showDocument(state.selectedDocumentId);
      if (result.applied.action === "remove") resetMap();
    } catch (error) {
      setStatus(error.message, "error");
    }
  }));
  actions.append(button("Keep collection unchanged", "", () => card.remove()));
  card.append(actions);
  return card;
}

function renderSourceImportPreview(preview) {
  const card = make("section", "preview-card");
  card.append(make("h3", "", "Review connected folders"), make("p", "", preview.message));
  const roots = make("ul", "source-preview-list");
  for (const root of preview.roots) {
    const detail = root.status === "ready"
      ? `${root.candidate_count} new supported files, ${root.imported_count} already imported`
      : "unavailable";
    roots.append(make("li", "", `${root.name}: ${detail}.`));
  }
  card.append(roots);
  if (!preview.candidate_count) {
    card.append(make("p", "subtle", "No new supported files were found."));
    return card;
  }
  const candidates = make("ul", "source-preview-list");
  for (const candidate of preview.candidates) {
    candidates.append(make("li", "", `${candidate.name} (${candidate.source_root})`));
  }
  if (preview.additional_candidate_count) {
    candidates.append(make("li", "", `${preview.additional_candidate_count} additional eligible files`));
  }
  card.append(candidates);
  const actions = make("div", "preview-actions");
  actions.append(button(`Import ${preview.candidate_count} reviewed files`, "button-primary", async () => {
    try {
      setStatus("Importing the reviewed files into Hearth.");
      const result = await api(`source-previews/${preview.id}/apply`, { method: "POST" });
      setStatus(result.applied.message, result.applied.failures.length ? "error" : "info");
      card.remove();
      await refresh();
    } catch (error) {
      setStatus(error.message, "error");
    }
  }));
  actions.append(button("Keep map unchanged", "", () => card.remove()));
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

function actionPreview(documentId, action) {
  return api(`documents/${documentId}/actions/${action}/preview`, { method: "POST" }).then((result) => result.preview);
}

function renderDetail(inspection) {
  const detail = elements.details;
  detail.replaceChildren();
  const header = make("div", "detail-header");
  const copy = document.createElement("div");
  copy.append(make("p", "eyebrow", "Selected source"), make("h2", "", inspection.document.name));
  header.append(copy, make("span", "count", `#${inspection.document.id}`));
  detail.append(header, make("p", "subtle", `${inspection.document.page_count} pages · ${inspection.document.chunk_count} chunks`));
  const pages = make("div", "page-list");
  inspection.pages.forEach((page) => pages.append(pageCard(page)));
  detail.append(pages);
  const related = relationshipsFor(inspection.document.id);
  if (related.length) {
    const section = make("section", "relationships");
    section.append(make("h3", "", "Related sources"));
    for (const edge of related) {
      const relatedId = edge.left_document_id === inspection.document.id ? edge.right_document_id : edge.left_document_id;
      const document = state.documents.find((item) => item.id === relatedId);
      if (!document) continue;
      section.append(button(`${document.name} · ${Math.round(edge.score * 100)}%`, "relationship-button", () => showRelationship(edge)));
    }
    detail.append(section);
  }
  const actionDisclosure = document.createElement("details");
  actionDisclosure.className = "actions";
  actionDisclosure.append(make("summary", "", "Record actions"));
  const actions = make("div", "action-content");

  addAction(actions, "Reindex from the current source", "Refresh derived evidence from the source file after you have reviewed a change.", async () => {
    try { detail.append(renderPreview(await actionPreview(inspection.document.id, "reindex"))); }
    catch (error) { setStatus(error.message, "error"); }
  });
  addAction(actions, "Remove this collection record", "This removes Hearth's local metadata and derived index data. It never deletes the original file.", async () => {
    try { detail.append(renderPreview(await actionPreview(inspection.document.id, "remove"))); }
    catch (error) { setStatus(error.message, "error"); }
  }, "button-danger");
  actionDisclosure.append(actions);
  detail.append(actionDisclosure);
}

function relationshipsFor(documentId) {
  return state.semanticEdges.filter((edge) => edge.left_document_id === documentId || edge.right_document_id === documentId);
}

function selectNeighborhood(neighborhoodId) {
  const neighborhood = semanticNeighborhoods().find((item) => item.id === neighborhoodId);
  if (!neighborhood) return;
  state.selectedDocumentId = null;
  state.selectedNeighborhoodId = neighborhoodId;
  renderDocuments();
  renderMap();
  renderNeighborhoodDetail(neighborhood);
  setStatus("Showing an explainable semantic neighborhood.");
}

async function previewSemanticIndex() {
  try {
    setStatus("Preparing a local semantic-index rebuild for review.");
    const result = await api("semantic-index/preview", { method: "POST" });
    elements.details.append(renderPreview(result.preview));
    setStatus("Review the local semantic-index rebuild before applying it.");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function cancelSemanticIndex() {
  try {
    const result = await api("semantic-index/cancel", { method: "POST" });
    state.semanticJob = result.job;
    renderSemanticSetup();
    setStatus("Hearth will stop after the current small embedding batch.");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function pollSemanticIndex() {
  if (!["running", "cancelling"].includes(state.semanticJob.status)) return;
  window.setTimeout(async () => {
    try {
      const result = await api("semantic-index");
      state.semanticJob = result.job;
      if (state.selectedDocumentId === null && state.selectedNeighborhoodId === null) renderSemanticSetup();
      if (["running", "cancelling"].includes(state.semanticJob.status)) {
        pollSemanticIndex();
      } else if (state.semanticJob.status === "completed") {
        await refresh();
        setStatus(state.semanticJob.warning
          ? "The semantic map is ready, but this rebuild was not memory-checked. See This rebuild."
          : "The local semantic map is ready.", state.semanticJob.warning ? "error" : undefined);
      } else if (state.semanticJob.status === "cancelled") {
        setStatus("The incomplete semantic-index rebuild was cancelled.");
      } else if (state.semanticJob.status === "failed") {
        setStatus(state.semanticJob.error || "The local semantic-index rebuild stopped before completion.", "error");
      }
    } catch (error) {
      setStatus(error.message, "error");
    }
  }, 1200);
}

async function showRelationship(edge) {
  try {
    const relationship = (await api(`relationships/${edge.left_document_id}/${edge.right_document_id}`)).relationship;
    state.selectedDocumentId = null;
    state.selectedNeighborhoodId = semanticNeighborhoods().find((item) => (
      item.documentIds.includes(edge.left_document_id) && item.documentIds.includes(edge.right_document_id)
    ))?.id || null;
    renderDocuments();
    renderMap();
    const detail = elements.details;
    detail.replaceChildren();
    detail.append(make("p", "eyebrow", "Semantic connection"));
    detail.append(make("h2", "", `${relationship.left_document.name} ↔ ${relationship.right_document.name}`));
    detail.append(make("p", "subtle", `${Math.round(relationship.score * 100)}% local similarity`));
    detail.append(make("p", "relationship-note", "Closest matching evidence from the active embedding index."));
    const evidence = make("div", "citation-list");
    for (const item of relationship.evidence) {
      const card = make("article", "citation");
      card.append(make("div", "citation-heading", `${item.document_name} · page ${item.page_number}${item.section ? ` · ${item.section}` : ""}`));
      card.append(make("p", "", item.quote));
      evidence.append(card);
    }
    detail.append(evidence, button("Back to source", "button-quiet", () => showDocument(edge.left_document_id)));
    setStatus("Showing the local evidence behind this connection.");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function showDocument(documentId) {
  try {
    const inspection = await api(`documents/${documentId}`);
    state.selectedDocumentId = documentId;
    state.selectedNeighborhoodId = semanticNeighborhoods().find((item) => item.documentIds.includes(documentId))?.id || null;
    renderDocuments();
    renderMap();
    renderDetail(inspection);
  } catch (error) {
    setStatus(error.message, "error");
  }
}

function resetMap() {
  state.selectedDocumentId = null;
  state.selectedNeighborhoodId = null;
  renderDocuments();
  renderMap();
  if (state.semanticIndexStatus === "ready" && state.semanticEdges.length) {
    elements.details.replaceChildren(make("p", "empty-state", "Select a semantic neighborhood to inspect the related sources and their evidence."));
  } else {
    renderSemanticSetup();
  }
  setStatus("Map selection cleared.");
}

async function refresh() {
  try {
    const [health, collectionMap, sources, semanticIndex] = await Promise.all([
      api("health"), api("map"), api("sources"), api("semantic-index"),
    ]);
    state.documents = collectionMap.documents;
    state.semanticEdges = collectionMap.semantic_edges;
    state.semanticIndexStatus = health.semantic_index_status;
    state.semanticJob = semanticIndex.job;
    state.sourceRoots = sources.roots;
    renderHealth(health);
    renderSourceRoots();
    renderDocuments();
    renderMap();
    if (state.selectedDocumentId === null && state.selectedNeighborhoodId === null) renderSemanticSetup();
    pollSemanticIndex();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function previewSourceImport() {
  try {
    setStatus("Scanning the connected folders for supported files.");
    const result = await api("sources/preview", { method: "POST" });
    elements.details.replaceChildren(renderSourceImportPreview(result.preview));
    setStatus(result.preview.candidate_count ? "Review the eligible files before importing." : "No new supported files were found.");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

elements.importButton.addEventListener("click", async () => {
  try {
    setStatus("Choose a file in the macOS dialog.");
    const result = await api("import", { method: "POST" });
    setStatus(`Imported ${result.import.document.name}.`);
    await refresh();
    await showDocument(result.import.document.id);
  } catch (error) {
    setStatus(error.message, "error");
  }
});

elements.scanSourcesButton.addEventListener("click", previewSourceImport);
elements.scanSourcesPanelButton.addEventListener("click", previewSourceImport);

elements.search.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    setStatus("Searching.");
    const result = await api("search", { method: "POST", body: JSON.stringify({ question: elements.question.value }) });
    renderAnswer(result.answer);
    setStatus(result.answer.status === "supported" ? "Evidence found." : "Hearth abstained because no evidence-bound answer was available.");
  } catch (error) {
    setStatus(error.message, "error");
  }
});

window.addEventListener("resize", renderMap);

refresh().then(() => setStatus("Ready."));
