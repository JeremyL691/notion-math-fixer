#!/usr/bin/env node
/**
 * Validate LaTeX expressions with KaTeX before trusting them to Notion.
 *
 *   npm i katex
 *   node tools/verify_katex.js expressions.json
 *
 * `expressions.json` is `{"expressions": ["\\boxed{...}", ...]}` — notion_math_fixer.py
 * writes exactly that file when you pass --katex.
 *
 * Exits non-zero when any expression fails to render, so it can gate a CI step.
 */
const fs = require("fs");
const path = require("path");

let katex;
try {
  // katex may live in the repo root (npm i katex) or next to this script.
  const bases = [process.cwd(), path.join(__dirname, ".."), __dirname];
  for (const base of bases) {
    try {
      katex = require(require.resolve("katex", { paths: [base] }));
      break;
    } catch { /* try next */ }
  }
  if (!katex) katex = require("katex");
} catch {
  console.error("katex not installed — run `npm i katex` first");
  process.exit(2);
}

const file = process.argv[2];
if (!file) {
  console.error("usage: node tools/verify_katex.js expressions.json");
  process.exit(2);
}

const data = JSON.parse(fs.readFileSync(file, "utf8"));
const list = data.expressions || [];
const bad = [];

list.forEach((expr, i) => {
  try {
    katex.renderToString(expr, { throwOnError: true, displayMode: true });
  } catch (e) {
    bad.push({ index: i, expression: String(expr).slice(0, 90), error: String(e.message).slice(0, 160) });
  }
});

console.log(`KaTeX: ${list.length - bad.length}/${list.length} expressions render cleanly`);
if (bad.length) {
  console.log(JSON.stringify(bad, null, 2));
  process.exit(1);
}
