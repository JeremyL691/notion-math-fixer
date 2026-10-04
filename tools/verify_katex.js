#!/usr/bin/env node
/**
 * Validate LaTeX expressions with KaTeX before trusting them to Notion.
 *
 *   npm i katex
 *   node tools/verify_katex.js [--json] expressions.json
 *
 * `expressions.json` is `{"expressions": [...]}` where each entry is either a
 * string (rendered in display mode) or `{"expr": "...", "display": true|false}`.
 * notion_math_fixer.py writes the object form when you pass --katex.
 *
 * Exit codes: 0 all render, 1 some fail, 2 setup error -- so it can gate a CI step.
 * With --json, stdout is a single object: {"ok": bool, "failures": [{index, error}]}.
 */
const fs = require("fs");
const path = require("path");

const args = process.argv.slice(2);
const asJson = args.includes("--json");
const file = args.find((a) => a !== "--json");

function fail(msg) {
  console.error(msg);
  process.exit(2);
}

let katex;
// katex may live in the repo root (npm i katex) or next to this script.
for (const base of [process.cwd(), path.join(__dirname, ".."), __dirname]) {
  try {
    katex = require(require.resolve("katex", { paths: [base] }));
    break;
  } catch { /* try next */ }
}
if (!katex) fail("katex not installed — run `npm i katex` first");
if (!file) fail("usage: node tools/verify_katex.js [--json] expressions.json");

let data;
try {
  data = JSON.parse(fs.readFileSync(file, "utf8"));
} catch (e) {
  fail(`cannot read ${file}: ${e.message}`);
}
const list = (data.expressions || []).map((x) =>
  typeof x === "string" ? { expr: x, display: true } : { expr: String(x.expr), display: x.display !== false });
const failures = [];

list.forEach(({ expr, display }, index) => {
  try {
    katex.renderToString(expr, { throwOnError: true, displayMode: display });
  } catch (e) {
    failures.push({ index, expression: expr.slice(0, 90), error: String(e.message).slice(0, 200) });
  }
});

if (asJson) {
  console.log(JSON.stringify({ ok: failures.length === 0, failures }));
} else {
  console.log(`KaTeX: ${list.length - failures.length}/${list.length} expressions render cleanly`);
  if (failures.length) console.log(JSON.stringify(failures, null, 2));
}
process.exit(failures.length ? 1 : 0);
