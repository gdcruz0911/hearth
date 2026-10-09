// Which backend the app starts, and from where it reads its data (ADR-0038).
//
// The data always comes from <HEARTH_HOME>/profile.json, given to the backend as an absolute path, so opening the app from
// Finder, a terminal in the repository, or desktop/ all use the same configuration. The backend is the installed runtime's,
// run in isolated mode from a scrubbed environment; HEARTH_BACKEND replaces it only for development and tests.

import { execFileSync } from "node:child_process";
import { lstatSync, readFileSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export const RUNTIME_ROOT = join(homedir(), "Library/Application Support/Hearth/runtime");  // As in hearth.install.
// Variables that would quietly load other code into the released runtime, such as the working checkout.
const SCRUBBED = ["PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"];

export interface BackendCommand {
  prefix: string[];  // Followed by the CLI's own arguments, such as ["--profile", path, "web", ...].
  env: NodeJS.ProcessEnv;
}

export function hearthHome(): string {
  return process.env.HEARTH_HOME || join(homedir(), ".hearth");
}

export function profilePath(): string {
  return join(hearthHome(), "profile.json");
}

// A link counts even when its target is gone: the backend then reports the broken profile instead of setup starting over it.
export function profilePresent(path: string): boolean {
  try {
    lstatSync(path);
    return true;
  } catch {
    return false;
  }
}

// The command, or why the app must not start one, with the command that fixes it.
export function backendCommand(): BackendCommand | { problem: string } {
  if (process.env.HEARTH_BACKEND) {
    const prefix = JSON.parse(process.env.HEARTH_BACKEND) as unknown;
    if (!Array.isArray(prefix) || prefix.length === 0 || !prefix.every((part) => typeof part === "string")) {
      return { problem: "HEARTH_BACKEND must be a JSON list that starts the CLI, such as [\"/path/to/python\", \"-m\", \"hearth.cli\"]." };
    }
    return { prefix, env: process.env };
  }
  const root = process.env.HEARTH_RUNTIME || RUNTIME_ROOT;
  const reinstall = (base: string, ref: string) =>
    `Next: from the Hearth checkout, run python3 -m hearth.install runtime --base "${base}" --ref ${ref}`;
  let current: string;
  let record: { base: { path: string; version: string }; ref: string };
  try {
    current = realpathSync(join(root, "current"));  // Resolved once, so this start uses one runtime even if it is switched.
    record = JSON.parse(readFileSync(join(current, "runtime.json"), "utf8"));
  } catch {
    return { problem: `Hearth's runtime is not installed at ${root}.\n${reinstall("/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14", "<release tag>")}` };
  }
  let found: string;
  try {
    found = execFileSync(record.base.path, ["-I", "-c", "import platform; print(platform.python_version())"], { encoding: "utf8", timeout: 15_000 }).trim();
  } catch {
    found = "nothing: it is missing or does not run";
  }
  if (found !== record.base.version) {
    return { problem: `Hearth's Python changed. Its runtime was built on Python ${record.base.version} at ${record.base.path}, which now reports ${found}, `
                      + `so Hearth will not start it.\n${reinstall(record.base.path, record.ref)}` };
  }
  const env: NodeJS.ProcessEnv = { ...process.env, PYTHONNOUSERSITE: "1" };
  for (const name of SCRUBBED) delete env[name];
  return { prefix: [join(current, "bin/python"), "-I", "-m", "hearth.cli"], env };
}
