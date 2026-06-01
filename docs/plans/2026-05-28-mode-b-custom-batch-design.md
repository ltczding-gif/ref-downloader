# Mode B — Custom Batch Download (Design, v2)

**Date**: 2026-05-28
**Target version**: v0.5
**Scope**: `SKILL.md` changes only — no Python code changes
**Status**: Design ready after Opus + Codex adversarial review

## Context

`ref-downloader` v0.4.0 has ONE flow:

> User gives ONE paper (DOI or PDF) → `extract_refs.py` pulls its
> reference list from Crossref → `validate_refs.py` classifies each
> → `download_refs.py` fetches PDFs.

Real users want a second flow:

> User has a custom batch of papers they want downloaded — not "all
> refs OF a paper", but "these papers themselves". The batch may
> be specified as DOIs, paper titles, non-DOI identifiers (arXiv /
> PMID / Semantic Scholar IDs), OR an abstract query like
> "Smith 在 Google Scholar 上的文章" / "Nature Energy 2023 papers".

The bet: this can be implemented purely in `SKILL.md` because
`validate_refs.py` auto-fills journal/author/year from Crossref per
DOI, so the agent only needs to hand it a minimal `{id, doi}` list as
`refs_raw.json`. DOI resolution from non-DOI inputs is done by the
agent using Crossref API + adjacent free APIs (OpenAlex, Semantic
Scholar, PubMed).

## What changed since v1 (review feedback)

Two adversarial reviews fed this v2 design:

- **Opus** caught: append-renumber would corrupt incremental skip;
  trigger table missed non-DOI identifiers (arXiv/PMID/SS); B.2
  absolute score thresholds don't compare; B.3 missing OpenAlex.
  Also (incorrect) warned `None` would propagate as `"None"` string.
- **Codex (read-only adversarial, repo workspace, gpt-5.5 high
  effort)** verified each claim against source:
  1. **Refuted the `None`→`"None"` claim**:
     `validate_refs.py:309,367,379-380` reads/writes raw JSON; `null`
     stays `null`. `parent_doi=""` is still cleaner for downstream
     labels, but the "becomes string `None`" justification was wrong.
  2. **Preview-only-5 hides bad matches**: 20-item batch can hide
     wrong B.2 matches after row 5. Need every non-DOI-source row in
     the confirm table with source + confidence; low-confidence rows
     require explicit pick.
  3. **BibTeX `}` survives strip**: `doi = {10.1021/jacs.5c05017}`
     leaves `10.1021/jacs.5c05017}` after regex; strip rule
     `.,;)"'` misses `}`. Need full canonicalization (braces, URL
     wrappers, full-width slashes `／`, Unicode punctuation) BEFORE
     regex.
  4. **Router not MECE**: "Smith 2024 Nature paper on X" routes
     ambiguously between B.2 and B.3; "上次给的那 5 篇" has no
     resolvable content and falls through; full-width slash misses
     the DOI regex.
  5. **`download_refs.py` doesn't use `parent_doi` AT ALL** — my v1
     wording at the script-verification section was misleading.

All 13 findings are folded into the v2 design below.

## Solution overview

```
INPUT                              resolve to DOIs                        PIPELINE
─────                              ───────────────                        ────────
[Mode A — unchanged]
1 DOI or 1 PDF  ──────────►  extract_refs.py (Crossref)  ──────────►  refs_raw.json
                                                                       ↓
                                                                       validate_refs.py
                                                                       ↓
                                                                       download_refs.py

[Mode B — new]
DOI list (any wrap)  ──┐
non-DOI IDs (arX/PM/SS)─┤
title list           ──┼─── 0. canonicalize input ──►
abstract query       ──┤    1. normalize non-DOI IDs (B.0)
mixed                ──┤    2. extract DOIs (B.1)              ──►   refs_raw.json
                       │    3. title lookup leftovers (B.2)         (hand-built,
                       │    4. discovery for queries (B.3)           parent_doi="")
                       │    5. consolidate + canonical dedupe        ↓
                       │    6. full confirm table                    validate_refs.py
                       │    7. project name + append dialog          ↓
                       └────                                         download_refs.py
```

Mode A is byte-identical. Mode B reuses `validate_refs.py` and
`download_refs.py` verbatim; only the upstream "produce
`refs_raw.json`" step is new.

## Proposed SKILL.md structure

Current SKILL.md (v0.4.0) layout has shared concerns at top but
they're all Mode-A-shaped (SKILL.md:31-137). Codex flagged this as
documentation drift risk. Restructure to:

