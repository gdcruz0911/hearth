// Hearth's app shell: the Hub, Tasks, and Knowledge views, with task data from api/workbench/tasks.
// Every value is written with textContent, so text agents wrote can never become markup.
const root = new URL(".", window.location.href).pathname;
// ADR-0037: the session token arrives once in the URL fragment, is cleared from the address bar, and lives only in this
// page's memory; every API call sends it in a header beside the cookie. A reload or another page needs it passed again.
const sessionToken = window.location.hash.startsWith("#session=") ? window.location.hash.slice("#session=".length) : "";
if (sessionToken) history.replaceState(null, "", window.location.pathname);
const SESSION_LOST = "This dashboard session ended. Run hearth web in your terminal to open a fresh one.";
const KNOWN = new Set(["running", "queued", "waiting", "done", "failed", "cancelled", "interrupted"]);
const VIEWS = ["hub", "tasks", "knowledge"];
const DOTS = 8; // Run dots on a task card.
const seen = new Map(); // Task id to its last status, so only real arrivals and changes move.
let firstRender = true;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function shown(value) {
  return value === null || value === undefined || value === "" ? "not reported" : String(value); // UI-8
}

function safeLink(url) {
  // Only an absolute https URL may become a link, so script, data, and relative URLs never navigate or run.
  if (typeof url !== "string") return null;
  try {
    return new URL(url).protocol === "https:" ? url : null;
  } catch {
    return null;
  }
}

function badge(status) {
  const known = KNOWN.has(status);
  return el("span", `badge ${known ? status : "other"}`, known ? status : `${shown(status)} (unrecognized)`);
}

function interactive(task) {
  const runs = task.runs || [];
  return task.status === "waiting" && runs.length > 0 && runs[runs.length - 1].interactive;
}

function reason(task) {
  if (task.questions && task.questions.length) return `${task.questions.length} open question${task.questions.length === 1 ? "" : "s"}`;
  if (task.stop_reason === "tests_to_approve") return "tests written first, waiting for your approval";
  if (task.status === "done" && !task.pr && task.artifacts && task.artifacts.outbound && task.artifacts.outbound.length) {
    return "outbound material waiting for your review before publishing";
  }
  if (interactive(task)) return "interactive session open; collect its work when you are done";
  if (task.status === "waiting") return "waiting for you";
  if (task.status === "interrupted") return "interrupted: its process ended without recording a result";
  if (task.failure) return `stopped: ${shown(task.failure.reason)}`;
  return null;
}

function command(task) {
  // UI-9: where the person would act, show the command instead of a control.
  if (task.questions && task.questions.length) return `hearth task show ${task.id}`;
  if (task.stop_reason === "tests_to_approve") return `hearth task approve-tests ${task.id}`;
  if (interactive(task)) return `hearth task collect ${task.id}`;
  if (task.status === "done" && !task.pr && task.artifacts && task.artifacts.outbound && task.artifacts.outbound.length) {
    return `hearth task publish ${task.id}`;
  }
  return `hearth task show ${task.id}`;
}

function time(value) {
  // Task records write offsets as -0400; Date wants -04:00.
  const parsed = value ? new Date(String(value).replace(/([+-]\d\d)(\d\d)$/, "$1:$2")) : null;
  return parsed && !Number.isNaN(parsed.getTime()) ? parsed : null;
}

