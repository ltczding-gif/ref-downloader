# Supported Publishers

This document describes which publishers `ref-downloader` recognizes, the
download strategy used for each, and the maturity tier.

## DOI prefix → publisher key

`validate_refs.py` classifies references by DOI prefix:

| Prefix | Key | Typical journals |
|---|---|---|
| `10.1038` | `nature` | Nature, Nature Energy, Nature Catalysis, Nature Materials |
| `10.1021` | `acs` | JACS, ACS Catalysis, Nano Letters, J. Phys. Chem. |
| `10.1126` | `science` | Science, Science Advances |
| `10.1016` | `elsevier` | Journal of Power Sources, Thin Solid Films, Electrochim. Acta |
| `10.1002` | `wiley` | Angewandte Chemie, Advanced Materials, ChemSusChem |
| `10.1039` | `rsc` | Journal of Materials Chemistry, Energy & Environmental Sci. |
| `10.1007` | `springer` | Springer journals |
| `10.1073` | `pnas` | PNAS |
| `10.1149` | `ecs` | Journal of The Electrochemical Society |
| `10.1088` | `iop` | IOP journals |
| `10.1063` | `aip` | Applied Physics Letters, Journal of Applied Physics |
| `10.1109` | `ieee` | IEEE conferences and journals |
| `10.1364` | `osa` | Optical Materials Express, Optics Express |
| `10.1103` | `aps` | Physical Review |
| `10.1080` | `tandfonline` | Taylor & Francis |
| `10.1116` | `avs` | J. Vac. Sci. & Technol. A/B (American Vacuum Society) |
| `10.1143` | `iop` | Japanese Journal of Applied Physics (now IOP-hosted) |
| `10.1147` | `springer` | IBM Journal of Research and Development |
| `10.1146` | `annualreviews` | Annual Reviews |
| `10.3938` | `kps` | Korean Physical Society |
| `10.3762` | `beilstein` | Beilstein Journals |
| `10.31635` | `ccs` | CCS Chemistry (Chinese Chemical Society) — Cloudflare-protected, needs `REF_DOWNLOADER_BROWSER=cloak` for reliable access |

Journal-name fragments give an additional override path for cases where the
DOI prefix is shared by multiple imprints (see `JOURNAL_PUBLISHER_MAP` in
`validate_refs.py`).

## Strategy tiers

`download_refs.py` groups publishers into three tiers based on how confident
we are in the download path:

### `specialized` (specialized download flow, well-tested)

| Publisher | Strategy | Notes |
|---|---|---|
| `wiley` | `specialized_wiley` | Opens the article page, then attempts `pdfdirect` from the page context. PDF main path is reliable; SI is the weakest area in the system. |
| `elsevier` | `specialized_elsevier` | Article page → `View PDF` as popup/new-tab; reuses live page + tokenized `main.pdf` candidates. v0.3.0 added a popup state machine (`wait_for_elsevier_pdf_button_ready` / `wait_for_elsevier_popup_after_click` / `wait_for_elsevier_popup_surface_ready`) that polls for actual UI readiness instead of fixed sleeps, and re-clicks at 10s if the popup is still on `about:blank`. Hot-session auto-retry covers `crasolve_shell` / `viewer_capture_failed` in interactive mode; `--auto` mode adds an async retry queue (60s later) for the same transient states. |
| `aip`, `avs` | `specialized_loading_wait` | Pubs.aip.org serves a `请稍候` (Please wait) loading page; the script waits up to 15s for it to resolve before searching for PDF links. The loading-page string is hardcoded and intentional — see CONTRIBUTING.md. |

### `generic_fallback` (works via the common pattern)

These publishers are downloaded via:

```
direct_pdf_url → article_url → PDF_SELECTORS → viewer fetch
```

Publishers in this tier:

`acs`, `nature`, `science`, `rsc`, `springer`, `pnas`, `ieee`, `osa`, `kps`

The flow is:
1. Try the publisher's direct PDF URL pattern (e.g., `link.springer.com/content/pdf/{doi}.pdf` for Springer).
2. If the direct URL fails or 403s, navigate to the article landing page.
3. Find the PDF link using publisher-specific CSS selectors.
4. Fetch the PDF either by HTTP request or by navigating into the viewer.

### `weak` (recognized but no specialized path; mostly DOI/article fallback)

| Publisher | Note |
|---|---|
| `aps` | Physical Review — `direct_pdf_url` now constructs `journals.aps.org/<slug>/pdf/<doi>` for 9 APS journals (PhysRevB, PhysRevLett, RevModPhys, etc) via `aps_pdf_url_from_doi`, bypassing the JS-driven landing page. Non-listed APS journals (e.g. niche subseries) still fall through to the article-page flow. |
| `annualreviews` | `href="#"` links that JS-open viewer/popup; covered by the popup hotpath but fragile |
| `tandfonline` | Taylor & Francis; relies on the generic flow |
| `beilstein` | Open-access; usually works via direct URL |
| `ccs` | CCS Chemistry (Chinese Chemical Society) — site sits behind Cloudflare. Generic flow may be blocked; use `REF_DOWNLOADER_BROWSER=cloak` for reliable access. SI capture not yet specialized. |

### `specialized but weakly validated`

| Publisher | Note |
|---|---|
| `ecs` | ECS Digital Library has a Radware barrier; barrier-aware routing exists but isn't fully validated under all conditions |
| `iop` | IOP family routing is implemented but the non-barrier branch isn't exhaustively tested |

## What "supported" means

Two things must be true for a publisher to be "supported":

1. `validate_refs.py` recognizes the DOI prefix → assigns a publisher key
   (visible in `refs_validated.json`).
2. `download_refs.py` has a verified download strategy for that key.

A publisher might pass step 1 but not step 2 (recognized but no specialized
path) — refs from that publisher will go through `generic_fallback` and
often work, but timing or selector tuning may be needed for full reliability.

## Operational notes

- **Verified environment**: headed Microsoft Edge on Windows. `headless=True`
  empirically yields empty SI for Wiley / ACS — keep headed.
- **Wiley**: main PDF path is corrected, but auto success depends on the
  challenge state of the live session.
- **Elsevier**: hot-session automation is strong in interactive mode; the
  popup state machine (v0.3.0) handles most transient `about:blank` /
  unready-button cases. Refs that still hit `manual_pending (elsevier_*)`
  in `--auto` mode get a single async retry attempt ~60s later — see
  [architecture.md](architecture.md#auto-mode-manual-retry-queue-v030).
- **Annual Reviews**: `href="#"` JS-driven navigation is now covered by the
  generic popup/viewer hotpath; no longer requires explicit `target="_blank"`.
- **Wiley SI**: `downloadSupplement` path has a longer post-challenge wait
  baked in, but SI overall remains the most fragile code path.
- Before any run, all `msedge.exe` background processes must be killed —
  otherwise Playwright's persistent profile launch fails immediately.

## Adding a publisher

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the step-by-step process of
adding a new DOI prefix and download strategy.
