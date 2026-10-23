// ADR-0044: a draft typed in a conversation and quit at once is saved before the backend stops, and is back on reopening;
// text written while a reply runs stays editable and is never taken for sent, since Hearth does not queue messages.

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, readFileSync, rmdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { openApp, pause, python, realBackend, repo, scratch, windowShowing, type Scratch } from "./helpers.ts";

function conversation(home: Scratch, request = "request-draft-01"): string {
  const project = join(home.dir, "project");
  const env = { PATH: "/usr/bin:/bin", HOME: join(home.dir, "home"), HEARTH_HOME: join(home.dir, "hearth"), PYTHONPATH: join(repo, "src"),
                GIT_AUTHOR_NAME: "Person", GIT_AUTHOR_EMAIL: "person@example.com", GIT_COMMITTER_NAME: "Person", GIT_COMMITTER_EMAIL: "person@example.com" };
  if (!existsSync(project)) {
    mkdirSync(project);
    const git = (...args: string[]) => execFileSync("git", ["-C", project, ...args], { env });
    git("init", "-q", "-b", "main");
    writeFileSync(join(project, "README.md"), "# Project\n");
    git("add", "README.md");
    git("commit", "-q", "-m", "init");
    mkdirSync(join(home.dir, "hearth"), { recursive: true });
    writeFileSync(join(home.dir, "hearth/projects.json"), JSON.stringify({ demo: { path: project, check: "true", providers: ["claude", "codex"] } }));
  }
  // Starting a conversation runs no agent, so none is needed here.
  return execFileSync(python, ["-c", `from hearth.workbench import sessions; print(sessions.create('demo', 'claude', 'edit', None, 'medium', '${request}')['id'])`],
                      { env }).toString().trim();
}

// The only Claude the app can find: it logs each launch, says it started, and replies after a few seconds.
function fakeClaude(home: Scratch): void {
  const bin = join(home.dir, "home/.local/bin");
  mkdirSync(bin, { recursive: true });
  writeFileSync(join(bin, "claude"), `#!/bin/sh
echo launch >> "$HOME/launches.log"
cat > /dev/null
echo '{"type": "system", "subtype": "init", "model": "fake", "session_id": "fake-1"}'
sleep 4
echo '{"type": "result", "result": "Done", "is_error": false, "session_id": "fake-1"}'
`);
  chmodSync(join(bin, "claude"), 0o755);
}

test("a draft typed just before quitting is saved, and is back in its conversation after reopening", async (t) => {
  const home = scratch();
  const id = conversation(home);
  const first = await openApp(t, home, realBackend(home.dir));
  const page = await windowShowing(first.app, "Live ·");
  await page.evaluate((session) => void (window.location.hash = `#/sessions/${session}`), id);
  await page.locator("#convo-message").waitFor();
  await page.locator("#convo-message").fill("Unsent words for Claude");
  await page.locator("#convo-message").blur();
  const exited = new Promise((done) => first.app.process().once("exit", done));
  await first.app.evaluate(({ app }) => app.quit());  // At once: well inside the save's pause after typing.
  await exited;

  const saved = join(home.dir, "hearth/tasks", id, "draft.json");
  assert.ok(existsSync(saved), "the draft was saved before the backend stopped");
  assert.equal(JSON.parse(readFileSync(saved, "utf8")).text, "Unsent words for Claude");

  const second = await openApp(t, home, realBackend(home.dir));
  const reopened = await windowShowing(second.app, "Live ·");
  await reopened.evaluate((session) => void (window.location.hash = `#/sessions/${session}`), id);
  await reopened.waitForFunction(() => (document.getElementById("convo-message") as HTMLTextAreaElement | null)?.value === "Unsent words for Claude");
});