function ago(value) {
  const then = time(value);
  if (!then) return "";
  const minutes = Math.max(0, Math.round((Date.now() - then.getTime()) / 60000));
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)}h`;
  return `${Math.round(minutes / 1440)}d`;
}

function span(start, end) {
  const from = time(start), to = end ? time(end) : new Date();
  if (!from || !to) return null;
  const seconds = Math.max(0, Math.round((to - from) / 1000));
  if (seconds >= 172800) return `${Math.floor(seconds / 86400)}d ${Math.floor(seconds / 3600) % 24}h`;
  return seconds < 60 ? `${seconds}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${Math.floor(seconds / 3600)}h ${Math.floor(seconds / 60) % 60}m`;
}

function tone(run, task) {
  // One color per meaning (UI-2): ember for a run still going, then the outcome's sense.
  if (!run.finished && task.status === "running") return "running";
  const outcome = String(run.outcome || "");
  if (/as required/.test(outcome)) return "ok"; // A tests-first run's tests are meant to fail.
  if (/fail|changes|reject|no usable|exit|refused/.test(outcome)) return "failed"; // Any failure outweighs a pass.
  if (/pass|approve|verified/.test(outcome)) return "ok";
  return "muted";
}

function card(task) {
  // Orca-style card: goal and status, where it lives, one dot per agent run, and its last word with its age.
  const motion = firstRender ? "" : !seen.has(task.id) ? " arrive" : seen.get(task.id) !== task.status ? " changed" : "";
  const item = el("li", `card-item${motion}`);
  const link = el("a", `card${selected.task === task.id ? " current" : ""}`);
  link.href = `#/tasks/${encodeURIComponent(task.id)}`;
  if (selected.task === task.id) link.setAttribute("aria-current", "page");
  const top = el("div", "row-top");
  if (task.status === "running") top.append(el("span", "pulse"));
  top.append(el("span", "goal", shown(task.goal)), badge(task.status));
  const place = el("div", "card-place");
  place.append(el("span", "project-tag", shown(task.project)), el("span", "id", task.branch || task.id));
  const runs = el("div", "card-runs");
  const all = task.runs || [];
  if (all.length > DOTS) runs.append(el("span", "card-age", `+${all.length - DOTS}`)); // The latest runs matter most.
  for (const run of all.slice(-DOTS)) {
    const dot = el("span", `run-dot ${tone(run, task)}`);
    dot.title = `${run.role} by ${run.provider}: ${shown(run.outcome)}`;
    runs.append(dot);
  }
  const doing = task.status === "queued" ? "waiting for a free slot" : task.active_run ? `${task.active_run.role} by ${task.active_run.provider}` : null;
  const last = reason(task) || doing || (task.review && task.review.verdict ? `review ${task.review.verdict}` : `stage ${shown(task.stage)}`);
  runs.append(el("span", "card-last", last), el("span", "card-age", ago(task.finished || (task.active_run && task.active_run.started) || task.created)));
  link.append(top, place, runs);
  item.append(link);
  return item;
}

function fill(list, tasks, render, emptyText) {
  list.replaceChildren(...(tasks.length ? tasks.map(render) : [el("li", "empty", emptyText)]));
}

function render(data) {
  const tasks = Array.isArray(data.tasks) ? data.tasks : [];
  const needs = tasks.filter((task) => reason(task));
  const progress = tasks.filter((task) => !needs.includes(task) && (task.status === "running" || task.status === "queued"));
  const finished = tasks.filter((task) => !needs.includes(task) && !progress.includes(task));
  fill(document.getElementById("needs"), needs, card, "Nothing needs you.");
  fill(document.getElementById("progress"), progress, card, "No agent is working.");
  fill(document.getElementById("finished"), finished, card, "No finished tasks yet.");
  document.getElementById("finished-count").textContent = String(finished.length);
  const group = document.getElementById("finished-group");
  if (finished.some((task) => task.id === selected.task)) group.open = true;

  const slots = data.slots || {};
  document.getElementById("slots").textContent = Object.entries(slots)
    .map(([kind, use]) => `${kind} ${shown(use && use.busy)}/${shown(use && use.limit)}`).join(" · ");

  const counts = new Map();
  tasks.forEach((task) => counts.set(task.project, (counts.get(task.project) || 0) + 1));
  document.getElementById("projects").replaceChildren(...[...counts].map(([name, count]) => {
    const item = el("li");
    item.append(el("span", "", name), el("span", "count", String(count)));
    return item;
  }));
  detail(tasks.find((task) => task.id === selected.task) || needs[0] || progress[0] || finished[0] || null);
}

// ---------- the task page ----------

const selected = { task: null, run: null };
let current = { task: null, run: null, session: null, diffKey: null };

function field(label, value) {
  const row = el("div", "field");
  row.append(el("span", "field-label", label), el("span", "field-value", shown(value)));
  return row;
}

