// ADR-0038's proofs for the shell, against the real app: what it trusts, what it sends, and what its window may do.

import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";
import { backendLog, fakeBackend, openApp, opened, pause, realBackend, scratch, seedTask, stranger, windowShowing } from "./helpers.ts";

test("a server that cannot answer the challenge is refused and receives no credential", async (t) => {
  const home = scratch();
  const { app } = await openApp(t, home, fakeBackend, { env: { FAKE_BACKEND_MODE: "wrong-answer" } });

  await windowShowing(app, "could not prove it is this launch's backend");

  const { requests } = backendLog(home);
  assert.deepEqual(requests.map((request) => request.path.split("?")[0]), ["/desktop/challenge"]);
  assert.equal(JSON.stringify(requests).toLowerCase().includes("cookie"), false);
  assert.equal(JSON.stringify(requests).toLowerCase().includes("x-hearth-session"), false);
});

test("the token goes only to API routes, the page cannot read either credential, and neither is ever printed", async (t) => {
  const home = scratch();
  const { app, output } = await openApp(t, home, fakeBackend);
  const page = await windowShowing(app, "fake dashboard");
  const [{ cookie, token }] = backendLog(home).secrets;

  await page.evaluate(async () => {
    await fetch("/api/workbench/tasks", { headers: { "X-Hearth-Session": "" } });  // As the dashboard sends it without a fragment.
    await fetch("/assets/dashboard.js", { headers: { "X-Hearth-Session": "forged" } });
  });

  const sent = (path: string) => backendLog(home).requests.find((request) => request.path === path)!.headers;
  const header = (headers: Record<string, string>) => Object.entries(headers).filter(([name]) => name.toLowerCase() === "x-hearth-session");
  assert.deepEqual(header(sent("/api/workbench/tasks")).map(([, value]) => value), [token]);
  assert.deepEqual(header(sent("/assets/dashboard.js")), []);
  assert.deepEqual(header(sent("/")), []);
  assert.match(sent("/").Cookie, new RegExp(cookie));
  assert.equal(await page.evaluate(() => document.cookie), "");
  assert.equal((await page.content()).includes(token), false);
  assert.equal(output().includes(token) || output().includes(cookie), false);
  assert.match(output(), /fake backend running/);  // The backend's own output is shown, so the check above means something.
});

test("other origins, redirects, new windows, frames, and device permissions are refused", async (t) => {
  const home = scratch();
  const other = await stranger(t);
  const { app } = await openApp(t, home, fakeBackend);
  const page = await windowShowing(app, "fake dashboard");
  const owned = new URL(page.url()).origin;

  const outcomes = await page.evaluate(async (elsewhere) => {
    const attempt = async (work: () => Promise<unknown>) => work().then(() => "allowed", () => "refused");
    const frame = document.createElement("iframe");
    frame.src = "/frame";
    document.body.append(frame);
    const image = new Image();
    image.src = `${elsewhere}/image`;
    document.body.append(image);
    return {
      fetchElsewhere: await attempt(() => fetch(`${elsewhere}/api/x`)),
      redirectElsewhere: await attempt(() => fetch(`/redirect?to=${encodeURIComponent(`${elsewhere}/api/x`)}`)),
      popup: window.open("https://github.com/gdcruz0911/hearth/pull/1") === null ? "refused" : "allowed",
      plainPopup: window.open("http://example.test/") === null ? "refused" : "allowed",
      camera: await attempt(() => navigator.mediaDevices.getUserMedia({ video: true })),
    };
  }, other.origin);
  await page.evaluate((elsewhere) => void (window.location.href = `${elsewhere}/navigated`), other.origin);
  await pause(300);
  await page.evaluate(() => void (window.location.href = "/redirect?to=" + encodeURIComponent("http://127.0.0.1:1/redirected")));
  await pause(300);
  await page.evaluate(() => void (window.location.href = "https://github.com/gdcruz0911/hearth/pull/2"));
  await pause(500);

  assert.deepEqual(outcomes, { fetchElsewhere: "refused", redirectElsewhere: "refused", popup: "refused", plainPopup: "refused", camera: "refused" });
  assert.deepEqual(other.received, []);
  assert.equal(new URL(page.url()).origin, owned);
  assert.deepEqual(await opened(app), ["https://github.com/gdcruz0911/hearth/pull/1", "https://github.com/gdcruz0911/hearth/pull/2"]);
  assert.equal(backendLog(home).requests.some((request) => request.path === "/frame"), false);
});

