// The Hearth app (ADR-0038): one window on the backend it started, credentials held here and never given to the page.

import { app, BrowserWindow, dialog, Menu, session, shell, type Session } from "electron";
import { randomUUID } from "node:crypto";
import { join } from "node:path";
import { get, launch, type Backend } from "./backend.js";
import { isExternalLink, isOwned, withSessionHeader } from "./guard.js";

interface Running extends Backend {
  session: Session;
}

let running: Running | null = null;
let window: BrowserWindow | null = null;
let quitting = false;
let stoppedReason = "";

// Development only: the backend command as a JSON list. D3 replaces it with the managed runtime under Application Support.
function backendCommand(): string[] {
  const command = JSON.parse(process.env.HEARTH_BACKEND ?? "null") as unknown;
  if (!Array.isArray(command) || command.length === 0 || !command.every((part) => typeof part === "string")) {
    throw new Error("Set HEARTH_BACKEND to the backend command, such as [\"/path/to/python\", \"-m\", \"hearth.cli\", \"web\", \"--desktop\", \"--handshake-fd\", \"3\"].");
  }
  return command;
}

async function start(): Promise<void> {
  stoppedReason = "";
  let backend: Backend;
  try {
    backend = await launch(backendCommand(), process.env);
  } catch (error) {
    return showStopped(`Hearth could not start its backend. ${(error as Error).message}`);
  }
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

// Order matters: block every request first, then drop the header hook, the secrets, and the stored session.
function stopped(backend: Running, how: string): void {
  if (running !== backend) return;
  running = null;
  backend.session.webRequest.onBeforeRequest((_details, decide) => decide({ cancel: true }));
  backend.session.webRequest.onBeforeSendHeaders(null);
  backend.handshake = { ...backend.handshake, cookie: "", token: "" };
  void backend.session.clearStorageData();
  if (quitting) return;
  showStopped(`Hearth's backend stopped (${how}). Tasks keep running in their own processes. Choose Hearth > Restart Backend to start it again.`);
}

function showStopped(reason: string): void {
  stoppedReason = reason;
  window?.destroy();
  window = null;
  openWindow();
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
    + `<title>Hearth</title><body style="font:15px -apple-system,sans-serif;margin:3rem;max-width:40rem"><h1>Hearth</h1><p>${escaped}</p>`;
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

async function rebuildRunning(backend: Running): Promise<boolean> {
  try {
    const reply = await get(`${backend.origin}/api/semantic-index`, {
      Cookie: `${backend.handshake.cookie_name}=${backend.handshake.cookie}`, "X-Hearth-Session": backend.handshake.token,
    });
    const job = (JSON.parse(reply.body) as { job?: { status?: string } }).job;
    return reply.status === 200 && (job?.status === "running" || job?.status === "cancelling");
  } catch {
    return false;
  }
}

// Quitting stops only the backend's own process, never its group, so resumed task loops keep running (ADR-0038).
async function quit(): Promise<void> {
  const backend = running;
  if (backend && await rebuildRunning(backend)) {
    const { response } = await dialog.showMessageBox({
      type: "warning", buttons: ["Keep Running", "Quit"], defaultId: 0, cancelId: 0,
      message: "A semantic-index rebuild is running.",
      detail: "Quitting cancels it and leaves the current index as it was. Hearth waits until the rebuild has stopped and cleaned up.",
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
        { label: "Restart Backend", click: () => { if (!running) void start(); } },
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