function detail(task) {
  const summary = document.getElementById("task-summary");
  const sessionBox = document.getElementById("task-session");
  if (!task) {
    summary.replaceChildren(el("p", "empty", "No tasks yet. Start one with hearth task new."));
    sessionBox.hidden = true;
    stopSession();
    diffPane(null);
    return;
  }
  const parts = [];
  const head = el("header", "task-head");
  const title = el("div", "row-top");
  if (task.status === "running") title.append(el("span", "pulse"));
  title.append(el("h2", "task-goal", shown(task.goal)), badge(task.status));
  const where = el("p", "meta");
  where.append(`${shown(task.project)} · `, el("span", "id", task.id), " · ", el("span", "id", shown(task.branch)));
  if (task.pr) {
    // A record's URL becomes a link only when it is https; anything else shows as plain text (ADR-0037).
    const link = el("a", "", `pull request #${shown(task.pr.number)}`);
    if (safeLink(task.pr.url)) link.href = safeLink(task.pr.url);
    link.rel = "noreferrer";
    where.append(" · ", link);
  }
  const worked = span(task.created, task.finished);
  head.append(title, where);
  if (worked) head.append(el("p", "meta", `${task.finished ? "Worked for" : "Working for"} ${worked}`));
  parts.push(head);

  const why = reason(task);
  if (why) {
    const box = el("section", "needs-box");
    box.append(el("h3", "group", "Needs you"), el("p", "", why));
    for (const question of task.questions || []) {
      const asked = el("div", "question");
      asked.append(el("p", "meta", `${shown(question.from)} asks (${shown(question.kind)})`), el("p", "question-body", shown(question.body)));
      box.append(asked);
    }
    box.append(el("code", "command", command(task))); // UI-9: the command until dashboard actions exist.
    parts.push(box);
  }
  if (task.stop_reason) parts.push(field("Stopped because", task.stop_reason));

  const timeline = el("ol", "timeline");
  for (const run of task.runs || []) {
    const item = el("li", `step ${tone(run, task)}${run.dir === current.run ? " current" : ""}`);
    const open = el("a", "step-head");
    open.href = `#/tasks/${encodeURIComponent(task.id)}/${encodeURIComponent(run.dir || "")}`;
    open.append(el("span", "run-dot " + tone(run, task)), el("span", "step-role", `${shown(run.role)} · ${shown(run.provider)}`),
      el("span", "step-outcome", shown(run.outcome)), el("span", "card-age", span(run.started, run.finished) || ""));
    item.append(open);
    if (run.model) item.append(el("p", "meta", run.model));
    if (Array.isArray(run.claims) && run.claims.length) {
      const claims = el("ul", "claims");
      for (const claim of run.claims) {
        const line = el("li", `claim ${claim && claim.result === "pass" ? "ok" : "failed"}`);
        line.append(el("span", "claim-result", shown(claim && claim.result)), el("span", "", shown(claim && claim.claim)));
        if (claim && claim.evidence) line.append(el("span", "id", ` ${claim.evidence}`));
        claims.append(line);
      }
      item.append(claims);
    }
    if (Array.isArray(run.not_checked) && run.not_checked.length) {
      item.append(el("p", "meta", "Not checked:"));
      const gaps = el("ul", "claims");
      run.not_checked.forEach((gap) => gaps.append(el("li", "claim muted", typeof gap === "string" ? gap : JSON.stringify(gap))));
      item.append(gaps);
    }
    if (run.role === "review" && run.risk) item.append(el("p", "meta", `Risk: ${run.risk}`));
    timeline.append(item);
  }
  parts.push(el("h3", "group", "Runs"), (task.runs || []).length ? timeline : el("p", "empty", "No runs yet."));

  const recall = task.recall || [];
  if (recall.length) {
    parts.push(el("h3", "group", "Knowledge it was given"));
    for (const record of recall) {
      const box = el("div", "recall");
      box.append(el("p", "meta", `${shown(record.provider)} · ${shown(record.mode)} search · from ${(record.folders || []).join(", ") || "no folders"}`));
      if (record.not_sent_because) box.append(el("p", "meta", `Nothing sent: ${record.not_sent_because}`));
      else box.append(el("p", "meta", `${record.excerpts} excerpts, accepted by the evidence check, not verified`)); // UI-7
      const stale = new Set(record.stale || []);
      const docs = el("ul", "docs");
      for (const name of record.documents || []) {
        const doc = el("li", "", name);
        if (stale.has(name)) doc.append(" ", el("span", "badge stale", "changed since import"));
        docs.append(doc);
      }
      box.append(docs);
      if (record.withheld) box.append(el("p", "meta", `${record.withheld} withheld by Hearth's checks`));
      parts.push(box);
    }
  }
  summary.replaceChildren(...parts);

  // The session survives refreshes; it restarts only when the task or the chosen run changes.
  const runs = task.runs || [];
  const run = runs.find((item) => item.dir === selected.run) || runs.find((item) => task.active_run && item.dir === task.active_run.dir) || runs[runs.length - 1];
  if (!run || !run.dir) {
    sessionBox.hidden = true;
    stopSession();
  } else if (current.task !== task.id || current.run !== run.dir) {
    stopSession();
    current.task = task.id;
    current.run = run.dir;
    sessionBox.hidden = false;
    document.getElementById("session-heading").textContent = `Session · ${run.role} by ${run.provider}`;
    const log = document.getElementById("session-log");
    log.replaceChildren();
    current.session = session(task.id, run.dir, log);
    summary.querySelectorAll(".step").forEach((step, index) => step.classList.toggle("current", runs[index] === run));
  }
  diffPane(task);
}