test("a draft typed just before closing the window is saved, and is back when the Dock icon reopens it", async (t) => {
  const home = scratch();
  const id = conversation(home);
  const { app } = await openApp(t, home, realBackend(home.dir));
  const page = await windowShowing(app, "Live ·");
  await page.evaluate((session) => void (window.location.hash = `#/sessions/${session}`), id);
  await page.locator("#convo-message").waitFor();
  await page.locator("#convo-message").fill("Typed, then the red button");
  await page.locator("#convo-message").blur();
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());  // At once, like the red button.
  for (let tries = 0; tries < 50 && (await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length)); tries++) await pause(100);

  const saved = join(home.dir, "hearth/tasks", id, "draft.json");
  assert.ok(existsSync(saved), "the draft was saved before the window closed");
  assert.equal(JSON.parse(readFileSync(saved, "utf8")).text, "Typed, then the red button");
  await app.evaluate(({ app }) => void app.emit("activate"));
  const reopened = await windowShowing(app, "Live ·");
  await reopened.evaluate((session) => void (window.location.hash = `#/sessions/${session}`), id);
  await reopened.waitForFunction(() => (document.getElementById("convo-message") as HTMLTextAreaElement | null)?.value === "Typed, then the red button");
});

test("a draft typed just before reloading is saved, and is back after the reload", async (t) => {
  const home = scratch();
  const id = conversation(home);
  const { app } = await openApp(t, home, realBackend(home.dir));
  const page = await windowShowing(app, "Live ·");
  await page.evaluate((session) => void (window.location.hash = `#/sessions/${session}`), id);
  await page.locator("#convo-message").waitFor();
  await page.locator("#convo-message").fill("Typed, then reloaded");
  await page.locator("#convo-message").blur();  // So keystrokes elsewhere on the Mac cannot land in it.
  await page.evaluate(() => void ((window as unknown as { before: boolean }).before = true));
  // What View > Reload (⌘R) does, from the app's own menu.
  await app.evaluate(({ Menu, BrowserWindow }) => {
    const reload = Menu.getApplicationMenu()?.items.find((item) => item.label === "View")?.submenu?.items[0];
    reload?.click(undefined, BrowserWindow.getAllWindows()[0], BrowserWindow.getAllWindows()[0].webContents);
  });
  await page.waitForFunction(() => !(window as unknown as { before?: boolean }).before, undefined, { timeout: 15_000 });  // A new page.
  await page.locator("#convo-message").waitFor();
  await page.evaluate((session) => void (window.location.hash = `#/sessions/${session}`), id);
  await page.waitForFunction(() => (document.getElementById("convo-message") as HTMLTextAreaElement | null)?.value === "Typed, then reloaded",
                             undefined, { timeout: 15_000 });
  assert.equal(JSON.parse(readFileSync(join(home.dir, "hearth/tasks", id, "draft.json"), "utf8")).text, "Typed, then reloaded");
});

test("text written while a reply runs stays editable, says it is not sent, and starts nothing", async (t) => {
  const home = scratch();
  const id = conversation(home);
  fakeClaude(home);
  const { app } = await openApp(t, home, realBackend(home.dir), { env: { PATH: "/usr/bin:/bin" } });
  const page = await windowShowing(app, "Live ·");
  await page.evaluate((session) => void (window.location.hash = `#/sessions/${session}`), id);
  const box = page.locator("#convo-message");
  await box.fill("First message");
  await box.blur();
  await page.getByRole("button", { name: "Send to Claude Code" }).click();
  await page.locator("#convo-stop").waitFor({ state: "visible" });

  await box.fill("A second thought");
  await box.blur();
  assert.equal(await box.isEditable(), true);
  assert.match(await page.locator("#convo-hint").textContent() ?? "", /^Not sent: Hearth does not queue messages/);
  assert.equal(await page.locator("#convo-send").isDisabled(), true);

  await page.locator("#convo-stop").waitFor({ state: "hidden", timeout: 20_000 });
  assert.equal(await box.inputValue(), "A second thought");
  assert.equal(readFileSync(join(home.dir, "home/launches.log"), "utf8"), "launch\n");
});

// The app's quit dialogs, answered in order, with each question kept for the test.
async function answering(app: import("playwright").ElectronApplication, responses: number[]): Promise<void> {
  await app.evaluate(({ dialog }, answers) => {
    const asked = ((globalThis as unknown as { asked?: string[] }).asked ??= []);
    dialog.showMessageBox = (async (options: Electron.MessageBoxOptions) =>
      (asked.push(options.message), { response: answers.shift() ?? 0, checkboxChecked: false })) as typeof dialog.showMessageBox;
  }, responses);
}

