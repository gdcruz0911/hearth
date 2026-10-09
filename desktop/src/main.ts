// The Hearth app (ADR-0038): one window on the backend it started, credentials held here and never given to the page.

import { app, BrowserWindow, dialog, Menu, session, shell, type Session } from "electron";
import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { homedir } from "node:os";
import { join } from "node:path";
import { get, launch, type Backend } from "./backend.js";
import { isExternalLink, isOwned, withSessionHeader } from "./guard.js";
import { backendCommand, hearthHome, profilePath, profilePresent, type BackendCommand } from "./runtime.js";

interface Running extends Backend {
  session: Session;
}

let running: Running | null = null;
let window: BrowserWindow | null = null;
let quitting = false;
let stoppedReason = "";

// One start at a time: a backend is not in `running` until its handshake succeeds, so `starting` covers the wait.
let starting: Promise<void> | null = null;

function start(): Promise<void> {
  starting ??= launchBackend().finally(() => (starting = null));
  return starting;
}

async function launchBackend(): Promise<void> {
  stoppedReason = "";
  const command = backendCommand();
  if ("problem" in command) return void showStopped(command.problem);
  // The same profile wherever the app was opened from; without one, nothing starts until the person sets it up.
  const profile = profilePath();
  if (!profilePresent(profile) && !await setUp(command, profile, showStopped(`Hearth is not set up yet: there is no profile at ${profile}.`))) {
    return void showStopped(`Hearth needs a profile at ${profile} that names your knowledge base. Choose Hearth > Restart Backend to set one up.`);
  }
  let backend: Backend;
  console.log("Hearth: starting the backend.");  // Lifecycle lines only; the handshake's secrets are never logged.
  try {
    // Started in the home folder, so nothing it does can land in whatever folder the app was opened from.
    backend = await launch([...command.prefix, "--profile", profile, "web", "--desktop", "--handshake-fd", "3"], command.env, homedir());
  } catch (error) {
    console.log(`Hearth: the backend did not start: ${(error as Error).message}`);
    return void showStopped(`Hearth could not start its backend. ${(error as Error).message}`);
  }
  console.log(`Hearth: the backend is ready at ${backend.origin}.`);
  // A fresh in-memory partition per backend: nothing from an earlier backend's session can reach this one.
  const partition = session.fromPartition(`hearth-${randomUUID()}`, { cache: false });
  const current: Running = { ...backend, session: partition };
  partition.setPermissionRequestHandler((_contents, _permission, decide) => decide(false));
  partition.setPermissionCheckHandler(() => false);
  partition.webRequest.onBeforeRequest((details, decide) => {
    decide({ cancel: running !== current || !isOwned(details.url, current.origin) || details.resourceType === "subFrame" });
  });
  partition.webRequest.onBeforeSendHeaders((details, decide) => {
    const token = running === current ? current.handshake.token : null;
    decide({ requestHeaders: withSessionHeader(details.requestHeaders, details.url, current.origin, token) });
  });
  // Running, and watched for its exit, before any await: a backend that dies from here on is noticed, never trusted.
  running = current;
  backend.child.once("exit", (code, signal) => stopped(current, signal ? `signal ${signal}` : `exit code ${code}`));
  if (backend.child.exitCode !== null || backend.child.signalCode !== null) {
    return stopped(current, "before it was ready");  // Exited while the challenge was answered, before the listener above.
  }
  await partition.cookies.set({ url: current.origin, name: backend.handshake.cookie_name, value: backend.handshake.cookie,
                                httpOnly: true, sameSite: "strict", path: "/" });
  if (running !== current) return;  // Stopped during the await; stopped() has shown it.
  window?.destroy();
  window = null;
  openWindow();
}

