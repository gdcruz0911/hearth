// The dashboard's home: what needs the person, what is running, and what finished, from api/workbench/tasks.
// Every value is written with textContent, so text agents wrote can never become markup.
const root = new URL(".", window.location.href).pathname;
const KNOWN = new Set(["running", "queued", "waiting", "done", "failed", "cancelled", "interrupted"]);
const RECENT = 12;

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

  fill(document.getElementById("needs"), needs, (task) => {
    const item = row(task, `${task.project} · ${reason(task)}`);
    item.append(el("code", "command", command(task)));
    return item;
  }, "Nothing needs you.");

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

function theme() {
  const button = document.getElementById("theme");
  let light = window.matchMedia("(prefers-color-scheme: light)").matches;
  try { light = (localStorage.getItem("hearth-theme") || (light ? "light" : "dark")) === "light"; } catch { /* storage may be blocked */ }
  const apply = () => {
    document.documentElement.classList.toggle("light", light);
    button.textContent = light ? "Dark mode" : "Light mode";
  };
  button.addEventListener("click", () => {
    light = !light;
    try { localStorage.setItem("hearth-theme", light ? "light" : "dark"); } catch { /* storage may be blocked */ }
    apply();
  });
  apply();
}

theme();
refresh();
setInterval(refresh, 5000);
