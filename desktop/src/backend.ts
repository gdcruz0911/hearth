// Starts the Python backend as this app's child and trusts it only once it proves it holds this launch's secret (ADR-0038).

import { spawn, type ChildProcess } from "node:child_process";
import { randomBytes } from "node:crypto";
import { request } from "node:http";
import { answerMatches, parseHandshake, type Handshake } from "./guard.js";

export interface Backend {
  child: ChildProcess;
  origin: string;
  handshake: Handshake;
}

const READY_TIMEOUT_MS = 30_000;

// The handshake arrives on fd 3, never on output, so the backend's own output can be shown as it is. Its stderr is passed on
// and its tail kept, so a backend that refuses to start, such as over a broken profile, says why in the window.
export async function launch(argv: string[], env: NodeJS.ProcessEnv, cwd: string): Promise<Backend> {
  const child = spawn(argv[0], argv.slice(1), { stdio: ["ignore", "inherit", "pipe", "pipe"], env, cwd });
  let said = "";
  child.stderr!.on("data", (chunk: Buffer) => {
    process.stderr.write(chunk);
    said = (said + chunk.toString("utf8")).slice(-4000);
  });
  try {
    const handshake = parseHandshake(await firstLine(child, () => said));
    const origin = `http://127.0.0.1:${handshake.port}`;
    const challenge = randomBytes(32).toString("hex");
    const reply = await get(`${origin}/desktop/challenge?c=${challenge}`, {});
    if (reply.status !== 200 || !answerMatches(handshake.token, challenge, (JSON.parse(reply.body) as { answer?: unknown }).answer)) {
      throw new Error("The server on the backend's port could not prove it is this launch's backend.");
    }
    return { child, origin, handshake };
  } catch (error) {
    child.kill("SIGTERM");
    throw error;
  }
}

function firstLine(child: ChildProcess, said: () => string): Promise<string> {
  return new Promise((resolve, reject) => {
    let text = "";
    const pipe = child.stdio[3]!;
    const timer = setTimeout(() => reject(new Error("The backend did not start within 30 seconds.")), READY_TIMEOUT_MS);
    const finish = (error: Error | null) => {
      clearTimeout(timer);
      pipe.removeAllListeners("data");
      child.removeListener("close", exited);
      child.removeListener("error", finish);
      if (error) reject(error);
      else resolve(text.slice(0, text.indexOf("\n")));
    };
    // "close" comes after the process has exited and its output has ended, so its last words come with the exit code.
    const exited = (code: number | null) => finish(new Error(
      `The backend stopped before it was ready (exit code ${code}).${said().trim() ? `\n${said().trim()}` : ""}`));
    pipe.on("data", (chunk: Buffer) => {
      text += chunk.toString("utf8");
      if (text.includes("\n")) finish(null);
    });
    child.once("close", exited);
    child.once("error", finish);
  });
}

export function get(url: string, headers: Record<string, string>): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    const sent = request(url, { headers, agent: false, timeout: 5_000 }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk: string) => (body += chunk));
      response.on("end", () => resolve({ status: response.statusCode ?? 0, body }));
    });
    sent.on("timeout", () => sent.destroy(new Error("The backend did not answer.")));
    sent.on("error", reject);
    sent.end();
  });
}
