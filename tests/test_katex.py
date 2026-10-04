"""The --katex gate, against the real KaTeX package. Skipped without node + katex."""

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest

from helpers import nmf

ROOT = os.path.join(os.path.dirname(__file__), "..")


def katex_available() -> bool:
    if not shutil.which("node"):
        return False
    return subprocess.run(["node", "-e", "require.resolve('katex')"], cwd=ROOT,
                          capture_output=True).returncode == 0


@unittest.skipUnless(katex_available(), "node + katex not installed (npm ci)")
class KatexGate(unittest.TestCase):
    def check(self, items):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as out:
            ok = nmf.katex_check(items, tmp)
        return ok, out.getvalue()

    def test_valid_expressions_pass(self):
        ok, out = self.check([("\\boxed{\n\\sigma,\\ \\pi\n}", True, "a"), (r"R\bowtie_C S", False, "b"),
                              (r"\text{user\_id}", False, "c")])
        self.assertTrue(ok, out)
        self.assertIn("3/3", out)

    def test_broken_expression_is_reported_with_its_block(self):
        ok, out = self.check([(r"\frac{a}{b}", True, "good"), (r"\text{user_id}", False, "bad-block")])
        self.assertFalse(ok)
        self.assertIn("bad-block", out)


if __name__ == "__main__":
    unittest.main()
