// SPDX-License-Identifier: Apache-2.0
// Copyright (C) 2026 Novikov Laboratories LLC
// The shared vectors (vectors/assignment.json, made by the Python code) reproduced exactly.
// Plain node:assert, so that the tests run on Node.js 16.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { assign, sha256, uniform, utf8 } from "../dist/index.js";

const hex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");

// FIPS 180-4 examples
assert.equal(hex(sha256(utf8(""))), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
assert.equal(hex(sha256(utf8("abc"))), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
assert.equal(hex(sha256(utf8("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"))),
  "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1");
assert.equal(hex(sha256(utf8("a".repeat(1000000)))),
  "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0");
assert.equal(hex(utf8("ключ🙂")), "d0bad0bbd18ed187f09f9982");
assert.throws(() => utf8("\ud800"), RangeError);
assert.throws(() => utf8("a\udc00"), RangeError);

const vectors = JSON.parse(readFileSync(new URL("../../../vectors/assignment.json", import.meta.url), "utf8"));
for (const u of vectors.uniform) {
  assert.equal(uniform(u.domain, u.salt, u.unit), u.value, JSON.stringify(u));
}
for (const c of vectors.cases) {
  const got = assign(vectors.configs[c.config], c.unit, c.time);
  assert.deepEqual(got, { variant: c.variant, probability: c.probability, reason: c.reason }, JSON.stringify(c));
}
assert.throws(() => assign(vectors.configs[4], "u1"), /needs the time/);
console.log(`ab-client: ${vectors.uniform.length} uniforms and ${vectors.cases.length} assignments reproduced`);