for (const [how, breakIt] of [
  ["a draft that fails to save", (home: Scratch, id: string) => chmodSync(join(home.dir, "hearth/tasks", id), 0o500)],
  ["a page that never answers", null],
] as const) {
  test(`quitting with ${how} asks first, keeps Hearth open by default, and quits only when told`, async (t) => {
    const home = scratch();
    const id = conversation(home);
    t.after(() => chmodSync(join(home.dir, "hearth/tasks", id), 0o755));  // So the scratch folder can be removed.
    const { app } = await openApp(t, home, realBackend(home.dir));
    const page = await windowShowing(app, "Live ·");
    await page.evaluate((session) => void (window.location.hash = `#/sessions/${session}`), id);
    await page.locator("#convo-message").waitFor();
    if (breakIt) breakIt(home, id);
    else await page.evaluate(() => { (window as unknown as { Hearth: { flushDrafts: () => Promise<boolean> } }).Hearth.flushDrafts = () => new Promise(() => {}); });
    await page.locator("#convo-message").fill("Words that must not vanish");
    await page.locator("#convo-message").blur();
    await answering(app, [0, 1]);  // Keep Hearth Open, then Quit Without Saving.

    await app.evaluate(({ app }) => app.quit());
    for (let tries = 0; tries < 80 && !(await app.evaluate(() => ((globalThis as unknown as { asked?: string[] }).asked ?? []).length)); tries++) await pause(100);
    const kept = { windows: await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), text: await page.locator("#convo-message").inputValue() };
    const exited = new Promise((done) => app.process().once("exit", done));
    const asked = await app.evaluate(async ({ app }) => {
      app.quit();
      await new Promise((done) => setTimeout(done, 6000));  // Past the five-second wait, so a page that never answers is asked about too.
      return (globalThis as unknown as { asked: string[] }).asked;
    }).catch(() => null);  // The app may exit before this returns.
    await exited;

    assert.deepEqual(kept, { windows: 1, text: "Words that must not vanish" });
    if (asked) assert.deepEqual(asked, ["Hearth could not save every draft.", "Hearth could not save every draft."]);
    if (breakIt) assert.equal(existsSync(join(home.dir, "hearth/tasks", id, "draft.json")), false, "the draft was never saved");
  });
}

test("leaving Sessions while its first list is loading never pulls the keeper back", async (t) => {
  const home = scratch();  // No conversations, so Sessions alone would open the new-conversation form.
  const { app } = await openApp(t, home, realBackend(home.dir));
  const page = await windowShowing(app, "Live ·");
  // Before the page's own scripts: hold the list reply, so it can arrive after the address changes but before the
  // page hears of the change, as when the keeper picks another view the moment Hearth opens.
  await app.context().addInitScript(() => {
    const real = window.fetch.bind(window);
    const held = window as unknown as { release?: () => void };
    window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      if (!String(input).endsWith("api/sessions")) return real(input, init);
      const answer = await real(input, init);
      const body = await answer.json();
      await new Promise<void>((done) => (held.release = done));
      return { ok: true, status: 200, json: async () => body } as Response;
    }) as typeof window.fetch;
  });
  await page.evaluate(() => history.replaceState(null, "", "/"));  // Opened as the app opens: no view in the address yet.
  await page.reload();
  await page.waitForFunction(() => Boolean((window as unknown as { release?: () => void }).release));
  await page.evaluate(() => {
    window.location.hash = "#/tasks";
    (window as unknown as { release: () => void }).release();  // The reply lands before the hashchange event for Tasks.
  });
  await new Promise((done) => setTimeout(done, 500));
  assert.equal(await page.evaluate(() => window.location.hash), "#/tasks");
});

