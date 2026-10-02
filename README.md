# notion-math-fixer

Turn **LLM-generated LaTeX into native Notion equation blocks** — through the official API, in one shot, with verification.

ChatGPT, Claude, and most "export my chat" flows hand you `\[ ... \]`, `\( ... \)`, `<br>` tags, escaped braces and raw HTML tables. Paste that into Notion and the math stays **plain text**: you see `\[ \boxed{ \sigma, \ \pi } \]` instead of a formula. Manually re-typing every equation into `/math` blocks is not a workflow.

This tool takes an existing Notion page, rebuilds it from its **block tree**, and writes it back with real equations — then proves it worked.

```
$ python3 notion_math_fixer.py <page-id>            # audit only, nothing written
$ python3 notion_math_fixer.py <page-id> --apply    # rebuild + verify
$ python3 notion_math_fixer.py <page-id> --apply --katex   # + KaTeX validation
```

---

## What it handles

| Input (what LLMs actually emit) | Output (native Notion) |
|---|---|
| `\[ ... \]` (multi-line, with `<br>` inside) | **block equation** (`$$ ... $$`) |
| `\( ... \)` | **inline equation** (`$ ... $`) — works in headings and table cells too |
| `\{ x \}` when the braces are otherwise unbalanced | `{ x }` (escaping artifact removed) |
| `<br>` inside math | newline inside the expression |
| `<table><tr><td>` HTML | **native Notion table** (cells may contain equations) |
| quote with nested paragraph children | preserved (a naive top-level walk drops them) |
| paragraph with a real line break | kept as `<br>`, Notion's in-paragraph break |

## Safety model

* **Dry run by default.** Writing requires `--apply`.
* **Backup before every write** — original block tree, generated markdown, and the pre-write export go to `~/.cache/notion-math-fixer/`.
* **Refuses to guess.** Block types it cannot express losslessly (callout, toggle, image, video, bookmark, …) abort the run; `--force` overrides.
* **Verifies after writing** — it does not trust the `200 OK`:

```
--- verification ---
  blocks            : 11  {'paragraph': 4, 'equation': 2, 'table': 1, 'quote': 3, 'code': 1}
  literal LaTeX left: 0   (must be 0)
  equation blocks   : 2   (expected 2)
  expressions match : 2/2
  text drift tokens : 0   (code-fence language labels are expected)
  RESULT            : PASS
KaTeX: 2/2 expressions render cleanly
```

