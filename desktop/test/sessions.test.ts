// ADR-0044: a draft typed in a conversation and quit at once is saved before the backend stops, and is back on reopening;
// text written while a reply runs stays editable and is never taken for sent, since Hearth does not queue messages.

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { openApp, pause, python, realBackend, repo, scratch, windowShowing, type Scratch } from "./helpers.ts";

function conversation(home: Scratch): string {
  const project = join(home.dir, "project");
  mkdirSync(project);
  const env = { PATH: "/usr/bin:/bin", HOME: join(home.dir, "home"), HEARTH_HOME: join(home.dir, "hearth"), PYTHONPATH: join(repo, "src"),
                GIT_AUTHOR_NAME: "Person", GIT_AUTHOR_EMAIL: "person@example.com", GIT_COMMITTER_NAME: "Person", GIT_COMMITTER_EMAIL: "person@example.com" };
  const git = (...args: string[]) => execFileSync("git", ["-C", project, ...args], { env });
  git("init", "-q", "-b", "main");
  writeFileSync(join(project, "README.md"), "# Project\n");
  git("add", "README.md");
  git("commit", "-q", "-m", "init");
  mkdirSync(join(home.dir, "hearth"), { recursive: true });
  writeFileSync(join(home.dir, "hearth/projects.json"), JSON.stringify({ demo: { path: project, check: "true", providers: ["claude", "codex"] } }));
  // Starting a conversation runs no agent, so none is needed here.
  return execFileSync(python, ["-c", "from hearth.workbench import sessions; print(sessions.create('demo', 'claude', 'edit', None, 'medium', 'request-draft-01')['id'])"],
                      { env }).toString().trim();
}

test("a draft typed just before quitting is saved, and is back in its conversation after reopening", async (t) => {
  const home = scratch();
  const id = conversation(home);
  const first = await openApp(t, home, realBackend(home.dir));
  const page = await windowShowing(first.app, "Live ·");
  await page.evaluate((session) => void (window.location.hash = `#/sessions/${session}`), id);
  await page.locator("#convo-message").waitFor();
  await page.locator("#convo-message").fill("Unsent words for Claude");
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

test("text written while a reply runs stays editable, says it is not sent, and starts nothing", async (t) => {
  const home = scratch();
  const id = conversation(home);
  // The only Claude the app can find: it logs each launch, says it started, and replies after a few seconds.
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
  const { app } = await openApp(t, home, realBackend(home.dir), { env: { PATH: "/usr/bin:/bin" } });
  const page = await windowShowing(app, "Live ·");
  await page.evaluate((session) => void (window.location.hash = `#/sessions/${session}`), id);
  const box = page.locator("#convo-message");
  await box.fill("First message");
  await page.getByRole("button", { name: "Send to Claude Code" }).click();
  await page.locator("#convo-stop").waitFor({ state: "visible" });

  await box.fill("A second thought");
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
