// The hub: one living graph of how Hearth, projects, tasks, agents, and knowledge connect (design.md UI-6).
// Node types and link kinds are rows in the tables below; adding or removing one is a row, not new logic.
// Text is set with textContent only, so nothing an agent wrote can become markup.
(() => {
  const H = () => window.Hearth;
  const SVG = "http://www.w3.org/2000/svg"; // the SVG namespace: an identifier, never fetched
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  const NODE_TYPES = {
    hearth: { radius: 18, label: "always" },
    project: { radius: 11, label: "always" },
    knowledge: { radius: 11, label: "always" },
    task: { radius: 7, label: "hover" },
    run: { radius: 4.5, label: "hover" },
    document: { radius: 4, label: "hover" },
  };
  const LINK_KINDS = {
    spoke: { length: 170, strength: 0.05 },
    belongs: { length: 64, strength: 0.08 },
    runs: { length: 46, strength: 0.14 },
    related: { length: 46, strength: 0.03 },
    recalled: { length: 120, strength: 0.012 },
  };
  const MAX_TASKS = 18;
  const MAX_DOCUMENTS = 36;

  const state = {
    nodes: new Map(), links: [], hidden: new Set(), selected: null, hovered: null,
    view: null, alpha: 0, frame: 0, map: null, tasks: [], transcript: null,
  };
  const els = {};

  // ---------- data → graph ----------

  function statusClass(status) {
    return ["running", "queued", "waiting", "done", "failed", "cancelled", "interrupted"].includes(status) ? status : "other";
  }

  function build() {
    const wanted = new Map();
    const links = [];
    const add = (id, type, data) => wanted.set(id, { id, type, ...data });
    const link = (source, target, kind, extra = {}) => links.push({ source, target, kind, ...extra });

    add("hearth", "hearth", { label: "Hearth" });
    const tasks = state.tasks;
    const urgent = tasks.filter((task) => H().reason(task) || task.status === "running" || task.status === "queued");
    const others = tasks.filter((task) => !urgent.includes(task)).slice(0, Math.max(0, MAX_TASKS - urgent.length));
    const shownTasks = [...urgent, ...others];

    if (!state.hidden.has("task")) {
      for (const task of shownTasks) {
        const project = `project:${task.project}`;
        if (!wanted.has(project)) { add(project, "project", { label: task.project, side: -1 }); link("hearth", project, "spoke"); }
        add(`task:${task.id}`, "task", { label: H().shown(task.goal), status: statusClass(task.status), task });
        link(project, `task:${task.id}`, "belongs");
        const expanded = task.status === "running" || state.selected === `task:${task.id}`;
        if (expanded && !state.hidden.has("run")) {
          for (const run of task.runs || []) {
            const id = `run:${task.id}:${run.dir}`;
            const running = task.status === "running" && task.active_run && task.active_run.dir === run.dir;
            add(id, "run", { label: `${run.role} · ${run.provider}`, status: running ? "running" : "idle", task, run });
            link(`task:${task.id}`, id, "runs", { flowing: running });
          }
        }
      }
    }

    if (!state.hidden.has("document") && state.map) {
      add("knowledge", "knowledge", { label: "Knowledge", side: 1 });
      link("hearth", "knowledge", "spoke");
      const byName = new Map(state.map.documents.map((doc) => [doc.name, doc]));
      const degree = new Map();
      const recalls = [];
      for (const task of state.hidden.has("task") ? [] : shownTasks) {
        for (const record of task.recall || []) {
          for (const name of record.documents || []) {
            const doc = byName.get(name);
            if (doc) { recalls.push([doc.id, task.id]); degree.set(doc.id, (degree.get(doc.id) || 0) + 3); }
          }
        }
      }
      for (const edge of state.map.semantic_edges || []) {
        for (const id of [edge.left_document_id, edge.right_document_id]) degree.set(id, (degree.get(id) || 0) + 1);
      }
      const docs = state.map.documents.filter((doc) => degree.has(doc.id))
        .sort((a, b) => degree.get(b.id) - degree.get(a.id)).slice(0, MAX_DOCUMENTS);
      const kept = new Set(docs.map((doc) => doc.id));
      for (const doc of docs) {
        add(`doc:${doc.id}`, "document", { label: doc.name, doc, status: "doc" });
        link("knowledge", `doc:${doc.id}`, "belongs");
      }
      for (const edge of state.map.semantic_edges || []) {
        if (kept.has(edge.left_document_id) && kept.has(edge.right_document_id)) {
          link(`doc:${edge.left_document_id}`, `doc:${edge.right_document_id}`, "related");
        }
      }
      for (const [docId, taskId] of recalls) {
        if (kept.has(docId) && wanted.has(`task:${taskId}`)) link(`doc:${docId}`, `task:${taskId}`, "recalled");
      }
    }

    // Keep positions of nodes that stay; new nodes start beside their parent so they grow out of it.
    const next = new Map();
    for (const [id, spec] of wanted) {
      const old = state.nodes.get(id);
      if (old) { Object.assign(old, spec); next.set(id, old); continue; }
      const parent = links.find((item) => item.target === id);
      const from = parent && (next.get(parent.source) || state.nodes.get(parent.source));
      // Knowledge starts to the right of Hearth and projects to the left, so the two worlds settle apart.
      const angle = spec.side ? (spec.side > 0 ? 0 : Math.PI) + (Math.random() - 0.5) : Math.random() * Math.PI * 2;
      const reach = spec.side ? 150 : 12;
      next.set(id, { ...spec, x: (from ? from.x : 0) + Math.cos(angle) * reach, y: (from ? from.y : 0) + Math.sin(angle) * reach, vx: 0, vy: 0 });
    }
    const hearth = next.get("hearth");
    hearth.fx = 0; hearth.fy = 0;
    state.nodes = next;
    state.links = links.filter((item) => next.has(item.source) && next.has(item.target));
    draw();
    reheat(0.6);
  }

  // ---------- physics ----------

  function tick() {
    const nodes = [...state.nodes.values()];
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = b.x - a.x, dy = b.y - a.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 0.01) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 0.25; }
        if (d2 > 90000) continue;
        // Bigger nodes push harder, so clusters keep apart; the distance floor keeps near neighbours from exploding apart.
        const weight = Math.max(0.7, NODE_TYPES[a.type].radius / 6) * Math.max(0.7, NODE_TYPES[b.type].radius / 6);
        const push = (700 * weight * state.alpha) / Math.max(d2, 400);
        const d = Math.sqrt(d2);
        a.vx -= (dx / d) * push; a.vy -= (dy / d) * push;
        b.vx += (dx / d) * push; b.vy += (dy / d) * push;
      }
      a.vx -= a.x * 0.004 * state.alpha;
      a.vy -= a.y * 0.004 * state.alpha;
    }
    for (const item of state.links) {
      const a = state.nodes.get(item.source), b = state.nodes.get(item.target);
      const kind = LINK_KINDS[item.kind];
      const dx = b.x - a.x, dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const pull = (d - kind.length) * kind.strength * state.alpha;
      a.vx += (dx / d) * pull; a.vy += (dy / d) * pull;
      b.vx -= (dx / d) * pull; b.vy -= (dy / d) * pull;
    }
    for (const node of nodes) {
      if (node.fx !== undefined) { node.x = node.fx; node.y = node.fy; node.vx = 0; node.vy = 0; continue; }
      node.vx *= 0.6; node.vy *= 0.6;
      const speed = Math.hypot(node.vx, node.vy);
      if (speed > 8) { node.vx *= 8 / speed; node.vy *= 8 / speed; } // a speed limit keeps every step small and stable
      node.x += node.vx; node.y += node.vy;
    }
    state.alpha *= 0.985;
  }

  function settle() {
    // Lay the graph out at once and show the result without movement.
    for (let i = 0; i < 400 && state.alpha > 0.005; i++) tick();
    state.alpha = 0;
    position();
  }

  function reheat(alpha) {
    state.alpha = Math.max(state.alpha, alpha);
    // Reduced motion, or a page nobody can see: settle now, so the graph is already laid out when looked at.
    if (reduceMotion.matches || document.hidden) { settle(); return; }
    if (!state.frame) state.frame = requestAnimationFrame(loop);
  }

  function loop() {
    state.frame = 0;
    if (document.hidden) { settle(); return; }
    if (els.view.hidden) return; // the hub tab is not shown; switching to it reheats
    tick();
    position();
    if (state.alpha > 0.005 || state.dragging) state.frame = requestAnimationFrame(loop);
  }

  // ---------- drawing ----------

  function svg(tag, attrs) {
    const node = document.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
    return node;
  }

  function draw() {
    els.links.replaceChildren(...state.links.map((item) => {
      const line = svg("line", { class: `link ${item.kind}${item.flowing ? " flowing" : ""}` });
      item.el = line;
      return line;
    }));
    els.nodes.replaceChildren(...[...state.nodes.values()].map((node) => {
      const group = svg("g", { class: `node ${node.type} ${node.status || ""}`, tabindex: "0", role: "button" });
      group.setAttribute("aria-label", `${node.type === "run" ? "agent" : node.type}: ${node.label}`);
      group.dataset.id = node.id;
      const radius = NODE_TYPES[node.type].radius;
      group.append(svg("circle", { class: "halo", r: String(radius + 6) }), svg("circle", { class: "dot", r: String(radius) }));
      const text = svg("text", { class: `label ${NODE_TYPES[node.type].label}`, y: String(radius + 14) });
      text.textContent = node.label.length > 34 ? `${node.label.slice(0, 33)}…` : node.label;
      group.append(text);
      node.el = group;
      return group;
    }));
    focus(state.hovered || state.selected);
    position();
  }

  function position() {
    for (const node of state.nodes.values()) node.el && node.el.setAttribute("transform", `translate(${node.x.toFixed(1)} ${node.y.toFixed(1)})`);
    for (const item of state.links) {
      const a = state.nodes.get(item.source), b = state.nodes.get(item.target);
      if (!item.el) continue;
      item.el.setAttribute("x1", a.x.toFixed(1)); item.el.setAttribute("y1", a.y.toFixed(1));
      item.el.setAttribute("x2", b.x.toFixed(1)); item.el.setAttribute("y2", b.y.toFixed(1));
    }
  }

  // On wide screens the graph starts right of the floating "Needs you" panel; on phones the panel sits below it.
  function home() {
    return window.innerWidth > 900 ? { x: 120, y: 30, k: 1 } : { x: 0, y: -40, k: 0.85 };
  }

  function applyView() {
    const { x, y, k } = state.view;
    const rect = els.svg.getBoundingClientRect();
    els.viewport.setAttribute("transform", `translate(${(rect.width / 2 + x).toFixed(1)} ${(rect.height / 2 + y).toFixed(1)}) scale(${k.toFixed(3)})`);
  }

  // Hovering or selecting a node lights it and its neighbours and lets everything else recede.
  function focus(id) {
    const lit = new Set(id ? [id] : []);
    if (id) for (const item of state.links) {
      if (item.source === id) lit.add(item.target);
      if (item.target === id) lit.add(item.source);
    }
    els.svg.classList.toggle("focusing", Boolean(id));
    for (const node of state.nodes.values()) node.el && node.el.classList.toggle("lit", lit.has(node.id));
    for (const item of state.links) item.el && item.el.classList.toggle("lit", lit.has(item.source) && lit.has(item.target) && (item.source === id || item.target === id));
    for (const node of state.nodes.values()) node.el && node.el.classList.toggle("selected", node.id === state.selected);
  }

  // ---------- pointer: drag nodes 1:1 with momentum, pan the stage, zoom at the cursor ----------

  function toGraph(event) {
    const rect = els.svg.getBoundingClientRect();
    return { x: (event.clientX - rect.left - rect.width / 2 - state.view.x) / state.view.k,
             y: (event.clientY - rect.top - rect.height / 2 - state.view.y) / state.view.k };
  }

  function onPointerDown(event) {
    if (event.button !== 0) return;
    const group = event.target.closest(".node");
    const start = { x: event.clientX, y: event.clientY, t: performance.now() };
    const history = [start];
    els.svg.setPointerCapture(event.pointerId);
    let moved = false;
    if (group) {
      const node = state.nodes.get(group.dataset.id);
      const grab = toGraph(event);
      const offset = { x: node.x - grab.x, y: node.y - grab.y }; // keep the point that was grabbed under the pointer
      state.dragging = node;
      node.el.classList.add("pressed");
      const move = (e) => {
        if (!moved && Math.hypot(e.clientX - start.x, e.clientY - start.y) < 4) return;
        moved = true;
        const at = toGraph(e);
        node.fx = at.x + offset.x; node.fy = at.y + offset.y;
        history.push({ x: e.clientX, y: e.clientY, t: performance.now() });
        if (history.length > 5) history.shift();
        reheat(0.25);
        if (reduceMotion.matches) position();
      };
      const up = () => {
        els.svg.removeEventListener("pointermove", move);
        node.el.classList.remove("pressed");
        state.dragging = null;
        if (!moved) { select(node.id); return; }
        if (node.id !== "hearth") {
          // Hand the release velocity to the node so the throw continues, then let the graph settle it.
          const first = history[0], last = history[history.length - 1];
          const dt = Math.max(16, last.t - first.t);
          node.vx = ((last.x - first.x) / dt) * 16 / state.view.k;
          node.vy = ((last.y - first.y) / dt) * 16 / state.view.k;
          delete node.fx; delete node.fy;
        }
        reheat(0.3);
      };
      els.svg.addEventListener("pointermove", move);
      els.svg.addEventListener("pointerup", up, { once: true });
      return;
    }
    const origin = { ...state.view };
    els.svg.classList.add("panning");
    const pan = (e) => {
      moved = true;
      state.view.x = origin.x + (e.clientX - start.x);
      state.view.y = origin.y + (e.clientY - start.y);
      applyView();
    };
    els.svg.addEventListener("pointermove", pan);
    els.svg.addEventListener("pointerup", () => {
      els.svg.removeEventListener("pointermove", pan);
      els.svg.classList.remove("panning");
      if (!moved) select(null);
    }, { once: true });
  }

  function onWheel(event) {
    event.preventDefault();
    const rect = els.svg.getBoundingClientRect();
    const px = event.clientX - rect.left - rect.width / 2, py = event.clientY - rect.top - rect.height / 2;
    const k = Math.min(2.5, Math.max(0.4, state.view.k * Math.exp(-event.deltaY * 0.0015)));
    const ratio = k / state.view.k;
    state.view.x = px - (px - state.view.x) * ratio; // keep the point under the cursor still
    state.view.y = py - (py - state.view.y) * ratio;
    state.view.k = k;
    applyView();
  }

  // ---------- inspector ----------

  function select(id) {
    const changed = state.selected !== id;
    state.selected = id;
    stopTranscript();
    if (!id) { closeInspector(); focus(state.hovered); if (changed) build(); return; }
    const node = state.nodes.get(id);
    if (changed && node && node.type === "task") build(); // a selected task shows its agents
    focus(id);
    openInspector(state.nodes.get(id) || node);
  }

  function field(label, value) {
    const row = H().el("div", "field");
    row.append(H().el("span", "field-label", label), H().el("span", "field-value", H().shown(value)));
    return row;
  }

  function openInspector(node) {
    if (!node) return;
    const { el } = H();
    els.title.textContent = node.type === "run" ? "Agent" : node.type[0].toUpperCase() + node.type.slice(1);
    const parts = [];
    if (node.type === "task") {
      const task = node.task;
      const head = el("div", "row-top");
      head.append(el("span", "goal wrap", H().shown(task.goal)), H().badge(task.status));
      parts.push(head, field("Project", task.project), field("Stage", task.stage), field("Task", task.id));
      const why = H().reason(task);
      if (why) parts.push(field("Needs you", why));
      const agents = el("div", "chips");
      for (const run of task.runs || []) {
        const chip = el("button", "chip", `${run.role} · ${run.provider}`);
        chip.type = "button";
        chip.addEventListener("click", () => select(`run:${task.id}:${run.dir}`));
        agents.append(chip);
      }
      if ((task.runs || []).length) parts.push(el("h3", "group", "Agents"), agents);
      const docs = [...new Set((task.recall || []).flatMap((record) => record.documents || []))];
      if (docs.length) parts.push(el("h3", "group", "Knowledge it was given"), el("p", "meta", docs.join(", ")));
      parts.push(el("code", "command", H().command(task)));
      const page = el("a", "link-button", "Open the task page");
      page.href = `#/tasks/${encodeURIComponent(task.id)}`;
      parts.push(page);
    } else if (node.type === "run") {
      const run = node.run;
      parts.push(field("Role", run.role), field("Provider", run.provider), field("Model", run.model), field("Outcome", run.outcome));
      const log = el("ol", "transcript");
      log.setAttribute("aria-live", "polite");
      const page = el("a", "link-button", "Open full session");
      page.href = `#/tasks/${encodeURIComponent(node.task.id)}/${encodeURIComponent(run.dir)}`;
      parts.push(page, el("h3", "group", node.status === "running" ? "Live session" : "Session"), log);
      startTranscript(node.task.id, run.dir, log);
    } else if (node.type === "document") {
      const doc = node.doc;
      parts.push(el("p", "goal wrap", doc.name), field("Pages", doc.page_count), field("Chunks", doc.chunk_count));
      const users = state.tasks.filter((task) => (task.recall || []).some((record) => (record.documents || []).includes(doc.name)));
      if (users.length) {
        parts.push(el("h3", "group", "Given to"));
        for (const task of users) {
          const link = el("button", "link-button", H().shown(task.goal));
          link.type = "button";
          link.addEventListener("click", () => select(`task:${task.id}`));
          parts.push(link);
        }
      }
      const neighbours = [];
      for (const item of state.links) {
        if (item.kind !== "related") continue;
        const other = item.source === node.id ? item.target : item.target === node.id ? item.source : null;
        if (other) neighbours.push(state.nodes.get(other));
      }
      if (neighbours.length) {
        parts.push(el("h3", "group", "Related by meaning"));
        for (const other of neighbours) {
          const link = el("button", "link-button", other.label);
          link.type = "button";
          link.addEventListener("click", () => select(other.id));
          parts.push(link);
        }
      }
    } else if (node.type === "project") {
      const own = state.tasks.filter((task) => `project:${task.project}` === node.id);
      parts.push(field("Tasks", own.length), field("Running", own.filter((task) => task.status === "running").length),
        field("Needs you", own.filter((task) => H().reason(task)).length));
    } else if (node.type === "knowledge") {
      parts.push(field("Documents", state.map ? state.map.documents.length : null),
        field("Shown", [...state.nodes.values()].filter((item) => item.type === "document").length),
        el("p", "meta", "The most connected documents: related by meaning, or given to a task."));
    } else {
      parts.push(field("Projects", [...state.nodes.values()].filter((item) => item.type === "project").length),
        field("Tasks", state.tasks.length), field("Running", state.tasks.filter((task) => task.status === "running").length));
    }
    els.body.replaceChildren(...parts);
    if (els.inspector.hidden) {
      els.inspector.hidden = false;
      els.inspector.classList.remove("open");
      void els.inspector.offsetWidth;
    }
    els.inspector.classList.add("open");
  }

  function closeInspector() {
    els.inspector.classList.remove("open");
    const done = () => { if (!els.inspector.classList.contains("open")) els.inspector.hidden = true; };
    if (reduceMotion.matches) done(); else setTimeout(done, 180);
  }

  // An agent's session, read by the shared reader in dashboard.js.
  function startTranscript(taskId, run, list) {
    state.transcript = H().session(taskId, run, list);
  }

  function stopTranscript() {
    if (!state.transcript) return;
    state.transcript.stop();
    state.transcript = null;
  }

  // ---------- "Needs you", floating over the graph ----------

  function needs() {
    const urgent = state.tasks.filter((task) => H().reason(task));
    document.getElementById("hub-needs-count").textContent = urgent.length ? String(urgent.length) : "";
    const list = document.getElementById("hub-needs");
    list.replaceChildren(...(urgent.length ? urgent.slice(0, 4).map((task) => {
      const item = H().el("li");
      const button = H().el("button", `mini-row ${statusClass(task.status)}`);
      button.type = "button";
      button.append(H().el("span", "mini-dot"), H().el("span", "mini-goal", H().shown(task.goal)), H().el("span", "mini-why", H().reason(task)));
      button.addEventListener("click", () => select(`task:${task.id}`));
      item.append(button);
      return item;
    }) : [H().el("li", "empty", "Nothing needs you.")]));
  }

  // ---------- wiring ----------

  async function loadMap() {
    try { state.map = await H().api("map"); build(); } catch { /* the graph still shows tasks without knowledge */ }
  }

  function init() {
    els.view = document.querySelector('.view[data-view="hub"]');
    els.svg = document.getElementById("hub-svg");
    els.viewport = document.getElementById("hub-viewport");
    els.links = document.getElementById("hub-links");
    els.nodes = document.getElementById("hub-nodes");
    els.inspector = document.getElementById("inspector");
    els.title = document.getElementById("inspector-title");
    els.body = document.getElementById("inspector-body");

    els.svg.addEventListener("pointerdown", onPointerDown);
    els.svg.addEventListener("wheel", onWheel, { passive: false });
    els.svg.addEventListener("pointerover", (event) => {
      const group = event.target.closest(".node");
      state.hovered = group ? group.dataset.id : null;
      focus(state.hovered || state.selected);
    });
    els.svg.addEventListener("pointerleave", () => { state.hovered = null; focus(state.selected); });
    els.svg.addEventListener("keydown", (event) => {
      const group = event.target.closest(".node");
      if (group && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); select(group.dataset.id); }
    });
    els.svg.addEventListener("focusin", (event) => {
      const group = event.target.closest(".node");
      if (group) focus(group.dataset.id);
    });
    document.getElementById("inspector-close").addEventListener("click", () => select(null));
    document.addEventListener("keydown", (event) => { if (event.key === "Escape" && state.selected) select(null); });
    document.querySelectorAll(".hub-tools .chip[data-type]").forEach((chip) => chip.addEventListener("click", () => {
      const type = chip.dataset.type;
      const on = state.hidden.has(type);
      if (on) state.hidden.delete(type); else state.hidden.add(type);
      chip.classList.toggle("on", on);
      chip.setAttribute("aria-pressed", String(on));
      build();
    }));
    document.getElementById("hub-reset").addEventListener("click", () => { state.view = home(); applyView(); reheat(0.5); });
    window.addEventListener("resize", applyView);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) reheat(0.05); });
    const take = (data) => {
      const tasks = Array.isArray(data.tasks) ? data.tasks : [];
      // Rebuild only when what the graph shows changed, so hover, focus, and positions survive each refresh.
      const signature = tasks.map((task) => [task.id, task.status, task.active_run && task.active_run.dir, (task.runs || []).length,
        (task.recall || []).length, (task.questions || []).length].join(":")).join("|");
      state.tasks = tasks;
      needs();
      if (signature === state.signature) return false;
      state.signature = signature;
      build();
      return true;
    };
    if (H().latest) take(H().latest);
    document.addEventListener("hearth:tasks", (event) => {
      if (!take(event.detail)) return;
      if (state.selected && state.nodes.has(state.selected) && state.nodes.get(state.selected).type === "task") {
        openInspector(state.nodes.get(state.selected)); // keep the open task current
      }
    });
    document.addEventListener("hearth:view", (event) => {
      if (event.detail === "hub") { applyView(); reheat(0.2); } else { stopTranscript(); closeInspector(); } // One session poller at a time.
    });
    state.view = home();
    applyView();
    loadMap();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
