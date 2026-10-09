// Launches the built app in a scratch home, so no test touches the person's Hearth, app data, or browser.

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync, mkdirSync } from "node:fs";
import { createServer, type IncomingHttpHeaders, type Server } from "node:http";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import type { TestContext } from "node:test";
import electronPath from "electron";
import { _electron, type ElectronApplication, type Page } from "playwright";

export const desktop = resolve(import.meta.dirname, "..");
export const repo = resolve(desktop, "..");
export const python = join(repo, ".venv/bin/python");

export interface Scratch {
  dir: string;
  log: string;
}

export function scratch(t: TestContext): Scratch {
  const dir = mkdtempSync(join(tmpdir(), "hearth-desktop-"));
  mkdirSync(join(dir, "home"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  return { dir, log: join(dir, "backend.jsonl") };
}

export const fakeBackend = [python, join(desktop, "test/fake_backend.py")];
export const realBackend = (dir: string) => [python, "-m", "hearth.cli", "--database", join(dir, "web.sqlite"), "web", "--desktop", "--handshake-fd", "3"];

export interface App {
  app: ElectronApplication;
  output: () => string;
}

export async function openApp(t: TestContext, home: Scratch, backend: string[], env: Record<string, string> = {}): Promise<App> {
  const app = await _electron.launch({
    executablePath: electronPath as unknown as string,
    args: [desktop],
    env: {
      PATH: "/usr/bin:/bin", HOME: join(home.dir, "home"), HEARTH_HOME: join(home.dir, "hearth"), PYTHONPATH: join(repo, "src"),
      PYTHONDONTWRITEBYTECODE: "1", HEARTH_BACKEND: JSON.stringify(backend), FAKE_BACKEND_LOG: home.log, ...env,
    },
  });
  const child = app.process();  // Kept: Playwright refuses process() once the app has closed.
  let output = "";
  child.stdout?.on("data", (chunk: Buffer) => (output += chunk.toString()));
  child.stderr?.on("data", (chunk: Buffer) => (output += chunk.toString()));
  // Links the app would hand to the person's browser are recorded instead.
  await app.evaluate(({ shell }) => {
    (globalThis as { opened?: string[] }).opened = [];
    shell.openExternal = async (url: string) => void (globalThis as unknown as { opened: string[] }).opened.push(url);
  });
  // Quit as the person would, so the app stops its own backend; a hung quit is killed and fails the test instead.
  t.after(async () => {
    if (child.exitCode !== null || child.signalCode !== null) return;  // The test quit it already.
    const closed = await Promise.race([app.close().then(() => true), pause(15_000).then(() => false)]);
    if (!closed) child.kill("SIGKILL");
    assert.equal(closed, true, "the app did not quit within 15 seconds");
  });
  return { app, output: () => output };
}

export async function opened(app: ElectronApplication): Promise<string[]> {
  return app.evaluate(() => (globalThis as unknown as { opened: string[] }).opened);
}

export async function windowShowing(app: ElectronApplication, text: string): Promise<Page> {
  for (let tries = 0; tries < 100; tries++) {
    for (const page of app.windows()) {
      const body = await page.evaluate(() => document.body?.innerText ?? "").catch(() => "");
      if (body.includes(text)) return page;
    }
    await new Promise((done) => setTimeout(done, 100));
  }
  throw new Error(`No window showed ${JSON.stringify(text)}.`);
}

export interface Logged {
  path: string;
  headers: Record<string, string>;
}

export function backendLog(home: Scratch): { secrets: { pid: number; port: number; cookie: string; token: string }[]; requests: Logged[] } {
  const lines = readFileSync(home.log, "utf8").trim().split("\n").map((line) => JSON.parse(line));
  return { secrets: lines.filter((line) => "pid" in line), requests: lines.filter((line) => "path" in line) };
}

// A server that is not the app's backend: anything it receives is something the app should never have sent.
export async function stranger(t: TestContext, port = 0): Promise<{ origin: string; received: { url: string; headers: IncomingHttpHeaders }[] }> {
  const received: { url: string; headers: IncomingHttpHeaders }[] = [];
  const server: Server = createServer((request, response) => {
    received.push({ url: request.url ?? "", headers: request.headers });
    response.end("stranger");
  });
  await new Promise<void>((done, fail) => server.once("error", fail).listen(port, "127.0.0.1", done));
  t.after(() => new Promise<void>((done) => server.close(() => done())));
  const address = server.address() as { port: number };
  return { origin: `http://127.0.0.1:${address.port}`, received };
}

export function seedTask(home: Scratch, task: Record<string, unknown>): void {
  const dir = join(home.dir, "hearth/tasks", task.id as string);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "task.json"), JSON.stringify(task));
}

export const pause = (ms: number) => new Promise((done) => setTimeout(done, ms));