`--katex` additionally renders every expression through [KaTeX](https://katex.org) with `throwOnError: true`, so a malformed formula fails the run instead of landing in your notes.

## Quickstart

```bash
git clone https://github.com/JeremyL691/notion-math-fixer && cd notion-math-fixer

# 1. token: create an integration at https://www.notion.so/my-integrations
export NOTION_TOKEN=ntn_xxx        # or --token-file ~/.notion_token
# 2. share the target page with the integration (page menu → Connect to)

python3 notion_math_fixer.py "https://www.notion.so/My-Page-<id>"           # look first
python3 notion_math_fixer.py "https://www.notion.so/My-Page-<id>" --apply   # do it

# optional KaTeX gate
npm i katex && python3 notion_math_fixer.py <id> --apply --katex
```

Requires Python 3.9+ (stdlib only) and Notion-Version `2025-09-03` or newer. `--notion-version` is overridable; `2026-03-11` also works.

### A real run

Before — four paragraphs whose math is literal text, an HTML table whose symbol column is literal text, and a quote with a nested child paragraph:

```
--- audit ---
  top-level blocks    : 9  {'paragraph': 5, 'table': 1, 'quote': 2, 'code': 1}
  blocks w/ literal TeX: 4
      #0 'This note is what an LLM export usually looks like: \[ ... \] blocks, '
      #2 '\[\n\boxed{\n\sigma,\ \pi,\ \rho\n}\n\]'
      #3 '记忆：\(\sigma\) 筛行，\(\pi\) 取列。'
      #8 '最后：\[\nR\bowtie_C S = \sigma_C(R\times S)\n\]'
  nested (non-table)  : [(5, 'quote', 1)]

--- rebuild ---
  394 chars | block equations 2 | inline equations 5
    leftover '\[': 1 kept in 1 prose paragraph(s)
    leftover '\(': 0 ok
    leftover '<table': 0 ok
    <br> inside math: 0 ok
```

After `--apply`: `literal LaTeX left: 0`, `equation blocks: 2 (expected 2)`, `expressions match: 2/2`, `RESULT: PASS`.

Note the deliberate non-conversion: paragraph `#0` *talks about* `\[ ... \]`. The tool leaves it as text (prose, not math) and says so, instead of inventing equations out of documentation.

---

## Five pitfalls this tool encodes

These are the reasons a naive "markdown → Notion" round trip silently destroys notes. All verified on 2026-10-02.

### 1. Notion's markdown export truncates some equations — never round-trip through it

`GET /v1/pages/{id}/markdown` escapes backslashes **and silently truncates some expressions**, while the block tree holds them intact. Controlled reproduction:

| expression | block tree | markdown export |
|---|---|---|
| `\boxed{\n\pi_{authors.name}\n\left(\nauthors\n\right)\n}` | complete | **truncated to `\boxed{\n\pi_{`** |
| `\boxed{\n\omega_{authors.name}\n...\n}` | complete | **truncated to `\boxed{\n\omega_{`** |
| `\pi_{authors.name}` | complete | **truncated to `\pi_{`** |
| `\boxed{\n\pi_{authorsname}\n...\n}` (no dot) | complete | complete |
| `\boxed{\n\pi_{a.b}\n\left(x\n\right)\n}` (short) | complete | complete |
| `\boxed{\n\text{some longer text ...}\n}` | complete | complete |

No error, no flag — `truncated: false`, `unknown_block_ids: []`. Any tool that reads a page as markdown, transforms it, and writes it back will **permanently destroy** the affected equations. Full report + repro: [`docs/export-truncation.md`](docs/export-truncation.md).

**So this tool rebuilds from `GET /v1/blocks/{id}/children`, and verifies against equation blocks — never against the export.**

### 2. Blocks nest — a top-level walk silently drops content

A `quote` can own `paragraph` children. Walking only the top level loses them (we lost two keyword lists that way). The tool walks recursively and merges children back into their parent.

### 3. `$$x$$` on one line is *not* an equation block

* `$$` on its own line, expression, `$$` on its own line → **equation block** ✅
* `$$x$$` inline in one line → a paragraph containing an inline equation ❌ (looks close, behaves differently)

Same for inline: `$x$` in a heading or a table cell becomes a real inline equation.

### 4. Pipe tables can hold equations in cells

Notion's markdown tables are pipe tables (`| a | b |` + `| --- |`), and `$...$` inside a cell becomes an `equation` rich_text. That is how the HTML tables in LLM output get repaired.

### 5. Line breaks inside a paragraph need `<br>`

A raw newline inside a paragraph is not preserved; `<br>` is the documented in-paragraph break. The tool converts prose newlines to `<br>` (and strips `<br>` from inside math, where it belongs as a real newline).

---

## Limits

* Equations, tables, nesting, code, lists, quotes, headings, dividers, child pages. **Not** images, callouts, toggles, bookmarks — it refuses rather than degrading them.
* It rebuilds the whole page (`replace_content`). Use it on notes you own; the backup is your undo.
* One page per run. Batch by looping the CLI.
* Notion rate-limits at ~3 req/s; the client retries 429/5xx with backoff.

## Prior art

Worth knowing before you pick a tool — the space is crowded, but almost all of it is UI-level:

* **Browser extensions** (the popular approach): [`voidCounter/noeqtion`](https://github.com/voidCounter/noeqtion), [`davidwkk/Notion-Equation-Converter`](https://github.com/davidwkk/Notion-Equation-Converter), [`GiulioTognetto/notion-latex-converter`](https://github.com/GiulioTognetto/notion-latex-converter), [`stanuch/NotionTeX`](https://github.com/stanuch/NotionTeX), and the "LLM to Notion – Math & LaTeX" extension.
* **Clipboard helpers**: [`Gallections/MathPaste`](https://github.com/Gallections/MathPaste), [`halilibrahimyesirci/EquaPaste`](https://github.com/halilibrahimyesirci/EquaPaste).
* **API/CLI**: [`enzo-boulin/notion-equation-converter`](https://github.com/enzo-boulin/notion-equation-converter), [`alasdairpan/mdsync`](https://github.com/alasdairpan/mdsync), [`amachino/notionx`](https://github.com/amachino/notionx).
* **Libraries**: [`tryfabric/martian`](https://github.com/tryfabric/martian), [`Cobertos/md2notion`](https://github.com/Cobertos/md2notion) — mature markdown→blocks, but they expect clean `$...$` input.

This tool's niche: **API-based, batch, eats the LLM dialect (`\[...\]`/`\(...\)`/`<br>`/HTML), and verifies instead of assuming.**

## License

MIT © 2026 Jiuyi (Jeremy) Liu