test("closing the window with a draft that fails to save asks first, and keeps the window by default", async (t) => {
  const home = scratch();
  const id = conversation(home);
  t.after(() => chmodSync(join(home.dir, "hearth/tasks", id), 0o755));
  const { app } = await openApp(t, home, realBackend(home.dir));
  const page = await windowShowing(app, "Live ·");
  await page.evaluate((session) => void (window.location.hash = `#/sessions/${session}`), id);
  await page.locator("#convo-message").waitFor();
  chmodSync(join(home.dir, "hearth/tasks", id), 0o500);
  await page.locator("#convo-message").fill("Keep me");
  await page.locator("#convo-message").blur();
  await answering(app, [0, 1]);  // Keep Hearth Open, then Close Without Saving.

  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  for (let tries = 0; tries < 80 && !(await app.evaluate(() => ((globalThis as unknown as { asked?: string[] }).asked ?? []).length)); tries++) await pause(100);
  await pause(300);
  const kept = { windows: await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), text: await page.locator("#convo-message").inputValue() };
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  for (let tries = 0; tries < 80 && (await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length)); tries++) await pause(100);

  assert.deepEqual(kept, { windows: 1, text: "Keep me" });
  assert.deepEqual(await app.evaluate(() => (globalThis as unknown as { asked: string[] }).asked), ["Hearth could not save every draft.", "Hearth could not save every draft."]);
  assert.equal(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 0, "closed when told");
});

test("reloading with a draft that fails to save asks first, and keeps the page by default", async (t) => {
  const home = scratch();
  const id = conversation(home);
  t.after(() => chmodSync(join(home.dir, "hearth/tasks", id), 0o755));
  const { app } = await openApp(t, home, realBackend(home.dir));
  const page = await windowShowing(app, "Live ·");
  await page.evaluate((session) => void (window.location.hash = `#/sessions/${session}`), id);
  await page.locator("#convo-message").waitFor();
  chmodSync(join(home.dir, "hearth/tasks", id), 0o500);
  await page.locator("#convo-message").fill("Keep me too");
  await page.locator("#convo-message").blur();
  await page.evaluate(() => void ((window as unknown as { before: boolean }).before = true));
  await answering(app, [0]);  // Keep Hearth Open.
  await app.evaluate(({ Menu, BrowserWindow }) => {
    const item = Menu.getApplicationMenu()?.items.find((entry) => entry.label === "View")?.submenu?.items[0];
    item?.click(undefined, BrowserWindow.getAllWindows()[0], BrowserWindow.getAllWindows()[0].webContents);
  });
  for (let tries = 0; tries < 80 && !(await app.evaluate(() => ((globalThis as unknown as { asked?: string[] }).asked ?? []).length)); tries++) await pause(100);
  await pause(500);

  assert.deepEqual(await app.evaluate(() => (globalThis as unknown as { asked: string[] }).asked), ["Hearth could not save every draft."]);
  assert.deepEqual(await page.evaluate(() => [(window as unknown as { before?: boolean }).before, (document.getElementById("convo-message") as HTMLTextAreaElement).value]),
                   [true, "Keep me too"]);  // The same page, text and all.
});

for (const other of ["Same words", "Other words"]) {
  test(`a reply to Send that arrives after switching clears only the sent conversation (other holds "${other}")`, async (t) => {
    const home = scratch();
    const sentFrom = conversation(home);
    const elsewhere = conversation(home, "request-draft-02");
    fakeClaude(home);
    const { app } = await openApp(t, home, realBackend(home.dir), { env: { PATH: "/usr/bin:/bin" } });
    const page = await windowShowing(app, "Live ·");
    // Hold each Send reply in the page until released, so it can land after the keeper has switched conversations.
    await app.context().addInitScript(() => {
      const real = window.fetch.bind(window);
      const held = window as unknown as { release?: () => void };
      window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
        if (!String(input).endsWith("/send")) return real(input, init);
        const body = await (await real(input, init)).json();
        await new Promise<void>((done) => (held.release = done));
        return { ok: true, status: 200, json: async () => ((window as unknown as { handled: boolean }).handled = true, body) } as Response;
      }) as typeof window.fetch;
    });
    await page.reload();
    const open = async (id: string) => {
      await page.evaluate((session) => void (window.location.hash = `#/sessions/${session}`), id);
      await page.waitForFunction((session) => window.location.hash.endsWith(session), id);
      await page.locator("#convo-message").waitFor();
      await pause(400);  // Its saved draft, if any, has arrived.
    };
    const box = page.locator("#convo-message");

    await open(sentFrom);
    await box.fill("Same words");
    await box.blur();
    await page.getByRole("button", { name: "Send to Claude Code" }).click();
    await page.waitForFunction(() => Boolean((window as unknown as { release?: () => void }).release));  // Sent; the reply is held.
    await open(elsewhere);
    await box.fill(other);
    await box.blur();
    const otherDraft = join(home.dir, "hearth/tasks", elsewhere, "draft.json");
    for (let tries = 0; tries < 50 && !(existsSync(otherDraft) && JSON.parse(readFileSync(otherDraft, "utf8")).text === other); tries++) await pause(100);
    await page.evaluate(() => (window as unknown as { release: () => void }).release());
    await page.waitForFunction(() => (window as unknown as { handled?: boolean }).handled === true);  // The reply was read,
    await page.evaluate(() => new Promise((done) => setTimeout(done, 0)));  // and what follows it in the page has run.

    assert.equal(await box.inputValue(), other, "the other conversation's box is untouched");
    await open(sentFrom);
    assert.equal(await box.inputValue(), "", "the sent text does not come back");
    await open(elsewhere);
    assert.equal(await box.inputValue(), other);
  });
}

