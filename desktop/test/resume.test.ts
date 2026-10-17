// ADR-0038: a task answered from the app keeps going after the app quits, through verification and review, with its receipts.

import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { openApp, pause, python, realBackend, repo, scratch, windowShowing, type Scratch } from "./helpers.ts";

// Every agent CLI a task can reach is the test suite's fake, so nothing here spends the person's quota. They sit only in the
// scratch home's ~/.local/bin, and the app gets a bare PATH, as when it is opened from Finder: the backend must find them.
const agentBin = (home: Scratch) => join(home.dir, "home/.local/bin");

function fakeAgents(home: Scratch): Record<string, string> {
  const bin = agentBin(home);
  mkdirSync(bin, { recursive: true });
  for (const [name, as] of [["claude", ""], ["codex", "--as codex"], ["agy", "--as antigravity"]]) {
    writeFileSync(join(bin, name), `#!/bin/sh\nexec "${python}" "${join(repo, "tests/fake_agent.py")}" ${as} "$@"\n`);
    chmodSync(join(bin, name), 0o755);
  }
  writeFileSync(join(home.dir, "home/.gitconfig"), "[user]\n\tname = Person\n\temail = person@example.com\n");
  return { PATH: "/usr/bin:/bin", FAKE_REVIEWS: "approve", FAKE_DELAY: "2", GIT_CONFIG_COUNT: "1", GIT_CONFIG_KEY_0: "maintenance.auto", GIT_CONFIG_VALUE_0: "false" };
}

function waitingTask(home: Scratch, env: Record<string, string>): string {
  const project = join(home.dir, "project");
  mkdirSync(project);
  const git = (...args: string[]) => execFileSync("git", ["-C", project, ...args], { env: { ...env, HOME: join(home.dir, "home") } });
  git("init", "-q", "-b", "main");
  writeFileSync(join(project, "README.md"), "# Project\n");
  writeFileSync(join(project, "VERIFY.md"), "Check that hello.txt exists.\n");  // Without it, the loop skips verification.
  git("add", "-f", "README.md", "VERIFY.md");
  git("commit", "-q", "-m", "init");
  mkdirSync(join(home.dir, "hearth"), { recursive: true });
  writeFileSync(join(home.dir, "hearth/projects.json"), JSON.stringify({ demo: { path: project, check: "test -f hello.txt", providers: ["claude", "codex"] } }));
  spawnSync(python, ["-m", "hearth.cli", "task", "new", "demo", "Add hello.txt"], {  // Exits 1: the task waits on its question.
    env: { ...env, PATH: `${agentBin(home)}:/usr/bin:/bin`, HOME: join(home.dir, "home"), HEARTH_HOME: join(home.dir, "hearth"), PYTHONPATH: join(repo, "src"),
           FAKE_OUTBOX: '{"to": "person", "kind": "question", "body": "Which greeting?"}' },
  });
  const [id] = readdirSync(join(home.dir, "hearth/tasks"));
  return id;
}

test("a task answered in the app finishes verification and review after the app quits, and shows as done on reopening", async (t) => {
  const home = scratch();
  const env = fakeAgents(home);
  const id = waitingTask(home, env);
  const record = () => JSON.parse(readFileSync(join(home.dir, "hearth/tasks", id, "task.json"), "utf8"));
  assert.equal(record().status, "waiting");

  const first = await openApp(t, home, realBackend(home.dir), { env });
  const page = await windowShowing(first.app, "Hub");
  await page.evaluate((task) => void (window.location.hash = `#/tasks/${task}`), id);
  await windowShowing(first.app, "Which greeting?");
  await page.locator("textarea.answer-input").fill("Say hello to the person.");
  await page.getByRole("button", { name: "Preview answer" }).click();
  await page.getByRole("button", { name: "Send answer" }).click();
  for (let tries = 0; tries < 100 && !existsSync(join(home.dir, "hearth/tasks", id, "resume.json")); tries++) await pause(100);
  await first.app.close();  // The app quits while the resumed loop is still at work.
  const quitAt = Math.floor(Date.now() / 1000);

  let finished = record();
  for (let tries = 0; tries < 600 && !finished.review && finished.status !== "failed"; tries++) {
    await pause(100);
    finished = record();
  }
  assert.deepEqual([finished.status, finished.stop_reason], ["done", null]);
  assert.deepEqual(finished.runs.map((run: { role: string }) => run.role), ["implement", "verify", "review"]);
  assert.equal(finished.review.verdict, "approve");
  const seconds = (stamp: string) => Date.parse(stamp.replace(/([+-]\d\d)(\d\d)$/, "$1:$2")) / 1000;
  const [, verify, review] = finished.runs;
  assert.ok(seconds(verify.finished) >= quitAt && seconds(review.started) >= quitAt, "verification ended and review began after the app quit");
  assert.match(readFileSync(join(home.dir, "hearth/tasks", id, "resume.log"), "utf8"), /approved/);

  const second = await openApp(t, home, realBackend(home.dir), { env });
  const reopened = await windowShowing(second.app, "Hub");
  await reopened.evaluate((task) => void (window.location.hash = `#/tasks/${task}`), id);
  await windowShowing(second.app, "approve");
});
