// Hearth's app shell: the Hub, Tasks, and Knowledge views, with task data from api/workbench/tasks.
// Every value is written with textContent, so text agents wrote can never become markup.
const root = new URL(".", window.location.href).pathname;
const KNOWN = new Set(["running", "queued", "waiting", "done", "failed", "cancelled", "interrupted"]);
const RECENT = 12;
const VIEWS = ["hub", "tasks", "knowledge"];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function shown(value) {
  return value === null || value === undefined || value === "" ? "not reported" : String(value); // UI-8
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

function row(task, detail) {
  const item = el("li", "row");
  const top = el("div", "row-top");
  if (task.status === "running") top.append(el("span", "pulse"));
  top.append(el("span", "goal", shown(task.goal)), badge(task.status), el("span", "id", task.id));
  item.append(top);
  if (detail) item.append(el("div", "meta", detail));
  return item;
}

function fill(list, tasks, render, emptyText) {
  list.replaceChildren(...(tasks.length ? tasks.map(render) : [el("li", "empty", emptyText)]));
}

function render(data) {
  const tasks = Array.isArray(data.tasks) ? data.tasks : [];
  const needs = tasks.filter((task) => reason(task));
  const progress = tasks.filter((task) => task.status === "running" || task.status === "queued");
  const recent = tasks.filter((task) => !needs.includes(task) && !progress.includes(task)).slice(0, RECENT);

  const needsRow = (task) => {
    const item = row(task, `${task.project} · ${reason(task)}`);
    item.append(el("code", "command", command(task)));
    return item;
  };
  fill(document.getElementById("needs"), needs, needsRow, "Nothing needs you.");
  fill(document.getElementById("hub-needs"), needs, needsRow, "Nothing needs you.");

  fill(document.getElementById("progress"), progress, (task) => {
    const run = task.active_run;
    const doing = task.status === "queued" ? "waiting for a free slot" : run ? `${shown(run.role)} by ${shown(run.provider)}` : `stage ${shown(task.stage)}`;
    return row(task, `${task.project} · ${doing}`);
  }, "No agent is working.");

  fill(document.getElementById("recent"), recent, (task) => {
    const verdict = task.review && task.review.verdict ? `review ${task.review.verdict}` : "no review";
    const item = row(task, `${task.project} · ${verdict}`);
    if (task.pr && task.pr.url) {
      const link = el("a", "", `pull request #${task.pr.number}`);
      link.href = task.pr.url;
      link.rel = "noreferrer";
      item.querySelector(".meta").append(" · ", link);
    }
    return item;
  }, "No finished tasks yet.");

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
}

async function refresh() {
  const status = document.getElementById("status");
  try {
    const response = await fetch(`${root}api/workbench/tasks`, { cache: "no-store" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || "Hearth could not read tasks.");
    render(body);
    status.className = "status";
    status.textContent = `Updated ${new Date().toLocaleTimeString()}.`;
  } catch (error) {
    status.className = "status error";
    status.textContent = error.message;
  }
}

async function api(path, body) {
  const response = await fetch(`${root}api/${path}`, body === undefined ? { cache: "no-store" } : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await response.json();
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
        row.append(el("span", "goal", shown(item.name)), el("span", "meta", `${shown(item.page_count)} pages`),
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

function stored(key, fallback) {
  try { return localStorage.getItem(key) || fallback; } catch { return fallback; } // storage may be blocked
}

function store(key, value) {
  try { localStorage.setItem(key, value); } catch { /* storage may be blocked */ }
}

function route() {
  // UI-6: one shell; the hash names the view, and an empty hash opens the person's default view.
  const asked = window.location.hash.replace(/^#\//, "");
  const view = VIEWS.includes(asked) ? asked : stored("hearth-default-view", "hub");
  document.querySelectorAll(".view").forEach((node) => { node.hidden = node.dataset.view !== view; });
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

theme();
defaultView();
knowledge();
route();
window.addEventListener("hashchange", route);
refresh();
setInterval(refresh, 5000);
