# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.1] — 2026-06-01

Docs-only release. Zero Python source changes; pytest 10/10 still passes.
Patch-level bump (not minor) because the underlying pipeline is unchanged
— this strictly expands what the agent runbook can route, not what the
scripts can do.

### Added — Mode B (custom batch download) in SKILL.md

- New top-level `## Mode router` section. Agents read this first to
  decide between Mode A (the v0.4.0 original — "all refs of a paper")
  and Mode B (new — "these papers themselves").
- 11-row Trigger family table covering: ≥2 DOIs in any wrapping
  (bare / `{}` / URL-prefixed / full-width slash); non-DOI identifiers
  (arXiv / PMID / Semantic Scholar IDs); title lists; abstract queries
  ("Wang 2024 Nature Energy papers on hydrogen evolution"); mixed
  input; ambiguous / insufficient input.
- 5 Mode B sub-flows:
  - **Step 0 — Canonicalize**: BibTeX braces, URL prefixes, full-width
    slashes, Unicode punctuation normalized BEFORE the regex pass.
  - **B.0 — Normalize non-DOI IDs**: arXiv → `10.48550/arXiv.<id>` (or
    journal DOI via Crossref bibliographic), PMID → eutils lookup,
    Semantic Scholar ID → SS API lookup. Network preflight via HEAD
    probe.
  - **B.1 — Direct DOI extraction**: regex character class excludes
    `{}` so BibTeX `doi = {10.x/y}` doesn't leak braces. <50% yield
    falls back to B.2.
  - **B.2 — Title → DOI lookup**: Crossref `query.title` with relative
    score ratio (top-2 < 1.5) + token overlap sanity check (Crossref
    `score` is unbounded, absolute thresholds are meaningless).
    Confidence: high / low / unresolved; low excluded by default.
  - **B.3 — Discovery from abstract description**: tool ladder
    Crossref → OpenAlex → Semantic Scholar (rate-limited, 30s backoff)
    → PubMed (biomed) → WebSearch (last resort). Explicitly forbids
    scraping Google Scholar. Open-ended-query clarifier asks user
    to scope when >50 candidates.
- **Full confirm table** (not top-5 preview): every row labeled with
  source + confidence; low-confidence rows excluded by default and
  require explicit user pick.
