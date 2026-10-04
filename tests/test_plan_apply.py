"""Planner + executor + restore + verify, end to end against an in-memory Notion."""

import contextlib
import io
import os
import tempfile
import unittest

from helpers import PAGE, B, FakeNotion, T, nmf, row

HTML_TABLE = """<table header-row="true">
<tr><td>Operation</td><td>Symbol</td></tr>
<tr><td>Projection</td><td>\\(\\pi_A(R)\\)</td></tr>
<tr><td>Difference</td><td>\\(R-S\\)</td></tr>
</table>"""


def sample() -> list[dict]:
    """examples/llm_note_sample.md as it lands in Notion, plus the shapes the old tool lost."""
    return [
        B("heading_2", T("Display math"), id="h-display"),
        B("paragraph", T("The six primitives of the course:\n\\[<br>\\boxed{<br>\\sigma,\\ \\pi,\\ \\rho<br>}<br>\\]"), id="p-lead"),
        B("paragraph", T(r"Recall: \(\sigma\) filters rows, \(\pi\) picks columns."), id="p-inline"),
        B("paragraph", T(r"\[<br>\boxed\{<br>\pi_A(\sigma_C(R))<br>\}<br>\]"), id="p-only-math"),
        B("paragraph", T(HTML_TABLE), id="p-html"),
        B("paragraph", T(r"Use \[ ... \] for display math and \( ... \) for inline math."), id="p-prose"),
        B("code", T("SELECT \\(x\\) FROM t;"), language="sql", id="code"),
        B("quote", T("Only one"), id="quote", children=[B("paragraph", T(r"no other \(x\)"), id="quote-child")]),
        B("paragraph", T("see "), T("docs", link="https://x.y"), T(r" for \(a+b\)"), id="p-link"),
        B("bulleted_list_item", T(r"result:\[ a+b \]"), id="li",
          children=[B("paragraph", T("existing child"), id="li-child")]),
        B("callout", T(r"note \(E=mc^2\)"), id="callout"),
        B("paragraph", T("parent"), id="p-parent",
          children=[B("paragraph", T(r"indented \(y\)"), id="p-indented")]),
        B("table", id="tbl", table_width=2, has_column_header=False, has_row_header=False,
          children=[row([T("v")], [T(r"\(|v|\)")]), row([T("w")], [T("plain")])]),
        B("paragraph", T("costs $5 and $10"), id="p-dollars"),
        B("paragraph", T(r"code: "), T(r"\(x\)", code=True), id="p-code-run"),
        B("paragraph", T("Finally:\\[<br>R\\bowtie_C S = \\sigma_C(R\\times S)<br>\\]"), id="p-last"),
    ]


UNTOUCHED = {"h-display", "p-prose", "code", "quote", "p-parent", "li-child", "p-dollars", "p-code-run", "tbl"}


def quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        res = fn(*a, **kw)
    return res, out.getvalue()


