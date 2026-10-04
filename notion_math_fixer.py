#!/usr/bin/env python3
"""
notion-math-fixer
=================

Turn LLM-generated / HTML-derived LaTeX inside an existing Notion page into
**native Notion equations** -- through the official API, block by block, with a
journal you can undo and a verification pass afterwards.

The dialect this tool eats (what ChatGPT, Claude, scraped HTML and most LLM
exporters actually produce):

    \\[ ... \\]          -> equation block
    \\( ... \\)          -> inline equation
    <br>               -> newline (inside math and in prose)
    \\boxed\\{ x \\}      -> \\boxed{ x }  (escaped argument braces; set braces stay)
    <table><tr><td>    -> a native Notion table (cells may hold equations)

Design notes -- why it works this way
-------------------------------------
1. The page is read from the **block tree**, never from the markdown export.
   `GET /v1/pages/{id}/markdown` silently *truncates* some equation expressions
   (verified 2026-10: `\\pi_{authors.name}` comes back as `\\pi_{`). See
   docs/export-truncation.md.
2. Nothing is rewritten through markdown either. Only blocks that actually hold
   literal LaTeX are touched, and they are patched in place: links, colors,
   mentions, comments, children and block ids all survive. Every other block is
   left byte-for-byte alone.
3. Blocks nest (a list item owns children, a quote owns paragraphs), so the tree
   is walked recursively -- including table rows.
4. Dry-run is the default. Every write is journaled first-to-last, `--restore`
   undoes a run, and the page is re-read and re-planned afterwards: a correct run
   leaves nothing left to convert.

Usage
-----
    export NOTION_TOKEN=ntn_xxx        # or --token-file ~/.notion_token
    python3 notion_math_fixer.py <page-id-or-url>              # audit + plan (safe)
    python3 notion_math_fixer.py <page-id-or-url> --apply      # write + verify
    python3 notion_math_fixer.py <page-id-or-url> --apply --katex   # + KaTeX gate
    python3 notion_math_fixer.py --restore <journal.jsonl>     # undo a run

License: MIT
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from html.parser import HTMLParser

API = "https://api.notion.com/v1"
# 2025-09-03 is the version this tool was tested against.
# Notion documents 2026-03-11 as the latest; override with --notion-version.
DEFAULT_VERSION = "2025-09-03"

# Notion API limits (https://developers.notion.com/reference/request-limits)
MAX_RICH_TEXT = 100       # elements per rich_text array
MAX_EXPRESSION = 1000     # characters per equation expression
MAX_CHILDREN = 100        # blocks per append request

# ---------------------------------------------------------------- text helpers

# \[ .. \] (group 1) or \( .. \) (group 2). A delimiter preceded by a backslash is
# not one: `\\[2pt]` is a LaTeX line break, not an opening bracket.
MATH = re.compile(r"(?<!\\)\\\[(.*?)(?<!\\)\\\]|(?<!\\)\\\((.*?)(?<!\\)\\\)", re.S)
BR = re.compile(r"<br\s*/?>", re.I)
PLACEHOLDER = re.compile(r"(?:\.{2,}|…|\\[lc]?dots|\s)*")
# A brace right after these is a literal delimiter (\left\{ ... \right.), never an argument.
SIZING = r"(?:left|right|middle|[bB]igg?[lrm]?)"
ARG_BRACE = re.compile(r"(?:[_^]|\\(?!" + SIZING + r"(?![A-Za-z]))[A-Za-z]+)\s*\\\{")
TEXT_CMD = re.compile(r"\\(?:text(?:rm|tt|bf|it|sf|normal|up)?|math(?:rm|tt|sf|bf|it)|"
                      r"operatorname\*?|mbox|hbox)\s*\{")

RICH = ("paragraph", "heading_1", "heading_2", "heading_3", "quote",
        "bulleted_list_item", "numbered_list_item", "to_do", "callout", "toggle")
# Display math in these is nested under the block instead of placed after it.
NESTING = {"bulleted_list_item", "numbered_list_item", "to_do", "quote", "callout", "toggle"}
WRITABLE_MENTIONS = {"user", "page", "database", "date"}


def is_placeholder(expr: str) -> bool:
    """`\\[ ... \\]` in prose is documentation about math, not math."""
    return PLACEHOLDER.fullmatch(expr) is not None


def _unescape_underscores(expr: str) -> str:
    """`\\_` -> `_`, except inside \\text{..}-like arguments where `\\_` is correct."""
    out, i, depth, text_at = [], 0, 0, []
    while i < len(expr):
        m = TEXT_CMD.match(expr, i)
        if m:
            depth += 1
            text_at.append(depth)
            out.append(m.group(0))
            i = m.end()
            continue
        c = expr[i]
        if c == "\\" and i + 1 < len(expr):
            two = expr[i:i + 2]
            out.append("_" if two == "\\_" and not text_at else two)
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            if text_at and text_at[-1] == depth:
                text_at.pop()
            depth -= 1
        out.append(c)
        i += 1
    return "".join(out)


def normalize_math(expr: str) -> tuple[str, list[str]]:
    """Clean one LaTeX expression; return (expression, notes)."""
    notes: list[str] = []
    out = BR.sub("\n", expr)
    # Markdown escaping turns every group brace into \{ \}. If the expression has
    # *no* bare brace at all and an escaped one sits in argument position
    # (\boxed\{, x_\{), the escaping is an artifact. Set braces (\{x \mid ..\})
    # and \left\{ never sit in argument position, so they are left alone.
    if ("\\{" in out and out.count("\\{") == out.count("\\}")
            and not re.search(r"(?<!\\)\{", out) and ARG_BRACE.search(out)):
        out = out.replace("\\{", "{").replace("\\}", "}")
        notes.append("unescaped argument braces \\{ \\}")
    if "\\_" in out:
        fixed = _unescape_underscores(out)
        if fixed != out:
            out = fixed
            notes.append("unescaped \\_ outside \\text{}")
    out = re.sub(r"\n\s*\n", "\n", out).strip()
    return out, notes


def plain_of(block: dict) -> str:
    return "".join(x.get("plain_text", "") for x in (block[block["type"]].get("rich_text") or []))


def run_text(run: dict) -> str:
    return (run.get("text") or {}).get("content", run.get("plain_text", ""))


def text_run(content: str, link: dict | None = None, annotations: dict | None = None) -> dict:
    out = {"type": "text", "text": {"content": content}}
    if link and link.get("url"):
        out["text"]["link"] = {"url": link["url"]}
    if annotations:
        out["annotations"] = dict(annotations)
    return out


def equation_run(expression: str, annotations: dict | None = None) -> dict:
    out = {"type": "equation", "equation": {"expression": expression}}
    if annotations:
        out["annotations"] = dict(annotations, code=False)
    return out


def writable(run: dict) -> dict | None:
    """A rich_text object as read -> the shape the API accepts on write (None if it can't)."""
    ann = run.get("annotations")
    t = run.get("type")
    if t == "text":
        return text_run(run_text(run), (run.get("text") or {}).get("link"), ann)
    if t == "equation":
        out = {"type": "equation", "equation": {"expression": run["equation"]["expression"]}}
    elif t == "mention":
        m = run["mention"]
        mt = m.get("type")
        if mt not in WRITABLE_MENTIONS:
            return None
        val = {"id": m[mt]["id"]} if mt in ("user", "page", "database") else m[mt]
        out = {"type": "mention", "mention": {"type": mt, mt: val}}
    else:
        return None
    if ann:
        out["annotations"] = dict(ann)
    return out


def _is_plain(run: dict) -> bool:
    return run.get("type") == "text" and not (run.get("annotations") or {}).get("code")


class Unwritable(Exception):
    """A block needs changing but holds rich text the API cannot write back."""


def convert_runs(runs: list[dict], allow_display: bool = True):
    """Convert literal LaTeX inside a rich_text array.

    Returns (segments, changed, notes). `segments` alternates
    ("runs", [rich_text...]) and ("display", expression); it always starts with a
    "runs" segment (possibly empty). Consecutive plain text runs are scanned as one
    string, so `\\(` and `\\)` may sit in differently formatted runs. Code-formatted
    runs, mentions and existing equations are boundaries and are never altered.
    """
    items: list = []          # rich_text dicts, ("display", expr) or Unwritable marker
    notes: list[str] = []
    changed = False
    i = 0
    while i < len(runs):
        if not _is_plain(runs[i]):
            w = writable(runs[i])
            items.append(w if w is not None else Unwritable)
            i += 1
            continue
        j = i
        while j < len(runs) and _is_plain(runs[j]):
            j += 1
        group = runs[i:j]
        texts = [run_text(r) for r in group]
        starts = [sum(len(t) for t in texts[:k]) for k in range(len(texts) + 1)]
        concat = "".join(texts)

        def emit(a: int, b: int) -> None:
            nonlocal changed
            for k, r in enumerate(group):
                lo, hi = max(a, starts[k]), min(b, starts[k + 1])
                if lo >= hi:
                    continue
                piece = concat[lo:hi]
                fixed = BR.sub("\n", piece)
                changed |= fixed != piece
                if fixed:
                    items.append(text_run(fixed, (r.get("text") or {}).get("link"), r.get("annotations")))

        def owner(pos: int) -> dict:
            return next(r for k, r in enumerate(group) if starts[k] <= pos < starts[k + 1])

        pos = 0
        for m in MATH.finditer(concat):
            display = m.group(1) is not None
            expr, n = normalize_math(m.group(1) if display else m.group(2))
            if is_placeholder(expr):
                notes.append("kept placeholder span as text (prose about math)")
                continue
            emit(pos, m.start())
            if display and allow_display:
                items.append(("display", expr))
            else:
                items.append(equation_run(" ".join(expr.split()), owner(m.start()).get("annotations")))
            notes += n
            changed = True
            pos = m.end()
        emit(pos, len(concat))
        i = j

    if changed and Unwritable in items:
        raise Unwritable("holds a mention type the API cannot write back")
    segments: list = [("runs", [])]
    for it in items:
        if isinstance(it, tuple):
            segments.append(it)
            segments.append(("runs", []))
        else:
            segments[-1][1].append(it)
    if len(segments) > 1:                     # text around display math: trim the seams
        segments = [(k, _trim(v)) if k == "runs" else (k, v) for k, v in segments]
    return segments, changed, notes


def _trim(runs: list[dict]) -> list[dict]:
    runs = [dict(r, text=dict(r["text"])) if r["type"] == "text" else r for r in runs]
    if runs and runs[0]["type"] == "text":
        runs[0]["text"]["content"] = runs[0]["text"]["content"].lstrip()
    if runs and runs[-1]["type"] == "text":
        runs[-1]["text"]["content"] = runs[-1]["text"]["content"].rstrip()
    return [r for r in runs if r["type"] != "text" or r["text"]["content"]]


def preview(runs: list[dict], width: int = 70) -> str:
    out = []
    for r in runs:
        if r.get("type") == "equation":
            out.append("$" + r["equation"]["expression"] + "$")
        elif r.get("type") == "text":
            out.append(run_text(r))
        else:
            out.append(r.get("plain_text", "@mention"))
    s = " ".join("".join(out).split())
    return s if len(s) <= width else s[:width - 1] + "…"


# ------------------------------------------------------------------ HTML tables

class _TableParser(HTMLParser):
    FMT = {"strong": "bold", "b": "bold", "em": "italic", "i": "italic", "code": "code",
           "s": "strikethrough", "del": "strikethrough", "u": "underline"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[list[tuple[str, frozenset]]]] = []
        self.cell: list | None = None
        self.fmt: Counter = Counter()
        self.header = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table" and a.get("header-row") == "true":
            self.header = True
        elif tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            if not self.rows:
                self.rows.append([])
            if tag == "th" and len(self.rows) == 1:
                self.header = True
            self.cell = []
            self.rows[-1].append(self.cell)
        elif tag == "br" and self.cell is not None:
            self.cell.append(("\n", frozenset()))
        elif tag in self.FMT:
            self.fmt[self.FMT[tag]] += 1

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self.cell = None
        elif tag in self.FMT and self.fmt[self.FMT[tag]]:
            self.fmt[self.FMT[tag]] -= 1

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append((data, frozenset(k for k, v in self.fmt.items() if v)))


def parse_html_table(source: str):
    """`<table>...</table>` text -> (rows of rich_text cells, has_column_header) or None."""
    p = _TableParser()
    try:
        p.feed(source)
        p.close()
    except Exception:
        return None
    rows = [r for r in p.rows if r]
    if not rows:
        return None
    width = max(len(r) for r in rows)
    out = []
    for r in rows:
        cells = []
        for cell in r + [[]] * (width - len(r)):
            runs = []
            for text, fmt in cell:
                ann = {k: True for k in fmt}
                runs.append({"type": "text", "text": {"content": text}, "plain_text": text,
                             "annotations": ann})
            segs, _, _ = convert_runs(runs, allow_display=False)
            cells.append(_trim(segs[0][1]))
        out.append(cells)
    return out, p.header


# ------------------------------------------------------------------ the client

class NotionError(RuntimeError):
    pass


class Notion:
    def __init__(self, token: str, version: str = DEFAULT_VERSION):
        self.token = token
        self.version = version

    def call(self, path: str, method: str = "GET", body: dict | None = None,
             tries: int = 4, idempotent: bool = True):
        """One API call with retries. Non-idempotent calls (appending children) are
        only retried on 429, which Notion guarantees was not processed."""
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(tries):
            req = urllib.request.Request(
                f"{API}/{path}", data=data, method=method,
                headers={"Authorization": f"Bearer {self.token}",
                         "Notion-Version": self.version,
                         "Content-Type": "application/json"})
            last = attempt == tries - 1
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                payload = e.read(2000).decode("utf-8", "ignore")
                if not last and (e.code == 429 or (idempotent and e.code in (500, 502, 503, 504, 529))):
                    wait = e.headers.get("Retry-After") if e.headers else None
                    try:
                        time.sleep(float(wait) if wait else 2 ** attempt)
                    except ValueError:
                        time.sleep(2 ** attempt)
                    continue
                return {"error": e.code, "body": payload}
            except (urllib.error.URLError, TimeoutError, socket.timeout) as e:  # network hiccup
                if idempotent and not last:
                    time.sleep(2 ** attempt)
                    continue
                return {"error": "network", "body": str(e)}
        return {"error": "exhausted"}

    @staticmethod
    def _ok(res: dict, what: str) -> dict:
        if "error" in res:
            raise NotionError(f"{what}: {res['error']} {res.get('body', '')}".strip())
        return res

    def page(self, page_id: str) -> dict:
        return self._ok(self.call(f"pages/{page_id}"), f"cannot read page {page_id}")

    def children(self, block_id: str) -> list[dict]:
        out, cursor = [], None
        while True:
            q = f"blocks/{block_id}/children?page_size=100"
            if cursor:
                q += f"&start_cursor={cursor}"
            page = self._ok(self.call(q), f"cannot read children of {block_id}")
            out += page["results"]
            if not page.get("has_more"):
                return out
            cursor = page["next_cursor"]

    def update_block(self, block_id: str, payload: dict) -> dict:
        return self._ok(self.call(f"blocks/{block_id}", "PATCH", payload), f"update {block_id}")

    def insert(self, parent_id: str, blocks: list[dict], after_id: str | None = None) -> list[str]:
        """Insert blocks after `after_id`, or as the first children when it is None."""
        pos = {"type": "after_block", "after_block": {"id": after_id}} if after_id else {"type": "start"}
        res = self.call(f"blocks/{parent_id}/children", "PATCH",
                        {"children": blocks, "position": pos}, idempotent=False)
        if res.get("error") == 400 and "position" in res.get("body", "") and after_id:
            res = self.call(f"blocks/{parent_id}/children", "PATCH",       # pre-`position` API
                            {"children": blocks, "after": after_id}, idempotent=False)
        res = self._ok(res, f"insert into {parent_id}")
        return [r["id"] for r in res.get("results", [])][:len(blocks)]

    def trash(self, block_id: str) -> None:
        self._ok(self.call(f"blocks/{block_id}", "DELETE"), f"trash {block_id}")


def fetch_tree(notion, block_id: str) -> list[dict]:
    """Children of `block_id`, each with a '_children' list when it has children.

    Child pages/databases are separate documents and are not descended into; a
    synced block that mirrors another page is not ours to edit.
    """
    blocks = notion.children(block_id)
    for b in blocks:
        if not b.get("has_children") or b["type"] in ("child_page", "child_database"):
            continue
        if b["type"] == "synced_block" and (b.get("synced_block") or {}).get("synced_from"):
            continue
        b["_children"] = fetch_tree(notion, b["id"])
    return blocks


def walk(blocks: list[dict]):
    for b in blocks:
        yield b
        yield from walk(b.get("_children", []))


# --------------------------------------------------------------------- planning
#
# An op is a JSON-able dict, journaled as it is executed:
#   {"op": "update", "block_id", "btype", "old": rich_text, "new": rich_text}
#   {"op": "cells",  "block_id", "old": cells, "new": cells}
#   {"op": "insert", "parent_id", "after_id" (None = first child), "blocks"}
#   {"op": "trash",  "block_id", "parent_id", "block": a copy to re-create on --restore}
#
# Restore re-creates trashed blocks rather than un-trashing them: Notion puts an
# un-trashed block back at the *end* of its parent, and there is no "move block".

def _seg_blocks(segments: list) -> list[dict]:
    out = []
    for kind, val in segments:
        if kind == "display":
            out.append({"type": "equation", "equation": {"expression": val}})
        elif val:
            out.append({"type": "paragraph", "paragraph": {"rich_text": val}})
    return out


def _limits(rich_texts: list[list[dict]], expressions: list[str]) -> str | None:
    if any(len(rt) > MAX_RICH_TEXT for rt in rich_texts):
        return f"more than {MAX_RICH_TEXT} rich text segments"
    if any(len(e) > MAX_EXPRESSION for e in expressions):
        return f"an expression longer than {MAX_EXPRESSION} characters"
    return None


def _run_exprs(runs: list[dict]) -> list[str]:
    return [r["equation"]["expression"] for r in runs if r.get("type") == "equation"]


def _trash(b: dict, parent_id: str, desc: str) -> dict:
    t = b["type"]
    body = {k: v for k, v in b[t].items() if k in ("color", "is_toggleable")}
    body["rich_text"] = [writable(r) for r in b[t].get("rich_text") or []]
    return {"op": "trash", "block_id": b["id"], "parent_id": parent_id,
            "block": {"type": t, t: body}, "desc": desc}


def _table_source(blocks: list[dict], i: int):
    """Paragraph(s) holding literal `<table>..</table>` HTML starting at blocks[i].

    Returns (count, table_block) or None."""
    if blocks[i]["type"] != "paragraph" or not plain_of(blocks[i]).lstrip().lower().startswith("<table"):
        return None
    parts = []
    for k in range(i, len(blocks)):
        b = blocks[k]
        if b["type"] != "paragraph" or b.get("_children"):
            return None
        parts.append(plain_of(b))
        if "</table>" in parts[-1].lower():
            parsed = parse_html_table("\n".join(parts))
            if not parsed:
                return None
            rows, header = parsed
            table = {"type": "table", "table": {
                "table_width": len(rows[0]), "has_column_header": header, "has_row_header": False,
                "children": [{"type": "table_row", "table_row": {"cells": c}} for c in rows]}}
            return k - i + 1, table
    return None


def plan(blocks: list[dict], parent_id: str) -> tuple[list[dict], list[str]]:
    """Block tree -> (ops, notes). Pure: reads nothing, writes nothing."""
    ops: list[dict] = []
    notes: list[str] = []
    _plan_level(blocks, parent_id, ops, notes)
    return ops, notes


def _plan_level(blocks: list[dict], parent_id: str, ops: list[dict], notes: list[str]) -> None:
    i = 0
    while i < len(blocks):
        b = blocks[i]
        src = _table_source(blocks, i)
        if src:
            count, table = src
            rows = table["table"]["children"]
            exprs = [e for r in rows for c in r["table_row"]["cells"] for e in _run_exprs(c)]
            why = (_limits([c for r in rows for c in r["table_row"]["cells"]], exprs)
                   or (f"more than {MAX_CHILDREN} rows" if len(rows) > MAX_CHILDREN else None))
            if why:
                notes.append(f"skipped HTML table at {b['id']}: {why}")
            else:
                ops.append({"op": "insert", "parent_id": parent_id, "after_id": blocks[i + count - 1]["id"],
                            "blocks": [table], "desc": f"native table ({len(rows)} rows) from HTML"})
                ops += [_trash(s, parent_id, "HTML table source paragraph") for s in blocks[i:i + count]]
            i += count
            continue
        _plan_block(b, parent_id, ops, notes)
        if b["type"] == "table":
            _plan_table(b, ops, notes)
        elif b.get("_children"):
            _plan_level(b["_children"], b["id"], ops, notes)
        i += 1


def _plan_block(b: dict, parent_id: str, ops: list[dict], notes: list[str]) -> None:
    t = b["type"]
    if t == "synced_block" and (b.get("synced_block") or {}).get("synced_from"):
        notes.append(f"skipped synced_block {b['id']}: mirrors another page")
        return
    if t not in RICH:
        return
    runs = b[t].get("rich_text") or []
    try:
        segments, changed, n = convert_runs(runs)
    except Unwritable as e:
        notes.append(f"skipped {t} {b['id']}: {e}")
        return
    notes += n
    if not changed:
        return
    first, rest = segments[0][1], _seg_blocks(segments[1:])
    exprs = _run_exprs(first) + [e for blk in rest for e in
                                 ([blk["equation"]["expression"]] if blk["type"] == "equation"
                                  else _run_exprs(blk["paragraph"]["rich_text"]))]
    why = _limits([first] + [blk["paragraph"]["rich_text"] for blk in rest if blk["type"] == "paragraph"],
                  exprs) or (f"more than {MAX_CHILDREN} new blocks" if len(rest) > MAX_CHILDREN else None)
    if why:
        notes.append(f"skipped {t} {b['id']}: {why}")
        return
    old = [writable(r) for r in runs]
    update = {"op": "update", "block_id": b["id"], "btype": t, "old": old, "new": first,
              "desc": f"{t}: {preview(old)!r} -> {preview(first)!r}"}
    if not rest:
        ops.append(update)
        return
    kinds = ", ".join(blk["type"] for blk in rest)
    if t in NESTING or b.get("has_children"):
        ops.append(update)
        ops.append({"op": "insert", "parent_id": b["id"], "after_id": None, "blocks": rest,
                    "desc": f"nested under {t}: {kinds}"})
    elif first:
        ops.append(update)
        ops.append({"op": "insert", "parent_id": parent_id, "after_id": b["id"], "blocks": rest,
                    "desc": f"after {t}: {kinds}"})
    else:
        ops.append({"op": "insert", "parent_id": parent_id, "after_id": b["id"], "blocks": rest,
                    "desc": f"replacing {t}: {kinds}"})
        ops.append(_trash(b, parent_id, f"{t} now empty: {preview(old)!r}"))


def _plan_table(table: dict, ops: list[dict], notes: list[str]) -> None:
    for row in table.get("_children", []):
        if row["type"] != "table_row":
            continue
        cells = row["table_row"]["cells"]
        new, any_change = [], False
        try:
            for cell in cells:
                segs, changed, n = convert_runs(cell, allow_display=False)
                notes += n
                any_change |= changed
                new.append(segs[0][1] if changed else [writable(r) for r in cell])
        except Unwritable as e:
            notes.append(f"skipped table_row {row['id']}: {e}")
            continue
        if not any_change:
            continue
        why = _limits(new, [e for c in new for e in _run_exprs(c)])
        if why:
            notes.append(f"skipped table_row {row['id']}: {why}")
            continue
        old = [[writable(r) for r in c] for c in cells]
        ops.append({"op": "cells", "block_id": row["id"], "old": old, "new": new,
                    "desc": "table_row: " + " | ".join(preview(c, 24) for c in new)})


def planned_expressions(ops: list[dict]) -> list[tuple[str, bool, str]]:
    """Every expression the ops will write: (expression, display?, block id)."""
    out = []
    for op in ops:
        if op["op"] == "update":
            out += [(e, False, op["block_id"]) for e in _run_exprs(op["new"])]
        elif op["op"] == "cells":
            out += [(e, False, op["block_id"]) for c in op["new"] for e in _run_exprs(c)]
        elif op["op"] == "insert":
            where = op["after_id"] or op["parent_id"]
            for blk in op["blocks"]:
                if blk["type"] == "equation":
                    out.append((blk["equation"]["expression"], True, where))
                elif blk["type"] == "paragraph":
                    out += [(e, False, where) for e in _run_exprs(blk["paragraph"]["rich_text"])]
                elif blk["type"] == "table":
                    out += [(e, False, where) for r in blk["table"]["children"]
                            for c in r["table_row"]["cells"] for e in _run_exprs(c)]
    return out


# ------------------------------------------------------------ apply / restore

def apply_ops(notion, ops: list[dict], journal_path: str) -> None:
    """Execute ops in order, appending each completed one to the journal.

    Raises NotionError on the first failure; the journal then holds exactly the
    steps that took effect, which is what --restore needs."""
    with open(journal_path, "a", encoding="utf-8") as j:
        for op in ops:
            rec = dict(op)
            if op["op"] == "update":
                notion.update_block(op["block_id"], {op["btype"]: {"rich_text": op["new"]}})
            elif op["op"] == "cells":
                notion.update_block(op["block_id"], {"table_row": {"cells": op["new"]}})
            elif op["op"] == "insert":
                for k in range(0, len(op["blocks"]), MAX_CHILDREN):
                    batch = op["blocks"][k:k + MAX_CHILDREN]
                    after = op["after_id"] if k == 0 else rec["created_ids"][-1]
                    ids = notion.insert(op["parent_id"], batch, after)
                    rec["created_ids"] = rec.get("created_ids", []) + ids
            elif op["op"] == "trash":
                live = [b["id"] for b in notion.children(op["parent_id"])]
                at = live.index(op["block_id"])
                rec["prev_id"] = live[at - 1] if at else None    # where --restore puts it back
                notion.trash(op["block_id"])
            j.write(json.dumps(rec, ensure_ascii=False) + "\n")
            j.flush()


def restore(notion, journal_path: str) -> bool:
    """Undo a journaled run, last step first.

    Trashed blocks come back as fresh copies in their original position (new block
    ids); every other block keeps its id."""
    with open(journal_path, encoding="utf-8") as f:
        recs = [json.loads(line) for line in f if line.strip()]
    failures = 0
    for rec in reversed(recs):
        try:
            if rec["op"] == "update":
                notion.update_block(rec["block_id"], {rec["btype"]: {"rich_text": rec["old"]}})
            elif rec["op"] == "cells":
                notion.update_block(rec["block_id"], {"table_row": {"cells": rec["old"]}})
            elif rec["op"] == "insert":
                for bid in rec.get("created_ids", []):
                    notion.trash(bid)
            elif rec["op"] == "trash":
                notion.insert(rec["parent_id"], [rec["block"]], rec["prev_id"])
            else:
                continue
            print(f"  undone: {rec['op']:6} {rec.get('desc', '')}")
        except NotionError as e:
            failures += 1
            print(f"  FAILED: {rec['op']:6} {e}")
    print(f"restore: {len([r for r in recs if r['op'] != 'meta']) - failures} step(s) undone, {failures} failed")
    return failures == 0


# ------------------------------------------------------------------ verification

TOKEN = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]+")