```
1. ## Mode router          ← NEW (top)
2. ## Mode A — Reference-list download  ← refactor of current sections
3. ## Mode B — Custom batch download    ← NEW
4. ## Install prerequisites             ← already shared
5. ## Useful flags                      ← already shared
6. ## Alternative backend: CloakBrowser ← already shared
7. ## Output layout                     ← already shared
8. ## Common failure modes              ← already shared, add Mode B rows
9. ## Manual / debug mode               ← already shared
10. ## See also                          ← unchanged
```

The Mode A section just absorbs the current "When to invoke" + "Primary
entry" + "Pre-flight checklist" content under its header. Below are
the NEW sections (1, 2 just structural, 3 substantive).

### Section 1 — Mode router (NEW, top)

```markdown
## Mode router

This skill handles two flows. **Pick before running.**

- **Mode A — Reference-list download** (original use case). User
  provides ONE paper (DOI or local PDF) and wants "all of its
  references". Pipeline: `extract_refs.py` → `validate_refs.py` →
  `download_refs.py`.

- **Mode B — Custom batch download**. User provides their own batch
  of papers — DOIs, paper titles, non-DOI identifiers (arXiv / PMID
  / Semantic Scholar IDs), OR an abstract query ("Smith 在 Google
  Scholar 上的文章" / "Nature Energy 2023 papers"). The agent
  resolves whatever was given to DOIs, then runs `validate_refs.py`
  → `download_refs.py` directly. **Skip the wrapper** —
  `run_ref_downloader.py` assumes a parent DOI and will fail.

Both modes share install, config, per-publisher strategies, failure
modes, output layout, and the CloakBrowser opt-in backend.

### Trigger family

| User input shape | Mode | Sub-flow |
|---|---|---|
| One DOI/PDF + "all refs of" / "全部参考文献" | A | — |
| ≥2 DOIs in input (any wrapping: bare / `{}` / `https://doi.org/…` / `dx.doi.org/…`; ASCII or full-width slashes) | B | B.1 (after canonicalize) |
| Non-DOI IDs only: `arXiv:` / `PMID:` / `S2:` / `corpusId:` | B | B.0 normalize → B.1 |
| Title list ("下载这几篇：title1, title2, …") | B | B.2 |
| Abstract query (author / topic / journal+year / "Google Scholar 上 …") | B | B.3 |
| Mixed (DOIs + titles + IDs + queries) | B | run each, merge |
| Single DOI without "of refs" qualifier | B | B.1 single-item |
| Title + author + year for ONE paper ("Smith 2024 Nature paper on X") | B | **B.2** (specific paper, lookup) |
| Open-ended query for a corpus ("Smith 2024 之后所有的 Nature 文章") | B | **B.3** (discovery) |
| **Insufficient resolvable content** ("上次给你的那 5 篇" / pure pronouns) | — | **Ask user to repaste / attach file**; do NOT guess |
| Genuinely ambiguous A vs B | — | ask user |

**Key disambiguators**:

