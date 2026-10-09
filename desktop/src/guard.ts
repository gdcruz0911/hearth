// The shell's decisions about what to trust, kept free of Electron so plain Node tests can check them (ADR-0038).

import { createHmac, timingSafeEqual } from "node:crypto";

export interface Handshake {
  port: number;
  cookie_name: string;
  cookie: string;
  token: string;
}

// The backend's one JSON line on the private pipe; anything else is refused rather than guessed at.
export function parseHandshake(line: string): Handshake {
  const value = JSON.parse(line) as Partial<Handshake>;
  const secret = (text: unknown) => typeof text === "string" && /^[A-Za-z0-9_-]{32,}$/.test(text);
  if (!Number.isInteger(value.port) || value.port! < 1 || value.port! > 65535 || value.cookie_name !== `hearth_${value.port}`
      || !secret(value.cookie) || !secret(value.token)) {
    throw new Error("The backend's handshake was not in the expected form.");
  }
  return value as Handshake;
}

export function expectedAnswer(token: string, challenge: string): string {
  return createHmac("sha256", token).update(`hearth-desktop-challenge\0${challenge}`).digest("hex");
}

export function answerMatches(token: string, challenge: string, answer: unknown): boolean {
  const expected = Buffer.from(expectedAnswer(token, challenge));
  return typeof answer === "string" && answer.length === expected.length && timingSafeEqual(Buffer.from(answer), expected);
}

// Scheme, host, and port compared exactly: cookies are not isolated by port, so 127.0.0.1 on another port is a stranger.
export function isOwned(url: string, origin: string | null): boolean {
  if (origin === null) return false;
  try {
    return new URL(url).origin === origin;
  } catch {
    return false;
  }
}

export function isApiRoute(url: string, origin: string | null): boolean {
  return isOwned(url, origin) && new URL(url).pathname.startsWith("/api/");
}

// The page sends an empty X-Hearth-Session when it has no token; the hook replaces it on API routes and removes it elsewhere.
export function withSessionHeader(headers: Record<string, string>, url: string, origin: string | null, token: string | null): Record<string, string> {
  const kept = Object.fromEntries(Object.entries(headers).filter(([name]) => name.toLowerCase() !== "x-hearth-session"));
  return token !== null && isApiRoute(url, origin) ? { ...kept, "X-Hearth-Session": token } : kept;
}

export function isExternalLink(url: string): boolean {
  try {
    return new URL(url).protocol === "https:";
  } catch {
    return false;
  }
}
