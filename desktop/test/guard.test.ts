import assert from "node:assert/strict";
import { test } from "node:test";
import { answerMatches, expectedAnswer, isApiRoute, isExternalLink, isOwned, parseHandshake, withSessionHeader } from "../out/guard.js";

const origin = "http://127.0.0.1:5000";
const secret = "a".repeat(43);

test("only the exact scheme, host, and port count as the backend", () => {
  assert.equal(isOwned(`${origin}/api/x`, origin), true);
  for (const url of ["http://127.0.0.1:5001/", "http://localhost:5000/", "https://127.0.0.1:5000/", "http://127.0.0.1:5000@evil.test/",
                     "data:text/html,x", "not a url"]) {
    assert.equal(isOwned(url, origin), false, url);
  }
  assert.equal(isOwned(`${origin}/`, null), false);
});

test("the token replaces the page's header on API routes and is removed everywhere else", () => {
  const sent = { "x-hearth-session": "", Accept: "*/*" };
  assert.deepEqual(withSessionHeader(sent, `${origin}/api/workbench/tasks`, origin, "T"), { Accept: "*/*", "X-Hearth-Session": "T" });
  assert.deepEqual(withSessionHeader(sent, `${origin}/assets/dashboard.js`, origin, "T"), { Accept: "*/*" });
  assert.deepEqual(withSessionHeader(sent, "http://127.0.0.1:5001/api/x", origin, "T"), { Accept: "*/*" });
  assert.deepEqual(withSessionHeader(sent, `${origin}/api/x`, origin, null), { Accept: "*/*" });
  assert.equal(isApiRoute(`${origin}/apix`, origin), false);
});

test("the challenge answer must be this launch's HMAC", () => {
  const challenge = "ab".repeat(32);
  assert.equal(answerMatches(secret, challenge, expectedAnswer(secret, challenge)), true);
  assert.equal(answerMatches(secret, challenge, expectedAnswer("b".repeat(43), challenge)), false);
  assert.equal(answerMatches(secret, challenge, undefined), false);
  assert.equal(answerMatches(secret, challenge, "short"), false);
});

test("a handshake in any other form is refused", () => {
  const good = { port: 5000, cookie_name: "hearth_5000", cookie: secret, token: secret };
  assert.deepEqual(parseHandshake(JSON.stringify(good)), good);
  for (const bad of [{ ...good, port: 0 }, { ...good, cookie_name: "hearth_5001" }, { ...good, token: "short" }, { ...good, cookie: 5 }]) {
    assert.throws(() => parseHandshake(JSON.stringify(bad)), /expected form/);
  }
});

test("only https links leave the app", () => {
  assert.equal(isExternalLink("https://github.com/p/r/pull/1"), true);
  for (const url of ["http://example.test/", "javascript:alert(1)", "file:///etc/hosts", "mailto:a@b.c", ""]) {
    assert.equal(isExternalLink(url), false, url);
  }
});
