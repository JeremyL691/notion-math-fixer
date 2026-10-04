"""Fixture builders and an in-memory stand-in for the Notion API."""

from __future__ import annotations

import copy
import itertools
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import notion_math_fixer as nmf  # noqa: E402

DEFAULT_ANN = {"bold": False, "italic": False, "strikethrough": False,
               "underline": False, "code": False, "color": "default"}
PAGE = "11111111-2222-3333-4444-555555555555"


def T(content: str, link: str | None = None, **ann) -> dict:
    """A text run in the shape the API returns."""
    return {"type": "text", "text": {"content": content, "link": {"url": link} if link else None},
            "annotations": dict(DEFAULT_ANN, **ann), "plain_text": content, "href": link}


def B(btype: str, *runs, children: list | None = None, id: str | None = None, **extra) -> dict:
    """A block in read shape; `children` become `_children`."""
    b = {"type": btype, btype: {"rich_text": list(runs), **extra}}
    if btype in ("divider", "table"):
        b[btype] = dict(extra)
    if children:
        b["_children"] = children
    if id:
        b["id"] = id
    return b


def row(*cells) -> dict:
    return {"type": "table_row", "table_row": {"cells": [list(c) for c in cells]}}


def read_run(run: dict) -> dict:
    """A run as written -> as the API would return it."""
    if "plain_text" in run:
        return copy.deepcopy(run)
    ann = dict(DEFAULT_ANN, **(run.get("annotations") or {}))
    t = run["type"]
    if t == "text":
        link = run["text"].get("link")
        return {"type": "text", "text": {"content": run["text"]["content"], "link": link},
                "annotations": ann, "plain_text": run["text"]["content"],
                "href": link["url"] if link else None}
    if t == "equation":
        return {"type": "equation", "equation": dict(run["equation"]), "annotations": ann,
                "plain_text": run["equation"]["expression"], "href": None}
    return dict(copy.deepcopy(run), annotations=ann, plain_text="@mention", href=None)


class FakeNotion:
    """Just enough of the Notion block API for fetch_tree / apply_ops / restore / fix_page.

    Like Notion, a trashed block cannot be moved back: restore must re-create it."""

    def __init__(self, tree: list[dict], page_id: str = PAGE):
        self.page_id = page_id
        self.store: dict[str, dict] = {}
        self.kids: dict[str, list[str]] = {page_id: []}
        self.ids = (f"00000000-0000-4000-8000-{n:012d}" for n in itertools.count(1))
        self.written: list[str] = []          # block ids touched by any write
        self.fail_at: int | None = None       # raise on the n-th write (1-based)
        self.edited = ["2026-10-03T00:00:00.000Z"]
        self._add(page_id, tree, None)

    # -- construction
    def _add(self, parent: str, blocks: list[dict], after: str | None, start: bool = False) -> list[str]:
        made = []
        sib = self.kids[parent]
        at = 0 if start else (sib.index(after) + 1 if after else len(sib))
        for b in blocks:
            b = copy.deepcopy(b)
            bid = b.pop("id", None) or next(self.ids)
            t = b["type"]
            payload = b[t]
            kids = b.pop("_children", None) or payload.pop("children", None) or []
            if "rich_text" in payload:
                payload["rich_text"] = [read_run(r) for r in payload["rich_text"]]
            if t == "table_row":
                payload["cells"] = [[read_run(r) for r in c] for c in payload["cells"]]
            self.store[bid] = {"object": "block", "id": bid, "type": t, t: payload,
                               "in_trash": False, "parent": parent}
            self.kids[bid] = []
            sib.insert(at, bid)
            at += 1
            self._add(bid, kids, None)
            made.append(bid)
        return made

    def _write(self, bid: str) -> None:
        self.written.append(bid)
        if self.fail_at is not None and len(self.written) == self.fail_at:
            raise nmf.NotionError(f"injected failure at write {self.fail_at}")

    # -- the API surface the tool uses
    def page(self, page_id: str) -> dict:
        stamp = self.edited[0] if len(self.edited) == 1 else self.edited.pop(0)
        return {"last_edited_time": stamp, "properties": {}}

    def children(self, block_id: str) -> list[dict]:
        out = []
        for bid in self.kids[block_id]:
            b = self.store[bid]
            if b["in_trash"]:
                continue
            b = copy.deepcopy(b)
            b["has_children"] = any(not self.store[k]["in_trash"] for k in self.kids[bid])
            out.append(b)
        return out

    def update_block(self, block_id: str, payload: dict) -> dict:
        self._write(block_id)
        b = self.store[block_id]
        for key, val in payload.items():
            if key in ("in_trash", "archived"):
                b["in_trash"] = bool(val)
            elif key == "table_row":
                b[key]["cells"] = [[read_run(r) for r in c] for c in val["cells"]]
            else:
                b[key]["rich_text"] = [read_run(r) for r in val["rich_text"]]
        return b

    def insert(self, parent_id: str, blocks: list[dict], after_id: str | None = None) -> list[str]:
        self._write(parent_id)
        return self._add(parent_id, blocks, after_id, start=after_id is None)

    def trash(self, block_id: str) -> None:
        self._write(block_id)
        self.store[block_id]["in_trash"] = True

    # -- inspection
    def tree(self) -> list[dict]:
        return nmf.fetch_tree(self, self.page_id)

    def snapshot(self, parent: str | None = None) -> list:
        """Comparable view of the live tree: types, content, nesting (not ids)."""
        out = []
        for b in self.children(parent or self.page_id):
            t = b["type"]
            body = b[t]
            if "rich_text" in body:
                content = [nmf.writable(r) for r in body["rich_text"]]
            elif t == "table_row":
                content = [[nmf.writable(r) for r in c] for c in body["cells"]]
            else:
                content = body
            out.append((t, content, self.snapshot(b["id"])))
        return out
