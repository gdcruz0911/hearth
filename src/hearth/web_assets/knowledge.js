// The Knowledge tab's collection tools: import, connected folders, the map, the semantic index, and record previews.
// Every change keeps the preview-then-apply flow: a button asks for a preview, and only a second button applies it.
// Text is set with textContent only, so nothing a document says can become markup.
(() => {
  const H = () => window.Hearth;
  const SVG = "http://www.w3.org/2000/svg"; // the SVG namespace: an identifier, never fetched
  const BUSY = ["running", "cancelling"];
  const W = 640, HEIGHT = 340, MAX_NEIGHBORHOODS = 14, MAX_DOCUMENTS = 24;
  const state = { documents: [], edges: [], indexStatus: "not configured", job: { status: "idle" }, roots: [], document: null, neighborhood: null, timer: 0 };
  const els = {};

  const el = (...args) => H().el(...args);
  const post = (path) => H().api(path, {});

  function button(label, className, onClick) {
    const node = el("button", `btn ${className || ""}`, label);
    node.type = "button";
    node.addEventListener("click", onClick);
    return node;
  }

  function say(message, kind) {
    els.status.textContent = message;
    els.status.className = kind === "error" ? "status error" : "status";
    els.status.hidden = !message;
  }

  async function attempt(work) {
    try { await work(); } catch (error) { say(error.message, "error"); }
  }

  function show(...parts) {
    els.panel.replaceChildren(...parts);
    els.panel.hidden = !parts.length;
    if (parts.length) els.panel.scrollIntoView({ block: "nearest" });
  }

  // ---------- loading ----------

  async function load() {
    await attempt(async () => {
      const [health, map, sources, index] = await Promise.all([H().api("health"), H().api("map"), H().api("sources"), H().api("semantic-index")]);
      state.documents = map.documents;
      state.edges = map.semantic_edges;
      state.indexStatus = health.semantic_index_status;
      state.job = index.job;
      state.roots = sources.roots;
      renderFolders();
      renderIndex();
      renderMap();
      poll();
    });
  }

  async function reload() {
    await load();
    await H().loadKnowledge();
  }

  // ---------- connected folders and import ----------

  function renderFolders() {
    const ready = state.roots.filter((root) => root.status === "ready");
    const away = state.roots.filter((root) => root.status !== "ready");
    els.folders.textContent = state.roots.length
      ? `${ready.map((root) => root.name).join(", ")} connected.${away.length ? ` ${away.map((root) => root.name).join(", ")} unavailable.` : ""}`
      : "No folders are connected. Create or update the runtime profile to choose source roots.";
    els.scan.disabled = !state.roots.length;
  }

  function importFile() {
    return attempt(async () => {
      say("Choose a file in the macOS dialog.");
      const result = await post("import");
      say(`Imported ${result.import.document.name}.`);
      await reload();
      await openDocument(result.import.document.id);
    });
  }

  function scanFolders() {
    return attempt(async () => {
      say("Scanning the connected folders for supported files.");
      const { preview } = await post("sources/preview");
      const card = el("section", "preview");
      card.append(el("h3", "group", "Review connected folders"), el("p", "meta", preview.message));
      const roots = el("ul", "plain");
      for (const root of preview.roots) {
        roots.append(el("li", "", `${root.name}: ${root.status === "ready" ? `${root.candidate_count} new supported files, ${root.imported_count} already imported` : "unavailable"}.`));
      }
      card.append(roots);
      if (!preview.candidate_count) {
        card.append(el("p", "empty", "No new supported files were found."));
        say("No new supported files were found.");
        return show(card);
      }
      const files = el("ul", "plain");
      for (const candidate of preview.candidates) files.append(el("li", "", `${candidate.name} (${candidate.source_root})`));
      if (preview.additional_candidate_count) files.append(el("li", "", `${preview.additional_candidate_count} additional eligible files`));
      card.append(files, actions(`Import ${preview.candidate_count} reviewed files`, "Keep collection unchanged", async () => {
        say("Importing the reviewed files into Hearth.");
        const result = await post(`source-previews/${preview.id}/apply`);
        say(result.applied.message, result.applied.failures.length ? "error" : "");
        show();
        await reload();
      }));
      show(card);
      say("Review the eligible files before importing.");
    });
  }

  function actions(applyLabel, keepLabel, apply, danger) {
    const row = el("div", "actions");
    row.append(button(applyLabel, danger ? "danger" : "primary", () => attempt(apply)), button(keepLabel, "", () => show()));
    return row;
  }

  // ---------- semantic index ----------

  function renderIndex() {
    const { job } = state;
    const parts = [];
    if (BUSY.includes(job.status)) {
      const done = Math.min(job.completed || 0, job.total || 0);
      const progress = document.createElement("progress");
      progress.max = Math.max(job.total || 1, 1);
      progress.value = done;
      progress.setAttribute("aria-label", "Semantic-index rebuild progress");
      parts.push(el("p", "meta", job.status === "cancelling" ? "Stopping after the current small embedding batch." : `${shown(job.phase || "embedding evidence")}, locally in the background.`),
        progress, el("p", "meta", `${done.toLocaleString()} of ${(job.total || 0).toLocaleString()} evidence units. You can keep using Hearth while this runs.`));
      const cancel = button(job.status === "cancelling" ? "Stopping" : "Cancel rebuild", "danger", () => attempt(async () => {
        state.job = (await post("semantic-index/cancel")).job;
        renderIndex();
        say("Hearth will stop after the current small embedding batch.");
      }));
      cancel.disabled = job.status === "cancelling";
      parts.push(cancel);
    } else {
      const lines = {
        "not configured": "Start Hearth with a local embedding model and index directory to build the semantic map.",
        "needs reindex": "Your imported evidence changed. Rebuild the local semantic index before using the map.",
        ready: "The local semantic index is current.",
      };
      parts.push(el("p", "meta", lines[state.indexStatus] || `${shown(state.indexStatus)} (unrecognized)`));
      if (job.status === "cancelled") parts.push(el("p", "meta", "The last rebuild was cancelled. No incomplete index was activated."));
      if (job.status === "failed") parts.push(el("p", "meta", job.error || "The last rebuild stopped before completion. No incomplete index was activated."));
      if (state.indexStatus !== "not configured") parts.push(button("Preview rebuild", "", previewIndex));
    }
    if (job.benchmark) parts.push(benchmark(job.benchmark));
    els.index.replaceChildren(...parts);
    // The memory-monitoring warning has its own line, so a later status message cannot replace it.
    els.warning.textContent = job.warning || "";
    els.warning.hidden = !job.warning;
  }

  function benchmark(figures) {
    const seconds = (value) => (!Number.isFinite(value) ? "not reported" : value < 60 ? `${value.toFixed(value < 10 ? 1 : 0)} sec` : `${Math.floor(value / 60)} min ${Math.round(value % 60)} sec`);
    const memory = figures.peak_resident_memory_bytes;
    const rows = [["Elapsed", seconds(figures.elapsed_seconds)], ["Hearth CPU", seconds(figures.cpu_seconds)],
      ["Peak memory", Number.isFinite(memory) && memory > 0 ? `${(memory / 1048576).toFixed(memory >= 10485760 ? 0 : 1)} MB` : "not reported"],
      ["Current rate", figures.evidence_units_per_minute ? `${Math.round(figures.evidence_units_per_minute).toLocaleString()} units/min` : "preparing"]];
    const box = el("div", "benchmark");
    box.append(el("h3", "group", "This rebuild"));
    for (const [label, value] of rows) {
      const row = el("div", "field");
      row.append(el("span", "field-label", label), el("span", "field-value", value));
      box.append(row);
    }
    box.append(el("p", "meta", "These figures measure the Hearth process, not total Mac power or fan activity."));
    return box;
  }

  const shown = (value) => H().shown(value);

  function previewIndex() {
    return attempt(async () => {
      const { preview } = await post("semantic-index/preview");
      show(previewCard("Preview semantic index rebuild", preview, async () => {
        const result = await post(`previews/${preview.id}/apply`);
        say(result.applied.message);
        state.job = result.applied.job;
        show();
        renderIndex();
        poll();
      }));
    });
  }

  function poll() {
    clearTimeout(state.timer);
    if (!BUSY.includes(state.job.status)) return;
    state.timer = setTimeout(() => attempt(async () => {
      state.job = (await H().api("semantic-index")).job;
      renderIndex();
      if (BUSY.includes(state.job.status)) return poll();
      if (state.job.status === "completed") {
        await reload();
        say(state.job.warning ? "The semantic map is ready, but this rebuild was not memory-checked." : "The local semantic map is ready.", state.job.warning ? "error" : "");
      } else {
        say(state.job.status === "failed" ? state.job.error || "The rebuild stopped before completion." : "The incomplete rebuild was cancelled.", state.job.status === "failed" ? "error" : "");
      }
    }), 1200);
  }

  function previewCard(title, preview, apply, danger) {
    const card = el("section", "preview");
    card.append(el("h3", "group", title), el("p", "meta", preview.message), actions("Apply this preview", "Keep collection unchanged", apply, danger));
    return card;
  }

  // ---------- one source ----------

  async function openDocument(id) {
    await attempt(async () => {
      const found = await H().api(`documents/${id}`);
      state.document = id;
      state.neighborhood = neighborhoods().find((group) => group.ids.includes(id))?.id || null;
      renderMap();
      const parts = [el("h3", "group", found.document.name),
        el("p", "meta", `${found.document.page_count} pages · ${found.document.chunk_count} chunks · #${found.document.id}`)];
      const pages = el("ul", "plain");
      for (const page of found.pages) {
        const method = page.extraction_method === "ocr" ? ` · OCR, confidence ${page.ocr_confidence == null ? "not reported" : page.ocr_confidence.toFixed(2)}; verify against the original` : "";
        pages.append(el("li", "", `Page ${page.page_number} · ${page.section || "no section"} · ${page.chunks.length} chunks${method}`));
      }
      parts.push(pages);
      const related = state.edges.filter((edge) => edge.left_document_id === id || edge.right_document_id === id);
      if (related.length) {
        parts.push(el("h3", "group", "Related sources"));
        for (const edge of related) {
          const other = state.documents.find((item) => item.id === (edge.left_document_id === id ? edge.right_document_id : edge.left_document_id));
          if (other) parts.push(button(`${other.name} · ${Math.round(edge.score * 100)}%`, "link", () => openRelationship(edge)));
        }
      }
      parts.push(el("h3", "group", "Record actions"),
        el("p", "meta", "Neither action touches the original file."),
        recordAction(id, "reindex", "Preview reindex", "Reindex from the current source", false),
        recordAction(id, "remove", "Preview removal", "Remove this collection record", true));
      show(...parts);
    });
  }

  function recordAction(id, action, label, title, danger) {
    const slot = el("div", "record-action");
    slot.append(button(label, danger ? "danger" : "", () => attempt(async () => {
      const { preview } = await post(`documents/${id}/actions/${action}/preview`);
      slot.querySelector(".preview")?.remove();
      slot.append(previewCard(title, preview, async () => {
        const result = await post(`previews/${preview.id}/apply`);
        say(result.applied.message || "The local action was applied.");
        if (action === "remove") { state.document = null; state.neighborhood = null; show(); }
        await reload();
        if (action === "reindex") await openDocument(id);
      }, danger));
    })));
    return slot;
  }

  async function openRelationship(edge) {
    await attempt(async () => {
      const { relationship } = await H().api(`relationships/${edge.left_document_id}/${edge.right_document_id}`);
      state.document = null;
      state.neighborhood = neighborhoods().find((group) => group.ids.includes(edge.left_document_id) && group.ids.includes(edge.right_document_id))?.id || null;
      renderMap();
      const parts = [el("h3", "group", `${relationship.left_document.name} ↔ ${relationship.right_document.name}`),
        el("p", "meta", `${Math.round(relationship.score * 100)}% local similarity. Closest matching evidence from the active embedding index.`)];
      for (const item of relationship.evidence) {
        const row = el("div", "row");
        row.append(el("p", "meta", `${item.document_name} · page ${item.page_number}${item.section ? ` · ${item.section}` : ""}`), el("blockquote", "excerpt open", item.quote));
        parts.push(row);
      }
      parts.push(button("Back to source", "link", () => openDocument(edge.left_document_id)));
      show(...parts);
    });
  }

  // ---------- map ----------

  function neighborhoods() {
    // Sources joined by semantic links form one neighborhood (union-find over the edges both ends of which are still imported).
    const known = new Set(state.documents.map((item) => item.id));
    const edges = state.edges.filter((edge) => known.has(edge.left_document_id) && known.has(edge.right_document_id));
    const parent = new Map();
    const weight = new Map();
    const find = (id) => (parent.get(id) === id ? id : (parent.set(id, find(parent.get(id))), parent.get(id)));
    for (const edge of edges) {
      for (const id of [edge.left_document_id, edge.right_document_id]) {
        parent.set(id, id);
        weight.set(id, (weight.get(id) || 0) + edge.score);
      }
    }
    for (const edge of edges) parent.set(find(edge.right_document_id), find(edge.left_document_id));
    const groups = new Map();
    for (const id of parent.keys()) groups.set(find(id), [...(groups.get(find(id)) || []), id]);
    return [...groups.values()].map((ids) => {
      ids.sort((a, b) => a - b);
      const lead = [...ids].sort((a, b) => (weight.get(b) || 0) - (weight.get(a) || 0) || a - b)[0];
      return { id: ids.join("-"), ids, lead: state.documents.find((item) => item.id === lead), edges: edges.filter((edge) => ids.includes(edge.left_document_id) && ids.includes(edge.right_document_id)) };
    }).sort((a, b) => b.ids.length - a.ids.length || a.id.localeCompare(b.id));
  }

  function renderMap() {
    const groups = neighborhoods();
    const open = groups.find((group) => group.id === state.neighborhood) || null;
    if (state.neighborhood && !open) state.neighborhood = null;
    const centre = { x: W / 2, y: HEIGHT / 2 };
    const ring = (index, total, rx, ry) => ({ x: centre.x + Math.cos(-Math.PI / 2 + (2 * Math.PI * index) / Math.max(total, 1)) * rx, y: centre.y + Math.sin(-Math.PI / 2 + (2 * Math.PI * index) / Math.max(total, 1)) * ry });
    const lines = [], nodes = [];
    const node = (label, kind, point, radius, pressed, onClick, description) => {
      const group = document.createElementNS(SVG, "g");
      group.setAttribute("class", `kmap-node ${kind}${pressed ? " selected" : ""}`);
      group.setAttribute("transform", `translate(${point.x} ${point.y})`);
      group.setAttribute("role", "button");
      group.setAttribute("tabindex", "0");
      group.setAttribute("aria-label", description || label);
      const dot = document.createElementNS(SVG, "circle");
      dot.setAttribute("r", String(radius));
      const text = document.createElementNS(SVG, "text");
      text.setAttribute("y", String(radius + 13));
      text.textContent = label.length > 26 ? `${label.slice(0, 25)}…` : label;
      group.append(dot, text);
      group.addEventListener("click", onClick);
      group.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onClick(); } });
      nodes.push(group);
    };
    const line = (from, to, className) => {
      const stroke = document.createElementNS(SVG, "line");
      Object.entries({ x1: from.x, y1: from.y, x2: to.x, y2: to.y, class: className }).forEach(([key, value]) => stroke.setAttribute(key, String(value)));
      lines.push(stroke);
    };
    const reset = () => { state.document = null; state.neighborhood = null; renderMap(); show(); };
    if (open) {
      const documents = open.ids.map((id) => state.documents.find((item) => item.id === id)).filter(Boolean).slice(0, MAX_DOCUMENTS);
      const points = new Map(documents.map((item, index) => [item.id, ring(index, documents.length, W * 0.34, HEIGHT * 0.3)]));
      for (const edge of open.edges) if (points.has(edge.left_document_id) && points.has(edge.right_document_id)) line(points.get(edge.left_document_id), points.get(edge.right_document_id), "kmap-link");
      node("Hearth", "hearth", centre, 16, false, reset, "Hearth, show all neighborhoods");
      for (const item of documents) node(item.name, "document", points.get(item.id), 8, item.id === state.document, () => openDocument(item.id), `${item.name}, ${item.chunk_count} indexed chunks`);
      els.summary.textContent = `Showing ${documents.length} of ${open.ids.length} sources in this semantic neighborhood.`;
    } else {
      const shownGroups = groups.slice(0, MAX_NEIGHBORHOODS);
      const points = shownGroups.map((_, index) => ring(index, shownGroups.length, W * 0.36, HEIGHT * 0.3));
      points.forEach((point) => line(centre, point, "kmap-link"));
      node("Hearth", "hearth", centre, 16, !state.document, reset, "Hearth, clear the selection");
      shownGroups.forEach((group, index) => node(`${group.lead.name} +${group.ids.length - 1}`, "neighborhood", points[index], Math.min(16, 8 + group.ids.length), false,
        () => { state.neighborhood = group.id; state.document = null; renderMap(); show(); }, `${group.lead.name}, semantic neighborhood with ${group.ids.length} sources`));
      const linked = new Set(groups.flatMap((group) => group.ids)).size;
      els.summary.textContent = !state.documents.length ? "Import a source to begin."
        : state.indexStatus !== "ready" ? `Semantic map unavailable: local index ${shown(state.indexStatus)}.`
        : !groups.length ? "No semantic neighborhoods cleared the current similarity threshold."
        : `${shownGroups.length} semantic neighborhood${shownGroups.length === 1 ? "" : "s"}, ${state.edges.length} explainable link${state.edges.length === 1 ? "" : "s"}, ${state.documents.length - linked} sources outside the current threshold.`;
    }
    els.map.replaceChildren(...lines, ...nodes);
  }

  // ---------- wiring ----------

  document.addEventListener("DOMContentLoaded", () => {
    for (const [name, id] of Object.entries({ status: "kn-status", panel: "kn-panel", folders: "kn-folders", scan: "kn-scan", index: "kn-index", warning: "kn-warning", map: "kn-map", summary: "kn-map-summary" })) {
      els[name] = document.getElementById(id);
    }
    document.getElementById("kn-import").addEventListener("click", importFile);
    els.scan.addEventListener("click", scanFolders);
    document.addEventListener("hearth:source", (event) => openDocument(event.detail));
    document.addEventListener("hearth:view", (event) => {
      if (event.detail === "knowledge") load(); else clearTimeout(state.timer);
    });
    if (!document.querySelector('.view[data-view="knowledge"]').hidden) load(); // the page opened on this view before this script ran
  });
})();
