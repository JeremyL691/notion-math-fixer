# Sample: what an LLM export actually looks like

This is the shape of content that breaks when pasted into Notion — every construct
below is one this tool repairs. It is also a regression fixture: run

```bash
python3 notion_math_fixer.py <a-page-you-pasted-this-into>
```

and the audit should report the literal-TeX blocks, then convert them on `--apply`.

---

## Display math (`\[ ... \]`) — arrives as literal text

The six primitives of the course:
\[<br>\boxed{<br>\sigma,\ \pi,\ \rho,\ \times,\ \cup,\ -<br>}<br>\]

## Inline math (`\( ... \)`)

Recall: \(\sigma\) filters rows, \(\pi\) picks columns.

## Escaped braces from markdown escaping

\[<br>\boxed\{<br>\pi_A(\sigma_C(R))<br>\}<br>\]

## HTML table with math in cells

<table header-row="true">
<tr>
<td>Operation</td>
<td>Symbol</td>
<td>Intuition</td>
</tr>
<tr>
<td>Projection</td>
<td>\(\pi_A(R)\)</td>
<td>picks <strong>columns</strong></td>
</tr>
<tr>
<td>Theta Join</td>
<td>\(R\bowtie_C S\)</td>
<td>join on a condition</td>
</tr>
<tr>
<td>Difference</td>
<td>\(R-S\)</td>
<td>in A but not in B</td>
</tr>
</table>

## Prose that mentions LaTeX (must be left alone)

Use \[ ... \] for display math and \( ... \) for inline math.

## A real line break inside a paragraph

First line
Second line

## Code and quotes survive untouched

```sql
SELECT *
FROM authors AS a
INNER JOIN books AS b ON a.author_id = b.author_id;
```

> Only one
> no other

Finally:\[<br>R\bowtie_C S = \sigma_C(R\times S)<br>\]