function stopSession() {
  if (current.session) current.session.stop();
  current = { ...current, task: null, run: null, session: null };
}

// An agent's session, read from its events as they arrive; the hub's panel uses the same reader.
function session(taskId, run, list) {
  const state = { offset: 0, timer: 0, stopped: false };
  const kinds = { start: "Started", message: "Said", tool: "Ran", output: "Output", result: "Result", error: "Error" };
  const poll = async () => {
    try {
      const page = await api(`workbench/transcript/${encodeURIComponent(taskId)}/${encodeURIComponent(run)}/${state.offset}`);
      if (state.stopped) return;
      for (const item of page.items) {
        const entry = el("li", `turn ${item.kind}${item.error ? " error" : ""}`);
        entry.append(el("span", "turn-kind", item.tool ? `${kinds[item.kind] || item.kind} ${item.tool}` : kinds[item.kind] || item.kind),
          el(item.kind === "message" || item.kind === "result" ? "p" : "pre", "turn-text", item.text));
        list.append(entry);
      }
      // Follow a live session as it grows; a finished one opens at its beginning.
      if (page.items.length && !page.finished) list.lastElementChild.scrollIntoView({ block: "nearest" });
      const more = page.offset > state.offset;
      state.offset = page.offset;
      if (!page.finished || more) state.timer = setTimeout(poll, more ? 50 : 2000);
      if (page.finished && !more && !list.children.length) list.append(el("li", "empty", "This run recorded no events."));
    } catch (error) {
      if (!state.stopped) list.append(el("li", "turn error", error.message));
    }
  };
  poll();
  return { stop() { state.stopped = true; clearTimeout(state.timer); } };
}

async function diffPane(task) {
  const pane = document.getElementById("task-diff");
  const key = task ? `${task.id}:${task.status}:${task.finished}` : null;
  if (key === current.diffKey) return; // Reloaded only when the task or its state changes.
  current.diffKey = key;
  if (!task) {
    pane.replaceChildren();
    return;
  }
  try {
    const data = await api(`workbench/diff/${encodeURIComponent(task.id)}`);
    if (current.diffKey !== key) return;
    const head = el("header", "diff-head");
    head.append(el("h3", "group", "Changes"));
    if (data.diff) head.append(el("span", "diff-count add", `+${data.added}`), el("span", "diff-count del", `−${data.removed}`));
    const parts = [head];
    if (!data.diff) parts.push(el("p", "empty", task.finished ? "This task recorded no changes." : "No changes recorded yet; the diff appears after the agent's checkpoint."));
    else parts.push(...files(data.diff));
    if (data.truncated) parts.push(el("p", "meta", "This diff is long, so only its beginning is shown. The full diff is in the task's receipts."));
    pane.replaceChildren(...parts);
  } catch (error) {
    pane.replaceChildren(el("p", "status error", error.message));
  }
}