- **Append rule**: canonicalize new DOIs before dedupe compare;
  new ids start at `max(existing_ids) + 1`; never renumber existing
  entries (preserves `validate_refs.py`'s `id`-keyed incremental skip).
- 10 Mode-B-specific failure mode rows added to the shared failure
  table (BibTeX `}` leak, Step 0 bypass, network down, Semantic
  Scholar 429, conversational input refuse, etc).
- Cross-mode compatibility documented: `--auto`, `--fail-fast`,
  `[user].verified_no_si_dois`, CloakBrowser backend all work with
  Mode B (verified by reading `download_refs.py:4686-4699, 5166-5177`).

### Added — Design doc

- `docs/plans/2026-05-28-mode-b-custom-batch-design.md` (530 lines)
  captures the full design, the 15 risks identified by 2-round
  adversarial review (Opus + Codex gpt-5.5 high effort), and the
  3 acknowledged-but-not-fully-fixed risks (Semantic Scholar 429
  honor-system pacing, B.3 discovery confidence ranking, Mode B
  resumption across sessions).

### Verified

- pytest 10/10 still passes.
- 13-test merged plan executed: Tier 1 simulations (S1 heredoc shape;
  M3 BibTeX brace exclusion; M4 full-width slash canonicalization with
  control proving raw regex misses it; S5 canonical dedupe) all pass.
  Tier 1 live scripts (S2 `validate_refs.py` accepts hand-built
  `refs_raw.json` with `parent_doi=""`; S4 two-round append with
  `Verified: 3 (2 cached + 1 new)` proving incremental skip works
  on Mode B input; S6 wrapper refuses list/garbage with clear error)
  all pass. Tier 3 routing dispatched to a fresh agent across 5
  scenarios (Mode A canonical / single DOI / conversational refuse /
  mixed bag / ambiguous ask): 5/5 correctly cite the matching
  trigger family row.

### Why patch-level (0.4.1) not minor (0.5.0)?

Strict semver would argue minor — Mode B is an agent-visible new
capability. But:
- Zero Python code changed. Anyone running scripts directly (not
  through an agent) sees byte-identical behavior.
- The pipeline (`extract_refs.py` / `validate_refs.py` /
  `download_refs.py`) is unchanged.
- The "new feature" is purely runbook routing on the agent side.

Patch-level signals "no install / re-pip needed; just re-copy the
skill folder if you want the new Mode B docs."

## [0.4.0] — 2026-05-28

Tranched merge of improvements from the maintainer's working copy. Five
focused commits, all already pushed before this version bump:

- **Commit A (`661e41c`)** — Optional CloakBrowser stealth Chromium backend
  for Cloudflare-heavy sites. Opt-in via `REF_DOWNLOADER_BROWSER=cloak`.
- **Commit B (`e20514c`)** — Auto-asset async download queue infrastructure
  for binary SI files (zip/docx/mp4/...). Dead code in this release; gets
  wired in by the v0.5 SI capture rework.
- **Commit C (`0c1d1a3`)** — Elsevier strengthening: 11 new PII / SI / URL
  helpers (mostly dead code) + `ELSEVIER_TRANSIENT_POPUP_REASONS` widened
  to 5 (active change — fewer false `manual_pending (elsevier_*)` refs).
- **Commit D (`bf00d0e`)** — `--fail-fast` mode for CI / batch runs.
  Terminates run after first actionable unresolved ref.
- **Commit E (`e500ce0`)** — CCS Chemistry publisher (`10.31635` →
  `ccs`), APS direct PDF URL wire-in (bypasses landing page for 9 APS
  journals), `[user].verified_no_si_dois` config section.

The dead-code helpers from Commits B and C are intentional staging for
the v0.5 SI capture refactor; documented as "Deferred to v0.5" below.

### Added — CCS Chemistry publisher + APS direct URL + `[user]` config section

- **CCS Chemistry** (Chinese Chemical Society) recognized as a publisher key:
  - `validate_refs.py` PUBLISHER_MAP: `10.31635` → `ccs`
  - `validate_refs.py` JOURNAL_PUBLISHER_MAP: `"ccs chemistry"` → `ccs`
  - `download_refs.py` PUBLISHER_STRATEGIES: `"ccs"` entry (generic_fallback,
    weak — `cloudflare_blocked_official_route`). The site sits behind
    Cloudflare; use `REF_DOWNLOADER_BROWSER=cloak` for reliable access.
  - `docs/SUPPORTED_PUBLISHERS.md`: new prefix row + operational note.
  - Pytest coverage: prefix + journal-name fallback both asserted.

- **APS direct PDF URL** wire-in: `direct_pdf_url()` now calls
  `aps_pdf_url_from_doi(doi)` (from Commit C) for `publisher=="aps"` before
  falling through. For 9 APS journals (PhysRevB, PhysRevLett, RevModPhys,
  PhysRevA/C/D/E/Materials/X) this bypasses the JS-driven landing page
  entirely. Non-listed APS journals still go through the article-page flow.

- **`[user]` config section** in `config.local.toml`:
  - New `verified_no_si_dois` list — DOIs the user has personally verified
    to have no SI material. Refs with these DOIs get `si_status=not_applicable (verified_no_si)`
    in the report instead of `not_found`. Avoids treating known-empty refs
    as failures + cuts noise on re-runs.
  - `config.example.toml` shows the syntax (commented-out example).
  - Implementation: new `UserConfig` dataclass in `_config.py`;
    `init_user_config()` parallel to `init_institution_config()` (same
    lru_cache + cache_clear contract); `verified_no_si_dois()` getter
    in `download_refs.py` (lru_cached, returns case-insensitive frozenset);
    wired into the SI capture point — when `get_si_links` returns empty
    AND the DOI is in the user-marked set, status becomes
    `not_applicable (verified_no_si)` and events.jsonl logs `ignored`
    with `verified_no_si` detail instead of `not_found`.

### Deferred to v0.5

- `try_ccs_si_asset` (CCS-specific SI capture path) — needs integration
  with existing SI flow; the `ccs` strategy currently falls through
  generic_fallback.
- SI post-retry sweep (`retry_pending_si_for_downloaded_mains` from
  the `.agents` working copy) — invasive end-of-main() rework, lands
  with the broader SI capture refactor.
- Wire-in of Commit C's Elsevier helpers (deferred-click integration,
  `auto_retry_elsevier_article_reclicks`, `try_elsevier_si_download_all`)
  — these mutate page state inside `try_elsevier_pdf` / `try_click_pdf`,
  the riskiest part of the codebase. Held until SI rework provides a
  clean integration shape.