test("after the backend exits, a server on its old port gets nothing, and a restart uses new secrets", async (t) => {
  const home = scratch();
  const { app } = await openApp(t, home, fakeBackend);
  const page = await windowShowing(app, "fake dashboard");
  await page.evaluate(() => void setInterval(() => fetch("/api/poll").catch(() => {}), 50));  // Like the dashboard's polling.
  const [first] = backendLog(home).secrets;

  process.kill(first.pid, "SIGKILL");
  const imposter = await bindWhenFree(t, first.port);
  await windowShowing(app, "Hearth's backend stopped");
  await pause(500);
  await app.evaluate(({ Menu }) => Menu.getApplicationMenu()!.items[0].submenu!.items[0].click());
  await windowShowing(app, "fake dashboard");

  const second = backendLog(home).secrets[1];
  assert.deepEqual(imposter.received, []);
  assert.notEqual(second.port, first.port);
  assert.notEqual(second.token, first.token);
  assert.notEqual(second.cookie, first.cookie);
  const later = backendLog(home).requests.slice(-1)[0];
  assert.equal(JSON.stringify(later).includes(first.cookie) || JSON.stringify(later).includes(first.token), false);
});

async function bindWhenFree(t: TestContext, port: number) {
  for (let tries = 0; tries < 50; tries++) {
    try {
      return await stranger(t, port);
    } catch {
      await pause(50);
    }
  }
  throw new Error(`Port ${port} never came free.`);
}

test("the real dashboard works through the shell, and agent-written text never runs or links anywhere but https", async (t) => {
  const home = scratch();
  const hostile = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
  const task = (id: string, url: string) => ({
    id, project: "demo", goal: hostile, status: "failed", stop_reason: hostile, runs: [], created: "2026-10-09T12:00:00", finished: null,
    branch: "hearth/x", base: "0".repeat(40), worktree: "/nonexistent", pr: { number: 7, url },
  });
  seedTask(home, task("20261009-120000", "javascript:alert(3)"));
  seedTask(home, task("20261009-120001", "https://github.com/gdcruz0911/hearth/pull/7"));
  const { app, output } = await openApp(t, home, realBackend(home.dir));
  const page = await windowShowing(app, "Hub");
  const [cookie] = await app.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.session.cookies.get({})).map((item) => item.value));
  const dialogs: string[] = [];
  page.on("dialog", (dialog) => void dialogs.push(dialog.message()));

  await page.evaluate(() => void (window.location.hash = "#/tasks/20261009-120000"));
  await windowShowing(app, "pull request #7");
  const unsafe = await page.evaluate(() => ({
    hrefs: [...document.querySelectorAll("a[href]")].map((link) => link.getAttribute("href")).filter((href) => !href!.startsWith("#")),
    injected: document.querySelectorAll("main img[src='x'], main script").length,
  }));
  await page.evaluate(() => void (window.location.hash = "#/tasks/20261009-120001"));
  await windowShowing(app, "pull request #7");
  // Clicked from the page: Playwright's own click would wait for the navigation the app refuses.
  await page.evaluate(() => [...document.querySelectorAll("a")].find((link) => link.textContent === "pull request #7")!.click());
  await pause(500);

  assert.deepEqual(unsafe, { hrefs: [], injected: 0 });
  assert.equal(await page.evaluate(() => document.body.innerText.includes("session ended")), false);
  assert.deepEqual(dialogs, []);
  assert.deepEqual(await opened(app), ["https://github.com/gdcruz0911/hearth/pull/7"]);
  assert.equal(new URL(page.url()).hostname, "127.0.0.1");
  assert.ok(cookie.length >= 32);
  assert.equal(output().includes(cookie), false, "the real backend's session cookie never appears in the app's or backend's output");
  assert.match(output(), /desktop backend is running/);
});

test("when the backend exits, its session stops sending and loses its cookie", async (t) => {
  const home = scratch();
  const { app } = await openApp(t, home, fakeBackend);
  await windowShowing(app, "fake dashboard");
  const [first] = backendLog(home).secrets;
  // The old window's session, kept here so it can be probed after the shell has replaced the window.
  await app.evaluate(({ BrowserWindow }) => void ((globalThis as unknown as { old: Electron.Session }).old = BrowserWindow.getAllWindows()[0].webContents.session));
  const before = await app.evaluate(async () => (await (globalThis as unknown as { old: Electron.Session }).old.cookies.get({})).length);

  process.kill(first.pid, "SIGKILL");
  const imposter = await bindWhenFree(t, first.port);
  await windowShowing(app, "Hearth's backend stopped");
  await pause(300);
  const after = await app.evaluate(async (_electron, origin) => {
    const old = (globalThis as unknown as { old: Electron.Session }).old;
    const sent = await old.fetch(`${origin}/api/x`).then(() => "sent", () => "refused");
    return { sent, cookies: (await old.cookies.get({})).length };
  }, `http://127.0.0.1:${first.port}`);

  assert.equal(before, 1);
  assert.deepEqual(after, { sent: "refused", cookies: 0 });
  assert.deepEqual(imposter.received, []);
});
