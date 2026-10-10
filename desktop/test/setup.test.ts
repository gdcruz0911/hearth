// D3c: the app reads its data only from <HEARTH_HOME>/profile.json, and asks the person to set one up when there is none.

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { backendLog, desktop, fakeBackend, openApp, python, realBackend, repo, scratch, windowShowing } from "./helpers.ts";

const hash = (path: string) => createHash("sha256").update(readFileSync(path)).digest("hex");
const asked = (app: Parameters<typeof windowShowing>[0]) => app.evaluate(() => (globalThis as unknown as { dialogs: { kind: string; message: string; detail: string }[] }).dialogs);

test("with no profile, setup can create a new knowledge base, and the dashboard opens on it", async (t) => {
  const home = scratch();
  const prefix = [python, "-m", "hearth.cli"];
  const fresh = join(home.dir, "Knowledge/hearth.sqlite");
  mkdirSync(join(home.dir, "Knowledge"));
  const { app } = await openApp(t, home, prefix, { profile: false, cwd: desktop, answers: [1, fresh] });

  await windowShowing(app, "Hub");

  const profile = JSON.parse(readFileSync(join(home.dir, "hearth/profile.json"), "utf8"));
  assert.equal(profile.database, realpathSync(fresh));
  assert.deepEqual((await asked(app)).map((dialog) => dialog.kind), ["box", "save"]);
  assert.equal(existsSync(join(desktop, ".hearth")), false, "nothing lands in the folder the app was opened from");
});

test("when a new knowledge base cannot be created, setup says why and another location still works", async (t) => {
  const home = scratch();
  writeFileSync(join(home.dir, "a-file"), "not a folder");  // Nothing can be created under a file.
  mkdirSync(join(home.dir, "Knowledge"));
  const good = join(home.dir, "Knowledge/hearth.sqlite");
  const { app } = await openApp(t, home, [python, "-m", "hearth.cli"], { profile: false, answers: [1, join(home.dir, "a-file/hearth.sqlite"), 0, 1, good] });

  await windowShowing(app, "Hub");

  const dialogs = await asked(app);
  assert.match(dialogs[2].detail, /could not create a knowledge base/);
  assert.equal(JSON.parse(readFileSync(join(home.dir, "hearth/profile.json"), "utf8")).database, realpathSync(good));
});

test("choosing a file that is not a Hearth database changes nothing, and quitting setup leaves no profile", async (t) => {
  const home = scratch();
  const other = join(home.dir, "notes.sqlite");
  execFileSync(python, ["-c", "import sqlite3, sys; sqlite3.connect(sys.argv[1]).executescript('CREATE TABLE notes (body TEXT);')", other]);
  const before = hash(other);
  const { app } = await openApp(t, home, [python, "-m", "hearth.cli"], { profile: false, answers: [0, other, 0, 2] });

  await windowShowing(app, "needs a profile");

  const dialogs = await asked(app);
  assert.equal(dialogs[2].message, "Hearth did not use that file.");
  assert.match(dialogs[2].detail, /not a current Hearth database/);
  assert.equal(hash(other), before);
  assert.equal(existsSync(join(home.dir, "hearth/profile.json")), false);
});

test("choosing an existing knowledge base uses it as it is", async (t) => {
  const home = scratch();
  const existing = join(home.dir, "Library/Hearth/hearth.sqlite");
  mkdirSync(join(home.dir, "Library/Hearth"), { recursive: true });
  writeFileSync(join(home.dir, "note.md"), "# Note\n\nThe owner is Ada.\n");
  execFileSync(python, ["-c", "import sys; from pathlib import Path; from hearth.service import HearthService\n"
                        + "s = HearthService(Path(sys.argv[1])); s.import_document(sys.argv[2]); s.close()", existing, join(home.dir, "note.md")],
               { env: { PYTHONPATH: join(repo, "src"), HOME: join(home.dir, "home") } });
  const before = hash(existing);
  const { app } = await openApp(t, home, [python, "-m", "hearth.cli"], { profile: false, answers: [0, existing] });

  await windowShowing(app, "Hub");

  assert.equal(JSON.parse(readFileSync(join(home.dir, "hearth/profile.json"), "utf8")).database, realpathSync(existing));
  assert.equal(hash(existing), before, "checking and opening a current knowledge base does not change it");
});

test("a broken profile is reported in the window with the backend's own reason, and left as it is", async (t) => {
  const home = scratch();
  mkdirSync(join(home.dir, "hearth"));
  writeFileSync(join(home.dir, "hearth/profile.json"), "{not json");
  const before = hash(join(home.dir, "hearth/profile.json"));
  const { app } = await openApp(t, home, [python, "-m", "hearth.cli"], { profile: false });

  const page = await windowShowing(app, "not valid JSON");

  assert.match(await page.evaluate(() => document.body.innerText), /profile\.json[\s\S]*Next:/);
  assert.equal(hash(join(home.dir, "hearth/profile.json")), before);
});

test("opened from any folder, the app gives the backend the same absolute profile and starts it in the home folder", async (t) => {
  for (const folder of ["/", repo, desktop]) {
    const home = scratch();
    const { app } = await openApp(t, home, fakeBackend, { cwd: folder });
    await windowShowing(app, "fake dashboard");
    const [started] = backendLog(home).secrets;
    await app.close();

    assert.deepEqual(started.argv, ["--profile", join(home.dir, "hearth/profile.json"), "web", "--desktop", "--handshake-fd", "3"], folder);
    assert.equal(realpathSync(started.cwd), realpathSync(join(home.dir, "home")), folder);
  }
});
