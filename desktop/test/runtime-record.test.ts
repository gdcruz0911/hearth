import assert from "node:assert/strict";
import { test } from "node:test";
import { runtimeRecord } from "../out/runtime.js";

const good = { base: { path: "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14", version: "3.14.6" }, ref: "v0.1.0" };

test("a runtime record is used only when every field the app relies on is there and of the right kind", () => {
  assert.deepEqual(runtimeRecord(good), good);
  assert.deepEqual(runtimeRecord({ ...good, source: "/Users/person/hearth" }), { ...good, source: "/Users/person/hearth" });
  for (const [raw, reason] of [[null, "not a JSON object"], [[], "not a JSON object"], [{}, "no absolute base Python"],
                               [{ ...good, base: { path: "python3", version: "3.14.6" } }, "no absolute base Python"],
                               [{ ...good, base: { path: good.base.path } }, "no base Python version"],
                               [{ ...good, ref: "" }, "no release"], [{ ...good, source: 7 }, "unexpected form"]] as const) {
    assert.match(runtimeRecord(raw) as string, new RegExp(reason), JSON.stringify(raw));
  }
});
