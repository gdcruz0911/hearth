// The workbench shell around the views: the project and conversation sidebar, the Inbox, the work inspector's Activity,
// the status bar, and Find. It reads only what sessions.js (hearth:sessions) and dashboard.js (hearth:tasks) already
// fetched, and writes every value with textContent.
(() => {
  const { el, shown, reason, command, ago } = window.Hearth;
  const $ = (id) => document.getElementById(id);
  const AGENTS = { claude: "Claude Code", codex: "Codex" };
  const agent = (provider) => AGENTS[provider] || shown(provider);
  const state = { sessions: [], tasks: [], folded: new Set(), view: null };
  const NEEDS_SESSION = { failed: "The last reply failed", interrupted: "The last reply was interrupted" };

  function stored(key, fallback) {
    try { return localStorage.getItem(key) || fallback; } catch { return fallback; } // storage may be blocked
  }

  function store(key, value) {
    try { localStorage.setItem(key, value); } catch { /* storage may be blocked */ }
  }

  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#i-${name}`);
    svg.setAttribute("aria-hidden", "true");
    svg.append(use);
    return svg;
  }

  function dot(status) {
    const node = el("span", `state-dot ${status || "new"}`);
    node.setAttribute("role", "img");
    node.setAttribute("aria-label", status || "no turn yet");
    return node;
  }

  // Polling rebuilds these lists, so the focused control is found again by its key or link and refocused.
  const keyOf = (node) => node.dataset.key || node.getAttribute("href");
  function refill(box, nodes) {
    const focused = box.contains(document.activeElement) ? keyOf(document.activeElement) : null;
    box.replaceChildren(...nodes);
    if (focused) [...box.querySelectorAll("a, button")].find((node) => keyOf(node) === focused)?.focus();
  }

  function selected() {
    const [view, id] = window.location.hash.replace(/^#\//, "").split("/").map(decodeURIComponent);
    return view === "sessions" ? id : null;
  }

  // ---------- what needs the keeper, and what is working ----------

  function needs() {
    // Sessions never wait for an answer (Claude approvals are not built), so only a failed or interrupted last reply counts.
    const sessions = state.sessions.filter((item) => NEEDS_SESSION[item.status]).map((item) => ({
      route: `sessions/${encodeURIComponent(item.id)}`, status: item.status, title: shown(item.title),
      why: `${item.project} · ${agent(item.provider)} · ${NEEDS_SESSION[item.status]}`, at: item.updated,
    }));
    const tasks = state.tasks.filter((task) => reason(task)).map((task) => ({
      route: `tasks/${encodeURIComponent(task.id)}`, task: task.id, status: task.status === "waiting" ? "waiting" : task.status, title: shown(task.goal),
      why: `${shown(task.project)} · task · ${reason(task)}`, command: command(task), at: task.finished || task.created,
    }));
    return [...tasks, ...sessions];
  }

  function working() {
    const sessions = state.sessions.filter((item) => item.status === "running").map((item) => ({
      route: `sessions/${encodeURIComponent(item.id)}`, status: "running", title: shown(item.title), why: `${agent(item.provider)} · ${item.project}`, at: item.updated,
    }));
    const tasks = state.tasks.filter((task) => task.status === "running" || task.status === "queued").map((task) => ({
      route: `tasks/${encodeURIComponent(task.id)}`, status: task.status, title: shown(task.goal),
      why: task.status === "queued" ? `${shown(task.project)} · waiting for a free slot` : `${shown(task.project)} · ${task.active_run ? `${task.active_run.role} by ${task.active_run.provider}` : "task"}`,
      at: (task.active_run && task.active_run.started) || task.created,
    }));
    return [...sessions, ...tasks];
  }

  // ---------- sidebar ----------

  function sidebar() {
    const current = selected();
    const groups = new Map();
    state.sessions.forEach((item) => groups.set(item.project, [...(groups.get(item.project) || []), item]));
    const box = $("projects");
    if (!groups.size) {
      refill(box, [el("p", "side-empty", "No conversations yet. Start one with +.")]);
      return;
    }
    refill(box, [...groups].map(([project, items]) => {
      const section = el("section", "project");
      const heading = el("button", "project-heading");
      heading.type = "button";
      heading.dataset.key = `project:${project}`;
      const open = !state.folded.has(project);
      heading.setAttribute("aria-expanded", String(open));
      heading.append(el("span", "project-mark", project.slice(0, 1).toUpperCase()), el("span", "session-title", project), icon("chevron"));
      heading.addEventListener("click", () => {
        if (state.folded.has(project)) state.folded.delete(project); else state.folded.add(project);
        sidebar();
      });
      const list = el("ul", "session-list");
      list.hidden = !open;
      items.forEach((item) => {
        const link = el("a", "session-link");
        link.href = `#/sessions/${encodeURIComponent(item.id)}`;
        if (item.id === current) link.setAttribute("aria-current", "page");
        link.title = `${shown(item.title)} · ${agent(item.provider)}`;
        const words = el("span", "session-words");
        words.append(el("span", "session-title", shown(item.title)), el("span", "session-sub", `${agent(item.provider)} · ${item.model || "CLI default"}`));
        link.append(dot(item.status), words, el("small", "", ago(item.updated)));
        const li = el("li");
        li.append(link);
        list.append(li);
      });
      section.append(heading, list);
      return section;
    }));
  }

  // ---------- the Inbox, Activity, and the status bar ----------

  function row(item, className) {
    const link = el("a", className);
    link.href = `#/${item.route}`;
    const words = el("span");
    words.append(el("strong", "", item.title), el("small", "", item.why));
    if (item.command) words.append(el("code", "", item.command)); // UI-9: the command where the dashboard has no control yet.
    link.append(dot(item.status || "running"), words);
    if (className === "inbox-row") link.append(icon("chevron"));
    else link.append(el("time", "", ago(item.at)));
    return link;
  }

  function attention(list) {
    $("attention-lead").textContent = list.length
      ? "The work that needs a decision or a look, in one place. Choose one to go to it."
      : "All caught up: no task is waiting for you and no conversation's last reply failed.";
    refill($("attention-list"), list.map((item) => {
      const li = el("li");
      li.append(row(item, "inbox-row"));
      return li;
    }));
  }

  function activity(list, busy) {
    const parts = [el("p", "work-label", "Inbox")];
    parts.push(...(list.length ? list.slice(0, 4).map((item) => row(item, "activity needs")) : [el("p", "empty", "Nothing in the Inbox.")]));
    if (list.length > 4) {
      const more = el("a", "link-button", `All ${list.length} in the Inbox`);
      more.href = `#/attention`;
      parts.push(more);
    }
    parts.push(el("p", "work-label", "Working now"));
    parts.push(...(busy.length ? busy.map((item) => row(item, "activity")) : [el("p", "empty", "No agent is working.")]));
    const recent = state.sessions.filter((item) => item.status && item.status !== "running").slice(0, 5);
    parts.push(el("p", "work-label", "Recent conversations"));
    parts.push(...(recent.length ? recent.map((item) => row({ route: `sessions/${encodeURIComponent(item.id)}`, status: item.status,
      title: shown(item.title), why: `${agent(item.provider)} · ${item.project} · ${item.status}`, at: item.updated }, "activity"))
      : [el("p", "empty", "No finished replies yet.")]));
    refill($("work-activity"), parts);
  }

  function render() {
    const list = needs();
    const busy = working();
    sidebar();
    attention(list);
    activity(list, busy);
    $("needs-count").textContent = list.length ? String(list.length) : "";
    const count = (id, number, words, status) => {
      $(id).replaceChildren(...(number ? [dot(status), el("span", "", words)] : []));
    };
    const running = busy.filter((item) => item.status === "running").length; // Queued tasks wait for a slot; no agent works on them yet.
    count("running-count", running, `${running} agent${running === 1 ? "" : "s"} working`, "running");
    count("waiting-count", list.length, `${list.length} in Inbox`, "waiting");
    // The Hub shows the same list, so both counts agree.
    document.dispatchEvent(new CustomEvent("hearth:inbox", { detail: list }));
    if ($("find").open) results();
  }

  // ---------- the work inspector ----------

  function tab(name) {
    document.querySelectorAll(".work-tab").forEach((button) => {
      const on = button.dataset.tab === name;
      button.setAttribute("aria-selected", String(on));
      button.tabIndex = on ? 0 : -1;
      $(button.getAttribute("aria-controls")).hidden = !on;
    });
    if (state.view !== "attention") store("hearth-work-tab", name);
  }

  function inspector(open) {
    // Beside Conversations and the Inbox only; Tasks, Hub, and Knowledge have panes of their own.
    const allowed = state.view === "sessions" || state.view === "attention";
    // Changes and Details describe one conversation, and the Inbox has none selected, so it shows Activity only.
    const inbox = state.view === "attention";
    document.querySelectorAll(".work-tab").forEach((button) => { if (button.dataset.tab !== "activity") button.hidden = inbox; });
    tab(inbox ? "activity" : stored("hearth-work-tab", "activity"));
    $("work").hidden = !(open && allowed);
    $("work-toggle").disabled = !allowed;
    $("work-toggle").setAttribute("aria-expanded", String(!$("work").hidden));
    $("work-toggle").setAttribute("aria-label", $("work").hidden ? "Show the work inspector" : "Hide the work inspector");
  }

  const narrow = () => window.matchMedia("(max-width: 1180px)").matches;
  // A drawer over the conversation in a smaller window starts closed; beside it, the keeper's last choice holds.
  const wanted = () => !narrow() && stored("hearth-work", "open") === "open";

  // ---------- Find ----------

  function results() {
    const term = $("find-input").value.trim().toLowerCase();
    const items = [
      ...state.sessions.map((item) => ({ route: `sessions/${encodeURIComponent(item.id)}`, status: item.status, title: shown(item.title),
        why: `${item.project} · ${agent(item.provider)} · conversation`, text: `${item.title} ${item.project} ${agent(item.provider)}` })),
      ...state.tasks.map((task) => ({ route: `tasks/${encodeURIComponent(task.id)}`, status: task.status, title: shown(task.goal),
        why: `${shown(task.project)} · task`, text: `${task.goal} ${task.project} ${task.id}` })),
    ].filter((item) => item.text.toLowerCase().includes(term)).slice(0, 30);
    refill($("find-results"), items.length ? items.map((item) => {
      const li = el("li");
      const link = el("a");
      link.href = `#/${item.route}`;
      link.append(dot(item.status), el("span", "session-title", item.title), el("small", "", item.why));
      li.append(link);
      return li;
    }) : [el("li", "empty", term ? "Nothing matches. Try a project or agent name." : "No conversations or tasks yet.")]);
  }

  function find() {
    if ($("find").open) return;
    $("find-input").value = "";
    results();
    $("find").showModal();
    $("find-input").focus();
  }

  // ---------- wiring ----------

  document.addEventListener("hearth:sessions", (event) => { state.sessions = event.detail.sessions || []; render(); });
  document.addEventListener("hearth:tasks", (event) => { state.tasks = Array.isArray(event.detail.tasks) ? event.detail.tasks : []; render(); });
  document.addEventListener("hearth:view", (event) => {
    state.view = event.detail;
    inspector(narrow() ? !$("work").hidden : wanted());
    $("side").classList.remove("open");
    $("side-toggle").setAttribute("aria-expanded", "false");
    sidebar();
  });
  window.addEventListener("hashchange", () => { if ($("find").open) $("find").close(); sidebar(); });
  document.addEventListener("hearth:inspect", (event) => {
    tab(event.detail);
    inspector(true);
  });
  $("work-toggle").addEventListener("click", () => {
    const open = $("work").hidden;
    if (!narrow()) store("hearth-work", open ? "open" : "closed");
    inspector(open);
  });
  $("work-close").addEventListener("click", () => {
    if (!narrow()) store("hearth-work", "closed");
    inspector(false);
    $("work-toggle").focus();
  });
  document.querySelectorAll(".work-tab").forEach((button) => button.addEventListener("click", () => tab(button.dataset.tab)));
  $("work").querySelector(".work-tabs").addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const tabs = [...document.querySelectorAll(".work-tab:not([hidden])")];
    const next = tabs[(tabs.indexOf(document.activeElement) + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
    tab(next.dataset.tab);
    next.focus();
  });
  $("side-toggle").addEventListener("click", () => {
    const open = $("side").classList.toggle("open");
    $("side-toggle").setAttribute("aria-expanded", String(open));
  });
  $("find-open").addEventListener("click", find);
  $("find-input").addEventListener("input", results);
  $("find-input").addEventListener("keydown", (event) => {
    const first = $("find-results").querySelector("a");
    if (event.key === "Enter" && first) {
      event.preventDefault();
      window.location.hash = first.getAttribute("href");
      $("find").close(); // The current conversation's result changes no route, so hashchange never closes it.
    }
  });
  $("find-results").addEventListener("click", (event) => { if (event.target.closest("a")) $("find").close(); });
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      find();
    }
    if (event.key === "Escape" && $("side").classList.contains("open")) {
      $("side").classList.remove("open");
      $("side-toggle").setAttribute("aria-expanded", "false");
    } else if (event.key === "Escape" && narrow() && $("work").contains(document.activeElement)) { inspector(false); $("work-toggle").focus(); }
  });
  window.matchMedia("(max-width: 1180px)").addEventListener("change", () => inspector(wanted()));
  tab(stored("hearth-work-tab", "activity"));
  state.view = (document.querySelector(".main > .view:not([hidden])") || {}).dataset?.view || null; // dashboard.js routed before this loaded
  inspector(wanted());
  render();
})();
