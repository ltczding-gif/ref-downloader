---
name: ref-downloader
description: >
  Use when the user asks to batch-download all references for a paper from a
  DOI or local PDF with ref-downloader. Not for one-off PDFs, paper search, or
  Zotero import.
---

# Ref Downloader — Agent Runbook

> Slim entry for agent mode. The full 8-step manual runbook with code
> snippets, DOI-resolution fallback chain, and PUBLISHER_MAP extension procedure
> lives in [references/agent-runbook.md](references/agent-runbook.md). Human users see
> [../../README.md](../../README.md).

`<SKILL_DIR>` = this folder (`skills/ref-downloader` in the source repo, or wherever the user copied this skill — e.g. `~/.claude/skills/ref-downloader/`). Python scripts live in `<SKILL_DIR>/scripts/`; config files (`config.example.toml`, `config.local.toml`) live at `<SKILL_DIR>/`.

## Install prerequisites (before first invocation)

The skill protocol can't manage Python deps. If `python -c "import playwright"` fails, the user needs:

```bash
cd "<SKILL_DIR>"
pip install playwright pymupdf
playwright install msedge          # downloads Edge driver
cp config.example.toml config.local.toml   # then user edits [crossref].mailto
```

If the user is developing from the source repo instead of an installed skill copy, they can also install from the repo root with `pip install -r requirements.txt -r requirements-dev.txt`.

## When to invoke

**Trigger phrases**:
- "帮我下载 [paper / DOI] 的参考文献" / "批量下载引用文献" / "把这篇论文的所有引用下载下来"
- "Download all refs of [paper / DOI]" / "Batch-download every reference"
- User provides a DOI (`10.x/y` form) or local PDF path and asks for "all references" / "全部参考文献"

**Don't invoke for**:
- Downloading one arbitrary PDF (not a reference list)
- Generic web scraping
- Paper search / Zotero import — different tools

## Primary entry

```bash
python "<SKILL_DIR>/scripts/run_ref_downloader.py" <DOI_OR_PDF_PATH>
```

The wrapper handles DOI resolution (Zotero → fitz fallback), output-dir layout,
sequential 3-stage pipeline (`extract_refs.py` → `validate_refs.py` →
`download_refs.py`), and end-of-run cleanup.

**Useful flags**:
- `--yes` — non-interactive (CI/batch), overwrite prompts default-yes
- `--auto` — forwarded to `download_refs.py`: skip "press Enter" confirm + shorter challenge wait + async retry queue for `manual_pending` refs (60s delay, single retry, max 3 concurrent). Use for CI / overnight runs; not for sessions where you want to drive captchas yourself.
- `--output-dir <path>` — override default output location
- `--config <path>` — alternate TOML config (overrides `config.local.toml`)

## Alternative backend: CloakBrowser (Cloudflare-heavy sites)

Default backend is Microsoft Edge. For sites that keep blocking ordinary Playwright (Cloudflare Turnstile, Radware, persistent `Just a moment` / 安全验证 pages), switch to the CloakBrowser stealth Chromium backend.

