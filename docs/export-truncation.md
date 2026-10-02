# Notion's markdown export silently truncates some equations

**Endpoint:** `GET /v1/pages/{page_id}/markdown`
**Notion-Version tested:** `2025-09-03` (also reproducible on `2026-03-11`)
**Date:** 2026-10-02
**Severity:** silent data loss for any tool that round-trips a page through markdown

## Summary

The markdown export can return a **truncated equation expression** while the block
tree (`GET /v1/blocks/{page_id}/children`) still holds the full expression.

The truncation is not flagged: `truncated` stays `false` and no `unknown_block_ids`
are returned. The docs describe `truncated`/`unknown_block_ids` as a *record-count*
limit, which is a different (and detectable) failure mode.

## Reproduction

Create a page whose markdown body contains these seven equation blocks, then read
the page back as markdown and compare with the block tree:

```markdown
### A
$$\boxed{\n\pi_{authors.name}\n\left(\nauthors\n\right)\n}$$
### B
$$\boxed{\n\pi_{authorsname}\n\left(\nauthors\n\right)\n}$$
### C
$$\boxed{\n\pi_{authors.name}\n(authors)\n}$$
### D
$$\boxed{\n\omega_{authors.name}\n\left(\nauthors\n\right)\n}$$
### E
$$\boxed{\n\text{some longer text here to pad it out considerably}\n}$$
### F
$$\pi_{authors.name}$$
### G
$$\boxed{\n\pi_{a.b}\n\left(x\n\right)\n}$$
```

Observed (block tree vs. markdown export):

| case | expression | block tree | markdown export |
|---|---|---|---|
| A | `\boxed{\n\pi_{authors.name}\n\left(\nauthors\n\right)\n}` | complete | **truncated to `\boxed{\n\pi_{`** |
| B | `\boxed{\n\pi_{authorsname}\n\left(\nauthors\n\right)\n}` | complete | complete |
| C | `\boxed{\n\pi_{authors.name}\n(authors)\n}` | complete | **truncated to `\boxed{\n\pi_{`** |
| D | `\boxed{\n\omega_{authors.name}\n\left(\nauthors\n\right)\n}` | complete | **truncated to `\boxed{\n\omega_{`** |
| E | `\boxed{\n\text{some longer text ...}\n}` | complete | complete |
| F | `\pi_{authors.name}` | complete | **truncated to `\pi_{`** |
| G | `\boxed{\n\pi_{a.b}\n\left(x\n\right)\n}` | complete | complete |

The pattern in this sample: an expression containing a **dotted subscript**
(`_{authors.name}`) is cut right after the opening brace of the subscript, while
`_{authorsname}` (no dot) and `_{a.b}` (short) survive. We did not chase the exact
predicate — the point is that the export is lossy *and silent*.

## Impact

Any pipeline that reads a page as markdown, transforms it, and writes it back will
**permanently destroy** the affected equations — the corrupted expression is what
gets written, and nothing in the response says so.

This bit us in practice: an earlier version of this tool rebuilt a page from the
exported markdown and would have replaced 4 complete equations with their truncated
prefixes.

## Workaround

Use the **block tree** as the source of truth:

```python
GET /v1/blocks/{page_id}/children   # paginate with start_cursor
# block["equation"]["expression"] holds the complete LaTeX
```

Then rebuild markdown yourself, and verify by comparing the resulting
`equation` blocks against what you intended to write — never by diffing the export.

## Notes for a bug report to Notion

- No error, no flag: `truncated: false`, `unknown_block_ids: []`.
- The affected expression renders **correctly in the UI** (the UI reads blocks),
  so this is export-only — but it is exactly the path agentic tools use.
