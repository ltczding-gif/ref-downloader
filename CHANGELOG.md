# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

### Added — CloakBrowser backend (optional)

- New alternative browser backend: set `REF_DOWNLOADER_BROWSER=cloak` to drive
  [cloakbrowser](https://pypi.org/project/cloakbrowser/) instead of Microsoft Edge.
  cloakbrowser is a stealth Chromium with humanized mouse / scroll / keyboard
  timings — useful for Cloudflare Turnstile, Radware, and other sites that
  detect ordinary Playwright fingerprints.
- cloakbrowser is **not** a hard dependency. Users install it themselves
  (`pip install cloakbrowser`); the import is lazy and only fires when the env
  var is set.
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