**What CloakBrowser is.** Third-party MIT-licensed Python package by CloakHQ ([github.com/CloakHQ/CloakBrowser](https://github.com/CloakHQ/CloakBrowser), [pypi:cloakbrowser](https://pypi.org/project/cloakbrowser/)). Ships a patched Chromium build with anti-fingerprint changes. Its `launch_persistent_context_async()` is Playwright-API-compatible, which is why ref-downloader can swap it in with one env var. **NOT a dependency of ref-downloader** — if the user doesn't `pip install cloakbrowser`, it's never imported and the default Edge path runs as normal. Beta software; user installs it at their own discretion.

```powershell
# One-time setup (separate from ref-downloader's `pip install playwright pymupdf`)
pip install cloakbrowser

# Switch backend (env vars; no CLI flag changes)
$env:REF_DOWNLOADER_BROWSER = "cloak"
$env:REF_DOWNLOADER_CLOAK_HUMAN_PRESET = "careful"   # optional: slower mouse/scroll
# Optional overrides:
# $env:REF_DOWNLOADER_CLOAK_PROFILE = "<custom path>"  # default: ~/.local/cloakbrowser/profiles/ref-downloader
# $env:REF_DOWNLOADER_CLOAK_PROXY = "http://..."
# $env:REF_DOWNLOADER_CLOAK_GEOIP = "1"
# $env:CLOAKBROWSER_PYTHONPATH = "<dev source>"      # sys.path hint if cloakbrowser is checked out, not pip-installed

python "<SKILL_DIR>/scripts/download_refs.py" <PROJECT_NAME>
```

Caveats:
- cloakbrowser uses its **own Chromium with a separate profile** — Edge does NOT need to be closed.
- The separate profile means your institutional cookies are NOT carried — best for open-Cloudflare sites, less useful for paywalled refs your institution licenses.
- A fresh cloak profile may still show Cloudflare/security-verification pages on first visit; warm it manually by opening the target site once with the same `REF_DOWNLOADER_CLOAK_PROFILE` and finishing any verification before running the downloader.
- `human_preset=careful` lowers behavior-detection trigger rates but is **not** a captcha solver.

## Pre-flight checklist (confirm before running)

1. **DOI correct?** Echo back to user: `即将下载参考文献：DOI=<doi>`
2. **Edge fully closed?** All `msedge.exe` processes killed (Task Manager check). The script claims the user's persistent Edge profile and needs exclusive access.
3. **Config set?** First-run users need `<SKILL_DIR>/config.local.toml` with `[crossref].mailto`. Missing config → wrapper prints a WARNING but continues with placeholder defaults.
4. **Output location agreed?** Default for DOI input: `<cwd>/<project_name>_refs/`. For PDF input: `<pdf_dir>/<pdf_stem>_refs/`. Override with `--output-dir`.

## Output layout

```
<OUTPUT_DIR>/
├── <PROJECT_NAME>/
│   ├── refs_raw.json           # extract_refs.py output
│   ├── refs_validated.json     # validate_refs.py output
│   ├── download_report.csv     # per-ref status (only on graceful completion)
│   ├── *.pdf                   # reference PDFs
│   └── *_SI.pdf                # supplementary files (where supported)
└── runs/<timestamp>-round-03/
    └── events.jsonl            # full event trace per ref
```

**Interruption note**: if the run is interrupted (Ctrl+C / Edge crash / VPN drop),
the root `download_report.csv` may be stale. Trust the latest
`runs/<timestamp>/events.jsonl` + actual files in `<PROJECT_NAME>/`.

## Common failure modes

| Status / symptom | Meaning | Action |
|---|---|---|
| `manual_pending (auth_redirect)` | Bounced to institution SSO | User signs in via live Edge tab; re-run (incremental skips done refs) |
| `manual_pending (challenge_timeout)` | Cloudflare / publisher challenge unsolved in time | Re-run interactively; solve captcha when prompted |
| `manual_pending (elsevier_crasolve_shell)` | Elsevier viewer stuck in transition | In `--auto` mode the async retry queue picks it up ~60s later; in interactive mode the hot-session retry usually catches it, else manual click in live page |
| `failed (auto)` | Generic auto path failed | Check `events.jsonl` for that ref; may need a publisher-specific patch |
| `ignored (ignored_institution_access)` | DOI listed in `[institution].ignored_access_dois` | Skip-by-design; remove from config to retry |
| Edge won't launch | Background `msedge.exe` still holding profile | Kill all `msedge.exe` in Task Manager, re-run |
| `ModuleNotFoundError: playwright` | Install prereqs not done | See "Install prerequisites" section above |
| `WARNING: crossref.mailto is the placeholder` | First-run config uncustomized | Edit `<SKILL_DIR>/config.local.toml` → set `[crossref].mailto` to a real email (Crossref polite pool) |

## Manual / debug mode

If the wrapper fails partway, run the 3 scripts standalone for partial re-execution:

```bash
python <SKILL_DIR>/scripts/extract_refs.py <DOI>           # → refs_raw.json
python <SKILL_DIR>/scripts/validate_refs.py <PROJECT>      # → refs_validated.json
python <SKILL_DIR>/scripts/download_refs.py <PROJECT>      # → PDFs + download_report.csv
```

Full 8-step manual flow with code snippets, DOI-resolution fallback chain (Zotero query → fitz text → user prompt), and the procedure for extending `PUBLISHER_MAP` when encountering an unknown DOI prefix → [references/agent-runbook.md](references/agent-runbook.md).

## See also

- [../../README.md](../../README.md) — human-facing setup, install, usage examples
- [references/agent-runbook.md](references/agent-runbook.md) — full manual runbook with code snippets
- [../../docs/SUPPORTED_PUBLISHERS.md](../../docs/SUPPORTED_PUBLISHERS.md) — publisher tier matrix
- [../../CONTRIBUTING.md](../../CONTRIBUTING.md) — adding new publisher / institution SSO
- [../../SECURITY.md](../../SECURITY.md) — Edge profile cookie risk
- [config.example.toml](config.example.toml) — full config schema
