"""Pure text-layer behaviour: no API, no fake client."""

import unittest

from helpers import T, nmf


def convert(*runs, **kw):
    return nmf.convert_runs(list(runs), **kw)


class NormalizeMath(unittest.TestCase):
    def test_br_becomes_newline_and_blank_lines_collapse(self):
        self.assertEqual(nmf.normalize_math("<br>a<br><br/>b<br />")[0], "a\nb")

    def test_escaped_argument_braces_are_unescaped(self):
        expr, notes = nmf.normalize_math(r"<br>\boxed\{<br>\pi_A(\sigma_C(R))<br>\}<br>")
        self.assertEqual(expr, "\\boxed{\n\\pi_A(\\sigma_C(R))\n}")
        self.assertTrue(notes)

    def test_subscript_braces_are_unescaped(self):
        self.assertEqual(nmf.normalize_math(r"x_\{ij\}")[0], "x_{ij}")

    def test_set_braces_are_kept(self):
        for expr in (r"\{x \mid x > 0\}", r"A = \{1, 2\}", r"\left\{ x \right\}", r"\bigl\{ x \bigr\}"):
            self.assertEqual(nmf.normalize_math(expr)[0], expr)

    def test_mixed_braces_are_left_alone(self):
        self.assertEqual(nmf.normalize_math(r"\frac{a}{b} + \{c\}")[0], r"\frac{a}{b} + \{c\}")

    def test_underscore_inside_text_is_kept(self):
        self.assertEqual(nmf.normalize_math(r"\text{user\_id} = x_1")[0], r"\text{user\_id} = x_1")
        self.assertEqual(nmf.normalize_math(r"\mathrm{a\_b}")[0], r"\mathrm{a\_b}")

    def test_underscore_outside_text_is_unescaped(self):
        self.assertEqual(nmf.normalize_math(r"x\_1 + \text{a\_b} + y\_2")[0], r"x_1 + \text{a\_b} + y_2")


class PageId(unittest.TestCase):
    ID = "0123456789abcdef0123456789abcdef"
    WANT = "01234567-89ab-cdef-0123-456789abcdef"

    def test_title_ending_in_hex_does_not_leak_into_id(self):
        for slug in ("Lecture-12", "Decade", "Cafe-Notes-2026", "abc"):
            self.assertEqual(nmf.page_id_from(f"https://www.notion.so/{slug}-{self.ID}"), self.WANT, slug)

    def test_query_fragment_and_workspace(self):
        self.assertEqual(nmf.page_id_from(f"https://www.notion.so/ws/Title-{self.ID}?pvs=4#abc"), self.WANT)
        self.assertEqual(nmf.page_id_from(f"https://www.notion.so/{self.ID}?v=" + "f" * 32), self.WANT)

    def test_bare_and_dashed(self):
        self.assertEqual(nmf.page_id_from(self.ID), self.WANT)
        self.assertEqual(nmf.page_id_from(self.WANT), self.WANT)
        self.assertEqual(nmf.page_id_from(self.ID.upper()), self.WANT)


