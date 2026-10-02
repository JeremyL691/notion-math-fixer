# Sample: what an LLM export actually looks like

This is the shape of content that breaks when pasted into Notion — every construct
below is one this tool repairs. It is also a regression fixture: run

```bash
python3 notion_math_fixer.py <a-page-you-pasted-this-into>
```

and the audit should report the literal-TeX blocks, then convert them on `--apply`.

---

## Display math (`\[ ... \]`) — arrives as literal text

课程的六个 primitives：
\[<br>\boxed{<br>\sigma,\ \pi,\ \rho,\ \times,\ \cup,\ -<br>}<br>\]

## Inline math (`\( ... \)`)

记忆：\(\sigma\) 筛行，\(\pi\) 取列。

## Escaped braces from markdown escaping

\[<br>\boxed\{<br>\pi_A(\sigma_C(R))<br>\}<br>\]

## HTML table with math in cells

<table header-row="true">
<tr>
<td>操作</td>
<td>符号</td>
<td>考试直觉</td>
</tr>
<tr>
<td>Projection</td>
<td>\(\pi_A(R)\)</td>
<td>选 <strong>columns</strong></td>
</tr>
<tr>
<td>Theta Join</td>
<td>\(R\bowtie_C S\)</td>
<td>按条件连接</td>
</tr>
<tr>
<td>Difference</td>
<td>\(R-S\)</td>
<td>A 中有、B 中没有</td>
</tr>
</table>

## Prose that mentions LaTeX (must be left alone)

Use \[ ... \] for display math and \( ... \) for inline math.

## A real line break inside a paragraph

第一行
第二行

## Code and quotes survive untouched

```sql
SELECT *
FROM authors AS a
INNER JOIN books AS b ON a.author_id = b.author_id;
```

> Only one
> no other

最后：\[<br>R\bowtie_C S = \sigma_C(R\times S)<br>\]
