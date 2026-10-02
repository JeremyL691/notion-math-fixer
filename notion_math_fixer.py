#!/usr/bin/env python3
"""
notion-math-fixer
=================

Turn LLM-generated / HTML-derived LaTeX inside an existing Notion page into
**native Notion equation blocks** -- through the official API, in one shot, with
verification.

The dialect this tool eats (what ChatGPT, Claude, scraped HTML and most LLM
exporters actually produce):

    \\[ ... \\]          -> block equation   ($$ ... $$)
    \\( ... \\)          -> inline equation  ($ ... $)
    <br> inside math   -> newline
    \\{ x \\}             -> { x }  (only when the braces are otherwise unbalanced)
    <table><tr><td>    -> a native Notion table (markdown pipe table)
    nested children    -> preserved (a quote that owns paragraph children, etc.)

Design notes -- why it works this way
-------------------------------------
1. The page is rebuilt from the **block tree**, never from the markdown export.
   `GET /v1/pages/{id}/markdown` escapes backslashes and silently *truncates* some
   equation expressions (verified 2026-10: `\\pi_{authors.name}` comes back as
   `\\pi_{`), while the block tree holds the full expression. See
   docs/export-truncation.md for a reproducible report.
2. Blocks can be nested (a quote can own paragraph children). A top-level-only walk
   silently drops content, so the tree is walked recursively.
3. The tool refuses to guess about content it cannot express losslessly: unsupported
   block types abort the run unless --force is passed.
4. Dry-run is the default. Every apply is backed up first and verified afterwards.

Usage
-----
    export NOTION_TOKEN=ntn_xxx        # or --token-file ~/.notion_token
    python3 notion_math_fixer.py <page-id-or-url>              # audit only (safe)
    python3 notion_math_fixer.py <page-id-or-url> --apply      # rebuild + verify
    python3 notion_math_fixer.py <page-id-or-url> --apply --katex   # + KaTeX check

License: MIT
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter

API = "https://api.notion.com/v1"
# 2025-09-03 is the version the markdown endpoints were tested against.
# Notion documents 2026-03-11 as the latest; override with --notion-version.
DEFAULT_VERSION = "2025-09-03"

# ---------------------------------------------------------------- text helpers

INLINE_TEX = re.compile(r"\\\((.*?)\\\)", re.S)
BLOCK_TEX = re.compile(r"\\\[(.*?)\\\]", re.S)   # re.S: display math spans lines
INLINE_COUNT = re.compile(r"(?<!\$)\$(?!\$)[^$\n]+\$(?!\$)")   # $...$ but not $$...$$
TEXTY = ("paragraph", "heading_1", "heading_2", "heading_3", "quote",
         "bulleted_list_item", "numbered_list_item", "to_do", "callout",
         "toggle", "code", "table_row")


def normalize_math(expr: str) -> tuple[str, list[str]]:
    """Clean one LaTeX expression; return (expression, notes)."""
    notes: list[str] = []
    out = expr.replace("<br>", "\n")
    # Escaped braces are an artifact of markdown/HTML escaping *unless* they are
    # meant as literal set braces. If the braces are unbalanced as-is but balance
    # after unescaping, they were an artifact -- fix them, and say so.
    if out.count("{") != out.count("}"):
        fixed = out.replace("\\{", "{").replace("\\}", "}")
        if fixed.count("{") == fixed.count("}"):
            out = fixed
            notes.append("unescaped unbalanced \\{ \\}")
    out = re.sub(r"\\_", "_", out) if out.count("_") and "\\_" in out else out
    out = re.sub(r"\n{2,}", "\n", out).strip("\n")
    return out, notes


def rich_text_to_md(runs: list[dict]) -> str:
    """rich_text array -> markdown, converting literal \\( .. \\) into $ .. $."""
    out = []
    for rt in runs:
        if rt.get("type") == "equation":
            out.append("$" + " ".join(rt["equation"]["expression"].split()) + "$")
            continue
        s = rt.get("plain_text", "")
        s = INLINE_TEX.sub(lambda m: "$" + " ".join(m.group(1).split()) + "$", s)
        ann = rt.get("annotations", {})
        if ann.get("code"):
            s = "`" + s + "`"
        if ann.get("bold"):
            s = "**" + s + "**"
        if ann.get("italic"):
            s = "*" + s + "*"
        if ann.get("strikethrough"):
            s = "~~" + s + "~~"
        out.append(s)
    return "".join(out)


def split_block_math(text: str, notes: list[str] | None = None) -> str:
    """`\\[ a \\]` (possibly several per paragraph) -> real $$ equation blocks.

    A paragraph that merely *talks about* display math ("use \\[ .. \\] for ...")
    is left alone: the prose around the span must be short for it to count as an
    equation, otherwise the tool would invent math blocks out of documentation.
    """
    if "\\[" not in text:
        return text
    rest = BLOCK_TEX.sub("", text).strip()
    if len(re.sub(r"\s+", "", rest)) > 60:
        if notes is not None:
            notes.append("kept \\[...\\] as text (paragraph is prose, not math)")
        return text
    parts, pos = [], 0
    for m in BLOCK_TEX.finditer(text):
        pre = text[pos:m.start()].strip()
        if pre:
            parts.append(pre)
        expr, n = normalize_math(m.group(1))
        if notes is not None:
            notes += n
        parts.append("$$\n" + expr + "\n$$")
        pos = m.end()
    tail = text[pos:].strip()
    if tail:
        parts.append(tail)
    return "\n\n".join(parts)


def paragraph_md(text: str) -> str:
    """Paragraph text -> markdown.

    Newlines *outside* math become `<br>`: that is the documented way to keep a
    line break inside a single paragraph block, and a bare newline would be
    silently merged away by the importer.
    """
    pieces, pos = [], 0
    for m in re.finditer(r"\$\$\n.*?\n\$\$", text, re.S):
        prose = text[pos:m.start()].strip()
        if prose:
            pieces.append(re.sub(r"\s*\n\s*", "<br>", prose))
        pieces.append(m.group(0))
        pos = m.end()
    tail = text[pos:].strip()
    if tail:
        pieces.append(re.sub(r"\s*\n\s*", "<br>", tail))
    return "\n\n".join(p for p in pieces if p)


def plain_of(block: dict) -> str:
    return "".join(x.get("plain_text", "") for x in (block[block["type"]].get("rich_text") or []))


# ------------------------------------------------------------------ the client

class Notion:
    def __init__(self, token: str, version: str = DEFAULT_VERSION):
        self.token = token
        self.version = version

    def call(self, path: str, method: str = "GET", body: dict | None = None, tries: int = 4):
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(tries):
            req = urllib.request.Request(
                f"{API}/{path}", data=data, method=method,
                headers={"Authorization": f"Bearer {self.token}",
                         "Notion-Version": self.version,
                         "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                payload = e.read(600).decode("utf-8", "ignore")
                if e.code in (429, 500, 502, 503, 504, 529) and attempt < tries - 1:
                    time.sleep(2 ** attempt)
                    continue
                return {"error": e.code, "body": payload}
            except urllib.error.URLError as e:  # network hiccup
                if attempt < tries - 1:
                    time.sleep(2 ** attempt)
                    continue
                return {"error": "network", "body": str(e)}
        return {"error": "exhausted"}

    def children(self, block_id: str) -> list[dict]:
        out, cursor = [], None
        while True:
            q = f"blocks/{block_id}/children?page_size=100"
            if cursor:
                q += f"&start_cursor={cursor}"
            page = self.call(q)
            if "results" not in page:
                raise RuntimeError(f"cannot read children of {block_id}: {page}")
            out += page["results"]
            if not page.get("has_more"):
                return out
            cursor = page["next_cursor"]

    def tree(self, block_id: str) -> list[dict]:
        """Top-level children, each with a '_children' key when it has children."""
        blocks = self.children(block_id)
        for b in blocks:
            if b.get("has_children") and b["type"] not in ("table", "table_row"):
                b["_children"] = self.tree(b["id"])
        return blocks

    def markdown(self, page_id: str) -> dict:
        return self.call(f"pages/{page_id}/markdown")


# ------------------------------------------------------------------- rendering

SAFE = {"heading_1", "heading_2", "heading_3", "paragraph", "quote", "divider", "code",
        "table", "table_row", "equation", "bulleted_list_item", "numbered_list_item",
        "to_do", "child_page", "child_database"}


def render_table(notion: Notion, block: dict) -> str:
    rows = notion.children(block["id"])
    lines = ["| " + " | ".join(" ".join(rich_text_to_md(c).split()) for c in r["table_row"]["cells"]) + " |"
             for r in rows]
    sep = "| " + " | ".join(["---"] * (len(lines[0].split("|")) - 2)) + " |"
    return "\n".join([lines[0], sep] + lines[1:])


def render(notion: Notion, blocks: list[dict], depth: int = 0) -> tuple[list[str], set[str], list[str]]:
    """block tree -> (markdown chunks, unsupported types, normalization notes)"""
    chunks: list[str] = []
    unsupported: set[str] = set()
    notes: list[str] = []
    pad = "  " * depth
    for b in blocks:
        t = b["type"]
        if t not in SAFE:
            unsupported.add(t)
            continue
        if t in ("heading_1", "heading_2", "heading_3"):
            chunks.append("#" * int(t[-1]) + " " + rich_text_to_md(b[t]["rich_text"]))
        elif t == "paragraph":
            chunks.append(paragraph_md(split_block_math(rich_text_to_md(b[t]["rich_text"]), notes)))
        elif t == "quote":
            lines = ["> " + paragraph_md(rich_text_to_md(b[t]["rich_text"]))]
            for child in b.get("_children", []):          # nested content must survive
                sub, u, n = render(notion, [child], depth)
                unsupported |= u
                notes += n
                for piece in sub:
                    lines += ["> " + ln for ln in piece.split("\n")]
            chunks.append("\n".join(lines))
        elif t == "divider":
            chunks.append("---")
        elif t == "code":
            lang = b[t].get("language") or ""
            lang = "" if lang == "plain text" else lang
            chunks.append("```" + lang + "\n" + plain_of(b) + "\n```")
        elif t == "table":
            chunks.append(render_table(notion, b))
        elif t in ("bulleted_list_item", "numbered_list_item", "to_do"):
            marker = {"bulleted_list_item": "- ", "numbered_list_item": "1. ",
                      "to_do": "- [x] " if b[t].get("checked") else "- [ ] "}[t]
            line = pad + marker + split_block_math(rich_text_to_md(b[t]["rich_text"])).replace("\n\n", "\n" + pad)
            if b.get("_children"):
                sub, u, n = render(notion, b["_children"], depth + 1)
                unsupported |= u
                notes += n
                line += "\n" + "\n".join(sub)
            chunks.append(line)
        elif t == "equation":
            chunks.append("$$\n" + b["equation"]["expression"] + "\n$$")
        elif t == "child_page":
            chunks.append(f'<page url="https://www.notion.so/{b["id"].replace("-", "")}">'
                          f'{b["child_page"]["title"]}</page>')
        elif t == "child_database":
            chunks.append(f'<database url="https://www.notion.so/{b["id"].replace("-", "")}">'
                          f'{b["child_database"]["title"]}</database>')
    return chunks, unsupported, notes


def build_markdown(notion: Notion, blocks: list[dict]) -> tuple[str, set[str], list[str]]:
    chunks, unsupported, notes = render(notion, blocks)
    return "\n\n".join(c for c in chunks if c.strip()) + "\n", unsupported, notes


# ------------------------------------------------------------------ verification

def alnum_tokens(md: str) -> list[str]:
    md = re.sub(r"<[^>]*>", "", md)
    return re.findall(r"[0-9A-Za-z\u4e00-\u9fff]+", md)


def verify(notion: Notion, page_id: str, expected_md: str, before_md: str) -> bool:
    blocks = notion.tree(page_id)
    types = Counter(b["type"] for b in blocks)
    literal = [b for b in blocks
               if b["type"] in TEXTY and ("\\[" in plain_of(b) or "\\(" in plain_of(b))]
    want = re.findall(r"\$\$\n(.*?)\n\$\$", expected_md, re.S)
    got = [b["equation"]["expression"] for b in blocks if b["type"] == "equation"]
    same = sum(1 for a, b in zip(got, want)
               if re.sub(r"\s+", " ", a) == re.sub(r"\s+", " ", b))
    tokens_before, tokens_after = alnum_tokens(before_md), alnum_tokens(notion.markdown(page_id).get("markdown", ""))
    import difflib
    drift = [l for l in difflib.unified_diff(tokens_before, tokens_after, lineterm="", n=0)
             if l[:1] in "+-" and l[:3] not in ("+++", "---")]

    print("\n--- verification ---")
    print(f"  blocks            : {len(blocks)}  {dict(types)}")
    print(f"  literal LaTeX left: {len(literal)}   (must be 0)")
    print(f"  equation blocks   : {types.get('equation', 0)}   (expected {len(want)})")
    print(f"  expressions match : {same}/{len(want)}")
    print(f"  text drift tokens : {len(drift)}   (code-fence language labels are expected)")
    ok = not literal and types.get("equation", 0) == len(want) and same == len(want)
    print(f"  RESULT            : {'PASS' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------------- KaTeX

def katex_check(expressions: list[str], workdir: str) -> bool:
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "verify_katex.js")
    if not os.path.exists(script):
        print("  (tools/verify_katex.js not found -- skipping KaTeX check)")
        return True
    import subprocess
    payload = os.path.join(workdir, "expressions.json")
    json.dump({"expressions": expressions}, open(payload, "w"))
    try:
        out = subprocess.run(["node", script, payload], capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        print("  (node not installed -- skipping KaTeX check)")
        return True
    print(out.stdout.strip() or out.stderr.strip()[-400:])
    return out.returncode == 0


# ------------------------------------------------------------------------ main

def page_id_from(arg: str) -> str:
    m = re.search(r"([0-9a-f]{32})", arg.replace("-", ""))
    if not m:
        m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", arg)
    if not m:
        sys.exit(f"cannot find a Notion page id in {arg!r}")
    raw = m.group(1).replace("-", "")
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def read_token(args) -> str:
    if args.token:
        return args.token
    if os.environ.get("NOTION_TOKEN"):
        return os.environ["NOTION_TOKEN"].strip()
    path = os.path.expanduser(args.token_file)
    if os.path.exists(path):
        return open(path).read().strip()
    sys.exit("no Notion token: set NOTION_TOKEN, or --token-file, or --token")


def main() -> int:
    ap = argparse.ArgumentParser(description="Convert LLM/HTML LaTeX in a Notion page into native equation blocks.")
    ap.add_argument("page", help="Notion page id or URL")
    ap.add_argument("--apply", action="store_true", help="write the rebuilt page (default: dry run)")
    ap.add_argument("--force", action="store_true", help="proceed even if unsupported block types are present")
    ap.add_argument("--katex", action="store_true", help="also validate every expression with KaTeX (needs node + katex)")
    ap.add_argument("--token", help="Notion integration token")
    ap.add_argument("--token-file", default="~/.notion_token", help="file holding the token")
    ap.add_argument("--notion-version", default=DEFAULT_VERSION)
    ap.add_argument("--backup-dir", default="~/.cache/notion-math-fixer")
    args = ap.parse_args()

    page = page_id_from(args.page)
    notion = Notion(read_token(args), args.notion_version)

    meta = notion.call(f"pages/{page}")
    if "error" in meta:
        sys.exit(f"cannot read page {page}: {meta}")
    title = ""
    for v in (meta.get("properties") or {}).values():
        if v.get("type") == "title":
            title = "".join(x.get("plain_text", "") for x in v["title"])
    print(f"page   : {title!r}  {page}")
    print(f"edited : {meta.get('last_edited_time')}")

    blocks = notion.tree(page)
    types = Counter(b["type"] for b in blocks)
    literal = [(i, plain_of(b)[:70]) for i, b in enumerate(blocks)
               if b["type"] in TEXTY and ("\\[" in plain_of(b) or "\\(" in plain_of(b))]
    html = [(i, b["type"]) for i, b in enumerate(blocks)
            if re.search(r"<(table|br|td|tr|div|ul|li|details|summary)\b", plain_of(b) or "")]
    nested = [(i, b["type"], len(b.get("_children", []))) for i, b in enumerate(blocks) if b.get("_children")]

    print(f"\n--- audit ---")
    print(f"  top-level blocks    : {len(blocks)}  {dict(types)}")
    print(f"  blocks w/ literal TeX: {len(literal)}")
    for i, s in literal[:6]:
        print(f"      #{i} {s!r}")
    print(f"  blocks w/ HTML tags : {len(html)} {html[:5]}")
    print(f"  nested (non-table)  : {nested}")
    print(f"  existing equations  : {types.get('equation', 0)}")

    md, unsupported, notes = build_markdown(notion, blocks)
    want = re.findall(r"\$\$\n(.*?)\n\$\$", md, re.S)
    print(f"\n--- rebuild ---")
    print(f"  {len(md)} chars | block equations {len(want)} | inline equations {len(INLINE_COUNT.findall(md))}")
    kept_prose = sum(1 for n in notes if n.startswith("kept"))
    for pat in ("\\[", "\\]", "\\(", "\\)", "<table", "<td>", "<tr>"):
        n = md.count(pat)
        if n == 0:
            verdict = "ok"
        elif pat in ("\\[", "\\]") and n == kept_prose:
            verdict = f"kept in {kept_prose} prose paragraph(s)"
        else:
            verdict = "<-- PROBLEM"
        print(f"    leftover {pat!r}: {n} {verdict}")
    math_spans = re.findall(r"\$\$.*?\$\$|\$[^$\n]+\$", md, re.S)
    br_in_math = sum(s.count("<br>") for s in math_spans)
    print(f"    <br> inside math: {br_in_math} {'ok' if br_in_math == 0 else '<-- PROBLEM'}")
    print(f"    <br> in prose   : {md.count('<br>')} (intentional line breaks)")
    if notes:
        print(f"  normalizations: {Counter(notes)}")
    if unsupported:
        print(f"  !! unsupported block types: {sorted(unsupported)}")

    backup_dir = os.path.expanduser(args.backup_dir)
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = os.path.join(backup_dir, f"{page}_{stamp}")
    json.dump(blocks, open(stem + ".blocks.json", "w"), ensure_ascii=False)
    open(stem + ".new.md", "w").write(md)
    before = notion.markdown(page).get("markdown", "")
    open(stem + ".before.md", "w").write(before)
    print(f"  backup: {stem}.{{blocks.json,new.md,before.md}}")

    if not args.apply:
        print("\ndry run -- nothing written. Re-run with --apply to rebuild the page.")
        return 0
    if unsupported and not args.force:
        print("\nrefusing to write: unsupported block types would be lost (use --force to override).")
        return 2

    res = notion.call(f"pages/{page}/markdown", "PATCH",
                      {"type": "replace_content", "replace_content": {"new_str": md}})
    if "error" in res:
        print(f"\nPATCH failed: {res}")
        return 1
    print("\nPATCH: ok")

    ok = verify(notion, page, md, before)
    if args.katex:
        ok = katex_check(want, backup_dir) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