// Setup, when there is no profile: point it at an existing knowledge base, or create a new one. The CLI writes the profile and
// checks the data, so these rules live in one place; it never overwrites a profile or changes a chosen database.
// The dialogs are sheets on the app's window: one with no window would be app-modal, and the app could not quit while it is open.
async function setUp(command: BackendCommand, profile: string, parent: BrowserWindow): Promise<boolean> {
  for (;;) {
    if (parent.isDestroyed()) return false;
    const { response } = await dialog.showMessageBox(parent, {
      type: "info", buttons: ["Choose Existing Database…", "Create New Knowledge Base…", "Quit"], defaultId: 0, cancelId: 2,
      message: "Set up Hearth's knowledge base",
      detail: `Hearth keeps its settings in ${profile}, which does not exist yet. Choose the Hearth database you already use, `
              + "or create a new, empty knowledge base. Nothing is moved or changed.",
    });
    if (response === 2) return false;
    const chosen = response === 0
      ? (await dialog.showOpenDialog(parent, { title: "Choose your Hearth database", properties: ["openFile"],
                                       filters: [{ name: "Hearth database", extensions: ["sqlite", "db"] }] })).filePaths[0]
      : (await dialog.showSaveDialog(parent, { title: "Create a new knowledge base", defaultPath: join(hearthHome(), "hearth.sqlite") })).filePath;
    if (!chosen) continue;
    const created = await profileCreate(command, [profile, "--database", chosen, ...(response === 1 ? ["--create-database"] : [])]);
    if (created === null) return true;
    await dialog.showMessageBox(parent, { type: "warning", message: "Hearth did not use that file.", detail: created });
  }
}

function profileCreate(command: BackendCommand, args: string[]): Promise<string | null> {
  return new Promise((done) => execFile(command.prefix[0], [...command.prefix.slice(1), "profile", "create", ...args],
                                        { env: command.env, cwd: homedir(), timeout: 60_000 },
                                        (error, _stdout, stderr) => done(error ? stderr.trim() || error.message : null)));
}

// Order matters: block every request first, then drop the header hook, the secrets, and the stored session.
function stopped(backend: Running, how: string): void {
  if (running !== backend) return;
  console.log(`Hearth: the backend stopped (${how}).`);
  running = null;
  backend.session.webRequest.onBeforeRequest((_details, decide) => decide({ cancel: true }));
  backend.session.webRequest.onBeforeSendHeaders(null);
  backend.handshake = { ...backend.handshake, cookie: "", token: "" };
  void backend.session.clearStorageData();
  if (quitting) return;
  showStopped(`Hearth's backend stopped (${how}). Tasks keep running in their own processes. Choose Hearth > Restart Backend to start it again.`);
}

function showStopped(reason: string): BrowserWindow {
  stoppedReason = reason;
  window?.destroy();
  window = null;
  openWindow();
  return window!;
}

function openWindow(): void {
  if (window) return void window.focus();
  // The stopped page has its own empty partition, which loads nothing from the network at all.
  const partition = running ? running.session : session.fromPartition(`hearth-stopped-${randomUUID()}`, { cache: false });
  if (!running) partition.webRequest.onBeforeRequest((details, decide) => decide({ cancel: !details.url.startsWith("data:") }));
  window = new BrowserWindow({
    width: 1280, height: 860, title: "Hearth",
    webPreferences: { session: partition, sandbox: true, contextIsolation: true, nodeIntegration: false, webviewTag: false, spellcheck: false },
  });
  window.on("closed", () => (window = null));
  if (running) void window.loadURL(`${running.origin}/`);
  else void window.loadURL(stoppedPage(stoppedReason));
}

