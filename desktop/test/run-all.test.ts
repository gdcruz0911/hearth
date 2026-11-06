// The guard in run-all.mjs: a run where a test file ended early fails, though node --test alone exits 0 for it.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

function runAll(t: { after: (hook: () => void) => void }, ...sources: string[]) {
  const dir = mkdtempSync(join(tmpdir(), "hearth-run-all-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const files = sources.map((source, index) => {
    const file = join(dir, `scratch${index}.test.ts`);
    writeFileSync(file, `import test from "node:test";\n${source}\n`);
    return file;
  });
  const { HEARTH_TEST_FINISHED: _, NODE_TEST_CONTEXT: __, ...env } = process.env;  // The child runs its own tests, not this file's.
  const shown = spawnSync(process.execPath, [join(import.meta.dirname, "run-all.mjs"), ...files], { encoding: "utf8", env });
  return { files, ...shown };
}

test("a run where every test file finishes passes, and names the checkout", (t) => {
  const { status, stdout, stderr } = runAll(t, `test("one", () => {});\ntest("two", { skip: true }, () => {});`);
  assert.equal(status, 0, stderr);
  assert.match(stdout, /All 1 test files finished in .+ at \w+\./);
});

test("a test file that exits with code 0 partway through fails the run, and is named", (t) => {
  const { status, stderr, files } = runAll(t, `test("one", () => {});`, `test("two", () => { process.exit(0); });\ntest("three", () => {});`);
  assert.equal(status, 1);
  assert.match(stderr, /1 of 2 test files finished/);
  assert.ok(stderr.includes(files[1]), stderr);
  assert.ok(!stderr.includes(files[0]), stderr);
});

test("a failing test fails the run without blaming an early end", (t) => {
  const { status, stderr } = runAll(t, `test("one", () => { throw new Error("no"); });`);
  assert.equal(status, 1);
  assert.doesNotMatch(stderr, /ended early/);
});