### Added — Fail-fast mode

- New flag `--fail-fast` (also `REF_DOWNLOADER_FAIL_FAST=1`) terminates the
  run after the first ref that hits an unresolved status it can't recover
  from. Useful in CI / overnight batches where waiting for 80 refs only
  to discover they're all failing wastes hours.
- `is_fail_fast_mode()` is the single entry point; the check fires at the
  end of each ref iteration. When triggered, the script:
  1. Logs `fail_fast_stop` to `events.jsonl` with the unresolved status.
  2. Cancels both async queues (`auto_manual_retries`,
     `auto_asset_downloads`) to free the Edge context.
  3. Breaks out of the main ref loop. The post-loop final-drain blocks
     (manual queue flush, wait=True async drains) all check
     `stop_after_current_ref` and skip cleanly.
- 5 new report inspection helpers categorize report rows for fail-fast
  decisions:
  - `report_row_has_unresolved(row)` — any non-terminal status
    (`manual_pending`, `failed`, `not_found`)?
  - `report_row_has_scheduled_auto_retry(row)` — waiting on a background
    auto-retry (`auto_retry=scheduled` or `auto_asset=scheduled` in the
    status string)?
  - `report_row_has_actionable_unresolved(row)` — unresolved AND not just
    waiting on a scheduled retry. **This is the fail-fast trigger.**
  - `first_actionable_unresolved_row(report)` — scan the report for the
    first row matching the above (returns None if everything's healthy
    or scheduled).
  - `unresolved_report_reason(row)` — formats the status pair as
    `pdf=... | si=...` for logging.
- Refs waiting on a background `auto_retry=scheduled` / `auto_asset=scheduled`
  status do **not** trigger fail-fast. The retry may still succeed and
  mark the ref `downloaded`; stopping the run before that resolves would
  surface a false positive.
- SI-only failures also trip fail-fast (e.g. `pdf_status=downloaded` +
  `si_status=failed (...)`). The reasoning: if SI capture is breaking
  consistently, you want to know now — not after 80 refs ran. Pass
  `--auto` without `--fail-fast` if you want to ride through SI hiccups.
- `main()` prints `Fail fast: True/False` at startup so users can
  confirm the flag took effect.

### Added — Elsevier strengthening (helpers, mostly dead code)

- `ELSEVIER_TRANSIENT_POPUP_REASONS` widened from 2 → 5 reasons (adds
  `elsevier_popup_not_captured`, `auto_retry_no_pdf`, `radware_bot_manager`).
  **`auto_retry_no_pdf` and `radware_bot_manager` are immediately active**:
  the popup-settle loop in `wait_for_elsevier_popup_surface_ready` now
  treats them as transient and keeps waiting instead of giving up. Both
  reasons are emitted upstream (`inspect_access_barrier` produces
  `radware_bot_manager`; `auto_manual_retry_worker` produces
  `auto_retry_no_pdf`). `elsevier_popup_not_captured` has no emitter yet
  — it gets one when the deferred-click integration lands in Commit E.
  Expected effect: fewer false `manual_pending (elsevier_*)` refs when
  Elsevier bounces through Radware or the auto-retry loop sees no PDF
  on first pass.
- 11 new helpers (all dead code until SI capture rework wires them in):
  - **PII reverse-lookup**: `extract_elsevier_pii_from_blob`,
    `extract_elsevier_pii_from_crossref_message`,
    `elsevier_pii_from_crossref(page, doi)`. Get the canonical PII when
    the article URL itself doesn't carry one (Crossref records the PII in
    `URL` / `resource` / `link` fields for Elsevier deposits).
  - **URL construction**: `elsevier_pdfft_url_from_pii`,
    `elsevier_si_candidate_urls_from_pii` (enumerates
    `1-s2.0-{PII}-mmc{N}.{ext}` on `ars.els-cdn.com`).
  - **SI probing**: `probe_elsevier_si_asset_urls` HEAD-probes which mmc
    URLs actually exist; uses Playwright's `context.request.head()` first,
    falls back to system curl (`probe_asset_via_curl_head` +
    `parse_curl_head_output`) when Playwright is blocked on the ARS CDN.
  - **SI evidence**: `elsevier_has_si_text_evidence(html, anchors)` —
    page has SI markers? Used to decide cheap probe (mmc1 only) vs
    expensive probe (all extensions × max_mmc indices).
  - **Article surface check**: `is_elsevier_article_surface(url)` matches
    sciencedirect + linkinghub.elsevier.com.
