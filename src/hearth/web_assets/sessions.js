// The Sessions view (ADR-0041): direct conversations with one CLI, from api/sessions, with each turn's events read
// through the same transcript endpoint as tasks. Every value is written with textContent.
(() => {
  const { el, shown, badge, api } = window.Hearth;
  const $ = (id) => document.getElementById(id);
  const AGENTS = { claude: "Claude Code", codex: "Codex" };
  const state = { visible: false, selected: null, options: null, sessions: [], detail: null, turns: new Map(), sending: null, timer: 0, said: "",
                  drafts: new Map(), saving: new Set(), seq: 0, places: new Map(), place: null, changesKey: "", formProject: null };

  function say(text) {
    // One polite live region for the conversation's state, announced only when it changes.
    if (text && text !== state.said) $("convo-status").textContent = state.said = text;
  }

  function agent(provider) {
    return AGENTS[provider] || shown(provider);
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
    // One dot per conversation state; the words beside it, or its label, carry the meaning too.
    const node = el("span", `state-dot ${status || "new"}`);
    node.setAttribute("role", "img");
    node.setAttribute("aria-label", status || "no turn yet");
    return node;
  }

  function renderList() {
    // The other conversations in this one's project, as compact tabs; the sidebar (shell.js) lists them all.
    const tabs = $("convo-tabs");
    const project = state.detail ? state.detail.project : null;
    const mine = state.sessions.filter((item) => item.project === project);
    tabs.replaceChildren(...mine.map((item) => {
      const tab = el("a", "convo-tab");
      tab.href = `#/sessions/${encodeURIComponent(item.id)}`;
      if (item.id === state.selected) tab.setAttribute("aria-current", "page");
      tab.title = `${agent(item.provider)} · ${shown(item.title)}`;
      tab.append(dot(item.status), el("span", "", `${agent(item.provider)} · ${shown(item.title)}`));
      return tab;
    }));
    const add = el("a", "convo-tab new", "+ New conversation");
    add.href = `#/sessions/new`;
    tabs.append(add);
  }

  // ---------- the new-conversation form ----------

  function showForm() {
    // As in Claude's and Codex's own apps, a new conversation starts where the keeper just was: the conversation they were
    // viewing, or else the latest one. Never a pick left from an earlier form, which sent two proof conversations astray.
    if ($("convo-form").hidden) {  // Only when the form opens: a second route to it while open keeps what it shows.
      const from = state.sessions.find((item) => item.id === state.selected);
      state.formProject = from ? from.project : null;
    }
    select(null);
    $("convo-pane").hidden = true;
    $("convo-form").hidden = false;
    fillForm();
    renderList();
    $("convo-project").focus();
  }

  function fillForm() {
    const options = state.options || { projects: {}, efforts: [] };
    const select = $("convo-project");
    const names = Object.keys(options.projects);
    const want = state.formProject || (state.sessions[0] || {}).project;
    select.replaceChildren(...names.map((name) => { const o = el("option", "", name); o.value = name; return o; }));
    if (names.includes(want)) select.value = want;
    $("convo-form-error").hidden = names.length > 0;
    if (!names.length) {
      $("convo-form-error").textContent = "No projects yet. Next: add one to ~/.hearth/projects.json, then reopen this form.";
    }
    $("convo-start").disabled = !names.length;
    fillAgents();
  }

  function fillAgents() {
    const options = state.options || { projects: {} };
    const project = options.projects[$("convo-project").value] || { providers: [], models: {} };
    const box = $("convo-agents");
    const was = (box.querySelector("input:checked") || {}).value;
    box.replaceChildren(...Object.keys(AGENTS).map((provider) => {
      const offered = project.providers.includes(provider);
      const label = el("label", `choice${offered ? "" : " off"}`);
      const input = el("input");
      Object.assign(input, { type: "radio", name: "convo-agent", value: provider, disabled: !offered });
      label.append(input, el("span", "", agent(provider)));
      if (!offered) label.append(el("span", "choice-note", "not allowed for this project"));
      return label;
    }));
    const pick = project.providers.includes(was) ? was : project.providers[0];
    if (pick) box.querySelector(`input[value="${pick}"]`).checked = true;
    $("convo-start").disabled = !pick;
    if (!pick && $("convo-project").value) {
      $("convo-form-error").hidden = false;
      $("convo-form-error").textContent = "This project allows neither Claude Code nor Codex. Next: add one to its providers in ~/.hearth/projects.json.";
    }
    fillModel();
  }

  function fillModel() {
    const options = state.options || { projects: {} };
    const project = options.projects[$("convo-project").value] || { models: {} };
    const provider = (document.querySelector("#convo-agents input:checked") || {}).value;
    $("convo-model").value = (provider && project.models[provider]) || "";
    $("convo-model").placeholder = provider ? "the CLI's default" : "";
  }

  async function start(event) {
    event.preventDefault();
    const button = $("convo-start");
    const provider = (document.querySelector("#convo-agents input:checked") || {}).value;
    button.disabled = true;
    button.textContent = "Starting…";
    try {
      const { session } = await api("sessions", {
        project: $("convo-project").value, provider, mode: document.querySelector("input[name=convo-mode]:checked").value,
        model: $("convo-model").value.trim(), effort: $("convo-effort").value, request: requestId(),
      });
      window.location.hash = `#/sessions/${encodeURIComponent(session.id)}`;
      setTimeout(() => $("convo-message").focus(), 0);
    } catch (error) {
      $("convo-form-error").hidden = false;
      $("convo-form-error").textContent = error.message;
    } finally {
      button.disabled = false;
      button.textContent = "Start conversation";
    }
  }

  function requestId() {
    return (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`).replace(/[^A-Za-z0-9_-]/g, "");
  }

  // ---------- the conversation ----------

  function select(id) {
    if (id === state.selected) return;
    if (state.selected) {
      saveDraft(state.selected); // Saved on every switch, not only after the pause.
    } // The reading position is kept by the scroll listener: here the view may have just been unhidden, with its offset lost.
    state.selected = id;
    state.place = id ? state.places.get(id) || { bottom: true } : null;
    state.changesKey = "";
    $("convo-changes-count").textContent = "";
    $("convo-changes-card").hidden = true;
    $("convo-changes-body").replaceChildren(el("p", "empty", id ? "Reading this conversation's changes…" : "Open a conversation to see what it changed."));
    $("convo-meta").replaceChildren(el("p", "empty", id ? "" : "Open a conversation to see how it runs."));
    $("convo-commands").replaceChildren();
    $("convo-open").disabled = !id;
    state.detail = null;
    state.turns = new Map();
    state.sending = null;
    $("convo-log").replaceChildren();
    $("convo-form").hidden = true;
    $("convo-pane").hidden = !id;
    $("convo-message").value = (state.drafts.get(id) || {}).text || "";
    $("convo-send-error").hidden = true;
    renderList();
  }

  // ---------- drafts (ADR-0044) ----------
  // Each draft has a version that only grows. The server keeps the newest it has seen, so a save that arrives late,
  // or after its text was sent, changes nothing. The shell's storage is in memory, so drafts live in the session's folder.

  function version() {
    state.seq = Math.max(Date.now(), state.seq + 1);
    return state.seq;
  }

  function typed(id, text) {
    const draft = state.drafts.get(id) || { saved: 0, timer: 0 };
    Object.assign(draft, { text, at: version(), clearing: false });
    state.drafts.set(id, draft);
    clearTimeout(draft.timer);
    draft.timer = setTimeout(() => saveDraft(id), 400);
  }

  function saveDraft(id) {
    const draft = state.drafts.get(id);
    if (!draft) return Promise.resolve();
    clearTimeout(draft.timer);
    if (draft.at <= draft.saved) return Promise.resolve();
    const { text, at } = draft;
    const saving = api(`sessions/${encodeURIComponent(id)}/draft`, { text, at })
      .then(() => { draft.saved = Math.max(draft.saved, at); draft.failed = false; })
      .catch(() => { draft.failed = true; }) // Kept in the page; the next keystroke, switch, or quit tries again.
      .then(() => { if (id === state.selected) composer(); })
      .finally(() => state.saving.delete(saving));
    state.saving.add(saving);
    return saving;
  }

  async function flushDrafts() {
    // The app calls this before it stops the backend: true only once every draft is saved, so a failed save is never quit past unseen.
    for (const id of state.drafts.keys()) saveDraft(id);
    await Promise.allSettled([...state.saving]);
    return [...state.drafts.values()].every((draft) => draft.at <= draft.saved);
  }
  window.Hearth.flushDrafts = flushDrafts;

  function adoptDraft(session) {
    // The saved draft fills the composer only when this page has nothing newer for the conversation.
    const local = state.drafts.get(session.id);
    if (local && local.at >= session.draft.at) return;
    state.drafts.set(session.id, { text: session.draft.text, at: session.draft.at, saved: session.draft.at, timer: 0 });
    state.seq = Math.max(state.seq, session.draft.at);
    if (session.id === state.selected) $("convo-message").value = session.draft.text;
  }

  function running() {
    const turns = (state.detail && state.detail.turns) || [];
    const last = turns[turns.length - 1];
    if (!last) return null;
    if (!last.finished) return last;
    return last.check && !last.check.finished ? last.check : null; // The check Hearth runs after a turn stops like a turn.
  }

  function renderDetail() {
    const session = state.detail;
    if (!session) return;
    $("convo-title").textContent = shown(session.title);
    $("convo-project-name").textContent = session.project;
    const field = (label, value) => {
      const row = el("div", "field");
      row.append(el("span", "field-label", label), el("span", "field-value", shown(value)));
      return row;
    };
    $("convo-meta").replaceChildren(field("Agent", agent(session.provider)), field("Model asked for", session.model || "the CLI's default"),
      field("Effort", session.effort), field("May", session.mode === "edit" ? "edit its own worktree" : "read only"),
      field("Agent session kept", session.resumable ? "yes, the next message resumes it" : "not yet"), field("Work folder", session.worktree));
    $("convo-meta").lastElementChild.lastElementChild.classList.add("path");
    $("convo-mode").replaceChildren(icon("shield"), el("span", "", session.mode === "edit" ? "Own worktree" : "Read only"));
    // The recipient is fixed for a conversation: another agent is another conversation.
    $("convo-recipient").replaceChildren(icon("flame"), el("span", "", "To "), el("strong", "", agent(session.provider)),
      el("span", "", `· ${session.model || "CLI default"} · ${session.effort}`));
    renderList();
    const commands = $("convo-commands");
    commands.replaceChildren(el("span", "", session.commands?.description || "Command permissions not reported."));
    if (session.commands?.check) commands.append(" ", el("code", "", session.commands.check), " may run.");
    if (session.commands?.turn_check) commands.append(" Codex was told to run ", el("code", "", session.commands.turn_check), " for its own checks.");
    if (session.commands?.host_check) commands.append(" After a turn that changes files, Hearth runs ", el("code", "", session.commands.host_check), " outside the sandbox.");
    commands.append(" Hearth cannot approve extra commands here.");
    $("convo-badge").replaceChildren(session.status ? badge(session.status) : el("span", "badge other", "new"));
    const finished = session.turns.filter((turn) => turn.finished).length;
    const key = `${session.id}:${finished}`;
    if (key !== state.changesKey) {
      state.changesKey = key;
      loadChanges(session.id, key);
    }
    const log = $("convo-log");
    if (!session.turns.length && !log.children.length) {
      log.append(el("li", "empty convo-empty", "Starting a conversation runs nothing. Your first message starts the agent; Hearth shows what it does as it happens."));
    }
    for (const turn of session.turns) {
      let view = state.turns.get(turn.dir);
      if (!view) {
        log.querySelector(".convo-empty")?.remove();
        view = turnView(turn);
        state.turns.set(turn.dir, view);
        stick(() => log.append(view.node));
      }
      view.turn = turn;
      footer(view);
    }
    composer();
  }

  function turnView(turn) {
    const node = el("li", "convo-turn");
    const clock = (value) => {
      const at = window.Hearth.time(value);
      return el("time", "", at ? at.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "");
    };
    const mine = el("div", "message mine");
    const myHead = el("div", "message-head", "You");
    myHead.append(clock(turn.started));
    const myBody = el("div");
    myBody.append(myHead, el("p", "said-text", turn.message));
    const me = el("span", "avatar you");
    me.append(icon("user"));
    mine.append(me, myBody);
    const reply = el("div", "message reply");
    const head = el("div", "message-head", agent(state.detail.provider));
    head.append(clock(turn.started), el("small", "", "agent output"));
    const items = el("ol", "reply-items");
    const foot = el("div", "turn-foot");
    const body = el("div");
    body.append(head, items, foot);
    const avatar = el("span", "avatar agent");
    avatar.append(icon("flame"));
    reply.append(avatar, body);
    node.append(mine, reply);
    return { node, items, foot, turn, offset: 0, done: false, draft: null, tool: null, footKey: "", work: null, steps: 0, failed: 0 };
  }

  function footer(view) {
    const { turn } = view;
    // Whether anything arrived is part of the key: on reopening, the footer is drawn before the transcript is read.
    const key = JSON.stringify([turn.finished, turn.outcome, turn.model_used, turn.warnings, turn.error, Boolean(view.draft),
      view.items.children.length > 0, turn.left_running, turn.left_in_folder, turn.cleanup_unchecked, turn.check]);
    if (key === view.footKey) return;
    view.footKey = key;
    const parts = [];
    if (!turn.finished) {
      const line = el("p", "turn-live");
      line.append(el("span", "pulse"), el("span", "", view.items.children.length ? "Working…" : "Starting the agent…"));
      parts.push(line);
    } else {
      const line = el("p", "turn-meta");
      const took = window.Hearth.span(turn.started, turn.finished);
      line.append(badge(turn.outcome), ...(took ? [el("span", "", took)] : []), el("span", "", `asked for ${turn.model || "the CLI's default"} · reported ${shown(turn.model_used)}`));
      if (turn.sandbox_used) line.append(el("span", "", `sandbox ${turn.sandbox_used}`));
      parts.push(line);
      if ((turn.warnings || []).some((warning) => warning.includes("unrecognized_model"))) {
        parts.push(el("p", "turn-note", "Claude Code flagged this model name as unrecognized. Whether a different model answered is not known."));
      }
      if (turn.outcome === "cancelled") {
        // Who stopped it comes only from the record; older records did not keep it, and it is never guessed.
        const who = { keeper: "You stopped this reply.", quit: "Hearth stopped this reply when it quit." }[turn.stopped_by] || "Stop reason not recorded.";
        const kept = view.draft || view.items.children.length
          ? " Hearth keeps what arrived above; the agent's own session does not, so it will not remember this reply."
          : " No output had arrived.";
        parts.push(el("p", "turn-note", who + kept));
      }
      // What a stop left behind comes only from the record, so a stop is never shown as complete when it was not.
      if ((turn.left_running || []).length) {
        parts.push(el("p", "turn-note failed", `Hearth could not stop ${turn.left_running.length} process(es) this reply started: ${turn.left_running.join(", ")}. Check them with ps.`));
      }
      if ((turn.left_in_folder || []).length) {
        parts.push(el("p", "turn-note failed", `Still running in this conversation's folder, and not stopped by Hearth: ${turn.left_in_folder.join(", ")}. Check them with ps.`));
      }
      if (turn.cleanup_unchecked) {
        parts.push(el("p", "turn-note failed", "Hearth could not list processes when it stopped this reply, so some of its work may still be running."));
      }
      if (turn.outcome === "interrupted") {
        parts.push(el("p", "turn-note failed", "Hearth stopped this reply when it quit or lost its backend. Nothing was resent; send again to continue."));
      }
      if (turn.outcome === "failed") parts.push(el("p", "turn-note failed", shown(turn.error)));
      if (turn.check) parts.push(checkView(turn.check));
    }
    if (view.draft && turn.finished) view.draft.classList.add("partial");
    workLine(view);
    view.foot.replaceChildren(...parts);
  }

  function checkView(check) {
    // ADR-0047: the project's check Hearth ran after this turn, outside the sandbox; its result opens the next message.
    const box = el("div", "turn-foot");
    const line = el("p", check.finished ? "turn-meta" : "turn-live");
    if (!check.finished) {
      line.append(el("span", "pulse"), el("span", "", "Hearth is running "), el("code", "", check.command), el("span", "", " outside the sandbox…"));
      box.append(line);
      return box;
    }
    line.append(badge(check.outcome), el("span", "", "Hearth ran "), el("code", "", check.command),
      el("span", "", check.exit_code === null ? "" : ` · exit code ${check.exit_code}`));
    box.append(line);
    if (check.outcome === "cancelled") {
      box.append(el("p", "turn-note", { keeper: "You stopped the check.", quit: "Hearth stopped the check when it quit." }[check.stopped_by] || "Stop reason not recorded."));
    }
    if (check.outcome === "interrupted") box.append(el("p", "turn-note failed", "Hearth stopped the check when it lost its backend."));
    if (check.error) box.append(el("p", "turn-note failed", check.error));
    if (check.tail) box.append(el("pre", "step-output", check.tail));
    box.append(el("p", "turn-note", "Your next message to the agent starts with this result."));
    return box;
  }

  async function readTurn(view) {
    if (view.reading) return; // A second refresh while one is reading would show the same events twice.
    view.reading = true;
    try {
      await readPages(view);
    } finally {
      view.reading = false;
    }
  }

  async function readPages(view) {
    const id = state.selected;
    for (;;) {
      const page = await api(`workbench/transcript/${encodeURIComponent(id)}/${encodeURIComponent(view.turn.dir)}/${view.offset}`);
      if (id !== state.selected) return;
      const more = page.offset > view.offset;
      view.offset = page.offset;
      stick(() => page.items.forEach((item) => add(view, item)));
      if (page.finished && !more) {
        view.done = true;
        footer(view);
      }
      if (!more) return;
    }
  }

  function add(view, item) {
    // Partial text grows in place and is replaced by the whole message when it lands; a stop keeps what arrived.
    if (item.kind === "delta") {
      if (!view.draft) {
        view.draft = el("li", "reply-text draft");
        view.items.append(view.draft);
      }
      view.draft.textContent += item.text;
    } else if (item.kind === "message") {
      if (view.draft) view.draft.remove();
      view.draft = null;
      const text = el("li", "reply-text md");
      text.append(...window.Hearth.rich(item.text));
      view.items.append(text);
    } else if (item.kind === "tool") {
      const step = el("li", "reply-step");
      const details = el("details");
      const summary = el("summary");
      summary.append(el("span", "step-tool", item.tool || "tool"), el("span", "step-arg", item.text));
      details.append(summary);
      step.append(details);
      steps(view).append(step);
      view.tool = details;
      view.steps += 1;
      workLine(view);
    } else if (item.kind === "output") {
      const pre = el("pre", `step-output${item.error ? " error" : ""}`, item.text || "(no output)");
      if (view.tool) view.tool.append(pre);
      else {  // Output with no tool line before it; append() returns nothing, so it cannot be chained.
        const step = el("li", "reply-step");
        step.append(pre);
        steps(view).append(step);
      }
      if (item.error) {
        view.tool?.classList.remove("done");
        view.tool?.classList.add("error");
        if (view.tool) view.tool.open = true;
        view.failed += 1;
        view.work.open = true; // Folding never hides a failed step.
        workLine(view);
      } else if (!view.tool?.classList.contains("error")) view.tool?.classList.add("done");
    } else if (item.kind === "error" || (item.kind === "result" && item.error)) {
      view.items.append(el("li", "reply-text failed", shown(item.text)));
    }
  }

  function steps(view) {
    // A turn's tool work folds into one line where it began, so the reply reads as text; opening it shows every step.
    if (!view.work) {
      view.work = el("details", "work-steps");
      view.work.append(el("summary"), el("ol", "work-list"));
      const holder = el("li", "reply-work");
      holder.append(view.work);
      view.items.append(holder);
    }
    return view.work.lastElementChild;
  }

  function workLine(view) {
    if (!view.work) return;
    const { turn } = view;
    const took = turn.finished && window.Hearth.span(turn.started, turn.finished);
    const words = [took ? `Worked for ${took}` : turn.finished ? "Worked" : "Working", `${view.steps} step${view.steps === 1 ? "" : "s"}`];
    if (view.failed) words.push(`${view.failed} failed`);
    view.work.firstElementChild.textContent = words.join(" · ");
    view.work.classList.toggle("error", view.failed > 0);
  }

  function restorePlace() {
    // Back where the keeper left this conversation, once its turns are shown; at the bottom if they were there.
    if (!state.place || state.turns.size === 0 || [...state.turns.values()].some((view) => view.reading)) return;
    const scroller = $("convo-scroll");
    scroller.scrollTop = state.place.bottom ? scroller.scrollHeight : state.place.top;
    state.place = null;
  }

  // ---------- changes and the work folder ----------

  async function loadChanges(id, key) {
    try {
      const { changes } = await api(`sessions/${encodeURIComponent(id)}/changes`);
      if (key !== state.changesKey) return;
      const count = changes.files.length + changes.untracked_hidden;
      $("convo-changes-count").textContent = count ? String(count) : "";
      $("convo-changes-card").hidden = !count;
      $("convo-changes-line").replaceChildren(el("strong", "", `${count} changed file${count === 1 ? "" : "s"}`),
        el("span", "diff-count add", `+${changes.added}`), el("span", "diff-count del", `−${changes.removed}`), el("span", "meta", "in this conversation's worktree"));
      const parts = [el("p", "meta", count ? `${count} file${count === 1 ? "" : "s"} · +${changes.added} −${changes.removed} since the conversation began` : "")];
      if (changes.files.length) {
        const list = el("ul", "changed-files");
        const names = { M: "changed", A: "added", D: "deleted", T: "type changed", "?": "new, not committed" };
        changes.files.forEach((file) => {
          const row = el("li");
          row.append(el("span", "id", file.path), el("span", "meta", names[file.status] || file.status));
          list.append(row);
        });
        parts.push(list);
      }
      if (changes.untracked_hidden) parts.push(el("p", "meta", `${changes.untracked_hidden} more new files are not listed. Open the work folder to see them.`));
      if (changes.diff) parts.push(...window.Hearth.files(changes.diff));
      else parts.push(el("p", "empty", "No changes in this conversation's worktree yet."));
      if (changes.truncated) parts.push(el("p", "meta", "This diff is long, so only its beginning is shown. Open the work folder for the rest."));
      $("convo-changes-body").replaceChildren(...parts);
    } catch (failure) {
      if (key !== state.changesKey) return;
      $("convo-changes-count").textContent = "unavailable";
      $("convo-changes-body").replaceChildren(el("p", "status error", failure.message));
    }
  }

  async function openFolder() {
    const from = state.selected;
    try {
      await api(`sessions/${encodeURIComponent(from)}/open`, {});
    } catch (failure) {
      if (state.selected !== from) return; // Shown only with the conversation it is about.
      $("convo-send-error").hidden = false;
      $("convo-send-error").textContent = failure.message;
    }
  }

  function stick(change) {
    // Follow new output only while the keeper is at the bottom; reading further up is never interrupted.
    const scroller = $("convo-scroll");
    const atBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
    change();
    if (atBottom) scroller.scrollTop = scroller.scrollHeight;
  }

  function composer() {
    const turn = running();
    const busy = Boolean(turn) || Boolean(state.sending);
    $("convo-send").disabled = busy || !$("convo-message").value.trim();
    $("convo-send").setAttribute("aria-label", state.sending ? "Sending…" : state.detail ? `Send to ${agent(state.detail.provider)}` : "Send");
    $("convo-send").title = $("convo-send").getAttribute("aria-label");
    $("convo-stop").hidden = !turn;
    $("convo-message").placeholder = state.detail ? `Message ${agent(state.detail.provider)}` : "";
    // Without a queue, text written during a reply waits here: say so, so it is never taken for sent.
    const mine = state.drafts.get(state.selected) || {};
    $("convo-hint").textContent = mine.failed && mine.clearing
      ? "Hearth could not remove the message you sent from this conversation's draft. It tries again; if you quit now, the sent message comes back as a draft when you reopen."
      : mine.failed ? "Draft not saved: Hearth did not answer. It stays here and is saved again as you type."
      : turn && $("convo-message").value.trim()
      ? "Not sent: Hearth does not queue messages. Send it when this reply finishes · Esc stops the reply"
      : turn ? "Esc stops the reply · its output so far is kept" : "Drafts stay with their conversation · ⌘↩ sends";
    if (turn) say(`${agent(state.detail.provider)} is replying.`);
    else if (state.detail && state.detail.status === "done") say("Reply finished.");
    else if (state.detail && state.detail.status === "cancelled") say("Stopped. What arrived before Stop is kept here.");
    else if (state.detail && state.detail.status) say(`The last turn ${state.detail.status === "failed" ? "failed" : "was interrupted"}.`);
  }

  async function send(event) {
    event.preventDefault();
    const message = $("convo-message").value.trim();
    if (!message || running() || state.sending) return;
    // The same ID goes with every retry of this message, so Hearth starts it once however often it arrives.
    state.sending = state.sending || { id: requestId(), message, at: (state.drafts.get(state.selected) || {}).at || 0 };
    composer();
    say(`Sending to ${agent(state.detail.provider)}.`);
    const error = $("convo-send-error");
    const from = state.selected; // The reply may land after the keeper has moved to another conversation.
    const sent = state.sending;
    try {
      const { session } = await api(`sessions/${encodeURIComponent(from)}/send`,
                                    { message: sent.message, send: sent.id, draft_at: sent.at });
      if (state.sending === sent) state.sending = null;
      // A turn that could not start keeps the text, and so does text typed after pressing Send, which is newer.
      // Only the sent conversation's own draft is cleared; the box on screen changes only if it still shows that draft.
      const draft = state.drafts.get(from);
      if (session.status !== "failed" && draft && draft.at === sent.at) {
        // Hearth emptied the saved draft when the turn started; if it could not, the page saves the empty draft as a newer
        // version, retried like any other save and asked about at quit, so the sent text never comes back.
        if (session.draft_cleared === true) Object.assign(draft, { text: "", saved: sent.at });
        else {
          Object.assign(draft, { text: "", at: version(), clearing: true });
          saveDraft(from);
        }
        if (state.selected === from && $("convo-message").value.trim() === sent.message) $("convo-message").value = "";
      }
      if (state.selected === from) error.hidden = true;
      await tick();
    } catch (failure) {
      const offline = failure instanceof TypeError; // The request may or may not have arrived: retry with the same ID.
      if (!offline && state.sending === sent) state.sending = null;
      if (state.selected !== from) return; // Its text stays in that conversation's draft; this box belongs to another.
      error.hidden = false;
      error.textContent = offline ? "Hearth did not answer. Your message is kept; Send retries it without sending it twice." : failure.message;
      composer();
    }
  }

  async function stop() {
    const turn = running();
    if (!turn) return;
    const button = $("convo-stop");
    const from = state.selected;
    button.disabled = true;
    button.textContent = "Stopping…";
    try {
      await api(`sessions/${encodeURIComponent(from)}/stop`, { run: turn.dir });
      await tick();
    } catch (failure) {
      if (state.selected !== from) return; // Shown only with the conversation it is about.
      $("convo-send-error").hidden = false;
      $("convo-send-error").textContent = failure.message;
    } finally {
      button.disabled = false;
      button.textContent = "Stop";
    }
  }

  // ---------- refresh ----------

  async function tick() {
    // The listing refreshes in every view, for the sidebar and the Inbox; a conversation is read only while it is shown.
    clearTimeout(state.timer);
    try {
      const listing = await api("sessions");
      state.sessions = listing.sessions;
      document.dispatchEvent(new CustomEvent("hearth:sessions", { detail: listing }));
      if (state.visible) {
        const changed = JSON.stringify(listing.options) !== JSON.stringify(state.options);
        state.options = listing.options;
        renderList();
        if (changed && !$("convo-form").hidden) fillForm(); // Never rebuilt under the keeper's pointer for nothing.
      }
      // Read from the address, not state.visible: the keeper may have just left, and the page hears of it only after this runs.
      const [view, id] = window.location.hash.replace(/^#\//, "").split("/");
      if (state.visible && !state.selected && $("convo-form").hidden && !id && (view === "sessions" || (view === "" && !$("convo-view").hidden))) {
        // Sessions alone opens the latest conversation, or the form when there is none yet.
        window.location.replace(state.sessions.length ? `#/sessions/${encodeURIComponent(state.sessions[0].id)}` : "#/sessions/new");
      }
      if (state.visible && state.selected) {
        const id = state.selected;
        const { session } = await api(`sessions/${encodeURIComponent(id)}`);
        if (id === state.selected && state.visible) {
          state.detail = session;
          adoptDraft(session);
          renderDetail();
          for (const view of state.turns.values()) if (!view.done) await readTurn(view);
          restorePlace();
        }
      }
    } catch (failure) {
      $("convo-send-error").hidden = false;
      $("convo-send-error").textContent = failure.message;
    }
    state.timer = setTimeout(tick, state.visible && running() ? 700 : 4000);
  }

  function route() {
    const [, id] = window.location.hash.replace(/^#\//, "").split("/").map(decodeURIComponent);
    const was = state.visible;
    state.visible = !$("convo-view").hidden;
    if (state.visible) {
      if (!was && state.selected && (id || null) === state.selected) state.place = state.places.get(state.selected) || { bottom: true };
      if (id === "new") showForm();
      else select(id || null);
      restorePlace();
    }
    tick();
  }

  $("convo-scroll").addEventListener("scroll", () => {
    // Kept as the keeper reads: a view hidden for Tasks or the Inbox has already lost its offset when the route changes.
    const scroller = $("convo-scroll");
    if (state.selected && state.visible && scroller.clientHeight && !state.place) {
      state.places.set(state.selected, { top: scroller.scrollTop, bottom: scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80 });
    }
  }, { passive: true });
  $("convo-cancel").addEventListener("click", () => {
    $("convo-form").hidden = true;
    window.location.hash = "#/sessions";
  });
  $("convo-project").addEventListener("change", () => { state.formProject = $("convo-project").value; fillAgents(); });
  $("convo-agents").addEventListener("change", fillModel);
  $("convo-form").addEventListener("submit", start);
  $("convo-composer").addEventListener("submit", send);
  $("convo-stop").addEventListener("click", stop);
  $("convo-open").addEventListener("click", openFolder);
  $("convo-changes-open").addEventListener("click", () => document.dispatchEvent(new CustomEvent("hearth:inspect", { detail: "changes" })));
  $("convo-message").addEventListener("input", () => {
    if (state.selected) typed(state.selected, $("convo-message").value);
    composer();
  });
  window.addEventListener("pagehide", flushDrafts);
  $("convo-message").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) $("convo-composer").requestSubmit();
  });
  $("convo-view").addEventListener("keydown", (event) => {
    if (event.key === "Escape" && running()) {
      event.preventDefault();
      stop();
    }
  });
  document.addEventListener("hearth:view", () => setTimeout(route, 0));
  window.addEventListener("hashchange", route);
  route();
})();
