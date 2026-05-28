# ref-downloader

> **Stop losing an afternoon to chasing dozens of reference PDFs by hand.**
> One DOI in, every reference PDF out — using your existing institutional access.

[![Version: 0.4.0](https://img.shields.io/badge/version-0.4.0-orange.svg)](CHANGELOG.md)
[![Status: beta](https://img.shields.io/badge/status-beta-orange.svg)](#known-limitations)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
![Verified on Windows + Edge](https://img.shields.io/badge/verified%20on-Windows%20+%20Edge-success)

[中文完整文档 / Full Chinese version](README.zh.md)

> **Status: beta (v0.4.0).** Windows + Microsoft Edge verified path. macOS / Linux / Chromium untested. Expect rough edges around supplementary downloads and publisher-site changes. PR-worthy issues welcome.

> **Heads up — not a paywall bypass.** ref-downloader uses _your_ institutional access. If your university or organization subscribes to a journal, those refs work. If they don't, those refs become `manual_pending` for you to follow up on by hand.

## Demo (30-second console preview)

```text
$ python run_ref_downloader.py 10.1021/jacs.5c05017

=== Ref Downloader Wrapper ===
DOI:         10.1021/jacs.5c05017
PROJECT:     jacs.5c05017
Config:      config.example.toml + config.local.toml

>>> extract_refs.py
  Title: Designing Natural Cell-Inspired Heme-Spurred Membrane...
  References found: 38

>>> validate_refs.py
  Total: 38  Verified: 38  Failed: 0  No DOI: 0

>>> download_refs.py
  [ 1] downloaded (842 KB)        Lee2016_NatEnergy.pdf
  [ 2] downloaded (1.2 MB)        Wang2018_AdvMater.pdf
  [ 3] manual_pending (auth_redirect)
  [ 4] downloaded (655 KB)        Chen2019_JACS.pdf
  [ 5] failed (challenge_timeout)
  [ 6] ignored (ignored_institution_access)
  ... 31 more refs processed ...
  [38] downloaded (956 KB)        Park2024_JElectrochemSoc.pdf

========== Download report ==========
Total references:  38
Main PDFs:         33 downloaded · 3 manual_pending · 1 failed · 1 ignored
SI files:          12 captured
PDFs land in:      ./jacs.5c05017_refs/jacs.5c05017/
=====================================
```

## Contents

- [What you get](#what-you-get)
- [Why not Zotero, scihub, or generic scrapers?](#why-not-zotero-scihub-or-generic-scrapers)
- [Quick start](#quick-start)
- [Requirements](#requirements)
- [Install](#install)
- [Usage examples](#usage-examples)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Supported publishers](#supported-publishers)
- [Known limitations](#known-limitations)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)

## What you get

- **Paywalled refs work without setup.** _Drives your real Microsoft Edge profile, so any institutional login already in your browser carries through. No API keys, no proxies, no reverse engineering._
- **One DOI in, every reference PDF out.** _Crossref-driven extraction + 17+ publisher-specific download paths (Wiley PDFDirect, Elsevier viewer, AIP loading-page wait — see [per-publisher reliability tier](docs/SUPPORTED_PUBLISHERS.md)), not generic scraping._
- **You always know which refs failed and why.** _`download_report.csv` gives every ref a status + reason (`manual_pending (auth_redirect)`, `failed (challenge_timeout)`, `ignored`); `events.jsonl` keeps the per-ref event trace._
- **Pick up where you left off** after a VPN drop, browser crash, or `Ctrl+C`. _State persists per project; rerunning skips already-downloaded refs and retries only the failures._

## Why not Zotero, scihub, or generic scrapers?

- **vs. Zotero's _Find Available PDF_** — walks one paper at a time and silently gives up at SSO redirects. ref-downloader walks the whole reference list at once and treats SSO as a configurable step instead of a dead end.
- **vs. scihub-style tools** — don't carry your institutional license, so paywalled refs you _legitimately_ have access to just fail. ref-downloader uses your authenticated browser session, so subscriptions you already pay for actually count.
- **vs. generic web scrapers** — don't know Wiley needs PDFDirect, Elsevier needs a viewer click, or AIP serves a Chinese loading page first. ref-downloader has 17+ publisher-specific paths plus Elsevier popup state machine + `--auto` mode retry queue (manual-pending refs get a second async attempt 60s later, hot-session preserved).
- **vs. raw Playwright** — gets blocked on Cloudflare / Radware / Turnstile-heavy sites. Set `REF_DOWNLOADER_BROWSER=cloak` to swap in [cloakbrowser](https://pypi.org/project/cloakbrowser/)'s stealth Chromium with humanized input — no code changes, same pipeline. See [Configuration](#configuration).

## Quick start

The skill is self-contained under `skills/ref-downloader/`. Pick the install path for your agent framework:

```powershell
git clone https://github.com/ltczding-gif/ref-downloader.git

# Pick ONE install destination for your agent framework:
#   Claude Code:        cp -r ref-downloader/skills/ref-downloader ~/.claude/skills/
#   Codex CLI:          cp -r ref-downloader/skills/ref-downloader ~/.codex/skills/
#   Copilot CLI / VSC:  cp -r ref-downloader/skills/ref-downloader .github/skills/
#   Project-local:      cp -r ref-downloader/skills/ref-downloader .agents/skills/

cd ~/.claude/skills/ref-downloader     # or wherever you copied it
pip install playwright pymupdf
playwright install msedge
cp config.example.toml config.local.toml      # then set [crossref].mailto

# In your agent: just describe the task; the skill triggers via its description.
# Direct CLI for testing: python scripts/run_ref_downloader.py 10.1021/jacs.5c05017
```

What you'll see: 30–80 refs discovered for a typical chemistry/physics paper, then a mix of `downloaded` (refs your institution covers), `manual_pending` (SSO bounce or paywall), and occasional `failed` (publisher quirk). Run on a DOI from a journal your institution actually subscribes to for the highest hit rate. Details below.

## Requirements

- **OS**: Windows 10/11 (verified). macOS / Linux untested — PRs welcome.
- **Browser**: Microsoft Edge (Stable channel). The script claims your persistent Edge profile, so close all Edge windows before running.
- **Python**: 3.11 or newer (uses stdlib `tomllib`).
- **Optional**: A Zotero installation (auto-detects DOI from a PDF's filename via Zotero's SQLite database — much faster than text extraction).
- **Optional**: PyMuPDF (`pip install pymupdf`) for DOI extraction from PDF text when Zotero lookup is unavailable.

## Install

### As an agent skill (recommended)

Pick the install path for your agent framework:

| Framework | Install command |
|---|---|
| Claude Code | `cp -r skills/ref-downloader ~/.claude/skills/` |
| Claude Agent SDK | same (auto-discovers `~/.claude/skills/`) |
| Codex CLI | `cp -r skills/ref-downloader ~/.codex/skills/` |
| Copilot CLI / VS Code agent | `cp -r skills/ref-downloader .github/skills/` |
| Any framework (project-local) | `cp -r skills/ref-downloader .agents/skills/` |

Then install Python prereqs INSIDE the copied skill folder (the skill protocol doesn't manage Python deps):

```powershell
cd ~/.claude/skills/ref-downloader            # or wherever you copied it
pip install playwright pymupdf                # or use the source's requirements.txt
playwright install msedge

cp config.example.toml config.local.toml
# Edit config.local.toml — at minimum set [crossref].mailto.
# Windows: notepad config.local.toml
# macOS / Linux: $EDITOR config.local.toml   (or vim / nano / code / ...)
```

### As a Python tool (for developers)

If you want to hack on the code, the skill folder _is_ a runnable Python project:

```powershell
git clone https://github.com/ltczding-gif/ref-downloader.git
cd ref-downloader

pip install -r requirements.txt -r requirements-dev.txt
playwright install msedge

cp skills/ref-downloader/config.example.toml skills/ref-downloader/config.local.toml
# Edit config.local.toml — at minimum set [crossref].mailto.

# Run the offline test suite
python -m pytest tests/ -v

# Run the tool directly
python skills/ref-downloader/scripts/run_ref_downloader.py 10.1021/jacs.5c05017
```

## Usage examples

(After install — paths assume the skill is at `<SKILL_DIR>`, e.g. `~/.claude/skills/ref-downloader/`. In source, `<SKILL_DIR>` = `skills/ref-downloader/`.)

### Input: a DOI

```powershell
python <SKILL_DIR>/scripts/run_ref_downloader.py 10.1021/jacs.5c05017
```

Default output: `<cwd>/jacs.5c05017_refs/jacs.5c05017/`

### Input: a local PDF (with DOI in metadata or in PDF text)

```powershell
python <SKILL_DIR>/scripts/run_ref_downloader.py "C:\path\to\your_paper.pdf"
```

Default output: `<pdf_dir>/your_paper_refs/<doi-derived-name>/`

### Custom output directory

```powershell
python <SKILL_DIR>/scripts/run_ref_downloader.py 10.1021/jacs.5c05017 --output-dir refs/
```

### Non-interactive (CI / batch)

```powershell
python <SKILL_DIR>/scripts/run_ref_downloader.py 10.1021/jacs.5c05017 --yes --auto
```

### Alternate config file

```powershell
python <SKILL_DIR>/scripts/run_ref_downloader.py 10.1021/jacs.5c05017 --config ./alt.toml
```

## Configuration

All configuration lives in `config.local.toml` (gitignored). Copy `config.example.toml` to bootstrap.

| Section | Key | Purpose |
|---|---|---|
| `[crossref]` | `mailto` | Your email — entry into Crossref polite pool |
| `[zotero]` | `db_path` | Optional path to `zotero.sqlite` for DOI lookup from PDF filename |
| `[browser]` | `edge_profile_dir` | Edge profile directory; empty = OS default |
| `[browser]` | `disable_extensions` | Set `true` to launch with `--disable-extensions` |
| `[institution]` | `auth_hosts` | Hostnames that mean "you got bounced to SSO" (e.g. `["sso.your-uni.edu"]`) |
| `[institution]` | `auth_url_fragments` | URL substrings indicating SSO (e.g. `["oauth", "saml"]`) |
| `[institution]` | `auth_page_titles` | `<title>` text for SSO pages (catches HTML served as PDF) |
| `[institution]` | `auth_loading_titles` | Loading-page titles (also reused for AIP/AVS publisher loading detection) |
| `[institution]` | `ignored_access_dois` | DOIs you know are paywalled at your institution; skipped without retry |

Environment variables override file values:

| Variable | Maps to |
|---|---|
| `REF_DOWNLOADER_MAILTO` | `crossref.mailto` |
| `REF_DOWNLOADER_ZOTERO_DB` | `zotero.db_path` |
| `REF_DOWNLOADER_EDGE_PROFILE` | `browser.edge_profile_dir` |
| `REF_DOWNLOADER_DISABLE_EXTENSIONS` | `browser.disable_extensions` (`1`/`true` to enable) |
| `REF_DOWNLOADER_CONFIG` | Path to alternate TOML file |

See [`skills/ref-downloader/config.example.toml`](skills/ref-downloader/config.example.toml) for full documentation.

### Alternative backend: CloakBrowser (optional, for Cloudflare-heavy sites)

**What it is.** [CloakBrowser](https://github.com/CloakHQ/CloakBrowser) is a third-party Python package by CloakHQ (MIT-licensed, available on [PyPI](https://pypi.org/project/cloakbrowser/) as `cloakbrowser`). It ships a patched Chromium build with source-level anti-fingerprint changes designed to look like a normal browser to common bot-detection layers (Cloudflare Turnstile, Radware, DataDome, FingerprintJS, etc). Its `launch_persistent_context_async()` API is intentionally compatible with Playwright's — that's what lets ref-downloader swap backends with a single env var instead of rewriting the download flow.

**Not a dependency of ref-downloader.** If you don't run `pip install cloakbrowser` it's never imported. The default Edge backend is unchanged. When CloakBrowser IS the active backend, ref-downloader uses Chromium under a **separate persistent profile** at `~/.local/cloakbrowser/profiles/ref-downloader` (or `REF_DOWNLOADER_CLOAK_PROFILE`), so your Edge profile is not touched — Edge does NOT need to be closed.

**When to use it.** Sites you'd reach for it on: CCS Chemistry (`10.31635`, Cloudflare-protected), some Elsevier paths gated by Radware, anything where the Edge backend keeps producing `manual_pending (radware_bot_manager)` or `failed (challenge_timeout)`. **Don't** reach for it as a default — the Edge backend is more reliable when your institutional access is the actual bottleneck, because Edge carries your authenticated cookies.

**Caveats.** CloakBrowser is **beta** third-party software; install + use at your own discretion (review its [repo](https://github.com/CloakHQ/CloakBrowser) before pulling it). It is **not a captcha solver** — interactive challenges still need you. It also does not carry your institutional cookies (separate profile), so it's most useful for open-Cloudflare sites, less useful for paywalled-but-license-covered refs.

```powershell
pip install cloakbrowser                              # one-time, separate from ref-downloader
$env:REF_DOWNLOADER_BROWSER = "cloak"
$env:REF_DOWNLOADER_CLOAK_HUMAN_PRESET = "careful"    # optional: slower mouse/scroll
python skills/ref-downloader/scripts/run_ref_downloader.py 10.31635/ccsorg...
```

CloakBrowser env vars (all optional):

| Variable | Default | Purpose |
|---|---|---|
| `REF_DOWNLOADER_BROWSER` | `edge` | Set to `cloak` (or `cloakbrowser`) to switch backend |
| `REF_DOWNLOADER_CLOAK_PROFILE` | `~/.local/cloakbrowser/profiles/ref-downloader` | Persistent Chromium profile path |
| `REF_DOWNLOADER_CLOAK_HUMANIZE` | `1` | `0`/`false` to disable humanized input |
| `REF_DOWNLOADER_CLOAK_HUMAN_PRESET` | `default` | `default` or `careful` (slower) |
| `REF_DOWNLOADER_CLOAK_PROXY` | _unset_ | HTTP/SOCKS proxy URL |
| `REF_DOWNLOADER_CLOAK_GEOIP` | auto | `1` to force GeoIP rerouting (auto when proxy is set) |
| `CLOAKBROWSER_PYTHONPATH` | _unset_ | sys.path hint for a local cloakbrowser source checkout |

Notes:
- **Edge does not need to be closed** when using the cloak backend — it uses its own Chromium.
- A fresh cloak profile may still hit Cloudflare/security pages on first visit — warm it manually with that profile before batch downloads.
- `human_preset=careful` reduces behavior-based detection but is **not** a captcha solver.
- cloakbrowser is NOT a hard dependency of ref-downloader. If you never set `REF_DOWNLOADER_BROWSER=cloak`, it's not imported.

## Architecture

Three-stage pipeline + a wrapper:

```
skills/ref-downloader/
├── SKILL.md                            agent runbook (slim entry)
├── references/agent-runbook.md         extended manual flow + DOI fallback
├── config.example.toml                 config schema (copy to config.local.toml)
└── scripts/
    ├── run_ref_downloader.py           entry — config + DOI resolution + sequencing
    │     └─> extract_refs.py    (1) Crossref API: fetch parent's reference list
    │     └─> validate_refs.py   (2) Crossref API: per-ref metadata + publisher classify
    │     └─> download_refs.py   (3) Playwright/Edge: download main PDF + SI per publisher
    └── _config.py                      TOML + env-var loader
```

You can also run the three scripts manually for debugging or partial restarts. See the agent runbook in [`skills/ref-downloader/references/agent-runbook.md`](skills/ref-downloader/references/agent-runbook.md) for the manual flow.

Agent users can install or inspect the packaged skill at [`skills/ref-downloader/SKILL.md`](skills/ref-downloader/SKILL.md). The repository root remains the human-facing Python project; the skill bundle is kept separate so Codex does not treat README, changelog, tests, and source files as always-associated skill context.

## Supported publishers

ACS, Nature, Science, Elsevier, Wiley, RSC, Springer, PNAS, ECS, IOP, AIP, AVS, IEEE, OSA, KPS, Beilstein, APS, Annual Reviews, Taylor & Francis, CCS Chemistry. Maturity varies — see [`docs/SUPPORTED_PUBLISHERS.md`](docs/SUPPORTED_PUBLISHERS.md) for the per-publisher tier table and known issues. CCS Chemistry sits behind Cloudflare; pair it with `REF_DOWNLOADER_BROWSER=cloak` for reliable access.

## Known limitations

- **Windows + Microsoft Edge only**: that's the verified path. macOS / Linux / Chromium support has not been tested. If you try, please open an issue with results.
- **Headed mode required**: empirically, `headless=True` yields empty results for Wiley / ACS supplementary downloads. The default is headed.
- **Edge must be fully closed before running**: Playwright needs exclusive access to the persistent profile. Check Task Manager for any background `msedge.exe` processes.
- **SSO redirects are detected, not solved**: when the script bounces to your institution's SSO, the ref becomes `manual_pending` so you can sign in interactively. Configure `[institution]` to teach it which redirects to recognize.
- **SI download is the most fragile path**: main PDFs are reliable; SI lookup varies by publisher and is the area most likely to need a tweak when a publisher updates their site.
- **Paywalled content needs institutional access**: this is not a bypass tool.
- **Crossref dependency**: papers with no reference list deposited at Crossref can't be processed automatically.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidance on:
- Adding a new publisher (DOI prefix → strategy)
- Adding institutional SSO patterns
- Reporting download failures with useful logs

## Security

This tool launches your real Edge profile, with all your cookies and saved sessions. Read [SECURITY.md](SECURITY.md) before running it against a profile you also use for daily browsing.

## License

MIT — see [LICENSE](LICENSE).