function stoppedPage(reason: string): string {
  const escaped = reason.replace(/[&<>"]/g, (character) => `&#${character.charCodeAt(0)};`);
  const html = `<!doctype html><meta charset=utf-8><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">`
    + "<style>:root{--bg:#17191d;--text:#ebecef;--muted:#969da9;--ember:#f2a26e;color-scheme:dark}"
    + "@media (prefers-color-scheme:light){:root{--bg:#f7f8fa;--text:#222b3c;--muted:#677184;--ember:#a8481c;color-scheme:light}}"
    + "body{margin:0;padding:3rem;background:var(--bg);color:var(--text);font:15px -apple-system,BlinkMacSystemFont,sans-serif}"
    + "h1{color:var(--ember);font-size:20px;margin:0 0 12px}p{max-width:46rem;line-height:1.5;white-space:pre-wrap}</style>"
    + `<title>Hearth</title><h1>Hearth</h1><p>${escaped}</p>`;
  return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
}

// Every web contents the app ever creates gets the same limits, not only the main window's.
app.on("web-contents-created", (_event, contents) => {
  const leave = (event: Electron.Event, url: string) => {
    if (isOwned(url, running?.origin ?? null)) return;
    event.preventDefault();
    if (isExternalLink(url)) void shell.openExternal(url);
  };
  contents.on("will-navigate", leave);
  contents.on("will-redirect", (event, url) => {
    if (!isOwned(url, running?.origin ?? null)) event.preventDefault();
  });
  contents.on("will-frame-navigate", (event) => {
    if (!event.isMainFrame) event.preventDefault();
  });
  contents.on("will-attach-webview", (event) => event.preventDefault());
  contents.setWindowOpenHandler(({ url }) => {
    if (isExternalLink(url)) void shell.openExternal(url);
    return { action: "deny" };
  });
});

// "unknown" when the backend cannot say, which is never taken to mean no rebuild is running.
async function rebuildState(backend: Running): Promise<"running" | "idle" | "unknown"> {
  try {
    const reply = await get(`${backend.origin}/api/semantic-index`, {
      Cookie: `${backend.handshake.cookie_name}=${backend.handshake.cookie}`, "X-Hearth-Session": backend.handshake.token,
    });
    const status = (JSON.parse(reply.body) as { job?: { status?: unknown } }).job?.status;
    if (reply.status !== 200 || typeof status !== "string") return "unknown";
    return status === "running" || status === "cancelling" ? "running" : "idle";
  } catch {
    return "unknown";
  }
}

// Quitting stops only the backend's own process, never its group, so resumed task loops keep running (ADR-0038).
async function quit(): Promise<void> {
  const backend = running;
  const state = backend ? await rebuildState(backend) : "idle";
  if (state !== "idle") {
    const { response } = await dialog.showMessageBox({
      type: "warning", buttons: ["Keep Running", "Quit"], defaultId: 0, cancelId: 0,
      message: state === "running" ? "A semantic-index rebuild is running." : "Hearth could not check whether a semantic-index rebuild is running.",
      detail: "Quitting cancels any rebuild and leaves the current index as it was. Hearth waits until the rebuild has stopped and cleaned up.",
    });
    if (response === 0) return;
  }
  quitting = true;
  if (backend && backend.child.exitCode === null && backend.child.signalCode === null) {
    const exited = new Promise((resolve) => backend.child.once("exit", resolve));
    backend.child.kill("SIGTERM");
    await exited;
  }
  app.exit(0);
}

// HEARTH_HOME moves Hearth's records for trial runs, so it moves the app's own data, and its single-instance lock, with them.
if (process.env.HEARTH_HOME) app.setPath("userData", join(process.env.HEARTH_HOME, "app"));

if (!app.requestSingleInstanceLock()) {
  app.exit(0);
} else {
  app.on("second-instance", () => openWindow());
  app.on("window-all-closed", () => {});  // Closing the window keeps the app and backend running in the Dock.
  app.on("activate", () => openWindow());
  // Every quit request waits for the same one: a second Cmd+Q must neither skip the wait nor stack another dialog.
  let asking: Promise<void> | null = null;
  app.on("before-quit", (event) => {
    event.preventDefault();
    asking ??= quit().finally(() => (asking = null));
  });
  void app.whenReady().then(() => {
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      { label: "Hearth", submenu: [
        { label: "Restart Backend", click: () => { if (!running) void start(); } },  // start() ignores a click while one is starting.
        { type: "separator" },
        { role: "quit" },
      ] },
      { role: "editMenu" },
      { label: "View", submenu: [{ role: "reload" }] },
      { role: "windowMenu" },
    ]));
    return start();
  });
}