function files(text) {
  // A unified diff as one collapsible block per file, with line numbers from each hunk header.
  const blocks = [];
  let body = null, oldLine = 0, newLine = 0;
  for (const line of text.split("\n")) {
    if (line.startsWith("diff --git ")) {
      const file = el("details", "diff-file");
      file.open = true;
      file.append(el("summary", "id", line.replace(/^diff --git a\/(.*) b\/.*$/, "$1")));
      body = el("div", "diff-body");
      file.append(body);
      blocks.push(file);
      continue;
    }
    if (!body || /^(index |--- |\+\+\+ |new file|deleted file|similarity|rename |old mode|new mode)/.test(line)) continue;
    const hunk = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)/);
    if (hunk) {
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[2]);
      body.append(el("div", "diff-line hunk", line));
      continue;
    }
    const kind = line[0] === "+" ? "add" : line[0] === "-" ? "del" : "ctx";
    const row = el("div", `diff-line ${kind}`);
    row.append(el("span", "diff-num", String(kind === "del" ? oldLine : newLine)), el("span", "diff-text", line.slice(1) || " "));
    body.append(row);
    if (kind !== "add") oldLine += 1;
    if (kind !== "del") newLine += 1;
  }
  return blocks;
}

async function refresh() {
  const status = document.getElementById("status");
  try {
    const response = await fetch(`${root}api/workbench/tasks`, { cache: "no-store", headers: { "X-Hearth-Session": sessionToken } });
    const body = await response.json();
    if (response.status === 401) throw new Error(SESSION_LOST);
    if (!response.ok) throw new Error(body.error || "Hearth could not read tasks.");
    render(body);
    (Array.isArray(body.tasks) ? body.tasks : []).forEach((task) => seen.set(task.id, task.status));
    firstRender = false;
    window.Hearth.latest = body;
    document.dispatchEvent(new CustomEvent("hearth:tasks", { detail: body }));
    status.hidden = true;
    live(true, `Live · ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`);
  } catch (error) {
    status.hidden = false;
    status.className = "status error";
    status.textContent = error.message;
    live(false, "Offline");
  }
}

async function api(path, body) {
  const response = await fetch(`${root}api/${path}`, body === undefined ? { cache: "no-store", headers: { "X-Hearth-Session": sessionToken } } : {
    method: "POST", headers: { "Content-Type": "application/json", "X-Hearth-Session": sessionToken }, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (response.status === 401) throw new Error(SESSION_LOST);
  if (!response.ok) throw new Error(data.error || "Hearth could not complete that.");
  return data;
}

function freshness(source) {
  const current = source === "current";
  return el("span", `badge ${current ? "current" : "stale"}`, shown(source));
}

function where(item) {
  return `${shown(item.document)} · page ${shown(item.page)} · chunk ${shown(item.chunk_id)}`;
}

function renderAnswer(report) {
  const result = document.getElementById("ask-result");
  const supported = report.status === "supported";
  const head = el("div", "row-top");
  head.append(el("span", `badge ${supported ? "done" : "cancelled"}`, supported ? "answer found" : "abstained"),
    el("span", "meta", `${shown(report.retrieval && report.retrieval.mode)} search · index ${shown(report.retrieval && report.retrieval.semantic_index)}`));
  const parts = [head];
  const evidence = report.evidence || [];
  if (evidence.length) {
    // UI-7: what the evidence check accepted is labeled not verified, apart from what was only retrieved.
    parts.push(el("h3", "group", "Accepted by the evidence check, not verified"));
    parts.push(el("p", "meta", shown(report.gate && report.gate.note)));
    const list = el("ul", "rows");
    evidence.forEach((item) => {
      const row = el("li", "row evidence");
      const top = el("div", "row-top");
      top.append(el("span", "goal", where(item)), freshness(item.source));
      const excerpt = el("blockquote", "excerpt", shown(item.excerpt));
      row.append(top, excerpt);
      if (shown(item.excerpt).length > 400) {
        const toggle = el("button", "more", "Show all");
        toggle.type = "button";
        toggle.addEventListener("click", () => {
          toggle.textContent = excerpt.classList.toggle("open") ? "Show less" : "Show all";
        });
        row.append(toggle);
      }
      list.append(row);
    });
    parts.push(list);
  } else {
    parts.push(el("p", "meta", "Hearth found no excerpt it could cite, so it does not answer."));
  }
  const others = (report.candidates || []).filter((item) => !item.cited);
  if (others.length) {
    const more = el("details", "candidates");
    more.append(el("summary", "", `Also retrieved, not cited (${others.length}); text not shown`));
    const list = el("ul", "rows");
    others.forEach((item) => {
      const row = el("li", "row-top candidate");
      row.append(el("span", "goal", where(item)), freshness(item.source));
      list.append(row);
    });
    more.append(list);
    parts.push(more);
  }
  result.replaceChildren(...parts);
}

async function loadKnowledge() {
  try {
    const [health, documents] = await Promise.all([api("health"), api("documents")]);
    document.getElementById("health").textContent =
      `${health.document_count} documents · ${health.chunk_count} chunks · semantic index ${shown(health.semantic_index_status)}`;
    const attention = new Map((health.source_attention || []).map((item) => [item.document_id, item.status]));
    fill(document.getElementById("attention"), health.source_attention || [], (item) => {
      const row = el("li", "row row-top");
      row.append(el("span", "goal", shown(item.document_name)), freshness(item.status));
      return row;
    }, "Every source is current.");
    const all = documents.documents || [];
    const filter = document.getElementById("source-filter");
    const draw = () => {
      const term = filter.value.trim().toLowerCase();
      const matching = all.filter((item) => String(item.name).toLowerCase().includes(term));
      fill(document.getElementById("sources"), matching, (item) => {
        const row = el("li", "source");
        row.append(el("span", "goal", shown(item.name)), el("span", "meta", item.page_count === 1 ? "1 page" : `${shown(item.page_count)} pages`),
          freshness(attention.get(item.id) || "current"));
        return row;
      }, term ? "No source matches." : "No sources imported yet.");
    };
    filter.oninput = draw;
    draw();
  } catch (error) {
    document.getElementById("health").textContent = error.message;
  }
}

function knowledge() {
  const form = document.getElementById("ask-form");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const question = document.getElementById("ask-question").value;
    const result = document.getElementById("ask-result");
    result.replaceChildren(el("p", "meta", "Searching your collection."));
    try {
      renderAnswer(await api("search/report", { question, keyword: document.getElementById("ask-keyword").checked }));
    } catch (error) {
      result.replaceChildren(el("p", "status error", error.message));
    }
  });
}