def _texts(b: dict) -> list[list[dict]]:
    t = b["type"]
    if t == "table_row":
        return b[t]["cells"]
    return [b[t].get("rich_text") or []] if isinstance(b.get(t), dict) and "rich_text" in b[t] else []


def all_equations(blocks: list[dict]) -> list[str]:
    out = []
    for b in walk(blocks):
        if b["type"] == "equation":
            out.append(b["equation"]["expression"])
        for runs in _texts(b):
            out += _run_exprs(runs)
    return out


def text_tokens(blocks: list[dict]) -> Counter:
    """Words of the page's prose with math removed -- must survive a run unchanged."""
    def strip_math(m):
        body = m.group(1) if m.group(1) is not None else m.group(2)
        return m.group(0) if is_placeholder(normalize_math(body)[0]) else " "
    c: Counter = Counter()
    for b in walk(blocks):
        for runs in _texts(b):
            s = "".join(run_text(r) if r.get("type") == "text" else " " for r in runs)
            s = BR.sub(" ", MATH.sub(strip_math, s))
            s = re.sub(r"<[^>]*>", " ", html.unescape(s))
            c.update(TOKEN.findall(s))
    return c


def squash(expr: str) -> str:
    return " ".join(expr.split())


def verify(notion, page_id: str, ops: list[dict], before: list[dict]) -> bool:
    after = fetch_tree(notion, page_id)
    left, _ = plan(after, page_id)
    want = Counter(squash(e) for e, _, _ in planned_expressions(ops))
    got = Counter(squash(e) for e in all_equations(after)) - Counter(squash(e) for e in all_equations(before))
    missing, extra = want - got, got - want
    tb, ta = text_tokens(before), text_tokens(after)
    drift = (tb - ta) + (ta - tb)

    print("\n--- verification ---")
    print(f"  blocks              : {sum(1 for _ in walk(after))}  {dict(Counter(b['type'] for b in walk(after)))}")
    print(f"  still convertible   : {len(left)}   (must be 0)")
    print(f"  equations written   : {sum(got.values())}   (planned {sum(want.values())})")
    for e in list(missing)[:5]:
        print(f"      missing: {e[:80]!r}")
    for e in list(extra)[:5]:
        print(f"      unexpected: {e[:80]!r}")
    print(f"  text drift tokens   : {sum(drift.values())}   {dict(list(drift.items())[:8]) if drift else ''}")
    ok = not left and not missing and not extra and not drift
    print(f"  RESULT              : {'PASS' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------------- KaTeX

def katex_check(items: list[tuple[str, bool, str]], workdir: str) -> bool:
    """Render every planned expression with KaTeX (throwOnError). False on any failure."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "verify_katex.js")
    payload = os.path.join(workdir, "expressions.json")
    with open(payload, "w", encoding="utf-8") as f:
        json.dump({"expressions": [{"expr": e, "display": d} for e, d, _ in items]}, f, ensure_ascii=False)
    try:
        out = subprocess.run(["node", script, "--json", payload], capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        print("  KaTeX: node is not installed -- cannot run the --katex gate")
        return False
    if out.returncode not in (0, 1):
        print("  KaTeX: " + (out.stderr.strip() or out.stdout.strip())[-400:])
        return False
    res = json.loads(out.stdout)
    print(f"  KaTeX: {len(items) - len(res['failures'])}/{len(items)} expressions render cleanly")
    for f in res["failures"]:
        e, _, where = items[f["index"]]
        print(f"      block {where}: {e[:60]!r}\n        {f['error'][:140]}")
    return res["ok"]


# ------------------------------------------------------------------------ main

HEX32 = re.compile(r"([0-9a-fA-F]{32})$")
UUID = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")


def page_id_from(arg: str) -> str:
    """Page id from a bare id or a URL. The id is the tail of the last path segment;
    the title in front of it (`Lecture-12-<id>`) may itself end in hex characters."""
    last = arg.strip().split("#")[0].split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    m = UUID.search(last) or HEX32.search(last)
    if not m:
        sys.exit(f"cannot find a Notion page id in {arg!r}")
    raw = m.group(1).replace("-", "").lower()
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def read_token(args) -> str:
    if args.token:
        return args.token
    if os.environ.get("NOTION_TOKEN"):
        return os.environ["NOTION_TOKEN"].strip()
    path = os.path.expanduser(args.token_file)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    sys.exit("no Notion token: set NOTION_TOKEN, or --token-file, or --token")


def audit(blocks: list[dict]) -> None:
    every = list(walk(blocks))
    literal = [b for b in every if b["type"] in RICH and ("\\[" in plain_of(b) or "\\(" in plain_of(b))]
    html_tags = [b for b in every if b["type"] in RICH
                 and re.search(r"<(table|br|td|tr|div|ul|li|details|summary)\b", plain_of(b), re.I)]
    print("\n--- audit ---")
    print(f"  blocks (all levels) : {len(every)}  {dict(Counter(b['type'] for b in every))}")
    print(f"  w/ literal TeX      : {len(literal)}")
    for b in literal[:6]:
        print(f"      {b['type']:<20} {plain_of(b)[:60]!r}")
    print(f"  w/ HTML tags        : {len(html_tags)}")
    print(f"  existing equations  : {len(all_equations(blocks))}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Convert LLM/HTML LaTeX in a Notion page into native equations.")
    ap.add_argument("page", nargs="?", help="Notion page id or URL")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--katex", action="store_true",
                    help="refuse to write unless every new expression renders in KaTeX (needs node + katex)")
    ap.add_argument("--restore", metavar="JOURNAL", help="undo a run from its .journal.jsonl")
    ap.add_argument("--token", help="Notion integration token")
    ap.add_argument("--token-file", default="~/.notion_token", help="file holding the token")
    ap.add_argument("--notion-version", default=DEFAULT_VERSION)
    ap.add_argument("--backup-dir", default="~/.cache/notion-math-fixer")
    args = ap.parse_args()
    if not args.page and not args.restore:
        ap.error("give a page id/URL, or --restore JOURNAL")

    notion = Notion(read_token(args), args.notion_version)
    if args.restore:
        return 0 if restore(notion, args.restore) else 1
    return fix_page(notion, page_id_from(args.page), apply=args.apply, katex=args.katex,
                    backup_dir=os.path.expanduser(args.backup_dir), version=args.notion_version)


def fix_page(notion, page: str, *, apply: bool, katex: bool, backup_dir: str,
             version: str = DEFAULT_VERSION) -> int:
    """Audit, plan, and (with apply) write + verify one page. Returns the exit code."""
    meta = notion.page(page)
    title = ""
    for v in (meta.get("properties") or {}).values():
        if v.get("type") == "title":
            title = "".join(x.get("plain_text", "") for x in v["title"])
    print(f"page   : {title!r}  {page}")
    print(f"edited : {meta.get('last_edited_time')}")

    blocks = fetch_tree(notion, page)
    audit(blocks)

    ops, notes = plan(blocks, page)
    touched = {op.get("block_id") or op.get("after_id") or op.get("parent_id") for op in ops}
    print("\n--- plan ---")
    print(f"  {len(ops)} operation(s) on {len(touched)} block(s); every other block is left untouched")
    for op in ops:
        print(f"    {op['op']:6} {op['desc']}")
    if notes:
        print(f"  notes: {dict(Counter(notes))}")

    os.makedirs(backup_dir, exist_ok=True)
    stem = os.path.join(backup_dir, f"{page}_{time.strftime('%Y%m%d-%H%M%S')}")
    with open(stem + ".tree.json", "w", encoding="utf-8") as f:
        json.dump(blocks, f, ensure_ascii=False)
    with open(stem + ".plan.json", "w", encoding="utf-8") as f:
        json.dump(ops, f, ensure_ascii=False, indent=1)
    print(f"  backup: {stem}.{{tree.json,plan.json}}")

    if katex and ops and not katex_check(planned_expressions(ops), backup_dir):
        print("\nrefusing to write: KaTeX rejected expression(s) above.")
        return 2
    if not apply:
        print("\ndry run -- nothing written. Re-run with --apply to write.")
        return 0
    if not ops:
        print("\nnothing to convert.")
        return 0

    now = notion.page(page).get("last_edited_time")
    if now != meta.get("last_edited_time"):
        print(f"\nrefusing to write: the page was edited while planning ({now}). Re-run.")
        return 3

    journal = stem + ".journal.jsonl"
    with open(journal, "w", encoding="utf-8") as f:
        f.write(json.dumps({"op": "meta", "page": page, "version": version}) + "\n")
    try:
        apply_ops(notion, ops, journal)
    except NotionError as e:
        print(f"\nwrite failed: {e}")
        print(f"partial run journaled -- undo with:\n  python3 {sys.argv[0]} --restore {journal}")
        return 1
    print(f"\nwrote {len(ops)} operation(s); journal: {journal}")
    print(f"undo with: python3 {sys.argv[0]} --restore {journal}")
    return 0 if verify(notion, page, ops, blocks) else 1


if __name__ == "__main__":
    sys.exit(main())