class PlanApply(unittest.TestCase):
    def setUp(self):
        self.notion = FakeNotion(sample())
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = os.path.join(self.tmp.name, "run.journal.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def run_all(self):
        before = self.notion.tree()
        ops, _ = nmf.plan(before, PAGE)
        nmf.apply_ops(self.notion, ops, self.journal)
        return before, ops

    def by_id(self, tree):
        return {b["id"]: b for b in nmf.walk(tree)}

    def test_converts_everything_and_is_idempotent(self):
        before, ops = self.run_all()
        after = self.notion.tree()
        self.assertEqual(nmf.plan(after, PAGE)[0], [], "a second run must find nothing to do")
        ok, out = quiet(nmf.verify, self.notion, PAGE, ops, before)
        self.assertTrue(ok, out)

    def test_untouched_blocks_are_never_written(self):
        self.run_all()
        self.assertFalse(UNTOUCHED & set(self.notion.written), self.notion.written)

    def test_page_shape(self):
        self.run_all()
        top = self.notion.children(PAGE)
        order = [(b["id"] if not b["id"].startswith("0000") else b["type"]) for b in top]
        self.assertEqual(order, [
            "h-display", "p-lead", "equation", "p-inline", "equation", "table", "p-prose", "code",
            "quote", "p-link", "li", "callout", "p-parent", "tbl", "p-dollars", "p-code-run", "p-last", "equation"])
        exprs = [b["equation"]["expression"] for b in top if b["type"] == "equation"]
        self.assertEqual(exprs, ["\\boxed{\n\\sigma,\\ \\pi,\\ \\rho\n}", "\\boxed{\n\\pi_A(\\sigma_C(R))\n}",
                                 "R\\bowtie_C S = \\sigma_C(R\\times S)"])

    def test_details(self):
        self.run_all()
        tree = self.by_id(self.notion.tree())
        link = tree["p-link"]["paragraph"]["rich_text"]
        self.assertEqual(link[1]["href"], "https://x.y")
        self.assertEqual(link[-1]["equation"]["expression"], "a+b")
        # display math in a list item nests under it, ahead of existing children
        li_kids = [b["type"] if b["id"] != "li-child" else "li-child" for b in tree["li"]["_children"]]
        self.assertEqual(li_kids, ["equation", "li-child"])
        self.assertEqual(nmf.plain_of(tree["li"]), "result:")
        # nested children the old tool dropped are converted in place
        self.assertEqual(tree["p-indented"]["paragraph"]["rich_text"][-1]["type"], "equation")
        self.assertEqual(tree["quote-child"]["paragraph"]["rich_text"][-1]["type"], "equation")
        # native table cells, including |v|
        cell = tree["tbl"]["_children"][0]["table_row"]["cells"][1]
        self.assertEqual(cell[0]["equation"]["expression"], "|v|")
        # HTML table became a native table with a header row
        table = next(b for b in tree.values() if b["type"] == "table" and b["id"] != "tbl")
        self.assertTrue(table["table"]["has_column_header"])
        self.assertEqual(len(table["_children"]), 3)
        self.assertNotIn("p-html", tree)
        self.assertNotIn("p-only-math", tree)
        # literal text stays literal
        self.assertEqual(nmf.plain_of(tree["p-dollars"]), "costs $5 and $10")
        self.assertEqual(nmf.plain_of(tree["code"]), "SELECT \\(x\\) FROM t;")

    def test_restore_round_trip(self):
        original = self.notion.snapshot()
        self.run_all()
        self.assertNotEqual(self.notion.snapshot(), original)
        ok, _ = quiet(nmf.restore, self.notion, self.journal)
        self.assertTrue(ok)
        self.assertEqual(self.notion.snapshot(), original)
        # only the trashed blocks come back under new ids
        live = {b["id"] for b in nmf.walk(self.notion.tree())}
        self.assertEqual({"p-lead", "p-inline", "li", "callout", "p-indented", "tbl"} - live, set())
        self.assertEqual({"p-only-math", "p-html"} & live, set())

    def test_restore_consecutive_trashed_blocks(self):
        lines = HTML_TABLE.split("\n")
        notion = FakeNotion([B("paragraph", T("before"))]
                            + [B("paragraph", T(r"\[ x_%d \]" % i)) for i in range(3)]
                            + [B("paragraph", T(line)) for line in lines] + [B("paragraph", T("after"))])
        original = notion.snapshot()
        ops, _ = nmf.plan(notion.tree(), PAGE)
        nmf.apply_ops(notion, ops, self.journal)
        self.assertEqual([b["type"] for b in notion.children(PAGE)],
                         ["paragraph", "equation", "equation", "equation", "table", "paragraph"])
        ok, _ = quiet(nmf.restore, notion, self.journal)
        self.assertTrue(ok)
        self.assertEqual(notion.snapshot(), original)

    def test_partial_failure_is_restorable(self):
        original = self.notion.snapshot()
        for fail_at in (1, 2, 5, 9):
            with self.subTest(fail_at=fail_at):
                self.tearDown()
                self.setUp()
                self.notion.fail_at = fail_at
                with self.assertRaises(nmf.NotionError):
                    self.run_all()
                self.notion.fail_at = None
                ok, _ = quiet(nmf.restore, self.notion, self.journal)
                self.assertTrue(ok)
                self.assertEqual(self.notion.snapshot(), original)

    def test_html_table_over_several_paragraphs(self):
        lines = HTML_TABLE.split("\n")
        notion = FakeNotion([B("paragraph", T(line), id=f"l{i}") for i, line in enumerate(lines)]
                            + [B("paragraph", T("after"), id="after")])
        ops, _ = nmf.plan(notion.tree(), PAGE)
        nmf.apply_ops(notion, ops, self.journal)
        self.assertEqual([b["type"] if b["id"] != "after" else "after" for b in notion.children(PAGE)],
                         ["table", "after"])

    def test_limits_skip_the_block(self):
        long = r"\(" + "x+" * 600 + r"x\)"
        notion = FakeNotion([B("paragraph", T(long), id="big")])
        ops, notes = nmf.plan(notion.tree(), PAGE)
        self.assertEqual(ops, [])
        self.assertTrue(any("longer than" in n for n in notes))


class FixPage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_writes_nothing(self):
        notion = FakeNotion(sample())
        code, out = quiet(nmf.fix_page, notion, PAGE, apply=False, katex=False, backup_dir=self.tmp.name)
        self.assertEqual(code, 0)
        self.assertEqual(notion.written, [])
        self.assertIn("dry run", out)

    def test_apply_passes(self):
        notion = FakeNotion(sample())
        code, out = quiet(nmf.fix_page, notion, PAGE, apply=True, katex=False, backup_dir=self.tmp.name)
        self.assertEqual(code, 0, out)
        self.assertIn("RESULT              : PASS", out)
        journals = [f for f in os.listdir(self.tmp.name) if f.endswith(".journal.jsonl")]
        self.assertEqual(len(journals), 1)

    def test_refuses_when_page_changed_meanwhile(self):
        notion = FakeNotion(sample())
        notion.edited = ["2026-10-03T00:00:00.000Z", "2026-10-03T00:05:00.000Z"]
        code, out = quiet(nmf.fix_page, notion, PAGE, apply=True, katex=False, backup_dir=self.tmp.name)
        self.assertEqual(code, 3)
        self.assertEqual(notion.written, [])


if __name__ == "__main__":
    unittest.main()