- APS direct PDF URL helper: `aps_pdf_url_from_doi` constructs
  `journals.aps.org/<slug>/pdf/<doi>` from the DOI for APS journals
  (PhysRevB, PhysRevLett, RevModPhys, etc — 9 slugs in `APS_JOURNAL_SLUGS`).
  Skips the JS-driven landing page entirely. Dead code until Commit E
  wires it into `direct_pdf_url`.

### Added — Elsevier SI / asset constants

- `ELSEVIER_SI_CANDIDATE_EXTENSIONS` (8 extensions: mp4 / pdf / docx / doc /
  xlsx / xls / zip / csv) + `ELSEVIER_SI_NO_EVIDENCE_PROBE_EXTENSIONS`
  (reduced set for "no evidence" probes).
- 6 timing constants (env-overridable):
  `ELSEVIER_SI_MAX_MMC=5`, `ELSEVIER_SI_CANDIDATE_LIMIT=50`,
  `ELSEVIER_SI_PROBE_TIMEOUT=5_000`, `ELSEVIER_SI_CURL_PROBE_TIMEOUT=10_000`,
  `ELSEVIER_SI_DOWNLOAD_ALL_TIMEOUT=25_000`, `ELSEVIER_ASSET_REQUEST_TIMEOUT=20_000`.

### Deferred to a follow-up commit

- `try_elsevier_pdf` / `try_click_pdf` integration of deferred-click
  retry (5 attempts × 5.5s interval × 30s total). Constants
  intentionally not added until the integration lands — unused
  constants are noise.
- `auto_retry_elsevier_article_reclicks` + `try_elsevier_si_download_all`
  — these mutate page state and need careful integration with the
  existing PDF capture path. Land with the SI rework.

### Added — Auto-asset async download queue (infrastructure only)

- New async queue dedicated to publisher-hosted binary SI assets
  (zip / docx / mp4 / xlsx / ...). Sibling infrastructure to the
  v0.3.0 `auto_manual_retry` queue, but specialized for binary downloads
  that Playwright's request pipe stalls on (Elsevier ARS, Wiley
  `downloadSupplement`, etc.).
- Two-tier download fallback (`download_asset_via_curl` →
  `download_asset_via_urllib`): curl for fast streaming on Windows when
  available, urllib with per-chunk stall detection as fallback. Both bypass
  Playwright entirely for the actual byte transfer.
- 10 new queue helpers, all gated on `RUN_CTX["auto_asset_tasks"]`:
  `get_auto_asset_sem`, `push_auto_asset_result`,
  `run_auto_asset_download_item`, `auto_asset_download_worker`,
  `materialize_auto_asset_result`, `collect_auto_asset_download_tasks`,
  `collect_auto_asset_download_results`, `schedule_auto_asset_download`,
  `drain_auto_asset_downloads`, `cancel_auto_asset_downloads`.
- Tunables (all env-var overridable):
  `AUTO_ASSET_DOWNLOAD_MAX_CONCURRENT=3`,
  `AUTO_ASSET_DOWNLOAD_MAX_PENDING=8`,
  `AUTO_ASSET_FINAL_DRAIN_TIMEOUT=180_000`. Plus 8 Elsevier-asset timing
  constants (`ELSEVIER_ASSET_CURL_*`, `ELSEVIER_ASSET_URLLIB_*`) and a
  shared `BINARY_ASSET_USER_AGENT` string.
- `restart_edge_context` now cancels both queues (manual_retry + asset)
  before relaunching Edge, so retries don't fire against a dead context.
- `main()` drains the asset queue at three points in auto-mode: before
  each `download_one`, after each `download_one`, and end-of-run (with
  `wait=True`). All three are no-ops until a follow-up commit wires
  `schedule_auto_asset_download` into the SI capture path.