- *"refs OF a paper"* (Mode A) vs *"these papers themselves"* (Mode B).
- *Identifies a specific paper* (B.2) vs *describes a set to discover*
  (B.3). If user names a specific paper with enough metadata to
  uniquely identify it (title + author + year), it's B.2 (lookup
  exact); if they describe a class of papers ("all Smith's 2024
  Nature papers"), it's B.3 (discover then filter).
```

### Section 3 — Mode B detailed flow (NEW)

```markdown
## Mode B — Custom batch download

Input variability is the point. Don't refuse — route.

### Step 0 — Canonicalize input (always runs first)

Before any extraction or routing, normalize the input string:

1. **Unwrap delimiters around DOIs**: strip leading/trailing
   `{}`, `<>`, `()`, `[]`, and quote marks.
2. **Strip URL prefixes**: `https://doi.org/`, `http://doi.org/`,
   `https://dx.doi.org/`, `http://dx.doi.org/` → bare DOI.
3. **Full-width / Unicode punctuation to ASCII**:
   - `／` (U+FF0F) → `/`
   - `：` (U+FF1A) → `:`
   - `．` (U+FF0E) → `.`
   - Smart quotes `""` `''` → straight `"` `'`
   - Em dash `—` / en dash `–` left alone (not DOI-relevant)
4. **Trim trailing punctuation**: `.,;)}"'` (note `}` — was missed
   in v1).

Apply step 0 BEFORE the regex pass in B.1 AND before the dedupe
compare in step 5.

### B.0 — Normalize non-DOI identifiers

For each non-DOI identifier the user gave:

| Input | Action |
|---|---|
| `arXiv:2401.12345` or bare arXiv ID | Use `10.48550/arXiv.<id>` (canonical), but prefer a journal DOI if the agent can discover one via Crossref `query.bibliographic=<arXiv_id>` |
| `PMID:12345678` or pubmed.ncbi.nlm.nih.gov/12345678 | Hit `eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&id=<pmid>` → grab `articleids[type=doi]` |
| Semantic Scholar paper ID (`S2:abc...` / `corpusId:N`) | Hit `api.semanticscholar.org/graph/v1/paper/<id>?fields=externalIds` → grab `externalIds.DOI` |
| Anything else non-DOI shaped | Leave for B.1 regex pass to ignore; if it survives B.1+B.2 unresolved, drop with a `skipped (unresolvable_identifier)` line in the confirm table — do NOT auto-fire requests on garbage |

**Network preflight**: before firing B.0 lookups, do one cheap
sanity probe (e.g. `HEAD https://api.crossref.org/`). If it fails,
tell the user "no network — Mode B can't resolve non-DOI identifiers
or do discovery; only direct DOI extraction will work" and let them
decide to proceed with B.1 only.

**Semantic Scholar rate limit**: unauthenticated ≤ 1 req/sec.
Pace batches; on `429` back off 30s then retry once; on second 429,
drop the entry with `skipped (ss_rate_limited)`.

### B.1 — DOI extraction

After Step 0 canonicalization:

1. Regex `10\.\d{4,9}/[^\s,;<>"'{}]+` on the canonicalized input
   (or file contents). The character class explicitly excludes
   `{}` so wrappers like BibTeX `doi = {10.x/y}` don't leak braces.
2. Strip trailing punctuation `.,;)}"'`.
3. Dedupe via canonical lowercase compare (see Step 5).
4. **Fallback rule**: if regex yields DOIs for less than 50% of the
   apparent entries (e.g. BibTeX with 20 `@article` entries but only
   5 have ASCII DOIs), send the remaining entries to **B.2 title
   lookup** rather than silently dropping them.

### B.2 — Title → DOI lookup

For each title:

1. Query Crossref: `GET https://api.crossref.org/works?
   query.title=<urlencode>&rows=5&mailto=<config crossref.mailto>`.
2. Pick top by `score`. **Note**: Crossref `score` is unbounded
   relevance, NOT 0–100 — absolute thresholds across queries don't
   compare. Use **relative + content** rules:
   - If `top1.score / top2.score < 1.5` → ambiguous; show user top 3.
   - **Always** sanity-check `matched_title` vs `input_title` by
     token overlap (or Levenshtein). If overlap < 50%, mark
     low-confidence regardless of score ratio.
3. If user gave an author or year ("Smith 2024"), cross-check
   against the candidate's `author[].family` and `issued.date-parts`.
4. Each B.2 result enters the confirm table as one of:
   - `confidence=high` (top1 score, ratio ≥ 1.5, overlap ≥ 50%, any
     author/year match consistent) — included by default.
   - `confidence=low` (any of: ratio < 1.5, overlap < 50%, no author
     match) — **excluded** by default; user must explicitly pick.
   - `unresolved` (0 candidates or all rejected) — dropped with
     `skipped (no_match)`.

### B.3 — Discovery from abstract description

Triggered by queries like "Smith 在 Google Scholar 上的文章", "topic
Y top 20", "Nature Energy 2023". **Do NOT scrape Google Scholar**
(anti-bot + ToS). Interpret "Scholar" semantically and use the
ladder below.

**Tool ladder** (try in order, use what's available):

1. **Crossref** (`api.crossref.org/works?query.author=` /
   `query.bibliographic=` / `query.container-title=`) — always
   available, no auth.
2. **OpenAlex** (`api.openalex.org/works?search=` or `?filter=
   author.id:A...`) — free, no auth, broader coverage than Crossref
   author search, returns DOIs directly.
3. **Semantic Scholar** (`api.semanticscholar.org/graph/v1/paper/
   search?query=...`) — better for topic / abstract search.
   **Rate limit ~1 req/sec unauthenticated**; pace requests, on 429
   back off 30s then drop the query on a second 429.
4. **PubMed** (`bio-research:pubmed` MCP) — if biomedical AND the
   MCP is loaded in the host framework.
5. **WebSearch** / Tavily / Exa — last-resort if a `web-search-*`
   skill is loaded. **Highest hallucination risk**; agent MUST
   round-trip every candidate through Crossref or OpenAlex to verify
   the DOI exists before accepting.

After discovery: present candidates as a numbered list with
`title + first-author + year + DOI + source` (which API found it).
Default top 20; ask if user wants more. User strikes out / picks
subset → final list locked.

**Open-ended-query clarifier**: if the agent's discovery would
return more than 50 candidates (e.g. user said "Smith 的所有文章"
and the author has 200+ publications), confirm scope with user
BEFORE returning — "found 200+; you want all of them, top 20 most
cited, or filter by year?".

### Mode B flow (after sub-flow resolution)

1. **Resolve** input via Step 0 → B.0 → B.1 → B.2 (leftovers + B.1
   fallback) → B.3 (for queries).
2. **Consolidate + dedupe**. Canonicalize EVERY DOI (lowercase,
   strip wrappers, no URL prefix, ASCII slash) BEFORE comparing.
   `10.X/ABC}` and `10.x/abc` must collapse to one entry.
3. **Confirm with user — FULL TABLE, not just first 5**:
   ```
   找到 N 个唯一 DOI（去重后）。完整列表：

     [ 1] doi=10.xxxx/yyy  source=B.1   confidence=high
          title= ...  author= ...  year= ...
     [ 2] doi=10.zzzz/www  source=B.2   confidence=high
          matched_title= ...  (input: "...")
     [ 3] doi=10.aaaa/bbb  source=B.3   confidence=high
          via=Crossref  (query: "...")
     [ 4] doi=10.cccc/ddd  source=B.2   confidence=LOW
          matched_title= ...  (input: "...")  ← excluded; pick to include
     [ 5] (unresolvable)   source=B.0   from: "PMID:99999"
          ← dropped
     ...

   开始下载吗？(y=accept all high-confidence / n=cancel /
   include 4 / exclude 1,3 / show <N> / ...)
   ```
   Default: download `confidence=high` rows only. `confidence=low`
   excluded unless user explicitly includes. Unresolvables dropped.
4. **Propose project name** (context-aware: "组会文献" →
   `groupmtg_<date>`; "Smith 综述补充" → `smith_review_extras`;
   nothing topical → `custom_<date>`). Ask user confirm.
5. **Append vs new** — if `<OUTPUT_DIR>/<project_name>/refs_raw.json`
   exists, ask `append / new / rename` (default: ask again on any
   other input — DO NOT default-append). **Append rules**:
   - Canonicalize new DOIs (Step 0) BEFORE compare.
   - Drop new DOIs already present in existing entries
     (case-insensitive canonical-form compare).
   - New ids start at `id = max(existing_ids) + 1`.
   - **Never renumber existing entries** — `validate_refs.py` keys
     its incremental skip on `id`, renumbering re-assigns prior
     verified metadata to the wrong DOI. Note: only verified rows
     are skipped; failed/pending rows revalidate on re-run, so
     append doesn't accidentally re-validate the whole list.
6. **Build `refs_raw.json`** (heredoc the agent runs):
   ```python
   import json
   from datetime import datetime
   dois = [...]  # finalized canonical-lowercase list
   start_id = 1  # or max(existing_ids)+1 in append mode
   data = {
     "parent_doi": "",  # empty string; downstream treats as string
                        # (validate_refs.py reads raw JSON; null would
                        # also work but "" is cleaner for report labels)
     "parent_title": f"Custom batch — {user_label}",
     "extracted_at": datetime.now().isoformat(timespec="seconds"),
     "total": len(dois),
     "with_doi": len(dois),
     "without_doi": 0,
     "references": [
       {"id": i, "doi": d,
        "key": "", "unstructured": "",
        "author": "", "year": "", "journal": "",
        "volume": "", "first_page": ""}
       for i, d in enumerate(dois, start=start_id)
     ],
   }
   with open(f"{project_name}/refs_raw.json", "w", encoding="utf-8") as f:
       json.dump(data, f, ensure_ascii=False, indent=2)
   ```
   All metadata fields stay empty — `validate_refs.py` fills them
   from Crossref per DOI on success. Rows whose DOI Crossref can't
   resolve become `status=failed` with empty metadata (not
   partially enriched).
7. **Run pipeline** (skip the wrapper):
   ```bash
   cd <OUTPUT_DIR>
   python <SKILL_DIR>/scripts/validate_refs.py <project_name>
   python <SKILL_DIR>/scripts/download_refs.py <project_name> [--auto] [--fail-fast]
   ```

### Mode B pre-flight checklist

1. **Full confirm table approved?** User saw EVERY row, not just top 5.
2. **Total count + estimated runtime?** Roughly: 0.35s × N for
   validate, ~30-90s per ref for download (publisher-dependent).
3. **Project name + new/append decided?** First-write = new project;
   second-write = append OR rename.
4. **Edge fully closed?** (Edge backend only — cloak skips.)
5. **Config set?** `[crossref].mailto` for polite-pool latency.
6. **Network reachable?** (`api.crossref.org` HEAD).

### Compatibility with v0.4.0 features

Verified by reading `download_refs.py:4686-4699,5166-5177`:

- `--auto`: works with Mode B. Manual-pending refs go to the
  async retry queue same as Mode A.
- `--fail-fast`: works with Mode B. Stops on first actionable
  unresolved ref.
- `[user].verified_no_si_dois`: works with Mode B. Matches by
  lowercase DOI — independent of how `refs_raw.json` was produced.
- CloakBrowser backend: works with Mode B (browser backend is
  decided by env var, independent of input mode).

### Output layout for Mode B

Same as Mode A:

```
<OUTPUT_DIR>/<project_name>/
├── refs_raw.json           # hand-built (Mode B) or extracted (Mode A)
├── refs_validated.json     # validate_refs.py
├── download_report.csv     # download_refs.py final summary
└── *.pdf, *_SI.pdf

<OUTPUT_DIR>/runs/<timestamp>-round-03/
└── events.jsonl            # per-ref event trace, append-only across runs
```

**Important — root `download_report.csv` is overwritten** at the end
of each run (graceful completion). It is NOT historical truth;
trust `runs/<timestamp>/events.jsonl` + actual files in
`<project_name>/` if a run was interrupted or you ran twice.

### Failure modes specific to Mode B

| Symptom | Resolution |
|---|---|
| Step 0 canonicalization left full-width slash unconverted | Add it to the U+FF mapping; reopen issue |
| BibTeX entry has DOI but regex left trailing `}` | Step 0 must run before regex; check input wasn't bypassed |
| Crossref title query 0 hits | Drop entry as `unresolved`; suggest user provide author/year/journal |
| Top B.2 match obviously wrong (low overlap, score-ratio borderline) | Marked `low`; user must explicitly include |
| Abstract query 0 candidates | Drop to next ladder step (Crossref → OpenAlex → SS → PubMed → WebSearch) |
| Discovery returns 200+ candidates | Ask user to narrow before showing |
| Same DOI surfaced by B.1 + B.2 | Step 5 canonical dedupe collapses to one |
| arXiv ID has both `10.48550/arXiv.X` and a journal DOI | Prefer journal DOI; arXiv is fallback only |
| File reads as binary garbage (e.g. PDF passed) | Refuse; tell user to extract text first |
| No network at runtime | B.0 + B.2 + B.3 fail; only B.1 viable; tell user before starting |
| `run_ref_downloader.py` invoked accidentally | It fails on DOI parsing — direct `validate_refs.py` + `download_refs.py` only |
| Semantic Scholar 429 | Back off 30s, retry once, then drop with `skipped (ss_rate_limited)` |
| PMID lookup returns no `articleids[type=doi]` | Entry has no DOI; tell user, suggest alternative identifier |
| User input is purely conversational ("上次那 5 篇") | Refuse; ask user to repaste / attach file |
```

## Why SKILL.md-only is sufficient (verified, corrected)

Verified by reading source (gpt-5.5 high-effort review with file:line
citations):

- **`validate_refs.py:173-185, 218, 235-258`**: per-DOI Crossref
  fetch fills `title`/`authors`/`year`/`journal` on success; failures
  become `status=failed` (not partially enriched). Agent does NOT
  need to pre-fill those fields.
- **`validate_refs.py:309, 367, 379-380`**: `parent_doi` read and
  written as raw JSON (no string cast). `null` stays `null`; `""`
  stays `""`. Recommend `""` only because it's cleaner in report
  labels — not because `null` would corrupt anything.
- **`validate_refs.py:317-340`**: incremental skip keyed on `id`,
  AND only verified rows are skipped (failed/pending revalidate).
  Append must NOT renumber existing rows.
- **`download_refs.py`** does NOT read `parent_doi` at all (search:
  zero hits). It reads `refs_validated.json` (which inherits
  whatever shape `validate_refs.py` wrote). Mode B handing
  `validate_refs.py` a custom `refs_raw.json` does not affect
  `download_refs.py`.

The bet holds.

## Risks fixed in this design (v2)

1. ~~`parent_doi=None` propagates as `"None"`~~ — actually false;
   keep `""` recommendation but drop the wrong justification.
2. **Append-and-renumber corrupts incremental skip** — Fixed by
   "new ids = max+1, never renumber".
3. **B.2 absolute score thresholds** — Fixed: relative ratio +
   token overlap.
4. **B.3 missing OpenAlex** — Fixed: ladder includes OpenAlex
   between Crossref and Semantic Scholar.
5. **Non-DOI IDs silently dropped** — Fixed by B.0 normalize.
6. **BibTeX/RIS DOI-bearing entries dropped if regex misses** —
   Fixed by B.1 "<50% yield → fall to B.2".
7. **`None` justification wrong** (Codex blocker 1) — Fixed by
   updating wording.
8. **Preview-only-5 hides bad matches** (Codex blocker 2) — Fixed
   by full confirm table with explicit confidence labels.
9. **BibTeX `doi = {10.x/y}` leaves trailing `}`** (Codex blocker 3)
   — Fixed by adding Step 0 canonicalization (braces, URL wrappers,
   full-width slash) BEFORE regex.
10. **Router not MECE** (Codex sub 7) — Fixed by:
    - Explicit B.2 vs B.3 disambiguator ("identifies a specific
      paper" vs "describes a set").
    - "Insufficient resolvable content → ask user" row.
    - Step 0 covers full-width slash.
    - "Mixed DOI+prose": each piece goes through its own sub-flow.
11. **No network preflight** (Codex sub 8) — Fixed by HEAD probe
    + tell user before starting.
12. **`download_refs.py` doesn't use parent_doi** (Codex sub 6) —
    Fixed wording in the "Why SKILL.md-only is sufficient" section.
13. **Append dialog has no default for invalid input** (Codex
    polish 11) — Fixed: "any other input → ask again, do NOT
    default-append".
14. **Compatibility with `--auto`/`--fail-fast`/`[user]` undefined**
    (Codex polish 12) — Fixed: dedicated "Compatibility" subsection.
15. **SKILL.md drift risk** (Codex polish 13) — Fixed by restructure:
    router top → Mode A → Mode B → shared sections at the bottom.

## Risks NOT fully addressed (acknowledged)

- **Semantic Scholar 429 honor-system pacing** — Skill says "≤ 1
  req/sec" + 30s backoff + drop on 2nd 429. Agent compliance not
  enforceable from SKILL.md; risk is "burning candidates on bursts".
- **Discovery confidence scoring** — When B.3 returns 100
  candidates, current design hands all 100 to the user; no
  automatic top-N-by-citations ranking. Acceptable for v0.5.
- **Mode B project resumption across sessions** — Agent must
  re-resolve from scratch each session unless the user re-pastes.
  Could be mitigated by writing a `mode_b_input.txt` alongside
  `refs_raw.json` for human readability + future agent context.
  Out of scope for v0.5 (would touch project layout).

## Future work (out of scope for v0.5)

- Extend `run_ref_downloader.py` wrapper to accept `--list <path>`
  + `--title <str>`. Eliminates the "skip the wrapper" footgun.
- Add `extract_refs_from_list.py` that consolidates Step 0 + B.0 +
  B.1 + dedupe so the agent doesn't need a heredoc. Cleaner CLI.
- B.3 discovery confidence ranking (citation count, recency).

## Acceptance criteria

- An agent reading the updated SKILL.md handles all 10 input shapes
  in the Trigger family table without falling through to "I can't
  help with that".
- BibTeX entries with `doi = {10.x/y}` parse cleanly (no trailing
  `}` in `refs_raw.json`).
- Full-width slash `10.1021／jacs.5c05017` resolves correctly.
- "上次给的那 5 篇" gets a clarifying ask, not a hallucinated
  download.
- B.2 lookups with low overlap don't auto-download.
- B.3 with 200+ candidates asks for scoping before listing.
- Mode A behavior on `10.1021/jacs.5c05017` is unchanged: 38 refs,
  same project name `jacs.5c05017`, same pre-flight, same output
  layout.
- pytest 10/10 still passes (defensive check — no code changes).
