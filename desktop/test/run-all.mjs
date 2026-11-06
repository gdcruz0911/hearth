// `npm test`: runs the desktop tests, and fails unless every test file finished. Under `node --test`, a file whose process exits
// with code 0 partway through counts as one passing test, and the tests it never ran are not reported anywhere. On success this
// names the checkout and commit, because a complete run of another checkout looks just as green.

import { execFileSync, spawnSync } from "node:child_process";
import { globSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const desktop = resolve(import.meta.dirname, "..");
const files = (process.argv.length > 2 ? process.argv.slice(2) : globSync("test/*.test.ts", { cwd: desktop })).map((file) => resolve(desktop, file));
const folder = mkdtempSync(join(tmpdir(), "hearth-tests-"));
const record = join(folder, "finished");
writeFileSync(record, "");

const run = spawnSync(process.execPath, ["--test", "--test-concurrency=1", "--import", join(import.meta.dirname, "finished.mjs"), ...files],
                      { cwd: desktop, stdio: "inherit", env: { ...process.env, HEARTH_TEST_FINISHED: record } });
const finished = new Set(readFileSync(record, "utf8").split("\n"));
rmSync(folder, { recursive: true, force: true });

const unfinished = files.filter((file) => !finished.has(file));
if (!files.length || unfinished.length) {
  console.error(`FAILED: ${files.length - unfinished.length} of ${files.length} test files finished; these ended early, so their tests may not have run:`);
  unfinished.forEach((file) => console.error(`  ${file}`));
  process.exit(1);
}
if (run.status !== 0) process.exit(1);
const commit = execFileSync("git", ["-C", desktop, "rev-parse", "--short", "HEAD"]).toString().trim();
console.log(`All ${files.length} test files finished in ${resolve(desktop, "..")} at ${commit}.`);