class ConvertRuns(unittest.TestCase):
    def test_inline(self):
        segs, changed, _ = convert(T(r"Recall: \(\sigma\) filters rows, \(\pi\) picks columns."))
        self.assertTrue(changed)
        self.assertEqual(len(segs), 1)
        runs = segs[0][1]
        self.assertEqual([r["type"] for r in runs], ["text", "equation", "text", "equation", "text"])
        self.assertEqual(runs[1]["equation"]["expression"], r"\sigma")

    def test_links_and_formatting_survive(self):
        segs, changed, _ = convert(T("see "), T("docs", link="https://x.y", color="red"), T(r" for \(a+b\)"))
        self.assertTrue(changed)
        runs = segs[0][1]
        self.assertEqual(runs[1]["text"], {"content": "docs", "link": {"url": "https://x.y"}})
        self.assertEqual(runs[1]["annotations"]["color"], "red")
        self.assertEqual(runs[-1]["equation"]["expression"], "a+b")

    def test_math_spanning_differently_formatted_runs(self):
        segs, changed, _ = convert(T(r"so \(a + "), T("b", bold=True), T(r"\) holds"))
        self.assertTrue(changed)
        eq = [r for r in segs[0][1] if r["type"] == "equation"]
        self.assertEqual(eq[0]["equation"]["expression"], "a + b")

    def test_code_runs_are_not_touched(self):
        _, changed, _ = convert(T(r"write \(x\) like this", code=True))
        self.assertFalse(changed)

    def test_dollars_are_not_math(self):
        _, changed, _ = convert(T("costs $5 and $10 today"))
        self.assertFalse(changed)

    def test_placeholder_spans_are_prose(self):
        _, changed, notes = convert(T(r"Use \[ ... \] for display math and \( ... \) for inline math."))
        self.assertFalse(changed)
        self.assertTrue(notes)

    def test_long_lead_in_still_converts(self):
        lead = ("So we get the following formula, where sigma is selection and pi is projection; "
                "this is the core of relational algebra and shows up on every exam:")
        segs, changed, _ = convert(T(lead + r"\[ a+b \]"))
        self.assertTrue(changed)
        self.assertEqual(segs[1], ("display", "a+b"))
        self.assertEqual(segs[0][1][0]["text"]["content"], lead)

    def test_display_math_splits_segments(self):
        segs, _, _ = convert(T("The six primitives of the course:\n\\[<br>\\boxed{<br>\\sigma<br>}<br>\\]\nThat's all."))
        self.assertEqual([k for k, _ in segs], ["runs", "display", "runs"])
        self.assertEqual(segs[1][1], "\\boxed{\n\\sigma\n}")
        self.assertEqual(segs[0][1][0]["text"]["content"], "The six primitives of the course:")
        self.assertEqual(segs[2][1][0]["text"]["content"], "That's all.")

    def test_display_in_cells_becomes_inline(self):
        segs, changed, _ = convert(T(r"\[ x \]"), allow_display=False)
        self.assertTrue(changed)
        self.assertEqual(segs, [("runs", [nmf.equation_run("x", T("")["annotations"])])])

    def test_br_in_prose_becomes_newline(self):
        segs, changed, _ = convert(T("First line<br>Second line"))
        self.assertTrue(changed)
        self.assertEqual(segs[0][1][0]["text"]["content"], "First line\nSecond line")

    def test_latex_linebreak_is_not_a_delimiter(self):
        segs, _, _ = convert(T(r"\[ a \\[2pt] b \]"))
        self.assertEqual(segs[1], ("display", r"a \\[2pt] b"))

    def test_unwritable_mention_blocks_conversion(self):
        mention = {"type": "mention", "mention": {"type": "link_preview", "link_preview": {"url": "u"}},
                   "plain_text": "u", "annotations": {}}
        with self.assertRaises(nmf.Unwritable):
            convert(T(r"\(x\) "), mention)
        _, changed, _ = convert(T("no math "), mention)       # untouched block: fine
        self.assertFalse(changed)


class HtmlTable(unittest.TestCase):
    SRC = """<table header-row="true">
<tr><td>Operation</td><td>Symbol</td><td>Notes</td></tr>
<tr><td>Projection</td><td>\\(\\pi_A(R)\\)</td><td>picks <strong>columns</strong></td></tr>
<tr><td>Norm</td><td>\\(|v|\\)</td><td>a &amp; b<br>c</td></tr>
</table>"""

    def test_parse(self):
        rows, header = nmf.parse_html_table(self.SRC)
        self.assertTrue(header)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][1][0]["equation"]["expression"], r"\pi_A(R)")
        self.assertEqual(rows[2][1][0]["equation"]["expression"], "|v|")
        bold = [r for r in rows[1][2] if r.get("annotations", {}).get("bold")]
        self.assertEqual(bold[0]["text"]["content"], "columns")
        self.assertEqual("".join(r["text"]["content"] for r in rows[2][2]), "a & b\nc")

    def test_ragged_rows_are_padded(self):
        rows, header = nmf.parse_html_table("<table><tr><th>a</th><th>b</th></tr><tr><td>1</td></tr></table>")
        self.assertTrue(header)
        self.assertEqual([len(r) for r in rows], [2, 2])


if __name__ == "__main__":
    unittest.main()