- Behavior is **completely unchanged** for users: no scheduler currently
  calls `schedule_auto_asset_download`, so the queue stays empty. This
  commit is preparatory infrastructure; the SI rework lands separately.

### Changed

- `url_asset_extension(url)` now also recovers the extension from common
  query-string filename keys (`file`, `filename`, `name`, `download`,
  `attachment`). Needed so the asset queue can correctly classify Wiley
  `/doi/.../downloadSupplement?file=foo.docx`-style URLs whose path has
  no extension.

### Added — CloakBrowser backend (optional, third-party)

- New alternative browser backend: set `REF_DOWNLOADER_BROWSER=cloak` to drive
  [CloakBrowser](https://github.com/CloakHQ/CloakBrowser) (third-party
  MIT-licensed package by CloakHQ, `cloakbrowser` on
  [PyPI](https://pypi.org/project/cloakbrowser/)) instead of Microsoft Edge.
  cloakbrowser ships a patched Chromium build with source-level
  anti-fingerprint changes that look like a normal browser to common
  bot-detection layers (Cloudflare Turnstile, Radware, DataDome, etc).
  Its `launch_persistent_context_async()` API is Playwright-compatible,
  which is what lets ref-downloader swap backends with one env var.
- cloakbrowser is **not** a hard dependency of ref-downloader. Users install
  it themselves (`pip install cloakbrowser`); the import is lazy and only
  fires when the env var is set, so users who never opt in pay zero cost.
- The cloak backend uses its own Chromium under a separate persistent
  profile — Edge does NOT need to be closed when running under cloak. The
  flip side: institutional cookies are NOT carried (separate profile),
  so the cloak backend is most useful for open-Cloudflare sites, less
  useful for paywalled-but-licensed refs.
- New env vars (all optional, no config-file equivalent yet):
  `REF_DOWNLOADER_BROWSER`, `REF_DOWNLOADER_CLOAK_PROFILE`,
  `REF_DOWNLOADER_CLOAK_HUMANIZE`, `REF_DOWNLOADER_CLOAK_HUMAN_PRESET`
  (`default` / `careful`), `REF_DOWNLOADER_CLOAK_PROXY`,
  `REF_DOWNLOADER_CLOAK_GEOIP`, `CLOAKBROWSER_PYTHONPATH` (dev-checkout hint).
- Default cloak profile path: `~/.local/cloakbrowser/profiles/ref-downloader`
  (cross-platform via `Path.home()`).
- When the cloak backend is active, Edge does not need to be closed and the
  "press Enter when Edge is closed" prompt is skipped.

### Implementation notes

- `launch_edge_context()` now dual-branches on `using_cloakbrowser()`. The Edge
  branch is bit-identical to v0.3.0; the cloak branch is gated behind the env
  var so users who never opt in pay zero cost.
- 4 new helpers: `selected_browser_backend()`, `using_cloakbrowser()`,
  `selected_cloak_human_preset()`, `cloak_si_fast_human_config()`. The last is
  the aggressive scroll-pause config used for SI capture under cloak (default
  preset is too slow for batch SI collection).

## [0.3.0] — 2026-05-23

### Added — Elsevier popup state machine

- Four new helpers replace the old single-shot popup capture in `try_elsevier_pdf`:
  - `find_elsevier_pdf_selector` — picks the right "View PDF" selector + describes which DOM path matched (for events.jsonl)
  - `wait_for_elsevier_pdf_button_ready` — replaces the 2.5s hard sleep with bounded polling on actual button readiness (default 8–10s)
  - `wait_for_elsevier_popup_after_click` — 15s polling for the popup to actually navigate away from `about:blank`; re-clicks at 10s if the popup is still blank
  - `wait_for_elsevier_popup_surface_ready` — 20s settle window for the viewer surface to finish hydrating before PDF capture
- Six new constants govern the timings (`ELSEVIER_POPUP_POLL_MS=15000`, `ELSEVIER_POPUP_SETTLE_MS=20000`, `ELSEVIER_POPUP_CAPTURE_WAIT_MS=8000`, `ELSEVIER_PRE_CLICK_MIN_WAIT_MS=8000`, `ELSEVIER_PRE_CLICK_MAX_WAIT_MS=10000`, `ELSEVIER_TRANSIENT_POPUP_REASONS`).
- Net effect: fewer `manual_pending (elsevier_*)` refs that turned out to be transient popup races; the script now waits for actual UI state instead of fixed sleeps.

### Added — auto-mode manual_pending retry queue

- New asynchronous retry queue scheduled when a ref hits `manual_pending` in `--auto` mode (gated by `is_auto_mode()`; non-auto mode is unchanged).
- Constants: `AUTO_MANUAL_RETRY_WAIT=60_000` (delay before retry), `AUTO_MANUAL_RETRY_TIMEOUT=20_000` (single-try timeout), `AUTO_MANUAL_RETRY_MAX_CONCURRENT=3`, `AUTO_MANUAL_RETRY_MAX_PENDING=8`.
- Workers: `schedule_auto_manual_retry` → `auto_manual_retry_worker` → `run_auto_manual_retry_item` → `auto_retry_manual_page_once`. Drained at three points: pre-`download_one`, post-`download_one`, and end-of-run (with `wait=True`).
- `CURRENT_REF` `contextvars.ContextVar` carries per-ref context across async hops so retries can attribute events to the right ref.
- `sync_report_with_existing_files` reconciles the in-memory report with the project directory before writing `download_report.csv`, picking up retries that finished after the main loop printed status.
- 11 wire-in sites: `restart_edge_context` (cancellation), `download_one` × 3 (gate manual_pending paths through the queue), `try_click_pdf.inspect_new_pdf_page` × 2, `try_browser_pdf_navigation_candidate` × 1, main loop drains × 3 + sync × 1.

### Behavioral change in `--auto` mode

- **Previously**: `--auto` produced `manual_pending` for any ref that needed institutional click-through or popup retry; the run ended without revisiting them.
- **Now**: same `manual_pending` refs are queued for a single asynchronous retry attempt while the main loop continues; refs that succeed on retry update the report to `downloaded`. Interactive (non-`--auto`) mode is unchanged.
- Practical impact: `--auto` is now appropriate for CI / overnight runs where Elsevier's `crasolve_shell` transitions or AIP loading pages may resolve a minute later.

### Changed

- `download_refs.py` grew from ~3,500 to ~4,300 lines. The retry-queue + popup-state-machine constants are at the top of the file alongside existing timing constants; helpers live mid-file before `download_one`.

### Migration note

No config or CLI changes. If you previously avoided `--auto` because it skipped retries, reconsider — that behavior is no longer accurate. Default interactive mode is unchanged.

### Known follow-ups (deferred to a future release)

- `response_body_with_timeout` and `with_auto_retry_result` are present but unused (dead carry-over from staged commits); harmless, scheduled for removal.
- Class-based `AutoManualRetryManager` refactor + module split (`barriers.py` / `pdf_capture.py` / `publishers/elsevier.py` / `manual_retry.py` / `reporting.py`).

## [0.2.0] — 2026-05-11

### Changed (breaking — install path)

- **Skill is now self-contained at `skills/ref-downloader/`.** Python sources
  moved from repo root to `skills/ref-downloader/scripts/`; `config.example.toml`
  moved to `skills/ref-downloader/`. The skill folder can now be copied
  directly to any agent framework's skill directory (`~/.claude/skills/`,
  `~/.codex/skills/`, `.github/skills/`, `.agents/skills/`) without dragging
  the rest of the repo along — Level-2 portable skill structure per
  `anthropics/skills` convention.
- `_config.py` constant `PACKAGE_DIR` → `_SKILL_DIR`; it now points to the
  skill root (parent of `scripts/`) so config files sit one level up from
  scripts — matches user-expected layout (config visible at skill root, not
  buried inside scripts/).
- `run_ref_downloader.py` constant `SKILL_DIR` → `SCRIPTS_DIR` to reflect the
  actual semantics after the move.
- Removed `skills/ref-downloader/agents/openai.yaml`. Codex's own skill format
  is now SKILL.md frontmatter (matching Anthropic's spec); the bespoke
  `openai.yaml` UI metadata file was not portable across frameworks.
  Users who specifically need Codex UI metadata can add their own.
- README install section restructured: "as agent skill" (per-framework
  `cp -r` command) vs "as Python tool" (clone + pip + pytest).
- `tests/conftest.py` `sys.path` now points to
  `skills/ref-downloader/scripts/` instead of repo root.

### Migration note for existing users

If you previously installed by cloning the repo and running scripts from root:
- Your local `config.local.toml` at repo root → move to `skills/ref-downloader/config.local.toml`
- Direct script invocations `python run_ref_downloader.py X` → become `python skills/ref-downloader/scripts/run_ref_downloader.py X`
- Agent-mode users: re-copy `skills/ref-downloader/` to your framework's skill path; old `SKILL.md` at repo root is gone.

## [0.1.0] — 2026-05-10

Initial open-source release. Refactored from a personal Claude Code skill;
all institution-specific and personal-path constants have been extracted to
configuration.

### Added

- Three-script pipeline: `extract_refs.py`, `validate_refs.py`,
  `download_refs.py` driven by single-entry wrapper `run_ref_downloader.py`
- Config layer (`_config.py`) reading TOML + environment variables
- `config.example.toml` documenting all options; `config.local.toml`
  gitignored for user-specific values
- Bilingual documentation: English `README.md` + `README.zh.md`
- Issue templates: `bug-report.md`, `new-publisher.md`
- `SECURITY.md` describing Edge profile access and recommendation for a
  dedicated profile
- `docs/SUPPORTED_PUBLISHERS.md` extracted from the original SKILL.md as
  reference documentation for contributors
- `[institution]` config section enabling per-organization SSO detection
  (auth host / URL / page title patterns) and DOI-level access exclusions
- Non-interactive mode: `--yes` flag and tty-detection in `extract_refs.py`
- Per-field environment variable overrides (`REF_DOWNLOADER_MAILTO`,
  `_ZOTERO_DB`, `_EDGE_PROFILE`, `_DISABLE_EXTENSIONS`, `_CONFIG`)
- Bilingual highlights table at the top of `README.md` and `README.zh.md`,
  framed as user-value-first ("what you get — *how it's distinctively
  delivered*"). 5 rows, mirrored row-for-row between languages, scannable
  in seconds.
- **Installable skill package**: agent-mode runbook now lives under
  `skills/ref-downloader/`, keeping the repository root as the human-facing
  Python project while the skill bundle stays small and installable.
- `skills/ref-downloader/agents/openai.yaml`: UI metadata for the packaged skill.
- **SKILL.md slim entry**: agent-mode runbook reduced from 420 → 105 lines,
  with the long 8-step manual flow + DOI-resolution code + `PUBLISHER_MAP`
  extension procedure moved to
  [skills/ref-downloader/references/agent-runbook.md](skills/ref-downloader/references/agent-runbook.md).
  SKILL.md now follows skill-creator best practice: trigger phrases,
  primary entry command, pre-flight checklist, common-failure lookup table,
  pointer to the extended runbook.
- README and README.zh.md gain a `status-beta` badge + an explicit "Status:
  beta (v0.1.0)" status line so users calibrate expectations.
- **Offline pytest suite** (`requirements-dev.txt` + `tests/test_*.py`):
  10 unit tests covering the config loader (chain merge order, env-var
  overrides, malformed-TOML exit, schema validation) + publisher detection
  (DOI prefix + journal fallback) + project-name sanitization.
  Runs in <1s, no Playwright dependency.
- `docs/architecture.md` (new): condensed design rationale for contributors
  (why real Edge profile, why one big `download_refs.py`, why the
  `auth_loading_titles` ambiguity is intentional, what's deliberately out
  of scope).
- `docs/plans/` (removed): the internal design + implementation diaries are
  no longer carried in the public-facing repo. Their substantive content
  lives in `docs/architecture.md`; their procedural content is captured
  in git history + commit messages.

### Publishers

Initial coverage (varying maturity; see `docs/SUPPORTED_PUBLISHERS.md`):
ACS, Nature, Science, Elsevier, Wiley, RSC, Springer, PNAS, ECS, IOP, AIP,
AVS, IEEE, OSA, KPS, Beilstein, APS, Annual Reviews, Taylor & Francis.

### Known limitations

- Windows + Microsoft Edge only (verified path); other OSes / browsers
  untested
- Headed mode required for Wiley / ACS supplementary downloads
- SI download is the most fragile code path — main PDFs are reliable
