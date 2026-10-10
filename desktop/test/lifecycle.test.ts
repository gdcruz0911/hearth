// ADR-0038's lifecycle, against the real app: reload, closing and reopening, a second launch, and quitting during a rebuild.

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import electronPath from "electron";
import { backendLog, desktop, fakeBackend, openApp, pause, scratch, windowShowing } from "./helpers.ts";

const alive = (pid: number) => { try { return process.kill(pid, 0); } catch { return false; } };

test("a reload keeps the session, because the app holds both credentials", async (t) => {
  const home = scratch();
  const { app } = await openApp(t, home, fakeBackend);
  const page = await windowShowing(app, "fake dashboard");

  await page.reload();
  await page.evaluate(() => fetch("/api/after-reload"));

  const sent = backendLog(home).requests.find((request) => request.path === "/api/after-reload")!.headers;
  assert.equal(sent["X-Hearth-Session"], backendLog(home).secrets[0].token);
});

test("closing the window keeps the app and backend running, and the Dock icon reopens it on the same session", async (t) => {
  const home = scratch();
  const { app } = await openApp(t, home, fakeBackend);
  await windowShowing(app, "fake dashboard");

  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  await pause(500);
  const whileClosed = { windows: await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), backend: alive(backendLog(home).secrets[0].pid) };
  await app.evaluate(({ app }) => void app.emit("activate"));
  await windowShowing(app, "fake dashboard");

  assert.deepEqual(whileClosed, { windows: 0, backend: true });
  assert.equal(backendLog(home).secrets.length, 1);
});

test("a second launch focuses the running app instead of starting another backend", async (t) => {
  const home = scratch();
  const { app } = await openApp(t, home, fakeBackend);
  await windowShowing(app, "fake dashboard");
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());

  const second = spawn(electronPath as unknown as string, [desktop], { env: { PATH: "/usr/bin:/bin", HOME: join(home.dir, "home"),
    HEARTH_HOME: join(home.dir, "hearth"), HEARTH_BACKEND: JSON.stringify(fakeBackend), FAKE_BACKEND_LOG: home.log }, stdio: "ignore" });
  t.after(() => void second.kill("SIGKILL"));
  const code = await Promise.race([new Promise((done) => second.once("exit", done)), pause(15_000).then(() => "still running")]);
  await windowShowing(app, "fake dashboard");

  assert.equal(code, 0);
  assert.equal(backendLog(home).secrets.length, 1);
});

test("quitting during a rebuild asks first, and exits only after the backend has released it", async (t) => {
  const home = scratch();
  const { app } = await openApp(t, home, fakeBackend, { env: { FAKE_BACKEND_MODE: "rebuilding" } });
  await windowShowing(app, "fake dashboard");
  const answer = (response: number) => app.evaluate(({ dialog }, chosen) => {
    const asked = ((globalThis as unknown as { asked?: string[] }).asked ??= []);
    dialog.showMessageBox = (async (options: Electron.MessageBoxOptions) => (asked.push(options.message), { response: chosen, checkboxChecked: false })) as typeof dialog.showMessageBox;
  }, response);

  await answer(0);  // Keep Running.
  await app.evaluate(({ app }) => app.quit());
  await pause(500);
  const kept = { backend: alive(backendLog(home).secrets[0].pid), windows: await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length),
                 asked: await app.evaluate(() => (globalThis as unknown as { asked: string[] }).asked) };
  await answer(1);  // Quit.
  const exited = new Promise<number>((done) => app.process().once("exit", () => done(Date.now() / 1000)));
  // A second quit while the first waits, like a second Cmd+Q, must neither cut the wait short nor ask again.
  const askedBefore = kept.asked.length;
  const askedAgain = await app.evaluate(async ({ app }) => {
    const asked = (globalThis as unknown as { asked: string[] }).asked;
    app.quit();
    await new Promise((done) => setTimeout(done, 200));
    app.quit();
    await new Promise((done) => setTimeout(done, 200));
    return asked.length;
  }).catch(() => -1);
  const exitedAt = await exited;

  const released = readFileSync(home.log, "utf8").trim().split("\n").map((line) => JSON.parse(line)).find((line) => "released" in line);
  assert.deepEqual(kept, { backend: true, windows: 1, asked: ["A semantic-index rebuild is running."] });
  assert.ok(released, "the backend released the rebuild");
  assert.ok(released.released <= exitedAt, "the app exited only after the release");
  assert.equal(askedAgain, askedBefore + 1, "one more question for both quits");
});

test("when the app cannot tell whether a rebuild runs, quitting asks instead of cancelling it unseen", async (t) => {
  const home = scratch();
  const { app } = await openApp(t, home, fakeBackend, { env: { FAKE_BACKEND_MODE: "status-hangs" } });
  await windowShowing(app, "fake dashboard");
  await app.evaluate(({ dialog }) => {
    const asked = ((globalThis as unknown as { asked?: string[] }).asked ??= []);
    dialog.showMessageBox = (async (options: Electron.MessageBoxOptions) => (asked.push(options.message), { response: 0, checkboxChecked: false })) as typeof dialog.showMessageBox;
  });

  await app.evaluate(({ app }) => app.quit());
  await pause(6_000);  // Past the status request's five-second limit.

  assert.deepEqual(await app.evaluate(() => (globalThis as unknown as { asked: string[] }).asked),
                   ["Hearth could not check whether a semantic-index rebuild is running."]);
  assert.equal(alive(backendLog(home).secrets[0].pid), true);
  await app.evaluate(({ dialog }) => void (dialog.showMessageBox = (async () => ({ response: 1, checkboxChecked: false })) as typeof dialog.showMessageBox));
});

test("clicking Restart twice while a backend starts launches only one", async (t) => {
  const home = scratch();
  const { app } = await openApp(t, home, fakeBackend);
  await windowShowing(app, "fake dashboard");
  process.kill(backendLog(home).secrets[0].pid, "SIGKILL");
  await windowShowing(app, "Hearth's backend stopped");

  await app.evaluate(({ Menu }) => {
    const restart = Menu.getApplicationMenu()!.items[0].submenu!.items[0];
    restart.click();
    restart.click();
  });
  await windowShowing(app, "fake dashboard");
  await pause(1_000);

  const replacements = backendLog(home).secrets.slice(1);
  assert.equal(replacements.length, 1);
  assert.equal(alive(replacements[0].pid), true);
});

test("the stopped page follows Hearth's dark and light theme", async (t) => {
  const home = scratch();
  const { app } = await openApp(t, home, fakeBackend, { env: { FAKE_BACKEND_MODE: "wrong-answer" } });
  const page = await windowShowing(app, "could not prove");
  const colors = () => page.evaluate(() => [getComputedStyle(document.body).backgroundColor, getComputedStyle(document.body).color]);

  // Playwright emulates a light scheme on the pages it drives, so the Mac's appearance is set through it.
  const shown = async (theme: "dark" | "light") => {
    await page.emulateMedia({ colorScheme: theme });
    return colors();
  };
  const dark = await shown("dark");
  const light = await shown("light");

  assert.deepEqual(dark, ["rgb(23, 25, 29)", "rgb(235, 236, 239)"]);
  assert.deepEqual(light, ["rgb(247, 248, 250)", "rgb(34, 43, 60)"]);
});