test("when Hearth cannot clear a sent draft, the page clears it, quit asks while it cannot, and reopening never brings it back", async (t) => {
  const home = scratch();
  const id = conversation(home);
  fakeClaude(home);
  const saved = join(home.dir, "hearth/tasks", id, "draft.json");
  const first = await openApp(t, home, realBackend(home.dir), { env: { PATH: "/usr/bin:/bin" } });
  const page = await windowShowing(first.app, "Live ·");
  await page.evaluate((session) => void (window.location.hash = `#/sessions/${session}`), id);
  const box = page.locator("#convo-message");
  await box.waitFor();
  await box.fill("Sent once");
  await box.blur();
  for (let tries = 0; tries < 50 && !(existsSync(saved) && JSON.parse(readFileSync(saved, "utf8")).text === "Sent once"); tries++) await pause(100);
  const blocker = join(home.dir, "hearth/tasks", id, "draft.json.partial");
  mkdirSync(blocker);  // A folder where the draft is written: every draft write fails until it goes.
  await page.getByRole("button", { name: "Send to Claude Code" }).click();
  for (let tries = 0; tries < 50 && !existsSync(join(home.dir, "home/launches.log")); tries++) await pause(100);
  await page.waitForFunction(() => (document.getElementById("convo-message") as HTMLTextAreaElement).value === "");
  await page.waitForFunction(() => document.getElementById("convo-hint")?.textContent?.startsWith("Hearth could not remove the message you sent"));

  await answering(first.app, [0]);  // Keep Hearth Open.
  await first.app.evaluate(({ app }) => app.quit());
  for (let tries = 0; tries < 80 && !(await first.app.evaluate(() => ((globalThis as unknown as { asked?: string[] }).asked ?? []).length)); tries++) await pause(100);
  const askedWhileBlocked = await first.app.evaluate(() => (globalThis as unknown as { asked: string[] }).asked);
  rmdirSync(blocker);
  const exited = new Promise((done) => first.app.process().once("exit", done));
  await first.app.evaluate(({ app }) => app.quit());  // The clear is retried, succeeds, and nothing is asked.
  await exited;

  assert.deepEqual(askedWhileBlocked, ["Hearth could not save every draft."]);
  assert.equal(JSON.parse(readFileSync(saved, "utf8")).text, "");
  const second = await openApp(t, home, realBackend(home.dir), { env: { PATH: "/usr/bin:/bin" } });
  const reopened = await windowShowing(second.app, "Live ·");
  await reopened.evaluate((session) => void (window.location.hash = `#/sessions/${session}`), id);
  await reopened.getByRole("button", { name: "Send to Claude Code" }).waitFor();  // Set with the conversation's details, its draft included.
  assert.equal(await reopened.locator("#convo-message").inputValue(), "");
});