function live(ok, text) {
  document.getElementById("live").classList.toggle("offline", !ok);
  document.getElementById("live-text").textContent = text;
}

function stored(key, fallback) {
  try { return localStorage.getItem(key) || fallback; } catch { return fallback; } // storage may be blocked
}

function store(key, value) {
  try { localStorage.setItem(key, value); } catch { /* storage may be blocked */ }
}

function route() {
  // UI-6: one shell; the hash names the view, and an empty hash opens the person's default view.
  const [asked, task, run] = window.location.hash.replace(/^#\//, "").split("/").map(decodeURIComponent);
  const view = VIEWS.includes(asked) ? asked : stored("hearth-default-view", "hub");
  selected.task = view === "tasks" && task ? task : null;
  selected.run = view === "tasks" && run ? run : null;
  if (window.Hearth && window.Hearth.latest) render(window.Hearth.latest);
  document.querySelectorAll(".view").forEach((node) => {
    const show = node.dataset.view === view;
    if (show && node.hidden) {
      node.classList.remove("entering");
      void node.offsetWidth; // restart the crossfade when switching back to a view
      node.classList.add("entering");
    }
    node.hidden = !show;
  });
  document.dispatchEvent(new CustomEvent("hearth:view", { detail: view }));
  if (view === "knowledge") loadKnowledge();
  document.querySelectorAll(".nav-item").forEach((link) => {
    const current = link.dataset.view === view;
    link.classList.toggle("current", current);
    if (current) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  });
}

function defaultView() {
  const select = document.getElementById("default-view");
  select.value = stored("hearth-default-view", "hub");
  select.addEventListener("change", () => store("hearth-default-view", select.value));
}

function theme() {
  const button = document.getElementById("theme");
  const system = window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  let light = stored("hearth-theme", system) === "light";
  const apply = () => {
    document.documentElement.classList.toggle("light", light);
    button.textContent = light ? "Dark mode" : "Light mode";
  };
  button.addEventListener("click", () => {
    light = !light;
    store("hearth-theme", light ? "light" : "dark");
    apply();
  });
  apply();
}

// Shared with hub.js, which draws the same tasks as a graph.
window.Hearth = { el, shown, badge, reason, command, api, session, root, latest: null };

theme();
defaultView();
// The classic knowledge page is another page, so its link carries the token the same way the launch did.
document.querySelectorAll('a[href="knowledge"]').forEach((link) => { link.href = `knowledge#session=${sessionToken}`; });
knowledge();
route();
window.addEventListener("hashchange", route);
refresh();
setInterval(refresh, 5000);
