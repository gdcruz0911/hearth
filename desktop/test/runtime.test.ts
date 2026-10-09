// D3c: without HEARTH_BACKEND the app starts the installed runtime, isolated from other Python code, and refuses when the base
// Python it was built on has changed (ADR-0038).

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, readFileSync, symlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { openApp, python, realBackend, scratch, windowShowing, type Scratch } from "./helpers.ts";

const base = execFileSync(python, ["-c", "import sys; print(sys._base_executable)"], { encoding: "utf8" }).trim();
const version = execFileSync(base, ["-I", "-c", "import platform; print(platform.python_version())"], { encoding: "utf8" }).trim();

// A runtime folder like hearth.install makes, whose python records how the app ran it and then runs the development Python.
function runtime(home: Scratch, recorded: string): { root: string; calls: string } {
  const root = join(home.dir, "runtime");
  const calls = join(root, "calls.log");
  mkdirSync(join(root, "v1/bin"), { recursive: true });
  writeFileSync(join(root, "v1/bin/python"), `#!/bin/sh\necho "$* | PYTHONPATH=$PYTHONPATH PYTHONNOUSERSITE=$PYTHONNOUSERSITE" >> "${calls}"\nexec "${python}" "$@"\n`);
  chmodSync(join(root, "v1/bin/python"), 0o755);
  writeFileSync(join(root, "v1/runtime.json"), JSON.stringify({ format: "hearth-runtime-v1", base: { path: base, version: recorded }, ref: "v0.1.0" }));
  symlinkSync("v1", join(root, "current"));
  return { root, calls };
}

test("the installed runtime starts in isolated mode from a scrubbed environment, so other Python code cannot load", async (t) => {
  const home = scratch();
  realBackend(home.dir);  // Its profile, naming a new, empty knowledge base.
  const { root, calls } = runtime(home, version);
  const decoy = join(home.dir, "decoy/hearth");
  mkdirSync(decoy, { recursive: true });
  writeFileSync(join(decoy, "__init__.py"), "raise SystemExit('the decoy hearth was imported')\n");
  const { app } = await openApp(t, home, null, { env: { HEARTH_RUNTIME: root, PYTHONPATH: join(home.dir, "decoy") } });

  await windowShowing(app, "Hub");

  const started = readFileSync(calls, "utf8").trim().split("\n").at(-1)!;
  assert.match(started, new RegExp(`^-I -m hearth\\.cli --profile ${join(home.dir, "hearth/profile.json")} web --desktop`));
  assert.match(started, /\| PYTHONPATH= PYTHONNOUSERSITE=1$/);
});

test("a changed base Python is refused with the command that rebuilds the runtime, and nothing starts", async (t) => {
  const home = scratch();
  realBackend(home.dir);
  const { root, calls } = runtime(home, "3.13.0");
  const { app } = await openApp(t, home, null, { env: { HEARTH_RUNTIME: root } });

  const page = await windowShowing(app, "Hearth's Python changed");

  const text = await page.evaluate(() => document.body.innerText);
  assert.match(text, new RegExp(`built on Python 3\\.13\\.0 at ${base.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}, which now reports ${version.replace(/\./g, "\\.")}`));
  assert.match(text, /python3 -m hearth\.install runtime --base "[^"]+" --ref v0\.1\.0/);
  assert.equal(existsSync(calls), false);
});

test("with no runtime installed, the app says how to install one", async (t) => {
  const home = scratch();
  const { app } = await openApp(t, home, null, { env: { HEARTH_RUNTIME: join(home.dir, "nowhere") } });

  const page = await windowShowing(app, "runtime is not installed");

  assert.match(await page.evaluate(() => document.body.innerText), /hearth\.install runtime --base "\/Library\/Frameworks\/Python\.framework/);
});