test("what a stop left behind shows in its reply even when it is recorded after the reply finished", async (t) => {
  const home = scratch();
  const id = conversation(home, "request-left-01");
  fakeClaude(home);
  const { app } = await openApp(t, home, realBackend(home.dir), { env: { PATH: "/usr/bin:/bin" } });
  const page = await windowShowing(app, "Live ·");
  await page.evaluate((session) => void (window.location.hash = `#/sessions/${session}`), id);
  await page.locator("#convo-message").fill("Run the check");
  await page.locator("#convo-message").blur();
  await page.getByRole("button", { name: "Send to Claude Code" }).click();
  await page.locator("#convo-stop").waitFor({ state: "visible" });
  await page.locator("#convo-stop").waitFor({ state: "hidden", timeout: 20_000 });

  const record = join(home.dir, "hearth/tasks", id, "task.json");
  const task = JSON.parse(readFileSync(record, "utf8"));
  Object.assign(task.runs.at(-1), { left_running: [4242], left_in_folder: [4343], cleanup_unchecked: true });
  writeFileSync(record, JSON.stringify(task));

  await page.waitForFunction(() => {
    const text = document.body.textContent ?? "";
    return text.includes("Hearth could not stop 1 process(es) this reply started: 4242")
      && text.includes("not stopped by Hearth: 4343") && text.includes("Hearth could not list processes");
  }, null, { timeout: 15_000 });
});

// Found in the slice 2 proof: the form kept the last project picked, or else the first in projects.json, so two conversations
// started in the wrong project. As in Claude's and Codex's own apps, a new conversation starts where the keeper just was.
test("the new-conversation form starts in the project of the conversation just viewed, or else the latest one", async (t) => {
  const home = scratch();
  const env = { PATH: "/usr/bin:/bin", HOME: join(home.dir, "home"), HEARTH_HOME: join(home.dir, "hearth"), PYTHONPATH: join(repo, "src"),
                GIT_AUTHOR_NAME: "Person", GIT_AUTHOR_EMAIL: "person@example.com", GIT_COMMITTER_NAME: "Person", GIT_COMMITTER_EMAIL: "person@example.com" };
  const projects: Record<string, unknown> = {};
  for (const name of ["alpha", "beta"]) {  // alpha is first in projects.json.
    const folder = join(home.dir, name);
    mkdirSync(folder);
    const git = (...args: string[]) => execFileSync("git", ["-C", folder, ...args], { env });
    git("init", "-q", "-b", "main");
    writeFileSync(join(folder, "README.md"), `# ${name}\n`);
    git("add", "README.md");
    git("commit", "-q", "-m", "init");
    projects[name] = { path: folder, check: "true", providers: ["claude", "codex"] };
  }
  mkdirSync(join(home.dir, "hearth"), { recursive: true });
  writeFileSync(join(home.dir, "hearth/projects.json"), JSON.stringify(projects));
  const create = (project: string, request: string) => execFileSync(python, ["-c",
    `from hearth.workbench import sessions; print(sessions.create('${project}', 'claude', 'edit', None, 'medium', '${request}')['id'])`], { env }).toString().trim();
  const alpha = create("alpha", "request-form-alpha");
  create("beta", "request-form-beta");  // The latest conversation.

  const { app } = await openApp(t, home, realBackend(home.dir));
  const page = await windowShowing(app, "Live ·");
  const formShows = async (project: string) => {
    await page.locator("#convo-form").waitFor({ state: "visible" });
    await page.waitForFunction((want) => (document.getElementById("convo-project") as HTMLSelectElement).value === want, project, { timeout: 10_000 })
      .catch(() => undefined);
    assert.equal(await page.locator("#convo-project").inputValue(), project);
  };
  const open = async (hash: string) => page.evaluate((to) => void (window.location.hash = to), hash);

  await open("#/sessions/new");  // Straight to the form after opening Hearth.
  await formShows("beta");
  await open(`#/sessions/${alpha}`);
  await page.locator("#convo-message").waitFor();
  await open("#/sessions/new");
  await formShows("alpha");
  await page.locator("#convo-project").selectOption("beta");  // Picked by hand, then left without starting.
  await open(`#/sessions/${alpha}`);
  await page.locator("#convo-message").waitFor();
  await open("#/sessions/new");
  await formShows("alpha");  // The conversation just viewed, not the earlier pick.
});
