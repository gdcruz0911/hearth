// ADR-0051: the task pipeline is hidden, not gone. Tasks leaves the sidebar unless a task is at work or waits on the
// keeper, the app opens on Conversations, and every task stays reachable.

import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import type { Page } from "playwright";
import { openApp, realBackend, scratch, seedTask, windowShowing, type Scratch } from "./helpers.ts";

const task = (id: string, fields: Record<string, unknown>) => ({
  id, project: "demo", goal: `Goal of ${id}`, status: "done", stop_reason: null, runs: [], created: "2026-11-05T12:00:00", finished: null,
  branch: `hearth/${id}`, base: "0".repeat(40), worktree: "/nonexistent", ...fields,
});

function question(home: Scratch, id: string, body: string): void {
  writeFileSync(join(home.dir, "hearth/tasks", id, "board.jsonl"), `${JSON.stringify({ id: "q1", from: "01-implement-claude", to: "person", kind: "question", body })}\n`);
}

const tasksLink = (page: Page) => page.locator("#tasks-link");
// The status bar counts the Inbox from the same reply the link is set from, so the tasks have arrived once it shows.
const tasksArrived = (page: Page) => page.locator("#waiting-count", { hasText: "in Inbox" }).waitFor();
const shownView = (page: Page) => page.evaluate(() => (document.querySelector<HTMLElement>(".main > .view:not([hidden])"))?.dataset.view);

test("with no active task, Tasks is gone from the sidebar, and the app opens on Conversations even after an older saved choice of Tasks", async (t) => {
  const home = scratch();
  seedTask(home, task("20261105-120000", { status: "done" }));
  seedTask(home, task("20261105-120001", { status: "failed", stop_reason: "tests failed" }));  // Stopped tasks stay in the Inbox for good.
  const { app } = await openApp(t, home, realBackend(home.dir));
  const page = await windowShowing(app, "Hub");
  await tasksArrived(page);

  assert.equal(await shownView(page), "sessions");
  assert.equal(await tasksLink(page).isVisible(), false);
  assert.deepEqual(await page.locator("#default-view option").evaluateAll((options) => options.map((option) => (option as HTMLOptionElement).value)),
                   ["last", "sessions", "attention", "hub", "knowledge"]);

  for (const saved of ["hearth-default-view", "hearth-last-view"]) {
    await page.evaluate((key) => {
      localStorage.setItem(key, "tasks");
      history.replaceState(null, "", "/");  // Opened as the app opens: no view in the address.
    }, saved);
    await page.reload();
    await tasksArrived(page);
    assert.equal(await shownView(page), "sessions", `${saved} saved as Tasks`);
    assert.equal(await page.locator("#default-view").inputValue(), "last");
  }
});

test("a direct task address still opens with Tasks out of the sidebar, and Find still reaches a finished task", async (t) => {
  const home = scratch();
  seedTask(home, task("20261105-120000", { status: "done" }));
  seedTask(home, task("20261105-120001", { status: "failed", stop_reason: "tests failed" }));
  const { app } = await openApp(t, home, realBackend(home.dir));
  const page = await windowShowing(app, "Hub");
  await tasksArrived(page);

  await page.evaluate(() => void (window.location.hash = "#/tasks/20261105-120000"));
  await page.locator("#task-summary", { hasText: "Goal of 20261105-120000" }).waitFor();
  assert.equal(await shownView(page), "tasks");
  assert.equal(await tasksLink(page).isVisible(), false);

  await page.evaluate(() => void (window.location.hash = "#/sessions"));
  await page.locator("#convo-view").waitFor();  // A hashchange still to fire would close Find as it opens.
  await page.locator("#find-open").click();
  await page.locator("#find-input").fill("20261105-120000");
  await page.locator("#find-results a", { hasText: "Goal of 20261105-120000" }).click();
  await page.locator("#task-summary", { hasText: "Goal of 20261105-120000" }).waitFor();
  assert.equal(await page.evaluate(() => window.location.hash), "#/tasks/20261105-120000");
});

test("a task with an open question brings Tasks back, and can be answered from there or from the Inbox", async (t) => {
  const home = scratch();
  const id = "20261105-120000";
  seedTask(home, task(id, { status: "waiting", stop_reason: "question" }));
  question(home, id, "Which greeting?");
  const { app } = await openApp(t, home, realBackend(home.dir));
  const page = await windowShowing(app, "Hub");

  await tasksLink(page).waitFor();
  await tasksLink(page).click();
  await page.locator(".tasks-view").waitFor();  // The view changes on hashchange, which fires after the click returns.
  await page.locator(".task-list a.card", { hasText: `Goal of ${id}` }).click();
  await page.locator("#task-summary", { hasText: "Which greeting?" }).waitFor();
  await page.locator("textarea.answer-input").waitFor();

  await page.evaluate(() => void (window.location.hash = "#/attention"));
  await page.locator("#attention-list a", { hasText: `Goal of ${id}` }).click();
  await page.locator("textarea.answer-input").waitFor();
  assert.equal(await page.evaluate(() => window.location.hash), `#/tasks/${id}`);
});

for (const [state, fields] of [
  // This test process stands in for the run's: a dead pid would read as interrupted.
  ["running", { status: "running", runs: [{ dir: "01-implement-claude", role: "implement", provider: "claude", pid: process.pid, exit_code: null, check_exit_code: null }] }],
  ["queued", { status: "queued" }],
  ["waiting for approval of its tests", { status: "waiting", stop_reason: "tests_to_approve" }],
] as const) {
  test(`a task ${state} brings Tasks back to the sidebar`, async (t) => {
    const home = scratch();
    seedTask(home, task("20261105-120000", fields));
    const { app } = await openApp(t, home, realBackend(home.dir));
    const page = await windowShowing(app, "Hub");
    await tasksLink(page).waitFor();
    await tasksLink(page).click();
    await page.locator(".task-list a.card", { hasText: "Goal of 20261105-120000" }).waitFor();
  });
}
