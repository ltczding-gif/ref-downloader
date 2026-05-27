#!/usr/bin/env python3
"""
Script 3: Download PDFs and SI using a real Microsoft Edge persistent profile.

Operational notes:
- Uses the user's normal Edge profile directory (`User Data\\Default`), not a fresh temp profile.
- Extensions stay enabled by default; set `REF_DOWNLOADER_DISABLE_EXTENSIONS=1` only when
  you explicitly want the older "disable extensions" behavior for debugging.
- The most reliable mode is headed interactive Edge. `--auto` is best treated as a smoke run,
  not as the primary workflow for sites that need challenge solving or institutional login.
- Root `download_report.csv` is written on graceful completion. If a run is interrupted midway,
  use the latest project-scoped `OUTPUT_DIR\\runs\\<timestamp>-round-03\\events.jsonl` plus the
  actual downloaded files as the source of truth.

Usage:
  python download_refs.py <project_name>
  python download_refs.py <path/to/refs_validated.json>

Example:
  python download_refs.py jacs.5c05017

IMPORTANT: Close ordinary Microsoft Edge windows before running so Playwright can open the
same persistent profile with exclusive access.

Requires:
  pip install playwright
  playwright install chromium   # (Edge will be used via channel="msedge")
"""

import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import asyncio
import base64
import contextvars
import csv
import html as html_lib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urljoin, urlparse

from playwright.async_api import BrowserContext, Page, async_playwright, Error as PlaywrightError

from functools import lru_cache

from _config import load_config, InstitutionConfig

# Institution-specific patterns are loaded from config.local.toml at startup
# via init_institution_config(). Stays empty for vanilla open-internet use.
# The 5 institution getters + get_edge_user_data_dir below are @lru_cache(1) to
# eliminate per-ref tuple/set/dict rebuild in download hot loops.
#
# Cache invariant: caches are populated at first call AFTER init_institution_config()
# fires. init_institution_config() also clears the cache for get_edge_user_data_dir
# even though that one reads [browser] not [institution] — this works because
# load_config() itself is uncached, so a fresh read after cache_clear sees current
# TOML / env var values. Prefer the simpler "init clears all caches" rule over
# splitting into per-section init functions.
#
# WARNING: any future mid-run reload of config.local.toml MUST also call
# .cache_clear() on every getter listed below, or the run will silently use
# stale values. There is no automatic file-watcher today; if you add one,
# extend init_institution_config or factor a shared `_clear_runtime_caches()`.
_INSTITUTION: InstitutionConfig = InstitutionConfig()


def init_institution_config(cfg_institution: InstitutionConfig) -> None:
    """Set institution patterns from config; called once from main().

    Also clears lru_caches on all 6 runtime-config getters so any read
    AFTER this call sees fresh values. Pre-init reads (which would be a
    contract violation by the caller) cache empty defaults; init then
    invalidates them.

    The cache_clear for `get_edge_user_data_dir` lives here despite reading
    [browser] (not [institution]) — see WARNING comment above for rationale.
    """
    global _INSTITUTION
    _INSTITUTION = cfg_institution
    _auth_hosts.cache_clear()
    _auth_url_fragments.cache_clear()
    _auth_page_titles.cache_clear()
    _auth_loading_titles.cache_clear()
    ignored_institution_access_dois.cache_clear()
    get_edge_user_data_dir.cache_clear()


@lru_cache(maxsize=1)
def get_edge_user_data_dir() -> str:
    """Resolve Edge profile dir at call time. Order:
        1. config.browser.edge_profile_dir if non-empty
        2. %LOCALAPPDATA%\\Microsoft\\Edge\\User Data (Windows default)

    On non-Windows OSes there is no built-in default — the user must set
    config.browser.edge_profile_dir or REF_DOWNLOADER_EDGE_PROFILE.
    """
    cfg = load_config()
    if cfg.browser.edge_profile_dir:
        return cfg.browser.edge_profile_dir
    if sys.platform != "win32":
        raise SystemExit(
            "ERROR: No Edge profile directory configured.\n"
            "  This tool's auto-default for the Edge profile only works on Windows.\n"
            "  On macOS/Linux you must set one of:\n"
            "    - browser.edge_profile_dir in config.local.toml\n"
            "    - REF_DOWNLOADER_EDGE_PROFILE environment variable\n"
            "  See README.md and config.example.toml for details."
        )
    return os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")


@lru_cache(maxsize=1)
def ignored_institution_access_dois() -> frozenset:
    return frozenset(_INSTITUTION.ignored_access_dois)


@lru_cache(maxsize=1)
def _auth_hosts() -> tuple:
    return tuple(_INSTITUTION.auth_hosts)


@lru_cache(maxsize=1)
def _auth_url_fragments() -> tuple:
    return tuple(_INSTITUTION.auth_url_fragments)


@lru_cache(maxsize=1)
def _auth_page_titles() -> tuple:
    return tuple(_INSTITUTION.auth_page_titles)


@lru_cache(maxsize=1)
def _auth_loading_titles() -> tuple:
    return tuple(_INSTITUTION.auth_loading_titles)

# ── Config ────────────────────────────────────────────────────────────────────
# These timeouts deliberately bias toward interactive stability instead of pure throughput.
# Publisher-specific hot-session and viewer-settle knobs live here so they are easy to tune
# after observing real runs.
DELAY        = 1.0        # seconds between articles
NAV_TIMEOUT  = 5_000      # page navigation timeout (ms)
DL_TIMEOUT   = 10_000     # download wait timeout (ms)
RESPONSE_BODY_TIMEOUT = 8_000
CAPTCHA_WAIT = 10_000     # 10s — auth walls need re-login, not just waiting
AUTO_CAPTCHA_WAIT = 15_000
AUTO_MANUAL_RETRY_WAIT = 60_000
AUTO_MANUAL_RETRY_TIMEOUT = 20_000
AUTO_MANUAL_RETRY_MAX_CONCURRENT = 3
AUTO_MANUAL_RETRY_MAX_PENDING = 8
AUTO_MANUAL_FINAL_DRAIN_TIMEOUT = 25_000
PDF_JS_FETCH_TIMEOUT = 20_000
GENERIC_POPUP_TIMEOUT = 3_500
MANUAL_QUEUE_LIMIT_DEFAULT = 3
MANUAL_QUEUE_LIMIT_BY_PUBLISHER = {
    "elsevier": 1,
}
MANUAL_RESUME_WAIT_MS_DEFAULT = 4_000
MANUAL_RESUME_WAIT_MS_BY_PUBLISHER = {
    "elsevier": 12_000,
}
ELSEVIER_HOT_WINDOW_SECONDS = 480
ELSEVIER_HOT_AUTO_RETRY_REASONS = {
    "elsevier_crasolve_shell",
    "viewer_capture_failed",
    "elsevier_pdf_security_verification",
}
ELSEVIER_TRANSIENT_POPUP_REASONS = {
    "elsevier_crasolve_shell",
    "elsevier_pdf_security_verification",
}
ELSEVIER_PRE_CLICK_MIN_WAIT_MS = 8_000
ELSEVIER_PRE_CLICK_MAX_WAIT_MS = 10_000
ELSEVIER_POPUP_POLL_MS = 15_000
ELSEVIER_POPUP_SETTLE_MS = 20_000
ELSEVIER_POPUP_CAPTURE_WAIT_MS = 8_000
DIRECT_CAPTURE_WAIT_CYCLES_DEFAULT = 15
SESSION_RESTART_LIMIT_PER_REF = 1

RUNS_DIR_NAME = "runs"
ROUND_NAME = "round-03"

# CloakBrowser (optional alt backend): when REF_DOWNLOADER_BROWSER=cloak, the script
# uses cloakbrowser (stealth Chromium) instead of Microsoft Edge. The cloakbrowser
# package is NOT a dependency — users install it themselves. We import lazily inside
# launch_edge_context so users who never set REF_DOWNLOADER_BROWSER=cloak don't
# need the package at all. The default profile path is Path.home()-derived so it
# works cross-platform; override with REF_DOWNLOADER_CLOAK_PROFILE.
CLOAKBROWSER_DEFAULT_PROFILE = str(Path.home() / ".local" / "cloakbrowser" / "profiles" / "ref-downloader")

STATUS_DOWNLOADED = "downloaded"
STATUS_FAILED = "failed_auto"
STATUS_MANUAL = "manual_pending"
STATUS_ALREADY_EXISTS = "already_exists"
STATUS_NOT_FOUND = "not_found"
STATUS_MANUAL_RESOLVED = "manual_resolved"
STATUS_IGNORED = "ignored"
SESSION_CLOSED_TOKENS = (
    "target page, context or browser has been closed",
    "target closed",
    "browsercontext.new_page: target page, context or browser has",
    "page.goto: target page, context or browser has been closed",
    "page.wait_for_timeout: target page, context or browser has been closed",
)


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def is_auto_mode() -> bool:
    return "--auto" in sys.argv


def selected_browser_backend() -> str:
    """Return the active browser backend: "edge" (default) or "cloak".

    Set REF_DOWNLOADER_BROWSER=cloak (or cloakbrowser) to switch to stealth Chromium
    for Cloudflare/Radware-heavy sites. The cloakbrowser package must be installed
    separately — it's not a hard dependency of ref-downloader.

    Unknown values fall back to "edge" with a stderr warning so typos like "cloack"
    don't silently mask intent.
    """
    raw = os.environ.get("REF_DOWNLOADER_BROWSER", "").strip()
    value = raw.lower()
    if value in ("cloak", "cloakbrowser"):
        return "cloak"
    if value and value not in ("edge", "msedge"):
        print(
            f"WARNING: REF_DOWNLOADER_BROWSER={raw!r} is not a recognized backend; "
            "falling back to 'edge'. Valid values: edge, cloak.",
            file=sys.stderr,
        )
    return "edge"


def using_cloakbrowser() -> bool:
    return selected_browser_backend() == "cloak"


def selected_cloak_human_preset() -> str:
    """Return the cloakbrowser humanize preset: "default" or "careful".

    "careful" uses slower mouse/keyboard/scroll timings — helps lower the chance of
    behavior-based challenges, but it's not a captcha solver.
    """
    value = os.environ.get("REF_DOWNLOADER_CLOAK_HUMAN_PRESET", "default").strip().lower()
    if value in ("default", "careful"):
        return value
    return "default"


def cloak_si_fast_human_config() -> Dict[str, Any]:
    """Aggressive scroll-pause config for SI capture under cloakbrowser.

    SI links sit deep in long pages; the default human preset is too slow for batch
    SI collection. This config keeps the cloak stealth surface but cuts idle gaps.
    """
    return {
        "idle_between_actions": False,
        "scroll_delta_base": (260, 420),
        "scroll_pause_fast": (8, 25),
        "scroll_pause_slow": (25, 75),
        "scroll_accel_steps": (1, 1),
        "scroll_decel_steps": (1, 1),
        "scroll_settle_delay": (80, 180),
        "scroll_pre_move_delay": (20, 60),
    }


# Shared run-level state. `manual_pages` are the live pages still awaiting attention;
# `manual_retry_pages` temporarily protects the current retry batch from being closed by
# unrelated page-cleanup logic during mixed publisher queues. In auto mode,
# `auto_manual_pages` are delayed retries that must not block the main ref loop.
RUN_CTX: Dict[str, Any] = {
    "run_dir": None,
    "events_path": None,
    "manual_pages": [],
    "manual_retry_pages": [],
    "active_main_pages": [],
    "auto_manual_pages": [],
    "auto_manual_active_pages": [],
    "auto_manual_tasks": set(),
    "auto_manual_results": [],
    "auto_manual_sem": None,
    "manual_deferred": False,
    "current_ref": None,
    "round_id": None,
    "elsevier_hot_until": 0.0,
    "elsevier_hot_reason": "",
}
CURRENT_REF: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar("current_ref", default=None)

PUBLISHER_STRATEGIES: Dict[str, Dict[str, str]] = {
    "acs": {"family": "generic_fallback", "support": "stable", "min_test": "live_or_route_smoke"},
    "nature": {"family": "generic_fallback", "support": "stable", "min_test": "route_selector_smoke"},
    "science": {"family": "generic_fallback", "support": "stable", "min_test": "viewer_regression_smoke"},
    "elsevier": {"family": "specialized_elsevier", "support": "specialized", "min_test": "live_pdfft_shell"},
    "wiley": {"family": "specialized_wiley", "support": "specialized", "min_test": "live_pdfdirect"},
    "rsc": {"family": "generic_fallback", "support": "stable", "min_test": "route_selector_smoke"},
    "springer": {"family": "generic_fallback", "support": "stable", "min_test": "route_selector_smoke"},
    "pnas": {"family": "generic_fallback", "support": "stable", "min_test": "route_selector_smoke"},
    "ecs": {"family": "specialized_iop_family", "support": "weak", "min_test": "live_barrier_or_viewer"},
    "iop": {"family": "specialized_iop_family", "support": "weak", "min_test": "live_barrier_or_viewer"},
    "aps": {"family": "generic_fallback", "support": "weak", "min_test": "strategy_coverage_smoke"},
    "annualreviews": {"family": "generic_fallback", "support": "weak", "min_test": "strategy_coverage_smoke"},
    "tandfonline": {"family": "generic_fallback", "support": "weak", "min_test": "strategy_coverage_smoke"},
    "aip": {"family": "specialized_loading_wait", "support": "specialized", "min_test": "loading_page_smoke"},
    "avs": {"family": "specialized_loading_wait", "support": "specialized", "min_test": "loading_page_smoke"},
    "ieee": {"family": "generic_fallback", "support": "stable", "min_test": "route_selector_smoke"},
    "osa": {"family": "generic_fallback", "support": "stable", "min_test": "route_selector_smoke"},
    "kps": {"family": "generic_fallback", "support": "stable", "min_test": "route_selector_smoke"},
    "beilstein": {"family": "generic_fallback", "support": "weak", "min_test": "strategy_coverage_smoke"},
}


class ManualInterventionRequired(Exception):
    def __init__(self, reason: str, url: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.url = url


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def current_ref_meta() -> Dict[str, Any]:
    ref = CURRENT_REF.get() or RUN_CTX.get("current_ref") or {}
    return {
        "ref_id": ref.get("id"),
        "label": ref.get("label"),
        "doi": ref.get("doi"),
        "publisher": ref.get("publisher"),
    }


def make_attempt(state: str, reason: str = "", size_kb: Optional[int] = None) -> Dict[str, Any]:
    return {"state": state, "reason": reason, "size_kb": size_kb}


async def response_body_with_timeout(resp, timeout_ms: int = RESPONSE_BODY_TIMEOUT):
    return await asyncio.wait_for(resp.body(), timeout=timeout_ms / 1000)


def with_auto_retry_result(original_reason: str, retry_attempt: Dict[str, Any]) -> Dict[str, Any]:
    if retry_attempt["state"] == STATUS_DOWNLOADED:
        return retry_attempt
    attempt = make_attempt(STATUS_MANUAL, original_reason)
    attempt["auto_retry_done"] = True
    attempt["auto_retry_state"] = retry_attempt["state"]
    attempt["auto_retry_reason"] = retry_attempt.get("reason", "")
    return attempt


def manual_pending_reason(attempt: Dict[str, Any]) -> str:
    reason = attempt.get("reason", "")
    if attempt.get("auto_retry_done"):
        return (
            f"{reason}; auto_retry="
            f"{attempt.get('auto_retry_state', STATUS_FAILED)}:{attempt.get('auto_retry_reason', '')}"
        )
    if attempt.get("auto_retry_scheduled"):
        return f"{reason}; auto_retry=scheduled"
    if attempt.get("auto_retry_dropped"):
        return f"{reason}; auto_retry=dropped:{attempt.get('auto_retry_dropped')}"
    return reason


def with_auto_retry_page(original_reason: str, page: Page) -> Dict[str, Any]:
    attempt = make_attempt(STATUS_MANUAL, original_reason)
    attempt["auto_retry_page"] = page
    return attempt


def publisher_strategy(publisher: str) -> Dict[str, str]:
    return PUBLISHER_STRATEGIES.get(
        publisher,
        {"family": "generic_fallback", "support": "unknown", "min_test": "strategy_coverage_smoke"},
    )


def append_history_text(existing: str, status: str) -> str:
    status = (status or "").strip()
    if not status:
        return existing or ""
    parts = [p.strip() for p in (existing or "").split(" || ") if p.strip()]
    if not parts or parts[-1] != status:
        parts.append(status)
    return " || ".join(parts)


def finalize_report_row(row: Dict[str, Any]) -> Dict[str, Any]:
    row.setdefault("publisher_strategy", publisher_strategy(row.get("publisher", "")).get("family", "generic_fallback"))
    row.setdefault("publisher_support", publisher_strategy(row.get("publisher", "")).get("support", "unknown"))
    row.setdefault("publisher_min_test", publisher_strategy(row.get("publisher", "")).get("min_test", "strategy_coverage_smoke"))
    row.setdefault("retry_count", 0)
    row.setdefault("session_restarts", 0)
    row.setdefault("session_last_error", "")
    row["pdf_history"] = append_history_text(row.get("pdf_history", ""), row.get("pdf_status", ""))
    row["si_history"] = append_history_text(row.get("si_history", ""), row.get("si_status", ""))
    return row


def merge_report_rows(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(new)
    merged["retry_count"] = int(old.get("retry_count") or 0) + 1
    merged["session_restarts"] = int(old.get("session_restarts") or 0) + int(new.get("session_restarts") or 0)
    merged["session_last_error"] = new.get("session_last_error") or old.get("session_last_error", "")
    merged["pdf_history"] = append_history_text(old.get("pdf_history", ""), new.get("pdf_status", ""))
    merged["si_history"] = append_history_text(old.get("si_history", ""), new.get("si_status", ""))
    merged["publisher_strategy"] = old.get("publisher_strategy") or new.get("publisher_strategy")
    merged["publisher_support"] = old.get("publisher_support") or new.get("publisher_support")
    merged["publisher_min_test"] = old.get("publisher_min_test") or new.get("publisher_min_test")
    return finalize_report_row(merged)


def unique_preserve_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def ensure_run_dir(base_dir: Path) -> Path:
    runs_root = Path(base_dir) / RUNS_DIR_NAME
    runs_root.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now().strftime('%Y-%m-%d-%H%M%S')}-{ROUND_NAME}"
    candidate = runs_root / stem
    idx = 1
    while candidate.exists():
        idx += 1
        candidate = runs_root / f"{stem}-{idx:02d}"
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def init_run_artifacts(project_dir: Path, validated_path: Path, total_refs: int, auto_mode: bool) -> Path:
    run_dir = ensure_run_dir(project_dir.parent)
    events_path = run_dir / "events.jsonl"
    RUN_CTX["run_dir"] = run_dir
    RUN_CTX["events_path"] = events_path
    RUN_CTX["manual_pages"] = []
    RUN_CTX["manual_retry_pages"] = []
    RUN_CTX["manual_deferred"] = False
    RUN_CTX["round_id"] = run_dir.name

    plan_path = run_dir / "plan.md"
    plan_path.write_text(
        "\n".join(
            [
                "# Round 3 Run Plan",
                "",
                f"- Started at: {now_iso()}",
                f"- Project: `{project_dir}`",
                f"- Input: `{validated_path}`",
                f"- Total verified refs: {total_refs}",
                f"- Auto mode: {auto_mode}",
                "- Scope:",
                "  - Wiley main-PDF candidate filtering",
                "  - Elsevier crasolve canonical article fallback",
                "  - Science SI viewer stabilization and one safe retry",
                "  - more specific postmortem buckets",
                "- This run is one full code-change round. No mid-run patching.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return run_dir


def log_event(stage: str, action: str, result: str, url: str = "", detail: str = "", extra: Optional[Dict[str, Any]] = None):
    events_path = RUN_CTX.get("events_path")
    if not events_path:
        return
    payload: Dict[str, Any] = {
        "timestamp": now_iso(),
        "stage": stage,
        "action": action,
        "result": result,
        "url": url,
        "detail": detail,
    }
    payload.update(current_ref_meta())
    if extra:
        payload.update(extra)
    serialized = json.dumps(payload, ensure_ascii=False) + "\n"
    last_error: Optional[Exception] = None

    for delay in (0.0, 0.05, 0.1, 0.2):
        if delay:
            time.sleep(delay)
        try:
            with open(events_path, "a", encoding="utf-8") as f:
                f.write(serialized)
            return
        except PermissionError as e:
            last_error = e
            continue
        except Exception as e:
            last_error = e
            break

    run_dir = RUN_CTX.get("run_dir")
    if run_dir:
        fallback_path = Path(run_dir) / "events-fallback.jsonl"
        payload["_log_fallback"] = True
        payload["_log_error"] = str(last_error)[:160] if last_error else "unknown"
        try:
            with open(fallback_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            return
        except Exception:
            pass

    print(f"[log_event warning] {action} {result} {type(last_error).__name__ if last_error else 'unknown'}")


def classify_status_text(status: str) -> str:
    sl = (status or "").lower()
    if "ignored_institution_access" in sl:
        return "ignored institution access"
    if "false_positive_guard" in sl:
        return "false positive guard"
    if "wiley_candidate_rejected" in sl:
        return "wiley candidate rejected"
    if "elsevier_crasolve_shell" in sl or "elsevier_article_reopen_failed" in sl:
        return "elsevier crasolve shell"
    if "elsevier_pdf_security_verification" in sl:
        return "elsevier pdf security verification"
    if "elsevier_content_error" in sl:
        return "elsevier content error"
    if "request_rejected_page" in sl:
        return "request rejected page"
    if "radware_bot_manager" in sl:
        return "radware bot manager"
    if "cloudflare_challenge_page" in sl:
        return "cloudflare challenge"
    if "science_viewer_navigation_race" in sl:
        return "science viewer navigation race"
    if "manual_pending" in sl and "auth" in sl:
        return "auth redirect"
    if "manual_pending" in sl and "captcha" in sl:
        return "challenge/captcha"
    if "manual_pending" in sl:
        return "manual pending"
    if "missing_eof" in sl or "missing_pdf_header" in sl:
        return "suspicious pdf content"
    if "supplementary_detected" in sl:
        return "wrong document type"
    if "non_pdf_asset_saved" in sl:
        return "non-pdf asset saved"
    if "http 403" in sl or "403" in sl:
        return "direct fetch 403"
    if "no_pdf_button_found" in sl:
        return "selector miss"
    if "page_context_fetch_empty" in sl:
        return "page-context fetch empty"
    if "no_pdf_captured" in sl or "viewer_fetch_empty" in sl:
        return "viewer fetch empty"
    if "auth_redirect" in sl:
        return "auth redirect"
    if "captcha" in sl or "challenge" in sl:
        return "challenge/captcha"
    if "not_found" in sl:
        return "not found"
    return "unknown"


def load_run_events_by_ref() -> Dict[int, List[Dict[str, Any]]]:
    events_path = RUN_CTX.get("events_path")
    grouped: Dict[int, List[Dict[str, Any]]] = {}
    if not events_path or not Path(events_path).exists():
        return grouped

    with open(events_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            ref_id = event.get("ref_id")
            if ref_id is None:
                continue
            grouped.setdefault(int(ref_id), []).append(event)
    return grouped


def classify_ref_issue(row: Dict[str, Any], events: List[Dict[str, Any]]) -> str:
    doi = (row.get("doi") or "").lower()
    if doi in ignored_institution_access_dois():
        return "ignored institution access"

    results = {(evt.get("result") or "").lower() for evt in events}
    details = " ".join((evt.get("detail") or "").lower() for evt in events)
    urls = " ".join((evt.get("url") or "").lower() for evt in events)

    if "false_positive_guard" in results:
        return "false positive guard"
    if "wiley_candidate_rejected" in results:
        return "wiley candidate rejected"
    if "elsevier_crasolve_shell" in results or "crasolve=1" in urls:
        return "elsevier crasolve shell"
    if "elsevier_pdf_security_verification" in results:
        return "elsevier pdf security verification"
    if "elsevier_content_error" in results:
        return "elsevier content error"
    if "request_rejected_page" in results:
        return "request rejected page"
    if "radware_bot_manager" in results:
        return "radware bot manager"
    if "cloudflare_challenge_page" in results:
        return "cloudflare challenge"
    if "science_viewer_navigation_race" in results or "execution context was destroyed" in details:
        return "science viewer navigation race"

    bucket = classify_status_text(row["pdf_status"])
    if bucket == "unknown":
        bucket = classify_status_text(row["si_status"])
    return bucket


def write_postmortem(report: List[Dict[str, Any]], project_dir: Path, report_path: Path):
    run_dir = RUN_CTX.get("run_dir")
    if not run_dir:
        return

    manual = [r for r in report if "manual_pending" in r["pdf_status"] or "manual_pending" in r["si_status"]]
    failed = [r for r in report if "failed" in r["pdf_status"] or "failed" in r["si_status"]]
    ignored = [r for r in report if "ignored" in r["pdf_status"] or "ignored" in r["si_status"]]
    grouped: Dict[str, List[str]] = {}
    events_by_ref = load_run_events_by_ref()

    for row in failed + manual + ignored:
        bucket = classify_ref_issue(row, events_by_ref.get(int(row["id"]), []))
        grouped.setdefault(bucket, []).append(f"[{row['id']:02d}] {row['label']}")

    lines = [
        "# Postmortem",
        "",
        f"- Finished at: {now_iso()}",
        f"- Project: `{project_dir}`",
        f"- Root report: `{report_path}`",
        f"- Run dir: `{run_dir}`",
        "",
        "## Totals",
        f"- Total refs: {len(report)}",
        f"- Main downloaded/already exists: {sum(1 for r in report if any(x in r['pdf_status'] for x in ('downloaded', 'exists')))}",
        f"- Main failed: {sum(1 for r in report if 'failed' in r['pdf_status'])}",
        f"- Main manual pending: {sum(1 for r in report if 'manual_pending' in r['pdf_status'])}",
        f"- Main ignored: {sum(1 for r in report if 'ignored' in r['pdf_status'])}",
        f"- SI downloaded/already exists: {sum(1 for r in report if any(x in r['si_status'] for x in ('downloaded', 'exists', 'non_pdf_asset_saved')))}",
        f"- SI manual pending: {sum(1 for r in report if 'manual_pending' in r['si_status'])}",
        f"- SI ignored: {sum(1 for r in report if 'ignored' in r['si_status'])}",
        "",
        "## Grouped Issues",
    ]
    if grouped:
        for bucket, refs in grouped.items():
            lines.append(f"- {bucket}: {', '.join(refs)}")
    else:
        lines.append("- none")

    if manual:
        lines.extend(["", "## Manual Pending"])
        for row in manual:
            lines.append(f"- [{row['id']:02d}] {row['label']}: pdf={row['pdf_status']} | si={row['si_status']}")

    retried = [r for r in report if int(r.get("retry_count") or 0) > 0]
    if retried:
        lines.extend(["", "## Retry History"])
        for row in retried:
            lines.append(
                f"- [{row['id']:02d}] {row['label']}: retries={row.get('retry_count', 0)}"
                f" | pdf_history={row.get('pdf_history', '')}"
                f" | si_history={row.get('si_history', '')}"
            )

    (run_dir / "postmortem.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def copy_report_to_run_dir(report_path: Path):
    run_dir = RUN_CTX.get("run_dir")
    if not run_dir or not report_path.exists():
        return
    shutil.copy(report_path, run_dir / report_path.name)


def preserved_pages() -> List[Page]:
    pages = []
    for bucket in ("manual_pages", "manual_retry_pages"):
        for item in RUN_CTX.get(bucket, []):
            page = item.get("page")
            if page and not page.is_closed() and page not in pages:
                pages.append(page)
    return pages


def clear_manual_retry_pages():
    RUN_CTX["manual_retry_pages"] = []


def set_manual_retry_pages(items: List[Dict[str, Any]]):
    RUN_CTX["manual_retry_pages"] = [item for item in items if item.get("page") and not item["page"].is_closed()]


def clean_manual_pages() -> List[Dict[str, Any]]:
    live_items = []
    for item in RUN_CTX.get("manual_pages", []):
        page = item.get("page")
        if page and not page.is_closed():
            live_items.append(item)
    RUN_CTX["manual_pages"] = live_items
    RUN_CTX["manual_retry_pages"] = [
        item for item in RUN_CTX.get("manual_retry_pages", [])
        if item.get("page") and not item["page"].is_closed()
    ]
    if not live_items:
        RUN_CTX["manual_deferred"] = False
    return live_items


def manual_queue_limit() -> int:
    live_items = clean_manual_pages()
    limit = MANUAL_QUEUE_LIMIT_DEFAULT
    for item in live_items:
        publisher = item.get("publisher") or ""
        limit = min(limit, MANUAL_QUEUE_LIMIT_BY_PUBLISHER.get(publisher, MANUAL_QUEUE_LIMIT_DEFAULT))
    return limit


def is_session_closed_error(exc_or_msg: Any) -> bool:
    lowered = lower_unquoted(str(exc_or_msg))
    return any(token in lowered for token in SESSION_CLOSED_TOKENS)


def clear_manual_queue(reason: str):
    dropped = len(RUN_CTX.get("manual_pages") or [])
    RUN_CTX["manual_pages"] = []
    RUN_CTX["manual_retry_pages"] = []
    RUN_CTX["manual_deferred"] = False
    if dropped:
        log_event("manual_queue", "session_reset", "cleared", "", f"{reason} | dropped={dropped}")


def mark_elsevier_hot(reason: str):
    RUN_CTX["elsevier_hot_until"] = time.monotonic() + ELSEVIER_HOT_WINDOW_SECONDS
    RUN_CTX["elsevier_hot_reason"] = reason
    log_event("elsevier_session", "heat", "active", "", f"{reason} ttl_s={ELSEVIER_HOT_WINDOW_SECONDS}")


def elsevier_session_is_hot() -> bool:
    return time.monotonic() < float(RUN_CTX.get("elsevier_hot_until") or 0.0)


def should_auto_retry_elsevier_queue(items: List[Dict[str, Any]], auto_retried_ids: set[int]) -> bool:
    if not items or not elsevier_session_is_hot():
        return False
    for item in items:
        if (item.get("publisher") or "") != "elsevier":
            return False
        if item.get("id") in auto_retried_ids:
            return False
        if (item.get("reason") or "") not in ELSEVIER_HOT_AUTO_RETRY_REASONS:
            return False
    return True


def attach_session_restart_metadata(
    row: Dict[str, Any],
    session_restarts: int = 0,
    session_last_error: str = "",
) -> Dict[str, Any]:
    updated = dict(row)
    updated["session_restarts"] = int(updated.get("session_restarts") or 0) + int(session_restarts or 0)
    if session_last_error:
        updated["session_last_error"] = session_last_error
    return finalize_report_row(updated)


def make_browser_error_row(
    ref: Dict[str, Any],
    err_msg: str,
    *,
    session_restarts: int = 0,
    session_last_error: str = "",
) -> Dict[str, Any]:
    return finalize_report_row(
        dict(
            id=ref["id"],
            label=ref["label"],
            doi=ref["doi"],
            publisher=ref["publisher"],
            pdf_status=f"failed (browser error: {err_msg[:60]})",
            si_status="skipped",
            pdf_history="",
            si_history="",
            retry_count=0,
            session_restarts=session_restarts,
            session_last_error=session_last_error or err_msg[:120],
        )
    )


def preserve_manual_page(page: Page, stage: str, reason: str):
    ref = RUN_CTX.get("current_ref") or {}
    for item in RUN_CTX.get("manual_pages", []):
        if item.get("page") is page:
            item.update(
                {
                    "stage": stage,
                    "reason": reason,
                    "id": ref.get("id"),
                    "label": ref.get("label"),
                    "doi": ref.get("doi"),
                    "publisher": ref.get("publisher"),
                    "url": page.url,
                }
            )
            print(f"       ⏸ manual pending: {reason}")
            print(f"         page kept open: {page.url[:100]}")
            log_event(stage, "preserve_manual_page", STATUS_MANUAL, page.url, reason)
            return
    RUN_CTX["manual_pages"].append(
        {
            "page": page,
            "stage": stage,
            "reason": reason,
            "id": ref.get("id"),
            "label": ref.get("label"),
            "doi": ref.get("doi"),
            "publisher": ref.get("publisher"),
            "url": page.url,
        }
    )
    print(f"       ⏸ manual pending: {reason}")
    print(f"         page kept open: {page.url[:100]}")
    log_event(stage, "preserve_manual_page", STATUS_MANUAL, page.url, reason)


def ref_output_paths(ref: Dict[str, Any], project_dir: Path) -> tuple[Path, Path]:
    prefix = f"{ref['id']:02d}_{ref['label']}"
    return project_dir / f"{prefix}.pdf", project_dir / f"{prefix}_SI"


def build_manual_pending_row(ref: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return finalize_report_row(
        dict(
            id=ref["id"],
            label=ref["label"],
            doi=ref["doi"],
            publisher=ref["publisher"],
            pdf_status=f"manual_pending ({reason})",
            si_status="skipped (manual_pending)",
            pdf_history="",
            si_history="",
            retry_count=0,
            publisher_strategy=publisher_strategy(ref["publisher"])["family"],
            publisher_support=publisher_strategy(ref["publisher"])["support"],
            publisher_min_test=publisher_strategy(ref["publisher"])["min_test"],
        )
    )


def manual_item_resume_priority(item: Dict[str, Any]) -> tuple[int, int]:
    stage = item.get("stage") or ""
    reason = item.get("reason") or ""
    url = lower_unquoted(item.get("url") or "")
    score = 0
    if stage == "main_pdf":
        score += 5
    if reason == "viewer_capture_failed":
        score += 4
    if "main.pdf" in url:
        score += 4
    if "/pdfft" in url or "/doi/pdf" in url or "pdfdirect" in url:
        score += 3
    if reason == "elsevier_crasolve_shell":
        score += 2
    return (-score, len(url))


async def close_page_quietly(page: Optional[Page]):
    if not page:
        return
    try:
        if not page.is_closed():
            await page.close()
    except Exception:
        pass


# ── auto-manual retry queue ──────────────────────────────────────────────────
# Background retry queue for manual_pending refs. Refs that hit a transient
# publisher state (Elsevier popup not settling, viewer capture failed, etc.)
# get scheduled here instead of stalling the main loop; a worker pool with
# capped concurrency picks them up after a delay. Empty / inactive until
# download_one wire-in (Commit 2b) starts calling schedule_auto_manual_retry.

def get_auto_manual_sem() -> asyncio.Semaphore:
    sem = RUN_CTX.get("auto_manual_sem")
    if sem is None:
        sem = asyncio.Semaphore(AUTO_MANUAL_RETRY_MAX_CONCURRENT)
        RUN_CTX["auto_manual_sem"] = sem
    return sem


def auto_manual_page_count() -> int:
    clean_auto_manual_pages()
    return len(RUN_CTX.get("auto_manual_pages") or []) + len(RUN_CTX.get("auto_manual_active_pages") or [])


def push_auto_manual_result(item: Dict[str, Any], attempt: Dict[str, Any]):
    if item.get("result_pushed"):
        return
    item["result_pushed"] = True
    RUN_CTX.setdefault("auto_manual_results", []).append(
        {
            "ref": dict(item.get("ref") or {}),
            "stage": item.get("stage") or "main_pdf",
            "reason": item.get("reason") or "manual_pending",
            "dest": str(item.get("dest") or ""),
            "attempt": dict(attempt),
        }
    )


async def close_auto_manual_item(item: Dict[str, Any], reason: str, result_reason: str):
    page = item.get("page")
    stage = item.get("stage") or "main_pdf"
    ref = item.get("ref") or {}
    token = CURRENT_REF.set(ref)
    try:
        log_event(stage, "auto_manual_retry_closed", STATUS_MANUAL, item.get("url") or "", reason)
        push_auto_manual_result(item, make_attempt(STATUS_MANUAL, result_reason))
        await close_page_quietly(page)
    finally:
        CURRENT_REF.reset(token)


async def enforce_auto_manual_capacity() -> bool:
    pending = clean_auto_manual_pages()
    active = RUN_CTX.get("auto_manual_active_pages") or []
    if len(pending) + len(active) < AUTO_MANUAL_RETRY_MAX_PENDING:
        return True

    if pending:
        victim = sorted(pending, key=lambda item: float(item.get("created_at") or 0.0))[0]
        try:
            RUN_CTX["auto_manual_pages"].remove(victim)
        except ValueError:
            pass
        task = victim.get("task")
        if task and not task.done():
            task.cancel()
        await close_auto_manual_item(
            victim,
            f"queue_full max_pending={AUTO_MANUAL_RETRY_MAX_PENDING}",
            "auto_retry_overflow_closed",
        )
        return len(RUN_CTX.get("auto_manual_pages") or []) + len(active) < AUTO_MANUAL_RETRY_MAX_PENDING

    return False


async def schedule_auto_manual_retry(
    ctx: BrowserContext,
    page: Page,
    dest: Path,
    stage: str,
    reason: str,
    ref: Dict[str, Any],
) -> Dict[str, Any]:
    if not page or page.is_closed():
        attempt = make_attempt(STATUS_MANUAL, reason)
        attempt["auto_retry_dropped"] = "page_closed"
        return attempt

    if not await enforce_auto_manual_capacity():
        log_event(stage, "auto_manual_retry_rejected", STATUS_MANUAL, page.url, f"queue_full max_pending={AUTO_MANUAL_RETRY_MAX_PENDING}")
        await close_page_quietly(page)
        attempt = make_attempt(STATUS_MANUAL, reason)
        attempt["auto_retry_dropped"] = "queue_full"
        return attempt

    now = time.monotonic()
    due_at = time.monotonic() + (AUTO_MANUAL_RETRY_WAIT / 1000)
    item = {
        "page": page,
        "dest": dest,
        "stage": stage,
        "reason": reason,
        "ref": dict(ref),
        "due_at": due_at,
        "created_at": now,
        "url": page.url,
    }
    RUN_CTX["auto_manual_pages"].append(item)
    task = asyncio.create_task(auto_manual_retry_worker(ctx, item))
    item["task"] = task
    RUN_CTX.setdefault("auto_manual_tasks", set()).add(task)
    log_event(
        stage,
        "auto_manual_retry_scheduled",
        STATUS_MANUAL,
        page.url,
        f"{reason} wait_ms={AUTO_MANUAL_RETRY_WAIT} timeout_ms={AUTO_MANUAL_RETRY_TIMEOUT}",
    )
    attempt = make_attempt(STATUS_MANUAL, reason)
    attempt["auto_retry_scheduled"] = True
    return attempt


async def run_auto_manual_retry_item(ctx: BrowserContext, item: Dict[str, Any]) -> Dict[str, Any]:
    page = item.get("page")
    stage = item.get("stage") or "main_pdf"
    reason = item.get("reason") or "manual_pending"
    dest = item.get("dest")
    ref = item.get("ref") or {}
    token = CURRENT_REF.set(ref)
    try:
        if not page or page.is_closed():
            attempt = make_attempt(STATUS_FAILED, "auto_manual_page_closed")
            log_event(stage, "auto_manual_retry_complete", attempt["state"], "", attempt["reason"])
            return attempt

        log_event(stage, "auto_manual_retry_due", "start", page.url, reason)
        attempt = await auto_retry_manual_page_once(page, ctx, dest, stage, reason, wait_ms=0)
        log_event(
            stage,
            "auto_manual_retry_complete",
            attempt["state"],
            page.url if not page.is_closed() else "",
            attempt.get("reason", ""),
        )
        return attempt
    finally:
        await close_page_quietly(page)
        CURRENT_REF.reset(token)


def clean_auto_manual_pages() -> List[Dict[str, Any]]:
    live_items = []
    for item in RUN_CTX.get("auto_manual_pages", []):
        page = item.get("page")
        if page and not page.is_closed():
            live_items.append(item)
    RUN_CTX["auto_manual_pages"] = live_items
    active_items = []
    for item in RUN_CTX.get("auto_manual_active_pages", []):
        page = item.get("page")
        task = item.get("task")
        if page and not page.is_closed() and task and not task.done():
            active_items.append(item)
    RUN_CTX["auto_manual_active_pages"] = active_items
    return live_items


async def auto_manual_retry_worker(ctx: BrowserContext, item: Dict[str, Any]):
    page = item.get("page")
    stage = item.get("stage") or "main_pdf"
    ref = item.get("ref") or {}
    token = CURRENT_REF.set(ref)
    attempt = make_attempt(STATUS_MANUAL, "auto_retry_not_run")
    try:
        delay = max(0.0, float(item.get("due_at") or 0.0) - time.monotonic())
        if delay:
            await asyncio.sleep(delay)

        try:
            RUN_CTX["auto_manual_pages"].remove(item)
        except ValueError:
            pass
        RUN_CTX.setdefault("auto_manual_active_pages", []).append(item)

        async with get_auto_manual_sem():
            attempt = await asyncio.wait_for(
                run_auto_manual_retry_item(ctx, item),
                timeout=AUTO_MANUAL_RETRY_TIMEOUT / 1000,
            )
    except asyncio.CancelledError:
        attempt = make_attempt(STATUS_MANUAL, "auto_retry_cancelled")
        log_event(stage, "auto_manual_retry_cancelled", STATUS_MANUAL, item.get("url") or "", "cancelled")
        raise
    except asyncio.TimeoutError:
        attempt = make_attempt(STATUS_MANUAL, f"auto_retry_timeout_{AUTO_MANUAL_RETRY_TIMEOUT // 1000}s")
        log_event(stage, "auto_manual_retry_timeout", STATUS_MANUAL, item.get("url") or "", f"timeout_ms={AUTO_MANUAL_RETRY_TIMEOUT}")
    except Exception as e:
        state = "session_closed" if is_session_closed_error(e) else STATUS_FAILED
        attempt = make_attempt(STATUS_FAILED, "auto_retry_exception")
        log_event(stage, "auto_manual_retry_exception", state, item.get("url") or "", str(e)[:120])
    finally:
        push_auto_manual_result(item, attempt)
        for bucket in ("auto_manual_pages", "auto_manual_active_pages"):
            try:
                RUN_CTX[bucket].remove(item)
            except (KeyError, ValueError):
                pass
        await close_page_quietly(page)
        CURRENT_REF.reset(token)


def collect_auto_manual_tasks():
    tasks = RUN_CTX.setdefault("auto_manual_tasks", set())
    done = {task for task in tasks if task.done()}
    tasks.difference_update(done)
    for task in done:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log_event("auto_manual_queue", "task_result", STATUS_FAILED, "", str(e)[:120])


def materialize_auto_retry_result(report: List[Dict[str, Any]], result: Dict[str, Any]):
    ref = result.get("ref") or {}
    if not ref.get("id"):
        return
    stage = result.get("stage") or "main_pdf"
    original_reason = result.get("reason") or "manual_pending"
    attempt = result.get("attempt") or make_attempt(STATUS_FAILED, "auto_retry_missing_result")

    row = None
    for existing in report:
        if int(existing.get("id") or 0) == int(ref["id"]):
            row = existing
            break
    if row is None:
        row = finalize_report_row(
            dict(
                id=ref["id"],
                label=ref.get("label", ""),
                doi=ref.get("doi", ""),
                publisher=ref.get("publisher", ""),
                pdf_status="skipped",
                si_status="not_attempted",
                pdf_history="",
                si_history="",
                retry_count=0,
                publisher_strategy=publisher_strategy(ref.get("publisher", ""))["family"],
                publisher_support=publisher_strategy(ref.get("publisher", ""))["support"],
                publisher_min_test=publisher_strategy(ref.get("publisher", ""))["min_test"],
            )
        )
        report.append(row)

    row["retry_count"] = int(row.get("retry_count") or 0) + 1
    status_key = "pdf_status" if stage == "main_pdf" else "si_status"
    history_key = "pdf_history" if stage == "main_pdf" else "si_history"
    if attempt.get("state") == STATUS_DOWNLOADED:
        row[status_key] = f"downloaded ({attempt.get('size_kb') or 0} KB)"
    else:
        current_status = row.get(status_key) or ""
        if any(token in current_status for token in ("downloaded", "already_exists", "non_pdf_asset_saved")):
            return
        retry_state = attempt.get("state") or STATUS_FAILED
        retry_reason = attempt.get("reason") or "unknown"
        row[status_key] = f"manual_pending ({original_reason}; auto_retry={retry_state}:{retry_reason})"
    row[history_key] = append_history_text(row.get(history_key, ""), row[status_key])


def collect_auto_manual_retry_results(report: List[Dict[str, Any]]):
    collect_auto_manual_tasks()
    results = RUN_CTX.get("auto_manual_results") or []
    RUN_CTX["auto_manual_results"] = []
    for result in results:
        materialize_auto_retry_result(report, result)


async def drain_due_auto_manual_retries(
    ctx: BrowserContext,
    report: Optional[List[Dict[str, Any]]] = None,
    *,
    wait: bool = False,
):
    if wait:
        tasks = {task for task in RUN_CTX.get("auto_manual_tasks", set()) if not task.done()}
        if tasks:
            done, pending = await asyncio.wait(
                tasks,
                timeout=AUTO_MANUAL_FINAL_DRAIN_TIMEOUT / 1000,
            )
            if pending:
                log_event(
                    "auto_manual_queue",
                    "final_drain",
                    STATUS_MANUAL,
                    "",
                    f"timeout_ms={AUTO_MANUAL_FINAL_DRAIN_TIMEOUT} cancelling={len(pending)}",
                )
                for task in pending:
                    task.cancel()
                done_after_cancel, still_pending = await asyncio.wait(pending, timeout=5.0)
                if still_pending:
                    log_event("auto_manual_queue", "final_cancel", STATUS_FAILED, "", f"still_pending={len(still_pending)}")
    collect_auto_manual_tasks()
    if report is not None:
        collect_auto_manual_retry_results(report)


async def cancel_auto_manual_retries(reason: str, report: Optional[List[Dict[str, Any]]] = None):
    for item in list(RUN_CTX.get("auto_manual_pages") or []):
        try:
            RUN_CTX["auto_manual_pages"].remove(item)
        except ValueError:
            pass
        await close_auto_manual_item(item, reason, f"auto_retry_cancelled:{reason[:60]}")

    tasks = {task for task in RUN_CTX.get("auto_manual_tasks", set()) if not task.done()}
    if tasks:
        for task in tasks:
            task.cancel()
        done_after_cancel, still_pending = await asyncio.wait(tasks, timeout=5.0)
        if still_pending:
            log_event("auto_manual_queue", "cancel", STATUS_FAILED, "", f"still_pending={len(still_pending)} reason={reason[:80]}")
    collect_auto_manual_tasks()
    if report is not None:
        collect_auto_manual_retry_results(report)


def sync_report_with_existing_files(report: List[Dict[str, Any]], project_dir: Path):
    for row in report:
        ref = {
            "id": int(row["id"]),
            "label": row["label"],
            "doi": row.get("doi", ""),
            "publisher": row.get("publisher", ""),
        }
        pdf_dest, si_base = ref_output_paths(ref, project_dir)
        if pdf_dest.exists() and not any(x in (row.get("pdf_status") or "") for x in ("downloaded", "already_exists")):
            size_kb = pdf_dest.stat().st_size // 1024
            row["pdf_status"] = f"downloaded ({size_kb} KB)"
            row["pdf_history"] = append_history_text(row.get("pdf_history", ""), row["pdf_status"])

        existing_si = find_existing_si_asset(si_base)
        si_status = row.get("si_status") or ""
        if existing_si and not any(x in si_status for x in ("downloaded", "already_exists", "non_pdf_asset_saved")):
            size_kb = existing_si.stat().st_size // 1024
            if existing_si.suffix.lower() == ".pdf":
                row["si_status"] = f"downloaded ({size_kb} KB)"
            else:
                row["si_status"] = f"non_pdf_asset_saved ({existing_si.suffix.lower()}, {size_kb} KB)"
            row["si_history"] = append_history_text(row.get("si_history", ""), row["si_status"])


async def auto_retry_manual_page_once(
    page: Page,
    ctx: BrowserContext,
    dest: Path,
    stage: str,
    reason: str,
    wait_ms: int = AUTO_MANUAL_RETRY_WAIT,
) -> Dict[str, Any]:
    """In --auto, give a manual/challenge page one short chance, then let caller close it."""
    if wait_ms > 0:
        log_event(stage, "auto_manual_retry_wait", "start", page.url, f"{reason} wait_ms={wait_ms}")
        try:
            await page.wait_for_timeout(wait_ms)
        except PlaywrightError as e:
            if is_session_closed_error(e):
                raise
            log_event(stage, "auto_manual_retry_wait", STATUS_FAILED, "", str(e)[:120])
            return make_attempt(STATUS_FAILED, "auto_manual_page_closed")
        except Exception as e:
            if is_session_closed_error(e):
                raise
            log_event(stage, "auto_manual_retry_wait", STATUS_FAILED, "", str(e)[:120])
            return make_attempt(STATUS_FAILED, "auto_manual_page_closed")

    if page.is_closed():
        log_event(stage, "auto_manual_retry", STATUS_FAILED, "", "page_closed")
        return make_attempt(STATUS_FAILED, "auto_manual_page_closed")

    try:
        barrier = await inspect_access_barrier(page)
        if barrier:
            log_event(stage, "auto_manual_retry", STATUS_MANUAL, barrier["url"], barrier["reason"])
            return make_attempt(STATUS_MANUAL, barrier["reason"])

        log_event(stage, "auto_manual_retry", "start", page.url, reason)
        viewer_attempt = await fetch_pdf_from_viewer(page, ctx, dest, stage)
        if viewer_attempt["state"] != STATUS_FAILED:
            return viewer_attempt

        current_url = page.url
        if current_url and current_url != "about:blank":
            direct_attempt = await try_direct_pdf(
                page,
                ctx,
                current_url,
                dest,
                stage=stage,
                allow_navigation=True,
                close_other_pages=False,
            )
            if direct_attempt["state"] != STATUS_FAILED:
                return direct_attempt

        log_event(stage, "auto_manual_retry", STATUS_FAILED, page.url, "no_pdf_after_retry")
        return make_attempt(STATUS_FAILED, "auto_retry_no_pdf")
    except ManualInterventionRequired as e:
        log_event(stage, "auto_manual_retry", STATUS_MANUAL, page.url, e.reason)
        return make_attempt(STATUS_MANUAL, e.reason)
    except PlaywrightError as e:
        if is_session_closed_error(e):
            raise
        log_event(stage, "auto_manual_retry", STATUS_FAILED, page.url if not page.is_closed() else "", str(e)[:120])
        return make_attempt(STATUS_FAILED, "auto_retry_exception")
    except Exception as e:
        if is_session_closed_error(e):
            raise
        log_event(stage, "auto_manual_retry", STATUS_FAILED, page.url if not page.is_closed() else "", str(e)[:120])
        return make_attempt(STATUS_FAILED, "auto_retry_exception")


def manual_resume_wait_ms(publisher: str) -> int:
    return MANUAL_RESUME_WAIT_MS_BY_PUBLISHER.get(publisher or "", MANUAL_RESUME_WAIT_MS_DEFAULT)


async def wait_for_manual_resume_page_ready(
    page: Page,
    publisher: str,
    stage: str,
    reason: str,
) -> Dict[str, Any]:
    budget_ms = manual_resume_wait_ms(publisher)
    deadline = time.monotonic() + (budget_ms / 1000)
    last_barrier = None
    last_surface = {"viewerish": False, "anchor_pdf_urls": [], "has_download_control": False}
    last_title = ""

    log_event(stage, "manual_resume_wait", "start", page.url, f"{publisher} {reason} budget_ms={budget_ms}")

    while True:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=1_500)
        except Exception:
            pass

        try:
            last_title = (await page.title()).strip()
        except Exception:
            last_title = ""

        try:
            last_barrier = await inspect_access_barrier(page)
        except Exception:
            last_barrier = None

        try:
            last_surface = await inspect_pdf_surface(page)
        except Exception:
            last_surface = {"viewerish": False, "anchor_pdf_urls": [], "has_download_control": False}

        url_lower = lower_unquoted(page.url)
        title_lower = last_title.lower()
        loading_title = (
            (not last_title)
            or any(t in last_title for t in _auth_loading_titles())
            or title_lower in ("loading", "loading...")
        )
        elsevier_pdf_route = publisher == "elsevier" and (
            "pdf.sciencedirectassets.com" in url_lower or "/pdfft" in url_lower or "main.pdf" in url_lower
        )

        ready = False
        if not last_barrier:
            if last_surface.get("viewerish") or last_surface.get("anchor_pdf_urls") or last_surface.get("has_download_control"):
                ready = True
            elif elsevier_pdf_route and not loading_title:
                ready = True

        if ready:
            log_event(stage, "manual_resume_wait", "ready", page.url, f"title={last_title[:80]}")
            return {"barrier": last_barrier, "surface": last_surface, "title": last_title, "timed_out": False}

        if time.monotonic() >= deadline:
            detail = f"title={last_title[:80]} barrier={last_barrier['reason'] if last_barrier else 'none'}"
            log_event(stage, "manual_resume_wait", "timeout", page.url, detail)
            return {"barrier": last_barrier, "surface": last_surface, "title": last_title, "timed_out": True}

        try:
            await page.wait_for_timeout(500)
        except Exception:
            return {"barrier": last_barrier, "surface": last_surface, "title": last_title, "timed_out": True}


async def resume_ref_from_manual_pages(
    ctx: BrowserContext,
    ref: Dict[str, Any],
    project_dir: Path,
    total: int,
    manual_items: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    main_items = [item for item in manual_items if item.get("stage") == "main_pdf"]
    if not main_items:
        return None

    pdf_dest, _ = ref_output_paths(ref, project_dir)
    if pdf_dest.exists():
        return None

    primary_manual_reason = ""
    prev_ref = RUN_CTX.get("current_ref")
    RUN_CTX["current_ref"] = ref

    try:
        seen_pages = set()
        ordered_items = []
        for item in sorted(main_items, key=manual_item_resume_priority):
            page = item.get("page")
            if not page or page.is_closed():
                continue
            page_id = id(page)
            if page_id in seen_pages:
                continue
            seen_pages.add(page_id)
            ordered_items.append(item)

        for item in ordered_items:
            page = item.get("page")
            if not page or page.is_closed():
                continue

            try:
                await page.bring_to_front()
            except Exception:
                pass

            print(f"       → manual-resume page: {page.url[:90]}")
            log_event("main_pdf", "manual_resume", "start", page.url, item.get("reason", ""))
            print(f"       → waiting for live page to settle ({manual_resume_wait_ms(ref['publisher']) / 1000:.0f}s budget)")
            resume_state = await wait_for_manual_resume_page_ready(page, ref["publisher"], "main_pdf", item.get("reason", ""))

            barrier = resume_state.get("barrier")
            if barrier:
                primary_manual_reason = primary_manual_reason or barrier["reason"]
                preserve_manual_page(page, "main_pdf", barrier["reason"])
                continue

            surface = resume_state.get("surface") or {"viewerish": False, "anchor_pdf_urls": []}

            if surface.get("viewerish"):
                viewer_attempt = await fetch_pdf_from_viewer(page, ctx, pdf_dest, "main_pdf")
                if viewer_attempt["state"] == STATUS_DOWNLOADED:
                    if ref["publisher"] == "elsevier":
                        mark_elsevier_hot("manual_resume_viewer_downloaded")
                    await close_page_quietly(page)
                    resumed = await download_one(ctx, ref, project_dir, total)
                    if resumed.get("pdf_status") == STATUS_ALREADY_EXISTS:
                        resumed["pdf_status"] = f"downloaded ({viewer_attempt['size_kb']} KB)"
                    return finalize_report_row(resumed)
                if viewer_attempt["state"] == STATUS_MANUAL:
                    primary_manual_reason = primary_manual_reason or viewer_attempt["reason"]
                    preserve_manual_page(page, "main_pdf", viewer_attempt["reason"])
                    continue

            try:
                page_candidates = await collect_candidate_urls(page, ref["publisher"])
            except Exception:
                page_candidates = []

            nav_candidates = prioritized_browser_navigation_candidates(
                ref["publisher"],
                page.url,
                page_candidates,
                surface,
            )
            for nav_url in nav_candidates:
                nav_attempt = await try_browser_pdf_navigation_candidate(ctx, nav_url, pdf_dest, "main_pdf")
                if nav_attempt["state"] == STATUS_DOWNLOADED:
                    if ref["publisher"] == "elsevier":
                        mark_elsevier_hot("manual_resume_browser_nav_downloaded")
                    await close_page_quietly(page)
                    resumed = await download_one(ctx, ref, project_dir, total)
                    if resumed.get("pdf_status") == STATUS_ALREADY_EXISTS:
                        resumed["pdf_status"] = f"downloaded ({nav_attempt['size_kb']} KB)"
                    return finalize_report_row(resumed)
                if nav_attempt["state"] == STATUS_MANUAL:
                    primary_manual_reason = primary_manual_reason or nav_attempt["reason"]

        for item in RUN_CTX.get("manual_pages", []):
            if item.get("id") == ref["id"] and item.get("stage") == "main_pdf":
                primary_manual_reason = item.get("reason") or primary_manual_reason
                break

        if primary_manual_reason:
            return build_manual_pending_row(ref, primary_manual_reason)
        return None
    finally:
        RUN_CTX["current_ref"] = prev_ref

# ── URL construction ──────────────────────────────────────────────────────────

def direct_pdf_url(doi: str, publisher: str) -> Optional[str]:
    """Construct direct PDF download URL. Returns None when only click/article flow is trusted."""
    nature_slug = doi.split("/")[-1].replace(".", "")
    urls = {
        "acs":      f"https://pubs.acs.org/doi/pdf/{doi}",
        "nature":   f"https://www.nature.com/articles/{nature_slug}.pdf",
        "science":  f"https://www.science.org/doi/pdf/{doi}",
        # Wiley pdfdirect = direct download, bypasses epdf iframe wrapper
        "wiley":    f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}",
        "pnas":     f"https://www.pnas.org/doi/pdf/{doi}",
        "springer": f"https://link.springer.com/content/pdf/{doi}.pdf",
        "ecs":      f"https://iopscience.iop.org/article/{doi}/pdf",
        "iop":      f"https://iopscience.iop.org/article/{doi}/pdf",
        # AIP: no reliable direct URL without article-id; use doi.org redirect
        # IEEE, OSA, KPS, APS, Annual Reviews, T&F: navigate to article page
        # AVS (JVST): hosted on pubs.aip.org via doi.org
        "avs":      f"https://doi.org/{doi}",
    }
    return urls.get(publisher)


def article_url(doi: str, publisher: str) -> str:
    """Article landing page URL (fallback)."""
    nature_slug = doi.split("/")[-1].replace(".", "")
    urls = {
        "nature":   f"https://www.nature.com/articles/{nature_slug}",
        "acs":      f"https://pubs.acs.org/doi/{doi}",
        "science":  f"https://www.science.org/doi/{doi}",
        "elsevier": f"https://doi.org/{doi}",
        "wiley":    f"https://doi.org/{doi}",
        "rsc":      f"https://doi.org/{doi}",
        "springer": f"https://link.springer.com/article/{doi}",
        "pnas":     f"https://www.pnas.org/doi/{doi}",
        "ecs":      f"https://iopscience.iop.org/article/{doi}",
        "iop":      f"https://iopscience.iop.org/article/{doi}",
        # AIP Publishing: doi.org resolves correctly
        "aip":      f"https://doi.org/{doi}",
        # IEEE Xplore: doi.org resolves to ieeexplore.ieee.org
        "ieee":     f"https://doi.org/{doi}",
        "aps":      f"https://doi.org/{doi}",
        "annualreviews": f"https://www.annualreviews.org/content/journals/{doi}",
        "tandfonline": f"https://doi.org/{doi}",
        # OSA / Optica
        "osa":      f"https://doi.org/{doi}",
        # Korean Physical Society
        "kps":      f"https://doi.org/{doi}",
        # AVS (American Vacuum Society): JVST-A, JVST-B — hosted on pubs.aip.org
        "avs":      f"https://doi.org/{doi}",
        "beilstein": f"https://doi.org/{doi}",
    }
    return urls.get(publisher, f"https://doi.org/{doi}")


PDF_SELECTORS = {
    "acs":      ['a[href*="/doi/pdf/"]', 'a[title*="PDF"]',
                 'a:has-text("Download PDF")', 'a[href*="epdf"]'],
    "nature":   ['a.c-pdf-download__link', 'a[data-track-action="download pdf"]',
                 'a[href*=".pdf"]', 'div.c-pdf-download a',
                 'a:has-text("Download PDF")', 'a:has-text("PDF")'],
    "science":  ['a[href*="/doi/pdf/"]', 'a[href*="epdf"]',
                 'a:has-text("PDF")'],
    "elsevier": ['a.link-button.accessbar-utility-link[target="_blank"][href*="pdfft"]',
                 'a[aria-label*="View PDF. Opens in a new window."]',
                 'a.pdf-download-btn-link', 'a[href*="pdfft"]',
                 'a[href*="/pdf"]', 'a:has-text("Download PDF")',
                 'a:has-text("View PDF")', 'a:has-text("PDF")'],
    "wiley":    ['a[href*="pdfdirect"]', 'a[href*="/doi/pdf/"]',
                 'a[href*="/doi/epdf/"]', 'a.pdf-download',
                 'a:has-text("Download PDF")', 'a:has-text("PDF")'],
    "rsc":      ['a[href*="articlepdf"]', 'a.btn--pdf',
                 'a:has-text("Article PDF")'],
    "springer": ['a[data-track-action*="pdf"]', 'a[href*="content/pdf"]',
                 'a:has-text("Download PDF")'],
    "pnas":     ['a[href*="/doi/pdf/"]', 'a:has-text("PDF")'],
    "ecs":      ['a[href$="/pdf"]', 'a[href*="/article/"][href*="/pdf"]',
                 'a:has-text("Full Text PDF")',
                 'a:has-text("Download article PDF")',
                 'a:has-text("PDF")'],
    "iop":      ['a[href$="/pdf"]', 'a[href*="/article/"][href*="/pdf"]',
                 'a:has-text("Full Text PDF")',
                 'a:has-text("Download article PDF")',
                 'a:has-text("PDF")'],
    # AIP Publishing (pubs.aip.org): Applied Physics Letters, JAP, etc.
    "aip":      ['a[href*="/pdf/"]', 'a[data-article-url*="pdf"]',
                 'a:has-text("PDF")', 'a:has-text("Download PDF")',
                 'button:has-text("PDF")', 'a[href*=".pdf"]'],
    # AVS (pubs.aip.org/avs): Journal of Vacuum Science & Technology A/B
    "avs":      ['a[href*="/pdf/"]', 'a:has-text("PDF")',
                 'a:has-text("Download PDF")', 'button:has-text("PDF")'],
    # IEEE Xplore
    "ieee":     ['a[href*="/stamp/"]', 'a.stats-document-lh-action-downloads-PDF',
                 'a:has-text("PDF")', 'button:has-text("PDF")'],
    "aps":      ['a[href*="/pdf/"]', 'a:has-text("PDF")', 'a:has-text("Download PDF")'],
    "annualreviews": ['a[href*="/doi/pdf/"]', 'a:has-text("PDF")', 'a:has-text("Download PDF")'],
    "tandfonline": ['a[href*="/doi/pdf/"]', 'a[href*="download?"]',
                    'a:has-text("PDF")', 'a:has-text("Download PDF")'],
    # OSA / Optica Publishing Group
    "osa":      ['a[href*="viewmedia"]', 'a[href*="/pdf"]',
                 'a:has-text("PDF")', 'a:has-text("Download PDF")'],
    # Korean Physical Society
    "kps":      ['a[href*=".pdf"]', 'a:has-text("PDF")'],
    "beilstein": ['a[href*="/downloads/pdf/"]', 'a[href*=".pdf"]',
                  'a:has-text("PDF")', 'a:has-text("Download PDF")'],
}

VIEWER_DOWNLOAD_SELECTORS = [
    '#download',
    '#downloadButton',
    'button#download',
    'a#download',
    'button[aria-label*="download" i]',
    'a[aria-label*="download" i]',
    'button[title*="download" i]',
    'a[title*="download" i]',
    'button[data-l10n-id="download"]',
    'a[data-l10n-id="download"]',
    'cr-icon-button#download',
    'button:has-text("Download")',
    'a:has-text("Download")',
]

VIEWER_SOURCE_BLOCKLIST = (
    "doubleclick.net",
    "googlesyndication.com",
    "googletagmanager.com",
    "google.com/recaptcha",
    "gstatic.com/recaptcha",
    "hcaptcha.com",
)

SI_DIRECT_FILE_EXTENSIONS = {
    ".pdf",
    ".zip",
    ".doc",
    ".docx",
    ".csv",
    ".xls",
    ".xlsx",
    ".cif",
    ".txt",
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    ".mol",
    ".mol2",
    ".mat",
    ".m",
    ".avi",
    ".mp4",
    ".mov",
    ".mpg",
}

SI_REGEX_PATTERNS: Dict[str, List[str]] = {
    "generic": [
        r'(https://static-content\.springer\.com/esm/[^"\'>\s]+)',
        r'href="(/doi/suppl/[^"]+)"',
        r'(https://(?:www\.)?rsc\.org/suppdata/[^"\'>\s]+)',
        r'((?:https://[^"\'>\s]+|/[^"\'>\s]+)mmc\d+\.[^"\'>\s]+)',
        r'(https://ars\.els-cdn\.com/content/image/[^"\'>\s]*-mmc\d+\.[^"\'>\s]+)',
        r'((?:https://[^"\'>\s]+|/[^"\'>\s]+)[_\-]si[_\-][^"\'>\s]+)',
        r'((?:https://[^"\'>\s]+|/[^"\'>\s]+)supporting[^"\'>\s]+\.(?:pdf|zip|docx?|csv|xlsx?|cif))',
    ],
    "wiley": [
        r'href="([^"]*/action/downloadSupplement\?[^"]+)"',
    ],
    "acs": [
        r'href="(/doi/suppl/[^"]+/suppl_file/[^"]+)"',
        r'href="(/doi/suppl/[^"]+\.pdf)"',
    ],
    "rsc": [
        r'(https://(?:www\.)?rsc\.org/suppdata/[^"\'>\s]+)',
    ],
    "nature": [
        r'(https://static-content\.springer\.com/esm/[^"\'>\s]+)',
    ],
    "springer": [
        r'(https://static-content\.springer\.com/esm/[^"\'>\s]+)',
    ],
    "elsevier": [
        r'(https://ars\.els-cdn\.com/content/image/[^"\'>\s]*-mmc\d+\.[^"\'>\s]+)',
        r'((?:https://[^"\'>\s]+|/[^"\'>\s]+)mmc\d+\.[^"\'>\s]+)',
    ],
    "tandfonline": [
        r'href="(/doi/suppl/[^"]+)"',
    ],
}

SI_WAIT_BUDGET_MS = {
    "elsevier": 5500,
    "wiley": 1800,
    "acs": 1200,
    "rsc": 1500,
    "nature": 1500,
    "springer": 1500,
    "annualreviews": 1800,
    "tandfonline": 1500,
    "aip": 2000,
    "avs": 2000,
    "ecs": 1200,
    "iop": 1200,
}

# JS fallback: collect any links that look like PDF download routes
JS_FIND_PDF_LINKS = """
(args) => {
    const allowSupplementary = !!(args && args.allowSupplementary);
    const candidates = [];
    const seen = new Set();
    const rank = (href, text) => {
        let score = 0;
        const h = href.toLowerCase();
        const t = (text || '').toLowerCase();
        if (h.includes('/doi/pdf/')) score += 5;
        if (h.includes('pdfdirect')) score += 5;
        if (h.includes('pdfft')) score += 5;
        if (h.includes('articlepdf')) score += 5;
        if (h.endsWith('.pdf')) score += 4;
        if (h.includes('/pdf/')) score += 4;
        if (/download\\s*pdf/.test(t)) score += 3;
        if (/view\\s*pdf/.test(t)) score += 3;
        if (/full\\s*text\\s*pdf/.test(t)) score += 3;
        if (/article\\s*pdf/.test(t)) score += 3;
        if (/\\bpdf\\b/.test(t)) score += 2;
        return score;
    };
    const blocked = (href, text) => {
        const h = href.toLowerCase();
        const t = (text || '').toLowerCase();
        if (allowSupplementary) return false;
        return h.includes('supplement') || h.includes('suppl') || h.includes('_si_')
            || h.includes('/si/') || t.includes('supplementary')
            || t.includes('supporting information');
    };
    for (const a of document.querySelectorAll('a[href]')) {
        const href = a.href || '';
        const text = (a.textContent || '').trim();
        if (!href || blocked(href, text)) continue;
        const score = rank(href, text);
        if (score <= 0 || seen.has(href)) continue;
        seen.add(href);
        candidates.push({href, score});
    }
    candidates.sort((a, b) => b.score - a.score);
    return candidates.slice(0, 12).map(x => x.href);
}
"""


# ── Captcha / challenge detection ────────────────────────────────────────────
# Auth patterns are loaded from config.institution at startup; see _auth_hosts()
# and friends near the top of this file. Empty by default for vanilla use.

CAPTCHA_SELECTORS = (
    "#px-captcha, "                              # PerimeterX (Elsevier)
    "div.cf-turnstile, "                         # Cloudflare Turnstile
    "#cf-challenge-running, "                    # Cloudflare challenge
    ".cf-browser-verification, "                 # Cloudflare browser check
    "#challenge-form, "                          # Generic challenge form
    "iframe[src*='hcaptcha'], "                  # hCaptcha
    "iframe[src*='recaptcha']"                   # reCAPTCHA
)

CHALLENGE_URL_PATTERNS = (
    "captcha",
    "challenge",
    "cf_chl",
    "px-captcha",
)

CAPTCHA_TITLE_PATTERNS = (
    "just a moment",
    "attention required",
    "security check",
    "one more step",
    "please verify",
    "access denied",
)

CLOUDFLARE_TITLE_PATTERNS = (
    "just a moment",
    "attention required",
    "security check",
    "please verify",
    "access denied",
    "请稍候",
    "请稍后",
)

CLOUDFLARE_TEXT_PATTERNS = (
    "cf-mitigated",
    "cloudflare",
    "please enable cookies",
    "please enable javascript",
    "verify you are human",
    "sorry, you have been blocked",
    "ray id",
    "challenge-platform",
)

RADWARE_TITLE_PATTERNS = (
    "radware bot manager captcha",
)

RADWARE_TEXT_PATTERNS = (
    "radware",
    "we apologize for the inconvenience",
    "confirm you are a human",
)

ELSEVIER_ERROR_TEXT_PATTERNS = (
    "there was a problem providing the content you requested",
    "please contact our support team for more information",
)

REQUEST_REJECTED_TITLE_PATTERNS = (
    "request rejected",
)

REQUEST_REJECTED_TEXT_PATTERNS = (
    "the requested url was rejected",
    "support id",
)

ELSEVIER_PDF_SECURITY_TITLE_PATTERNS = (
    "security verification",
)

ELSEVIER_PDF_SECURITY_TEXT_PATTERNS = (
    "request verification: in progress",
    "if you are unable to access your content",
    "please supply the following details",
    "request id:",
    "utc time:",
)


def contains_any_token(text: str, patterns: tuple[str, ...]) -> bool:
    lowered = (text or "").lower()
    return any(pat in lowered for pat in patterns)


async def page_marker_snapshot(page: Page) -> Dict[str, str]:
    try:
        data = await page.evaluate(
            """() => {
                const html = (document.documentElement && document.documentElement.outerHTML) || '';
                const bodyText = (document.body && document.body.innerText) || '';
                return {
                    title: document.title || '',
                    bodyText: bodyText.slice(0, 4000),
                    html: html.slice(0, 6000),
                };
            }"""
        )
    except Exception:
        data = {}
    return {
        "title": (data.get("title") or "").strip(),
        "body_text": (data.get("bodyText") or "").strip(),
        "html": (data.get("html") or "").strip(),
    }


async def inspect_access_barrier(page: Page) -> Optional[Dict[str, str]]:
    url = (page.url or "").lower()

    if any(host in url for host in _auth_hosts()) or any(pat in url for pat in _auth_url_fragments()):
        return {"kind": "auth_redirect", "reason": "institution_auth_redirect", "url": page.url}

    title = ""
    try:
        title = (await page.title()).strip()
    except Exception:
        pass

    if title and any(pat in title for pat in _auth_page_titles()):
        return {"kind": "auth_redirect", "reason": "institution_auth_redirect", "url": page.url}

    if is_elsevier_crasolve_shell(page.url):
        return {"kind": "publisher_shell", "reason": "elsevier_crasolve_shell", "url": page.url}

    snapshot = await page_marker_snapshot(page)
    if not title:
        title = snapshot["title"]
    body_text = snapshot["body_text"].lower()
    html = snapshot["html"].lower()
    title_lower = title.lower()

    if contains_any_token(title_lower, RADWARE_TITLE_PATTERNS) or (
        contains_any_token(body_text, RADWARE_TEXT_PATTERNS) and "captcha" in body_text
    ):
        return {"kind": "publisher_shell", "reason": "radware_bot_manager", "url": page.url}

    if "pdf.sciencedirectassets.com" in url and (
        contains_any_token(title_lower, ELSEVIER_PDF_SECURITY_TITLE_PATTERNS)
        or contains_any_token(body_text, ELSEVIER_PDF_SECURITY_TEXT_PATTERNS)
    ):
        return {"kind": "publisher_shell", "reason": "elsevier_pdf_security_verification", "url": page.url}

    if "sciencedirect.com" in url and all(pat in body_text for pat in ELSEVIER_ERROR_TEXT_PATTERNS):
        return {"kind": "publisher_shell", "reason": "elsevier_content_error", "url": page.url}

    if contains_any_token(title_lower, REQUEST_REJECTED_TITLE_PATTERNS) and contains_any_token(body_text, REQUEST_REJECTED_TEXT_PATTERNS):
        return {"kind": "publisher_shell", "reason": "request_rejected_page", "url": page.url}

    try:
        # Must be VISIBLE — PerimeterX (#px-captcha) is always in DOM on ACS pages
        # but only shows when actually triggered; count() alone causes false positives
        if await page.locator(CAPTCHA_SELECTORS).filter(has_not_text="").count() > 0:
            for loc in await page.locator(CAPTCHA_SELECTORS).all():
                if await loc.is_visible():
                    return {"kind": "captcha", "reason": "captcha_dom_detected", "url": page.url}
    except Exception:
        pass

    if contains_any_token(body_text, CLOUDFLARE_TEXT_PATTERNS) or contains_any_token(html, CLOUDFLARE_TEXT_PATTERNS):
        if contains_any_token(title_lower, CLOUDFLARE_TITLE_PATTERNS) or any(pat in url for pat in CHALLENGE_URL_PATTERNS):
            return {"kind": "captcha", "reason": "cloudflare_challenge_page", "url": page.url}

    if title and len(title) < 60:
        if any(pat in title_lower for pat in CAPTCHA_TITLE_PATTERNS) and any(pat in url for pat in CHALLENGE_URL_PATTERNS):
            return {"kind": "captcha", "reason": "challenge_title_detected", "url": page.url}

    return None


async def handle_access_barrier(page: Page, stage: str, timeout: int = None) -> bool:
    if timeout is None:
        timeout = AUTO_CAPTCHA_WAIT if ("--auto" in sys.argv) else CAPTCHA_WAIT

    barrier = await inspect_access_barrier(page)
    if not barrier:
        return False

    if barrier["kind"] == "auth_redirect":
        log_event(stage, "inspect_access_barrier", STATUS_MANUAL, barrier["url"], barrier["reason"])
        raise ManualInterventionRequired(barrier["reason"], barrier["url"])

    if barrier["kind"] == "publisher_shell":
        log_event(stage, "inspect_access_barrier", STATUS_MANUAL, barrier["url"], barrier["reason"])
        raise ManualInterventionRequired(barrier["reason"], barrier["url"])

    print(f"       ⏳ Challenge detected — waiting up to {timeout // 1000}s...")
    log_event(stage, "inspect_access_barrier", "challenge_wait", barrier["url"], barrier["reason"])

    try:
        await page.wait_for_function(
            """() => {
                const sels = '#px-captcha, div.cf-turnstile, #cf-challenge-running, '
                    + '.cf-browser-verification, #challenge-form, '
                    + 'iframe[src*="hcaptcha"], iframe[src*="recaptcha"]';
                return document.querySelectorAll(sels).length === 0;
            }""",
            timeout=timeout,
        )
        await page.wait_for_timeout(200)
    except Exception:
        log_event(stage, "challenge_wait", STATUS_MANUAL, page.url, "challenge_timeout")
        raise ManualInterventionRequired("captcha_timeout", page.url) from None

    barrier = await inspect_access_barrier(page)
    if barrier:
        if barrier["kind"] == "auth_redirect":
            log_event(stage, "post_wait_barrier", STATUS_MANUAL, barrier["url"], barrier["reason"])
            raise ManualInterventionRequired(barrier["reason"], barrier["url"])
        log_event(stage, "post_wait_barrier", STATUS_MANUAL, barrier["url"], barrier["reason"])
        raise ManualInterventionRequired("captcha_timeout", barrier["url"])

    print("       ✓ challenge resolved")
    log_event(stage, "challenge_wait", "resolved", page.url, "")
    return True


# ── Core download logic ──────────────────────────────────────────────────────

# JS: fetch current page URL as binary, return base64
# Uses browser cookies + cache, works even when PDF viewer is active.
JS_FETCH_PAGE_PDF = """
async (url) => {
    try {
        const resp = await fetch(url, {credentials: 'include'});
        if (!resp.ok) return null;
        const ct = resp.headers.get('content-type') || '';
        const buf = await resp.arrayBuffer();
        if (buf.byteLength < 5000) return null;
        const bytes = new Uint8Array(buf);
        const startsPdf = bytes.length >= 5
            && bytes[0] === 0x25
            && bytes[1] === 0x50
            && bytes[2] === 0x44
            && bytes[3] === 0x46
            && bytes[4] === 0x2D;
        if (!ct.toLowerCase().includes('pdf') && !startsPdf) return null;
        let binary = '';
        const chunk = 8192;
        for (let i = 0; i < bytes.length; i += chunk) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, Math.min(i + chunk, bytes.length)));
        }
        return btoa(binary);
    } catch(e) { return null; }
}
"""


async def fetch_pdf_via_page_context(page: Page, url: str, dest: Path, stage: str, source: str) -> Dict[str, Any]:
    log_event(stage, source, "start", url, "page_context_fetch")
    try:
        pdf_b64 = await page.evaluate(JS_FETCH_PAGE_PDF, url)
    except Exception as e:
        log_event(stage, source, STATUS_FAILED, url, str(e)[:120])
        return make_attempt(STATUS_FAILED, "page_context_fetch_exception")

    if not pdf_b64:
        log_event(stage, source, STATUS_FAILED, url, "empty_or_not_pdf")
        return make_attempt(STATUS_FAILED, "page_context_fetch_empty")

    data = base64.b64decode(pdf_b64)
    dest.write_bytes(data)
    ok, reason = looks_like_valid_pdf(dest, stage)
    if not ok:
        try:
            dest.unlink()
        except Exception:
            pass
        log_event(stage, source, STATUS_FAILED, url, reason)
        return make_attempt(STATUS_FAILED, reason)

    kb = len(data) // 1024
    log_event(stage, source, STATUS_DOWNLOADED, url, f"{kb} KB")
    return make_attempt(STATUS_DOWNLOADED, "page_context_fetch", kb)


def normalize_href(base_url: str, href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    return urljoin(base_url, href)


def lower_unquoted(text: str) -> str:
    return unquote(text or "").lower()


def host_matches_suffix(host: str, suffixes: tuple[str, ...]) -> bool:
    host = (host or "").lower()
    return any(host == suffix.lstrip(".") or host.endswith(suffix) for suffix in suffixes)


def same_wiley_host_family(candidate_host: str, resolved_host: str) -> bool:
    suffixes = (".onlinelibrary.wiley.com", ".wiley.com")
    return host_matches_suffix(candidate_host, suffixes) and host_matches_suffix(resolved_host, suffixes)


def filter_wiley_main_pdf_candidates(candidates: List[str], resolved_url: str, doi: str) -> tuple[List[str], List[Dict[str, str]]]:
    resolved_host = (urlparse(resolved_url).netloc or "").lower()
    filtered: List[str] = []
    rejected: List[Dict[str, str]] = []
    doi_token = lower_unquoted(doi)

    for href in candidates:
        parsed = urlparse(href)
        host = (parsed.netloc or "").lower()
        lowered = lower_unquoted(href)
        path = lower_unquoted(parsed.path)
        reason = ""

        if not same_wiley_host_family(host, resolved_host):
            reason = "external_host"
        elif any(tok in lowered for tok in ("downloadsupplement", "pb-assets", "supporting-information", "supportinginformation")):
            reason = "supplementary_or_asset"
        elif any(tok in lowered for tok in ("supplement", "suppl", "_si_")) or "/si/" in path:
            reason = "supplementary_or_asset"
        elif not any(tok in path for tok in ("/doi/pdf/", "/doi/epdf/", "pdfdirect")):
            reason = "not_main_pdf_route"
        elif doi_token not in lowered:
            reason = "doi_mismatch"

        if reason:
            rejected.append({"url": href, "reason": reason})
            continue
        if href not in filtered:
            filtered.append(href)

    return filtered, rejected


def extract_elsevier_pii(url: str) -> Optional[str]:
    match = re.search(r"/pii/([A-Za-z0-9]+)", url or "")
    return match.group(1) if match else None


def elsevier_article_url_from_pii(pii: str) -> str:
    return f"https://www.sciencedirect.com/science/article/pii/{pii}"


def is_elsevier_crasolve_shell(url: str) -> bool:
    lowered = (url or "").lower()
    return "sciencedirect.com" in lowered and "crasolve=1" in lowered and "/pdfft" in lowered


def looks_like_valid_pdf(path: Path, stage: str) -> tuple[bool, str]:
    if not path.exists():
        return False, "file_missing"

    size = path.stat().st_size
    if size < 5000:
        return False, "too_small"

    with open(path, "rb") as f:
        head = f.read(4096)
        tail_size = min(4096, size)
        f.seek(max(size - tail_size, 0))
        tail = f.read(tail_size)

    if b"%PDF-" not in head[:64]:
        return False, "missing_pdf_header"
    if b"%%EOF" not in tail:
        return False, "missing_eof"

    upper_head = head.upper()
    if stage != "si" and b"SUPPLEMENT" in upper_head and size < 300 * 1024:
        return False, "supplementary_detected"

    decoded = head.decode("utf-8", errors="ignore").lower()
    if "<html" in decoded or "<!doctype" in decoded:
        return False, "html_instead_of_pdf"
    if any(t in decoded for t in _auth_page_titles()):
        return False, "auth_page_instead_of_pdf"

    return True, "ok"


async def maybe_wait_for_viewer_settle(page: Page, stage: str, reason: str = ""):
    lowered = (page.url or "").lower()
    detail = reason or "viewer_settle"
    if "science.org" in lowered and "downloadsupplement" in lowered:
        detail = reason or "science_downloadSupplement"
    elif "wiley.com" in lowered and "downloadsupplement" in lowered:
        detail = reason or "wiley_downloadSupplement"
    else:
        return

    log_event(stage, "viewer_settle", "start", page.url, detail)
    for state in ("domcontentloaded", "load", "networkidle"):
        try:
            await page.wait_for_load_state(state, timeout=5_000)
        except Exception:
            continue
    await page.wait_for_timeout(500)
    log_event(stage, "viewer_settle", "ready", page.url, detail)


def direct_capture_wait_cycles(url: str, stage: str) -> int:
    lowered = lower_unquoted(url)
    if stage == "si" and "downloadsupplement" in lowered:
        return 60
    if "pdf.sciencedirectassets.com" in lowered or "main.pdf" in lowered:
        return 25
    return DIRECT_CAPTURE_WAIT_CYCLES_DEFAULT


def is_probable_pdf_source_url(url: str) -> bool:
    lowered = lower_unquoted(url)
    if not lowered or lowered.startswith("javascript:") or lowered.endswith("#"):
        return False
    if any(token in lowered for token in VIEWER_SOURCE_BLOCKLIST):
        return False
    if url_asset_extension(url) == ".pdf":
        return True
    parsed = urlparse(lowered)
    if "downloadsupplement" in parsed.path and ".pdf" in parsed.query:
        return True
    if re.search(r"(?:^|[&;])(?:file|filename|download|name|attachment)=[^&;#]+\.pdf(?:$|[&;#])", parsed.query):
        return True
    if "mimetype=application/pdf" in lowered:
        return True
    return any(
        token in lowered
        for token in (
            "/pdf/",
            "/pdf?",
            "pdfdirect",
            "pdfft",
            "main.pdf",
            "/content/pdf/",
            "/docserver/fulltext/",
            "/stamp/stamp.jsp",
        )
    )


def response_content_type(headers: Dict[str, Any]) -> str:
    for key, value in (headers or {}).items():
        if str(key).lower() == "content-type":
            return str(value or "")
    return ""


def cdp_response_is_pdf_candidate(url: str, headers: Dict[str, Any]) -> bool:
    ct = response_content_type(headers)
    return "pdf" in ct.lower() or is_probable_pdf_source_url(url)


async def start_cdp_pdf_capture(ctx: BrowserContext, page: Page, stage: str, hint_url: str = "") -> Dict[str, Any]:
    capture: Dict[str, Any] = {"client": None, "requests": {}}
    try:
        client = await ctx.new_cdp_session(page)
        await client.send(
            "Network.enable",
            {
                "maxTotalBufferSize": 100 * 1024 * 1024,
                "maxResourceBufferSize": 50 * 1024 * 1024,
            },
        )
    except Exception as e:
        log_event(stage, "cdp_capture", STATUS_FAILED, hint_url, str(e)[:120])
        return capture

    capture["client"] = client

    def on_response(params: Dict[str, Any]):
        try:
            request_id = params.get("requestId")
            response = params.get("response") or {}
            url = response.get("url") or ""
            headers = response.get("headers") or {}
            if not request_id or not cdp_response_is_pdf_candidate(url, headers):
                return
            ct = response_content_type(headers)
            capture["requests"][request_id] = {
                "url": url,
                "content_type": ct,
                "finished": False,
                "failed": False,
            }
            log_event(stage, "cdp_response", "candidate", url, ct[:80])
        except Exception:
            pass

    def on_finished(params: Dict[str, Any]):
        request = capture["requests"].get(params.get("requestId"))
        if request is not None:
            request["finished"] = True

    def on_failed(params: Dict[str, Any]):
        request = capture["requests"].get(params.get("requestId"))
        if request is not None:
            request["failed"] = True
            request["error_text"] = params.get("errorText") or ""

    client.on("Network.responseReceived", on_response)
    client.on("Network.loadingFinished", on_finished)
    client.on("Network.loadingFailed", on_failed)
    return capture


async def save_from_cdp_pdf_capture(capture: Dict[str, Any], dest: Path, stage: str) -> Dict[str, Any]:
    client = capture.get("client")
    requests = capture.get("requests") or {}
    if not client or not requests:
        return make_attempt(STATUS_FAILED, "cdp_no_pdf_candidate")

    for request_id, item in list(requests.items()):
        if item.get("saved") or item.get("failed") or not item.get("finished"):
            continue
        url = item.get("url") or ""
        try:
            body_info = await client.send("Network.getResponseBody", {"requestId": request_id})
        except Exception as e:
            log_event(stage, "cdp_getResponseBody", STATUS_FAILED, url, str(e)[:120])
            item["saved"] = True
            continue

        raw_body = body_info.get("body") or ""
        try:
            if body_info.get("base64Encoded"):
                data = base64.b64decode(raw_body)
            else:
                data = raw_body.encode("utf-8", errors="ignore")
        except Exception as e:
            log_event(stage, "cdp_response_body", STATUS_FAILED, url, str(e)[:120])
            item["saved"] = True
            continue

        if not data or len(data) <= 5000:
            log_event(stage, "cdp_response_body", STATUS_FAILED, url, "too_small")
            item["saved"] = True
            continue

        dest.write_bytes(data)
        ok, reason = looks_like_valid_pdf(dest, stage)
        if ok:
            kb = len(data) // 1024
            log_event(stage, "cdp_response_body", STATUS_DOWNLOADED, url, f"{kb} KB")
            return make_attempt(STATUS_DOWNLOADED, "cdp_response_body", kb)

        try:
            dest.unlink()
        except Exception:
            pass
        log_event(stage, "cdp_response_body", STATUS_FAILED, url, reason)
        item["saved"] = True

    pending = sum(1 for item in requests.values() if not item.get("finished") and not item.get("failed"))
    if pending:
        log_event(stage, "cdp_capture", "pending", "", f"{pending} candidate response(s) not finished")
    return make_attempt(STATUS_FAILED, "cdp_no_pdf_body")


async def stop_cdp_pdf_capture(capture: Optional[Dict[str, Any]], stage: str):
    if not capture:
        return
    client = capture.get("client")
    if not client:
        return
    try:
        await client.detach()
    except Exception as e:
        log_event(stage, "cdp_capture", "detach_failed", "", str(e)[:120])


async def inspect_pdf_surface(page: Page) -> Dict[str, Any]:
    cur_url = page.url or ""
    current_url_pdf_like = is_probable_pdf_source_url(cur_url)
    try:
        data = await page.evaluate(
            """() => ({
                title: document.title || '',
                bodyText: (document.body && document.body.innerText || '').slice(0, 2000),
                iframeSrcs: Array.from(document.querySelectorAll('iframe[src]')).map((n) => n.src || ''),
                embedSrcs: Array.from(document.querySelectorAll('embed[src]')).map((n) => n.src || ''),
                objectData: Array.from(document.querySelectorAll('object[data]')).map((n) => n.data || ''),
                anchorHrefs: Array.from(document.querySelectorAll('a[href]')).map((n) => n.href || '').slice(0, 300),
            })"""
        )
    except Exception:
        data = {}

    title = (data.get("title") or "").strip()
    body_text = (data.get("bodyText") or "").strip().lower()
    embedded_urls = []
    for key in ("iframeSrcs", "embedSrcs", "objectData"):
        value = data.get(key) or []
        if isinstance(value, list):
            embedded_urls.extend([u for u in value if isinstance(u, str)])
    anchor_urls = data.get("anchorHrefs") or []
    if not isinstance(anchor_urls, list):
        anchor_urls = []

    source_urls = unique_preserve_order([u for u in embedded_urls if is_probable_pdf_source_url(u)])
    anchor_pdf_urls = unique_preserve_order([u for u in anchor_urls if isinstance(u, str) and is_probable_pdf_source_url(u)])

    has_download_control = False
    for sel in VIEWER_DOWNLOAD_SELECTORS:
        try:
            locator = page.locator(sel).first
            if await locator.count() > 0 and await locator.is_visible():
                has_download_control = True
                break
        except Exception:
            continue

    viewerish = current_url_pdf_like or bool(source_urls)

    return {
        "current_url_pdf_like": current_url_pdf_like,
        "source_urls": source_urls,
        "anchor_pdf_urls": anchor_pdf_urls,
        "has_download_control": has_download_control,
        "viewerish": viewerish,
        "title": title,
    }


async def fetch_pdf_from_viewer(page: Page, ctx: BrowserContext, dest: Path, stage: str) -> Dict[str, Any]:
    cur_url = page.url
    surface = await inspect_pdf_surface(page)
    if not surface["viewerish"]:
        return make_attempt(STATUS_FAILED, "viewer_not_pdf_like")

    barrier = await inspect_access_barrier(page)
    if barrier:
        log_event(stage, "viewer_fetch_barrier", STATUS_MANUAL, barrier["url"], barrier["reason"])
        return make_attempt(STATUS_MANUAL, barrier["reason"])

    print("       → PDF viewer detected, fetching via page JS...")
    log_event(stage, "viewer_fetch", "start", cur_url, "")
    download_obj = None
    download_body = None

    def on_download(dl):
        nonlocal download_obj
        if download_obj is None:
            download_obj = dl

    async def on_response(resp):
        nonlocal download_body
        if download_body is not None:
            return
        try:
            ct = resp.headers.get("content-type", "")
            if "pdf" in ct.lower() and resp.ok:
                body = await resp.body()
                if body and len(body) > 5000:
                    download_body = body
        except Exception:
            pass

    page.on("download", on_download)
    page.on("response", on_response)
    try:
        def save_pdf_bytes(data: bytes, source: str) -> Dict[str, Any]:
            dest.write_bytes(data)
            ok, reason = looks_like_valid_pdf(dest, stage)
            if not ok:
                try:
                    dest.unlink()
                except Exception:
                    pass
                log_event(stage, source, STATUS_FAILED, cur_url, reason)
                return make_attempt(STATUS_FAILED, reason)
            kb = len(data) // 1024
            return make_attempt(STATUS_DOWNLOADED, source, kb)

        async def save_via_ctx_request(url: str, source: str) -> Dict[str, Any]:
            try:
                resp = await ctx.request.get(url, timeout=DL_TIMEOUT, max_redirects=5)
                ct = resp.headers.get("content-type", "")
                log_event(stage, source, f"http_{resp.status}", url, ct[:80])
                if not resp.ok:
                    return make_attempt(STATUS_FAILED, "viewer_ctx_request_failed")
                if "pdf" not in ct.lower() and not is_probable_pdf_source_url(url):
                    return make_attempt(STATUS_FAILED, "viewer_ctx_request_not_pdf")
                body = await resp.body()
                if not body or len(body) <= 5000:
                    return make_attempt(STATUS_FAILED, "viewer_ctx_request_too_small")
                saved = save_pdf_bytes(body, source)
                if saved["state"] == STATUS_DOWNLOADED:
                    print(f"       ✓ fetched from viewer url ({saved['size_kb']} KB)")
                    log_event(stage, source, STATUS_DOWNLOADED, url, f"{saved['size_kb']} KB")
                return saved
            except Exception as e:
                log_event(stage, source, STATUS_FAILED, url, str(e)[:120])
                return make_attempt(STATUS_FAILED, "viewer_ctx_request_exception")

        if surface["current_url_pdf_like"]:
            saved = await save_via_ctx_request(cur_url, "viewer_ctx_request")
            if saved["state"] == STATUS_DOWNLOADED:
                return saved
            try:
                pdf_b64 = await page.evaluate(JS_FETCH_PAGE_PDF, cur_url)
            except Exception as e:
                msg = str(e)
                if "Execution context was destroyed" in msg:
                    log_event(stage, "viewer_fetch", "science_viewer_navigation_race", cur_url, msg[:120])
                    await maybe_wait_for_viewer_settle(page, stage, "retry_after_navigation_race")
                    cur_url = page.url
                    try:
                        log_event(stage, "viewer_fetch_retry", "start", cur_url, "retry_after_navigation_race")
                        pdf_b64 = await page.evaluate(JS_FETCH_PAGE_PDF, cur_url)
                    except Exception as retry_e:
                        log_event(stage, "viewer_fetch_retry", STATUS_FAILED, cur_url, str(retry_e)[:120])
                        pdf_b64 = None
                else:
                    log_event(stage, "viewer_fetch", STATUS_FAILED, cur_url, msg[:120])
                    pdf_b64 = None

            if pdf_b64:
                data = base64.b64decode(pdf_b64)
                saved = save_pdf_bytes(data, "viewer_fetch")
                if saved["state"] == STATUS_DOWNLOADED:
                    print(f"       ✓ extracted from viewer ({saved['size_kb']} KB)")
                    log_event(stage, "viewer_fetch", STATUS_DOWNLOADED, cur_url, f"{saved['size_kb']} KB")
                    return saved
                return saved

            print("       → JS fetch returned empty")
            log_event(stage, "viewer_fetch", STATUS_FAILED, cur_url, "empty")

        for source_url in unique_preserve_order(surface["source_urls"] + surface.get("anchor_pdf_urls", [])):
            if source_url == cur_url:
                continue
            saved = await save_via_ctx_request(source_url, "viewer_source_ctx_request")
            if saved["state"] == STATUS_DOWNLOADED:
                return saved
            log_event(stage, "viewer_source_fetch", "start", source_url, surface["title"][:80])
            try:
                source_b64 = await page.evaluate(JS_FETCH_PAGE_PDF, source_url)
            except Exception as e:
                log_event(stage, "viewer_source_fetch", STATUS_FAILED, source_url, str(e)[:120])
                continue
            if not source_b64:
                log_event(stage, "viewer_source_fetch", STATUS_FAILED, source_url, "empty_or_not_pdf")
                continue
            data = base64.b64decode(source_b64)
            saved = save_pdf_bytes(data, "viewer_source_fetch")
            if saved["state"] == STATUS_DOWNLOADED:
                print(f"       ✓ extracted from viewer source ({saved['size_kb']} KB)")
                log_event(stage, "viewer_source_fetch", STATUS_DOWNLOADED, source_url, f"{saved['size_kb']} KB")
                return saved

        for sel in VIEWER_DOWNLOAD_SELECTORS:
            try:
                locator = page.locator(sel).first
                if await locator.count() == 0 or not await locator.is_visible():
                    continue
                print(f"       → clicking viewer download control: {sel}")
                log_event(stage, "viewer_download_click", "clicked", page.url, sel)
                await locator.click(timeout=2_000)
                for _ in range(20):
                    if download_obj or download_body:
                        break
                    await page.wait_for_timeout(200)

                if download_obj:
                    saved = await save_download(download_obj, dest, stage)
                    if saved["state"] == STATUS_DOWNLOADED:
                        print(f"       ✓ downloaded from viewer control ({saved['size_kb']} KB)")
                        log_event(stage, "viewer_download_click", STATUS_DOWNLOADED, page.url, f"{saved['size_kb']} KB")
                        return saved
                    log_event(stage, "viewer_download_click", STATUS_FAILED, page.url, saved["reason"])

                if download_body and len(download_body) > 5000:
                    dest.write_bytes(download_body)
                    ok, reason = looks_like_valid_pdf(dest, stage)
                    if ok:
                        kb = len(download_body) // 1024
                        print(f"       ✓ saved from viewer response ({kb} KB)")
                        log_event(stage, "viewer_download_click", STATUS_DOWNLOADED, page.url, f"{kb} KB")
                        return make_attempt(STATUS_DOWNLOADED, "viewer_download_click", kb)
                    try:
                        dest.unlink()
                    except Exception:
                        pass
                    log_event(stage, "viewer_download_click", STATUS_FAILED, page.url, reason)
            except Exception as e:
                log_event(stage, "viewer_download_click", STATUS_FAILED, page.url, f"{sel}: {str(e)[:80]}")

        return make_attempt(STATUS_MANUAL, "viewer_capture_failed")
    finally:
        try:
            page.remove_listener("download", on_download)
            page.remove_listener("response", on_response)
        except Exception:
            pass


def prioritized_browser_navigation_candidates(
    publisher: str,
    current_url: str,
    candidates: List[str],
    surface: Optional[Dict[str, Any]] = None,
) -> List[str]:
    urls = list(candidates or [])
    if surface:
        urls = list(surface.get("anchor_pdf_urls") or []) + urls
    urls = unique_preserve_order([u for u in urls if is_probable_pdf_source_url(u)])
    if not urls:
        return []

    if publisher != "elsevier":
        return urls[:3]

    pii = extract_elsevier_pii(current_url)

    def score(url: str) -> tuple[int, int]:
        lowered = lower_unquoted(url)
        s = 0
        if pii and pii.lower() in lowered:
            s += 5
        if "main.pdf" in lowered:
            s += 4
        if "/pdfft" in lowered:
            s += 3
        if "sciencedirect.com" in lowered:
            s += 2
        return (-s, len(url))

    return sorted(urls, key=score)[:3]


async def try_browser_pdf_navigation_candidate(
    ctx: BrowserContext,
    url: str,
    dest: Path,
    stage: str,
) -> Dict[str, Any]:
    nav_page = await ctx.new_page()
    keep_page = False
    try:
        print(f"       → browser-nav candidate: {url[:90]}")
        log_event(stage, "viewer_browser_nav", "start", url, "")
        attempt = await try_direct_pdf(
            nav_page,
            ctx,
            url,
            dest,
            stage=stage,
            allow_navigation=True,
            close_other_pages=False,
        )
        if attempt["state"] == STATUS_MANUAL:
            if not is_auto_mode():
                keep_page = True
                preserve_manual_page(nav_page, stage, attempt["reason"])
            else:
                keep_page = True
                return with_auto_retry_page(attempt["reason"], nav_page)
        return attempt
    finally:
        if not keep_page:
            try:
                await nav_page.close()
            except Exception:
                pass


async def save_download(dl, dest: Path, stage: str) -> Dict[str, Any]:
    tmp = await dl.path()
    if not tmp:
        return make_attempt(STATUS_FAILED, "download_path_missing")
    shutil.copy(tmp, dest)
    ok, reason = looks_like_valid_pdf(dest, stage)
    if not ok:
        try:
            dest.unlink()
        except Exception:
            pass
        return make_attempt(STATUS_FAILED, reason)
    return make_attempt(STATUS_DOWNLOADED, "downloaded", dest.stat().st_size // 1024)


async def collect_candidate_urls(page: Page, publisher: str, allow_supplementary: bool = False) -> List[str]:
    candidates: List[str] = []
    base = page.url

    for sel in PDF_SELECTORS.get(publisher, ['a:has-text("PDF")']):
        try:
            locator = page.locator(sel)
            count = min(await locator.count(), 3)
            for idx in range(count):
                href = await locator.nth(idx).get_attribute("href")
                full = normalize_href(base, href)
                if full:
                    candidates.append(full)
        except Exception:
            continue

    try:
        js_urls = await page.evaluate(JS_FIND_PDF_LINKS, {"allowSupplementary": allow_supplementary})
        if isinstance(js_urls, list):
            candidates.extend([u for u in js_urls if isinstance(u, str)])
    except Exception:
        pass

    return unique_preserve_order(candidates)


async def close_extra_pages(ctx: BrowserContext, keep: Page):
    """Close any popup pages that aren't the main page."""
    preserved = set(preserved_pages())
    for p in ctx.pages:
        if p != keep and p not in preserved:
            try:
                await p.close()
            except Exception:
                pass


async def try_direct_pdf(
    page: Page,
    ctx: BrowserContext,
    url: str,
    dest: Path,
    stage: str = "main_pdf",
    allow_navigation: bool = True,
    close_other_pages: bool = True,
    timeout: int = DL_TIMEOUT,
) -> Dict[str, Any]:
    print(f"       → direct: {url}")
    log_event(stage, "direct_start", "start", url, f"allow_navigation={allow_navigation}")

    try:
        resp = await ctx.request.get(url, timeout=timeout, max_redirects=5)
        ct = resp.headers.get("content-type", "")
        log_event(stage, "ctx.request.get", f"http_{resp.status}", url, ct[:80])
        if resp.ok and ("pdf" in ct.lower() or url.lower().endswith(".pdf") or "/pdf/" in url.lower() or "pdfft" in url.lower()):
            body = await resp.body()
            if body and len(body) > 5000:
                dest.write_bytes(body)
                ok, reason = looks_like_valid_pdf(dest, stage)
                if ok:
                    kb = len(body) // 1024
                    print(f"       ✓ fetched ({kb} KB)")
                    log_event(stage, "ctx.request.get", STATUS_DOWNLOADED, url, f"{kb} KB")
                    return make_attempt(STATUS_DOWNLOADED, "ctx_request_fetch", kb)
                try:
                    dest.unlink()
                except Exception:
                    pass
                log_event(stage, "ctx.request.get", STATUS_FAILED, url, reason)
                return make_attempt(STATUS_FAILED, reason)
            log_event(stage, "ctx.request.get", STATUS_FAILED, url, "too_small")
            return make_attempt(STATUS_FAILED, "too_small")
    except Exception as e:
        log_event(stage, "ctx.request.get", STATUS_FAILED, url, str(e)[:120])

    if not allow_navigation:
        return make_attempt(STATUS_FAILED, "fetch_only_failed")

    pdf_body = None
    dl_obj = None
    new_page_pdf = None
    cdp_capture = None

    async def on_response(resp):
        nonlocal pdf_body
        try:
            ct = resp.headers.get("content-type", "")
            if "pdf" in ct.lower() and resp.ok and pdf_body is None:
                pdf_body = await resp.body()
        except Exception:
            pass

    def on_download(dl):
        nonlocal dl_obj
        if dl_obj is None:
            dl_obj = dl

    page.on("response", on_response)
    page.on("download", on_download)

    try:
        cdp_capture = await start_cdp_pdf_capture(ctx, page, stage, url)
        try:
            await page.goto(url, wait_until="load", timeout=timeout)
        except PlaywrightError as e:
            if "ERR_ABORTED" in str(e) and page.url and page.url != "about:blank":
                log_event(stage, "page.goto", "aborted_continue", page.url, str(e)[:120])
            else:
                log_event(stage, "page.goto", STATUS_FAILED, url, str(e)[:120])
        await page.wait_for_timeout(200)
        await handle_access_barrier(page, stage)

        wait_cycles = direct_capture_wait_cycles(page.url or url, stage)
        if wait_cycles > DIRECT_CAPTURE_WAIT_CYCLES_DEFAULT:
            log_event(stage, "direct_capture_wait", "extended", page.url or url, f"cycles={wait_cycles}")
        for _ in range(wait_cycles):
            if dl_obj or pdf_body:
                break
            await page.wait_for_timeout(200)

        cdp_attempt = await save_from_cdp_pdf_capture(cdp_capture, dest, stage)
        if cdp_attempt["state"] == STATUS_DOWNLOADED:
            print(f"       ✓ saved from browser network ({cdp_attempt['size_kb']} KB)")
            return cdp_attempt

        if dl_obj:
            saved = await save_download(dl_obj, dest, stage)
            if saved["state"] == STATUS_DOWNLOADED:
                print(f"       ✓ downloaded ({saved['size_kb']} KB)")
                log_event(stage, "download_event", STATUS_DOWNLOADED, page.url, f"{saved['size_kb']} KB")
                return saved
            log_event(stage, "download_event", STATUS_FAILED, page.url, saved["reason"])

        if pdf_body and len(pdf_body) > 5000:
            dest.write_bytes(pdf_body)
            ok, reason = looks_like_valid_pdf(dest, stage)
            if ok:
                kb = len(pdf_body) // 1024
                print(f"       ✓ saved from response ({kb} KB)")
                log_event(stage, "response_body", STATUS_DOWNLOADED, page.url, f"{kb} KB")
                return make_attempt(STATUS_DOWNLOADED, "response_body", kb)
            try:
                dest.unlink()
            except Exception:
                pass
            log_event(stage, "response_body", STATUS_FAILED, page.url, reason)

        await maybe_wait_for_viewer_settle(page, stage, "post_direct_navigation")
        cdp_attempt = await save_from_cdp_pdf_capture(cdp_capture, dest, stage)
        if cdp_attempt["state"] == STATUS_DOWNLOADED:
            print(f"       ✓ saved from browser network ({cdp_attempt['size_kb']} KB)")
            return cdp_attempt

        viewer_attempt = await fetch_pdf_from_viewer(page, ctx, dest, stage)
        if viewer_attempt["state"] != STATUS_FAILED:
            return viewer_attempt

        try:
            cur_url = page.url
            title = await page.title()
            if title:
                print(f"       → got: {title[:50]} ({cur_url[:60]})")
            else:
                print(f"       → no PDF captured from: {cur_url[:60]}")
        except Exception:
            print("       → no PDF captured (page unresponsive)")

        log_event(stage, "direct_complete", STATUS_FAILED, page.url, "no_pdf_captured")
        return make_attempt(STATUS_FAILED, "no_pdf_captured")
    except ManualInterventionRequired as e:
        return make_attempt(STATUS_MANUAL, e.reason)
    finally:
        try:
            page.remove_listener("response", on_response)
            page.remove_listener("download", on_download)
        except Exception:
            pass
        await stop_cdp_pdf_capture(cdp_capture, stage)
        if close_other_pages:
            await close_extra_pages(ctx, page)


def url_asset_extension(url: str) -> str:
    ext = Path(urlparse(url or "").path).suffix.lower()
    return ext[:12]


def is_non_pdf_asset_url(url: str) -> bool:
    ext = url_asset_extension(url)
    return bool(ext) and ext != ".pdf"


def body_looks_like_html(body: bytes) -> bool:
    head = body[:2048].decode("utf-8", errors="ignore").lower()
    if "<html" in head or "<!doctype" in head:
        return True
    return any(t in head for t in _auth_page_titles())


def find_existing_si_asset(base: Path) -> Optional[Path]:
    for path in sorted(base.parent.glob(base.name + ".*")):
        if path.is_file():
            return path
    return None


async def try_direct_asset(
    page: Page,
    ctx: BrowserContext,
    url: str,
    dest_base: Path,
    stage: str = "si",
    timeout: int = DL_TIMEOUT,
) -> Dict[str, Any]:
    ext = url_asset_extension(url) or ".bin"
    dest = dest_base.with_suffix(ext)
    print(f"       → asset: {url}")
    log_event(stage, "asset_start", "start", url, ext)

    try:
        resp = await ctx.request.get(url, timeout=timeout, max_redirects=5)
        ct = resp.headers.get("content-type", "")
        log_event(stage, "asset_request.get", f"http_{resp.status}", url, ct[:80])
        if resp.ok:
            body = await resp.body()
            if body and not body_looks_like_html(body):
                dest.write_bytes(body)
                kb = max(1, len(body) // 1024)
                log_event(stage, "asset_request.get", STATUS_DOWNLOADED, url, f"{ext} {kb} KB")
                return {
                    "state": STATUS_DOWNLOADED,
                    "reason": "non_pdf_asset_saved",
                    "size_kb": kb,
                    "asset_ext": ext,
                    "asset_path": str(dest),
                }
    except Exception as e:
        log_event(stage, "asset_request.get", STATUS_FAILED, url, str(e)[:120])

    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except PlaywrightError as e:
            if "ERR_ABORTED" in str(e) and page.url and page.url != "about:blank":
                log_event(stage, "asset_page.goto", "aborted_continue", page.url, str(e)[:120])
            else:
                log_event(stage, "asset_page.goto", STATUS_FAILED, url, str(e)[:120])
        await page.wait_for_timeout(200)
        await handle_access_barrier(page, stage)
    except ManualInterventionRequired as e:
        return make_attempt(STATUS_MANUAL, e.reason)

    return make_attempt(STATUS_FAILED, "asset_fetch_failed")


def normalize_si_link(base_url: str, href: str) -> Optional[str]:
    full = normalize_href(base_url, href)
    if not full:
        return None
    if full.startswith("javascript:") or full.endswith("#"):
        return None
    parsed = urlparse(full)
    if not parsed.scheme.startswith("http"):
        return None
    normalized = html_lib.unescape(full.split("#", 1)[0])
    return normalized


def si_link_score(url: str, publisher: str, text: str = "") -> int:
    lowered = lower_unquoted(url)
    text_lower = (text or "").lower()
    ext = url_asset_extension(url)
    score = 0

    if publisher == "wiley" and "downloadsupplement" in lowered:
        score += 120
    if publisher == "acs" and "/doi/suppl/" in lowered:
        score += 110
    if publisher == "rsc" and "rsc.org/suppdata/" in lowered:
        score += 110
    if publisher in ("nature", "springer") and "static-content.springer.com/esm/" in lowered:
        score += 110
    if publisher == "elsevier" and re.search(r"(?:-|\b)mmc\d+\.", lowered):
        score += 110

    if "downloadsupplement" in lowered:
        score += 80
    if "/doi/suppl/" in lowered:
        score += 75
    if "/suppdata/" in lowered:
        score += 75
    if "/esm/" in lowered or "_esm" in lowered:
        score += 75
    if re.search(r"mmc\d+\.", lowered):
        score += 70
    if any(token in lowered for token in ("supplement", "supporting", "suppl", "_si_", "-si-", "_si.", "appendix")):
        score += 25
    if any(token in text_lower for token in ("supplement", "supporting", "supplementary", "appendix")):
        score += 15

    if ext == ".pdf":
        score += 40
    elif ext in SI_DIRECT_FILE_EXTENSIONS:
        score += 30

    if "/article/" in lowered and ext not in SI_DIRECT_FILE_EXTENSIONS and "downloadsupplement" not in lowered:
        score -= 40
    if "googlescholar" in lowered or "getftrlinkout" in lowered:
        score -= 80

    return score


def extract_si_links_from_html(html: str, base_url: str, publisher: str) -> List[str]:
    links: List[str] = []
    patterns = list(SI_REGEX_PATTERNS.get("generic", []))
    if publisher in SI_REGEX_PATTERNS:
        patterns = SI_REGEX_PATTERNS[publisher] + patterns

    for pat in patterns:
        for match in re.findall(pat, html, re.IGNORECASE):
            link = match if isinstance(match, str) else match[0]
            full = normalize_si_link(base_url, link)
            if full and full not in links:
                links.append(full)
    return links


def is_probable_si_anchor(url: str, text: str, publisher: str) -> bool:
    lowered = lower_unquoted(url)
    text_lower = (text or "").lower()
    ext = url_asset_extension(url)

    if lowered.startswith("javascript:") or lowered.endswith("#"):
        return False

    if publisher == "wiley" and "downloadsupplement" in lowered:
        return True
    if publisher == "acs" and "/doi/suppl/" in lowered:
        return True
    if publisher == "rsc" and "rsc.org/suppdata/" in lowered:
        return True
    if publisher in ("nature", "springer") and "static-content.springer.com/esm/" in lowered:
        return True
    if publisher == "elsevier" and re.search(r"(?:-|\b)mmc\d+\.", lowered):
        return True

    generic_route_hit = any(
        token in lowered
        for token in ("downloadsupplement", "/doi/suppl/", "/suppdata/", "/esm/", "_esm")
    )
    if generic_route_hit:
        return True

    if ext in SI_DIRECT_FILE_EXTENSIONS and any(
        token in lowered for token in ("supp", "suppl", "support", "appendix", "mmc", "esm")
    ):
        return True

    if ext in SI_DIRECT_FILE_EXTENSIONS and any(
        token in text_lower for token in ("supplement", "supporting", "supplementary", "appendix")
    ):
        return True

    return False


async def collect_anchor_records(page: Page) -> List[Dict[str, str]]:
    try:
        anchors = await page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href]')).map((a) => ({
                href: a.href || '',
                text: (a.textContent || '').trim(),
            }))"""
        )
    except Exception:
        anchors = []
    if not isinstance(anchors, list):
        return []
    out: List[Dict[str, str]] = []
    for item in anchors:
        if not isinstance(item, dict):
            continue
        href = (item.get("href") or "").strip()
        if not href:
            continue
        out.append({"href": href, "text": (item.get("text") or "").strip()})
    return out


async def expand_si_disclosures(page: Page, publisher: str, pass_no: int) -> int:
    if publisher != "wiley":
        return 0
    try:
        result = await page.evaluate(
            """() => {
                const textRe = /supporting\\s+information|supporting\\s+material|supporting\\s+materials|supplementary\\s+information|supplementary\\s+material|supplementary\\s+materials|supplemental\\s+material/i;
                const skipHrefRe = /downloadSupplement|\\/doi\\/pdf|\\/doi\\/epdf|pdfdirect|\\.pdf(?:$|[?#])/i;
                const selectors = [
                    'button',
                    'summary',
                    '[role="button"]',
                    '[aria-expanded]',
                    '[aria-controls]',
                    'a[href^="#"]',
                    'a[aria-controls]',
                    '[data-toggle="collapse"]',
                    '[data-bs-toggle="collapse"]',
                    '.accordion__control',
                    '.accordion__button',
                    '.accordion__heading',
                    '.accordion__title'
                ].join(',');
                const nodes = Array.from(document.querySelectorAll(selectors));
                const clicked = [];

                for (const el of nodes) {
                    const text = [
                        el.innerText,
                        el.textContent,
                        el.getAttribute('aria-label'),
                        el.getAttribute('title'),
                        el.getAttribute('aria-controls'),
                        el.id
                    ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                    const href = el.href || el.getAttribute('href') || '';
                    if (!textRe.test(text)) continue;
                    if (skipHrefRe.test(href)) continue;

                    const expanded = (el.getAttribute('aria-expanded') || '').toLowerCase();
                    if (expanded === 'true') continue;

                    try {
                        el.scrollIntoView({block: 'center', inline: 'nearest'});
                        el.click();
                        clicked.push(`${el.tagName.toLowerCase()}:${text.slice(0, 100)}:${expanded || 'na'}`);
                    } catch (e) {}
                    if (clicked.length >= 4) break;
                }
                return clicked;
            }"""
        )
    except Exception as e:
        log_event("si", "expand_disclosure", STATUS_FAILED, page.url, str(e)[:120])
        return 0

    if not isinstance(result, list):
        result = []
    count = len(result)
    log_event("si", "expand_disclosure", "clicked" if count else "no_match", page.url, f"pass={pass_no} | " + " || ".join(result[:3]))
    if count:
        await page.wait_for_timeout(900)
    return count


async def get_si_links(page: Page, publisher: str = "", doi: str = "") -> List[str]:
    base_url = page.url
    budget_ms = SI_WAIT_BUDGET_MS.get(publisher, 800)
    deadline = time.monotonic() + (budget_ms / 1000.0)
    best_links: List[str] = []
    expand_passes = 0

    try:
        await page.wait_for_load_state("load", timeout=min(8_000, NAV_TIMEOUT + 3_000))
    except Exception:
        pass

    while True:
        try:
            base_url = page.url or base_url
            html = await page.content()
        except Exception:
            if time.monotonic() >= deadline:
                return best_links[:5]
            await page.wait_for_timeout(500)
            continue
        anchors = await collect_anchor_records(page)

        scored: List[tuple[int, str]] = []
        seen = set()

        for link in extract_si_links_from_html(html, base_url, publisher):
            score = si_link_score(link, publisher, "")
            if score > 0 and link not in seen:
                seen.add(link)
                scored.append((score, link))

        for item in anchors:
            link = normalize_si_link(base_url, item["href"])
            if not link or link in seen:
                continue
            if not is_probable_si_anchor(link, item["text"], publisher):
                continue
            score = si_link_score(link, publisher, item["text"])
            if score <= 0:
                continue
            seen.add(link)
            scored.append((score, link))

        scored.sort(key=lambda item: item[0], reverse=True)
        links = unique_preserve_order([link for _, link in scored])
        if links:
            return links[:5]
        best_links = links

        if publisher == "wiley" and expand_passes < 2:
            expand_passes += 1
            clicked = await expand_si_disclosures(page, publisher, expand_passes)
            if clicked:
                deadline = max(deadline, time.monotonic() + 2.0)
                continue

        if time.monotonic() >= deadline:
            return best_links[:5]

        await page.wait_for_timeout(500)


async def find_elsevier_pdf_selector(page: Page) -> tuple[str, str]:
    last_detail = "no_elsevier_pdf_selector"
    for sel in PDF_SELECTORS.get("elsevier", []):
        try:
            loc = page.locator(sel).first
            if await loc.count() <= 0:
                continue
            visible = await loc.is_visible(timeout=250)
            enabled = await loc.is_enabled(timeout=250)
            try:
                detail_text = await loc.evaluate(
                    """(node) => {
                        const href = node.href || node.getAttribute('href') || '';
                        const aria = node.getAttribute('aria-label') || '';
                        const target = node.getAttribute('target') || '';
                        return [href, aria, target].filter(Boolean).join(' ');
                    }"""
                )
            except Exception:
                detail_text = ""
            last_detail = f"{sel} visible={visible} enabled={enabled} {str(detail_text)[:120]}"
            if visible and enabled:
                return sel, last_detail
        except Exception as e:
            last_detail = f"{sel}: {str(e)[:80]}"
    return "", last_detail


async def wait_for_elsevier_pdf_button_ready(page: Page, stage: str) -> str:
    start = time.monotonic()
    logged_probe = False
    first_ready = ""
    last_detail = "not_checked"

    while True:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        sel, detail = await find_elsevier_pdf_selector(page)
        last_detail = detail
        if sel and not first_ready:
            first_ready = sel

        if elapsed_ms >= 5_000 and not logged_probe:
            log_event(
                stage,
                "elsevier_pdf_button_probe",
                "ready" if sel else "waiting",
                page.url,
                f"elapsed_ms={elapsed_ms} {detail}",
            )
            logged_probe = True

        if sel and elapsed_ms >= ELSEVIER_PRE_CLICK_MIN_WAIT_MS:
            log_event(
                stage,
                "elsevier_pdf_button_ready",
                "ready",
                page.url,
                f"elapsed_ms={elapsed_ms} {detail}",
            )
            return sel

        if elapsed_ms >= ELSEVIER_PRE_CLICK_MAX_WAIT_MS:
            log_event(
                stage,
                "elsevier_pdf_button_ready",
                "timeout",
                page.url,
                f"elapsed_ms={elapsed_ms} {last_detail}",
            )
            return first_ready

        await page.wait_for_timeout(500)


async def wait_for_elsevier_popup_after_click(
    ctx: BrowserContext,
    known_pages,
    stage: str,
    selector: str,
    retry_locator=None,
) -> Optional[Page]:
    start = time.monotonic()
    deadline = time.monotonic() + (ELSEVIER_POPUP_POLL_MS / 1000)
    candidate = None
    first_seen_at = 0.0
    retry_sent = False
    log_event(
        stage,
        "elsevier_popup_poll",
        "start",
        "",
        f"timeout_ms={ELSEVIER_POPUP_POLL_MS} selector={selector}",
    )

    while time.monotonic() < deadline:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        try:
            new_pages = [p for p in ctx.pages if p not in known_pages and not p.is_closed()]
        except Exception as e:
            log_event(stage, "elsevier_popup_poll", STATUS_FAILED, "", str(e)[:120])
            return None

        if new_pages:
            candidate = new_pages[-1]
            if not first_seen_at:
                first_seen_at = time.monotonic()
                log_event(stage, "elsevier_popup_poll", "seen", getattr(candidate, "url", ""), selector)
            try:
                candidate_url = candidate.url or ""
            except Exception:
                candidate_url = ""
            if candidate_url and candidate_url != "about:blank":
                log_event(stage, "elsevier_popup_poll", "captured", candidate_url, selector)
                return candidate
            if time.monotonic() - first_seen_at >= 2.0:
                log_event(stage, "elsevier_popup_poll", "captured_blank", candidate_url or "about:blank", selector)
                return candidate

        if retry_locator is not None and not retry_sent and candidate is None and elapsed_ms >= 10_000:
            try:
                await retry_locator.click()
                retry_sent = True
                log_event(stage, "elsevier_popup_poll", "retry_click_10s", "", selector)
            except Exception as e:
                retry_sent = True
                log_event(stage, "elsevier_popup_poll", STATUS_FAILED, "", f"retry_click_10s {str(e)[:100]}")

        await asyncio.sleep(0.25)

    log_event(stage, "elsevier_popup_poll", "timeout", "", selector)
    return candidate


async def wait_for_elsevier_popup_surface_ready(page: Page, stage: str) -> str:
    start = time.monotonic()
    deadline = start + (ELSEVIER_POPUP_SETTLE_MS / 1000)
    last_url = ""
    last_reason = ""
    logged_reason = ""
    log_event(
        stage,
        "elsevier_popup_settle",
        "start",
        page.url,
        f"timeout_ms={ELSEVIER_POPUP_SETTLE_MS}",
    )

    while time.monotonic() < deadline:
        for state in ("domcontentloaded", "load"):
            try:
                await page.wait_for_load_state(state, timeout=1_500)
            except Exception:
                pass

        cur_url = page.url or ""
        if cur_url != last_url:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            log_event(stage, "elsevier_popup_settle", "url", cur_url, f"elapsed_ms={elapsed_ms}")
            last_url = cur_url

        barrier = None
        try:
            barrier = await inspect_access_barrier(page)
        except Exception as e:
            last_reason = f"barrier_check_failed:{str(e)[:80]}"

        reason = (barrier or {}).get("reason", "")
        if reason:
            last_reason = reason
        if reason in ELSEVIER_TRANSIENT_POPUP_REASONS:
            if reason != logged_reason:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                log_event(stage, "elsevier_popup_settle", "waiting", cur_url, f"{reason} elapsed_ms={elapsed_ms}")
                logged_reason = reason
            await page.wait_for_timeout(1_000)
            continue

        try:
            surface = await inspect_pdf_surface(page)
        except Exception:
            surface = {}

        lowered = cur_url.lower()
        if surface.get("viewerish") or "pdf.sciencedirectassets.com" in lowered:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            log_event(stage, "elsevier_popup_settle", "ready", cur_url, f"elapsed_ms={elapsed_ms}")
            return ""

        await page.wait_for_timeout(1_000)

    log_event(stage, "elsevier_popup_settle", "timeout", page.url, last_reason or "surface_not_ready")
    return last_reason or "elsevier_popup_settle_timeout"


async def try_elsevier_pdf(page: Page, ctx: BrowserContext, doi: str,
                           dest: Path) -> Dict[str, Any]:
    """Elsevier/ScienceDirect: prefer article-page popup flow; keep pdfft out of fallback."""
    url = f"https://doi.org/{doi}"
    stage = "main_pdf"
    print(f"       → elsevier: {url}")
    log_event(stage, "elsevier_start", "start", url, "")

    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        except PlaywrightError as e:
            if "ERR_ABORTED" in str(e) and page.url and "sciencedirect.com" in page.url:
                log_event(stage, "elsevier_open", "aborted_continue", page.url, str(e)[:120])
            else:
                log_event(stage, "elsevier_open", STATUS_FAILED, url, str(e)[:120])
                return make_attempt(STATUS_FAILED, "elsevier_goto_failed")

        await page.wait_for_timeout(200)
        await handle_access_barrier(page, stage)

        cur_url = page.url
        pii = extract_elsevier_pii(cur_url)
        canonical_article_url = elsevier_article_url_from_pii(pii) if pii else ""

        if not pii:
            print(f"       → can't extract PII from: {cur_url[:70]}")
            log_event(stage, "pii_extract", STATUS_FAILED, cur_url, "pii_missing")
            return await try_click_pdf(page, ctx, doi, "elsevier", dest, stage=stage, use_current_page=True, skip_candidate_fetch=True)

        popup_attempt = await try_click_pdf(
            page,
            ctx,
            doi,
            "elsevier",
            dest,
            stage=stage,
            use_current_page=True,
            skip_candidate_fetch=True,
        )
        if popup_attempt["state"] != STATUS_FAILED:
            return popup_attempt

        reason = popup_attempt.get("reason", "elsevier_popup_flow_failed")
        log_event(stage, "pdfft_fallback", "skipped", canonical_article_url or page.url, f"reason={reason} auto_mode={'--auto' in sys.argv}")
        return make_attempt(STATUS_MANUAL, reason)

    except ManualInterventionRequired as e:
        return make_attempt(STATUS_MANUAL, e.reason)
    except Exception as e:
        if is_session_closed_error(e):
            raise
        print(f"       → elsevier failed: {str(e)[:60]}")
        log_event(stage, "try_elsevier_pdf", STATUS_FAILED, page.url, str(e)[:120])
        return make_attempt(STATUS_FAILED, str(e)[:80])


async def try_click_pdf(page: Page, ctx: BrowserContext, doi: str,
                        publisher: str, dest: Path, stage: str = "main_pdf",
                        use_current_page: bool = False,
                        skip_candidate_fetch: bool = False) -> Dict[str, Any]:
    """Visit article page, click PDF button, save result."""
    url = article_url(doi, publisher)
    pdf_body = None
    dl_obj = None
    new_page_pdf = None

    async def on_response(resp):
        nonlocal pdf_body
        try:
            ct = resp.headers.get("content-type", "")
            if "pdf" in ct.lower() and resp.ok and pdf_body is None:
                pdf_body = await resp.body()
        except Exception:
            pass

    def on_download(dl):
        nonlocal dl_obj
        if dl_obj is None:
            dl_obj = dl

    async def on_ctx_response(resp):
        nonlocal new_page_pdf
        try:
            ct = resp.headers.get("content-type", "")
            lowered_url = (resp.url or "").lower()
            if new_page_pdf is not None:
                return
            if not resp.ok:
                return
            if "pdf" in ct.lower() or lowered_url.endswith(".pdf") or "main.pdf" in lowered_url:
                body = await resp.body()
                if body and len(body) > 5000:
                    new_page_pdf = body
        except Exception:
            pass

    async def inspect_new_pdf_page(new_p):
        keep_new_page = False
        popup_attempt = make_attempt(STATUS_FAILED, "popup_unhandled")
        is_elsevier_popup = publisher == "elsevier" and stage == "main_pdf"
        try:
            await new_p.wait_for_load_state("domcontentloaded", timeout=8_000)
            try:
                await new_p.wait_for_load_state("load", timeout=8_000)
            except Exception:
                pass
            if is_elsevier_popup:
                await wait_for_elsevier_popup_surface_ready(new_p, stage)
            else:
                await new_p.wait_for_timeout(800)

            try:
                await handle_access_barrier(new_p, stage)
            except ManualInterventionRequired as e:
                if not is_auto_mode():
                    keep_new_page = True
                    preserve_manual_page(new_p, stage, e.reason)
                else:
                    keep_new_page = True
                    popup_attempt = with_auto_retry_page(e.reason, new_p)
                    return popup_attempt
                popup_attempt = make_attempt(STATUS_MANUAL, e.reason)
                return popup_attempt

            async def grab_new(r):
                nonlocal new_page_pdf
                try:
                    ct2 = r.headers.get("content-type", "")
                    if "pdf" in ct2.lower() and r.ok and new_page_pdf is None:
                        new_page_pdf = await r.body()
                except Exception:
                    pass
            new_p.on("response", grab_new)
            popup_capture_wait_ms = ELSEVIER_POPUP_CAPTURE_WAIT_MS if is_elsevier_popup else 3_000
            await new_p.wait_for_timeout(popup_capture_wait_ms)
            if new_page_pdf is None:
                popup_attempt = await fetch_pdf_from_viewer(new_p, ctx, dest, stage)
                if popup_attempt["state"] == STATUS_MANUAL:
                    if not is_auto_mode():
                        keep_new_page = True
                        preserve_manual_page(new_p, stage, popup_attempt["reason"])
                    else:
                        keep_new_page = True
                        popup_attempt = with_auto_retry_page(popup_attempt["reason"], new_p)
                return popup_attempt
            if isinstance(new_page_pdf, (bytes, bytearray)):
                return new_page_pdf
        except Exception:
            return make_attempt(STATUS_FAILED, "popup_inspection_failed")
        finally:
            if not keep_new_page:
                try:
                    await new_p.close()
                except Exception:
                    pass
        return popup_attempt

    try:
        if not use_current_page:
            print(f"       → article: {url}")
            log_event(stage, "article_open", "start", url, publisher)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            except PlaywrightError as e:
                if "ERR_ABORTED" in str(e) and page.url and page.url != "about:blank":
                    log_event(stage, "article_open", "aborted_continue", page.url, str(e)[:120])
                else:
                    log_event(stage, "article_open", STATUS_FAILED, url, str(e)[:120])
                    return make_attempt(STATUS_FAILED, "article_goto_failed")
            await page.wait_for_timeout(200)
            # AIP/AVS: pubs.aip.org shows a "请稍候…" (Please wait) loading page
            # Wait up to 15s for it to resolve before searching for PDF links
            if publisher in ("aip", "avs"):
                try:
                    title = (await page.title()).strip()
                    inst_loading = any(t in title for t in _auth_loading_titles())
                    is_aip_loading = "稍候" in title  # AIP/AVS literally serves Chinese loading text
                    if inst_loading or is_aip_loading or title in ("", "Loading..."):
                        log_event(stage, "aip_loading_wait", "start", page.url, f"title={title!r}")
                        await page.wait_for_function(
                            "() => !document.title.includes('稍候') && document.title.length > 2",
                            timeout=20_000,
                        )
                        await page.wait_for_timeout(200)
                        log_event(stage, "aip_loading_wait", "ready", page.url, "")
                except Exception:
                    pass  # best-effort
        else:
            print(f"       → article(current): {page.url}")
            log_event(stage, "article_open", "reuse_current_page", page.url, publisher)

        await handle_access_barrier(page, stage)

        if publisher == "elsevier" and stage == "main_pdf":
            try:
                await page.wait_for_load_state("load", timeout=8_000)
            except Exception:
                pass
            await wait_for_elsevier_pdf_button_ready(page, stage)
            log_event(stage, "elsevier_article_stabilize", "ready", page.url, "pre_click_wait")

        page.on("response", on_response)
        page.on("download", on_download)
        ctx.on("response", on_ctx_response)

        raw_candidates = await collect_candidate_urls(page, publisher)
        candidates = raw_candidates
        rejected_candidates: List[Dict[str, str]] = []
        if publisher == "wiley" and stage == "main_pdf":
            candidates, rejected_candidates = filter_wiley_main_pdf_candidates(raw_candidates, page.url, doi)
            log_event(
                stage,
                "wiley_candidate_filter",
                "filtered",
                page.url,
                f"raw={len(raw_candidates)} filtered={len(candidates)} rejected={len(rejected_candidates)}",
            )
            for item in rejected_candidates:
                result = "false_positive_guard" if item["reason"] == "external_host" else "wiley_candidate_rejected"
                log_event(stage, "wiley_candidate_filter", result, item["url"], item["reason"])

        if candidates:
            log_event(stage, "collect_candidate_urls", "found", page.url, ", ".join(candidates[:3]), {"candidate_count": len(candidates)})
        elif raw_candidates:
            log_event(stage, "collect_candidate_urls", "filtered_out", page.url, publisher, {"raw_candidate_count": len(raw_candidates)})

        if not skip_candidate_fetch:
            for href in candidates:
                print(f"         candidate: {href[:70]}")
                if href.startswith("javascript:"):
                    continue  # skip JS pseudo-URLs — just click the button below
                if publisher == "wiley" and stage == "main_pdf":
                    fetch_only = await fetch_pdf_via_page_context(page, href, dest, stage, "wiley_page_context_fetch")
                else:
                    fetch_only = await try_direct_pdf(page, ctx, href, dest, stage=stage, allow_navigation=False)
                if fetch_only["state"] != STATUS_FAILED:
                    return fetch_only

        clicked = False
        click_selector = None
        popup_expected = False
        for sel in PDF_SELECTORS.get(publisher, ['a:has-text("PDF")']):
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    click_selector = sel
                    known_pages = set(ctx.pages)
                    try:
                        popup_expected = await el.evaluate(
                            """(node) => {
                                const target = (node.getAttribute('target') || '').toLowerCase();
                                const aria = (node.getAttribute('aria-label') || '').toLowerCase();
                                return target === '_blank' || aria.includes('opens in a new window');
                            }"""
                        )
                    except Exception:
                        popup_expected = False
                    print(f"         clicking: {sel}")
                    popup_page = None
                    click_sent = False
                    elsevier_popup_flow = publisher == "elsevier" and stage == "main_pdf" and popup_expected
                    if elsevier_popup_flow:
                        try:
                            await el.click()
                            click_sent = True
                            popup_page = await wait_for_elsevier_popup_after_click(ctx, known_pages, stage, sel, el)
                        except Exception as popup_err:
                            log_event(stage, "selector_popup_wait", STATUS_FAILED, page.url, str(popup_err)[:120])
                    else:
                        popup_timeout = 8_000 if popup_expected else GENERIC_POPUP_TIMEOUT
                        try:
                            async with ctx.expect_page(timeout=popup_timeout) as popup_info:
                                await el.click()
                                click_sent = True
                            popup_page = await popup_info.value
                        except Exception as popup_err:
                            if not click_sent:
                                await el.click()
                            if popup_expected:
                                log_event(stage, "selector_popup_wait", STATUS_FAILED, page.url, str(popup_err)[:120])
                            elif "Timeout" not in str(popup_err):
                                log_event(stage, "selector_popup_wait", "no_new_page", page.url, str(popup_err)[:120])
                    if popup_page is None and popup_expected:
                        try:
                            await page.wait_for_timeout(500)
                            recovered_pages = [p for p in ctx.pages if p not in known_pages and not p.is_closed()]
                            if recovered_pages:
                                popup_page = recovered_pages[-1]
                                log_event(stage, "selector_popup_recover", "recovered", popup_page.url, sel)
                        except Exception as recover_err:
                            log_event(stage, "selector_popup_recover", STATUS_FAILED, page.url, str(recover_err)[:120])
                    clicked = True
                    click_detail = f"{sel} popup_expected={popup_expected} popup_captured={popup_page is not None}"
                    log_event(stage, "selector_click", "clicked", page.url, click_detail)
                    if popup_page is not None:
                        new_page_pdf = await inspect_new_pdf_page(popup_page)
                    break
            except Exception as e:
                log_event(stage, "selector_click", STATUS_FAILED, page.url, f"{sel}: {str(e)[:80]}")
                continue

        if clicked:
            try:
                await page.wait_for_timeout(200)
                await handle_access_barrier(page, stage)
            except ManualInterventionRequired as e:
                return make_attempt(STATUS_MANUAL, e.reason)

            wait_cycles = 40 if popup_expected else 20
            for _ in range(wait_cycles):
                if dl_obj or pdf_body or new_page_pdf:
                    break
                await page.wait_for_timeout(200)

            if dl_obj:
                saved = await save_download(dl_obj, dest, stage)
                if saved["state"] == STATUS_DOWNLOADED:
                    print(f"       ✓ downloaded ({saved['size_kb']} KB)")
                    log_event(stage, "download_event", STATUS_DOWNLOADED, page.url, f"{saved['size_kb']} KB")
                    return saved
                log_event(stage, "download_event", STATUS_FAILED, page.url, saved["reason"])

            if isinstance(new_page_pdf, dict):
                if new_page_pdf["state"] != STATUS_FAILED:
                    return new_page_pdf
                log_event(stage, "new_tab_pdf", STATUS_FAILED, page.url, new_page_pdf["reason"])

            # PDF captured from new tab (IEEE etc.)
            new_tab_body = new_page_pdf if isinstance(new_page_pdf, (bytes, bytearray)) else None
            if new_tab_body and len(new_tab_body) > 5000:
                dest.write_bytes(new_tab_body)
                ok, reason = looks_like_valid_pdf(dest, stage)
                if ok:
                    kb = len(new_tab_body) // 1024
                    print(f"       ✓ saved from new tab ({kb} KB)")
                    log_event(stage, "new_tab_pdf", STATUS_DOWNLOADED, page.url, f"{kb} KB")
                    return make_attempt(STATUS_DOWNLOADED, "new_tab_pdf", kb)
                try:
                    dest.unlink()
                except Exception:
                    pass

            if pdf_body and len(pdf_body) > 5000:
                dest.write_bytes(pdf_body)
                ok, reason = looks_like_valid_pdf(dest, stage)
                if ok:
                    kb = len(pdf_body) // 1024
                    print(f"       ✓ saved from response ({kb} KB)")
                    log_event(stage, "response_body", STATUS_DOWNLOADED, page.url, f"{kb} KB")
                    return make_attempt(STATUS_DOWNLOADED, "response_body", kb)
                try:
                    dest.unlink()
                except Exception:
                    pass
                log_event(stage, "response_body", STATUS_FAILED, page.url, reason)

            await maybe_wait_for_viewer_settle(page, stage, "post_click")
            viewer_attempt = await fetch_pdf_from_viewer(page, ctx, dest, stage)
            if viewer_attempt["state"] == STATUS_DOWNLOADED:
                return viewer_attempt
            if viewer_attempt["state"] == STATUS_MANUAL and viewer_attempt["reason"] != "viewer_capture_failed":
                return viewer_attempt

            if candidates:
                surface = None
                try:
                    surface = await inspect_pdf_surface(page)
                except Exception:
                    surface = None
                nav_candidates = prioritized_browser_navigation_candidates(publisher, page.url, candidates, surface)
                for nav_url in nav_candidates:
                    nav_attempt = await try_browser_pdf_navigation_candidate(ctx, nav_url, dest, stage)
                    if nav_attempt["state"] != STATUS_FAILED:
                        return nav_attempt

            if viewer_attempt["state"] == STATUS_MANUAL:
                return viewer_attempt

        if candidates and not skip_candidate_fetch:
            await maybe_wait_for_viewer_settle(page, stage, "post_direct_candidate")
            nav_attempt = await try_direct_pdf(page, ctx, candidates[0], dest, stage=stage, allow_navigation=True)
            if nav_attempt["state"] != STATUS_FAILED:
                return nav_attempt

        if not clicked and (not candidates or skip_candidate_fetch):
            try:
                title = await page.title()
                print(f"       ✗ no PDF button found on: {title[:50]}")
                print(f"         url: {page.url[:80]}")
            except Exception:
                print("       ✗ no PDF button found (page unresponsive)")
            reason = "wiley_candidate_rejected" if publisher == "wiley" and rejected_candidates else "no_pdf_button_found"
            log_event(stage, "selector_search", STATUS_FAILED, page.url, reason)
            return make_attempt(STATUS_FAILED, reason)

        print("       ✗ no PDF captured after click")
        log_event(stage, "click_complete", STATUS_FAILED, page.url, f"clicked={clicked} selector={click_selector}")
        return make_attempt(STATUS_FAILED, "no_pdf_captured_after_click")

    except ManualInterventionRequired as e:
        return make_attempt(STATUS_MANUAL, e.reason)
    except Exception as e:
        print(f"       ✗ click failed: {e}")
        log_event(stage, "try_click_pdf", STATUS_FAILED, page.url, str(e)[:120])
        return make_attempt(STATUS_FAILED, str(e)[:80])
    finally:
        try:
            page.remove_listener("response", on_response)
            page.remove_listener("download", on_download)
            ctx.remove_listener("response", on_ctx_response)
        except Exception:
            pass
        await close_extra_pages(ctx, page)


# ── Per-article orchestration ─────────────────────────────────────────────────

async def download_one(ctx: BrowserContext, ref: dict,
                       project_dir: Path, total: int) -> dict:
    """Download PDF + SI for one reference on its own page."""
    ref_id    = ref["id"]
    label     = ref["label"]
    doi       = ref["doi"]
    publisher = ref["publisher"]
    prefix    = f"{ref_id:02d}_{label}"
    pdf_dest  = project_dir / f"{prefix}.pdf"
    si_base   = project_dir / f"{prefix}_SI"

    result = dict(id=ref_id, label=label, doi=doi, publisher=publisher,
                  pdf_status="skipped", si_status="not_attempted",
                  pdf_history="", si_history="", retry_count=0,
                  publisher_strategy=publisher_strategy(publisher)["family"],
                  publisher_support=publisher_strategy(publisher)["support"],
                  publisher_min_test=publisher_strategy(publisher)["min_test"])

    RUN_CTX["current_ref"] = ref
    page = await ctx.new_page()
    keep_page = False

    print(f"\n[{ref_id:02d}/{total}] {label}")
    log_event("ref", "start", "start", "", "")

    try:
        if not doi:
            print("       ✗ no DOI, skipping")
            result["pdf_status"] = "failed (no DOI)"
            log_event("ref", "validate", STATUS_FAILED, "", "no_doi")
            return finalize_report_row(result)

        if doi.lower() in ignored_institution_access_dois():
            print("       PDF: ignored (institution access)")
            print("       SI: ignored (institution access)")
            result["pdf_status"] = "ignored (ignored_institution_access)"
            result["si_status"] = "ignored (ignored_institution_access)"
            log_event("ref", "ignore_ref", STATUS_IGNORED, "", "ignored_institution_access")
            return finalize_report_row(result)

        if pdf_dest.exists():
            print("       PDF: already exists")
            result["pdf_status"] = STATUS_ALREADY_EXISTS
            log_event("main_pdf", "exists_check", STATUS_ALREADY_EXISTS, str(pdf_dest), "")
        else:
            if publisher == "elsevier":
                pdf_attempt = await try_elsevier_pdf(page, ctx, doi, pdf_dest)
            elif publisher == "wiley":
                pdf_attempt = await try_click_pdf(page, ctx, doi, publisher, pdf_dest, stage="main_pdf")
            else:
                pdf_attempt = make_attempt(STATUS_FAILED, "not_attempted")
                pdf_url = direct_pdf_url(doi, publisher)
                if pdf_url:
                    pdf_attempt = await try_direct_pdf(page, ctx, pdf_url, pdf_dest, stage="main_pdf", allow_navigation=True)
                if pdf_attempt["state"] == STATUS_FAILED:
                    pdf_attempt = await try_click_pdf(page, ctx, doi, publisher, pdf_dest, stage="main_pdf")

            if pdf_attempt["state"] == STATUS_DOWNLOADED:
                if publisher == "elsevier":
                    mark_elsevier_hot("main_pdf_downloaded")
                result["pdf_status"] = f"downloaded ({pdf_attempt['size_kb']} KB)"
            elif pdf_attempt["state"] == STATUS_MANUAL:
                if is_auto_mode():
                    retry_page = pdf_attempt.get("auto_retry_page") or page
                    scheduled_attempt = await schedule_auto_manual_retry(
                        ctx,
                        retry_page,
                        pdf_dest,
                        "main_pdf",
                        pdf_attempt["reason"],
                        ref,
                    )
                    if retry_page is page:
                        keep_page = True
                    result["pdf_status"] = f"manual_pending ({manual_pending_reason(scheduled_attempt)})"
                    result["si_status"] = "skipped (manual_pending)"
                    return finalize_report_row(result)
                else:
                    keep_page = True
                    preserve_manual_page(page, "main_pdf", pdf_attempt["reason"])
                    result["pdf_status"] = f"manual_pending ({pdf_attempt['reason']})"
                    result["si_status"] = "skipped (manual_pending)"
                    return finalize_report_row(result)
            else:
                result["pdf_status"] = f"failed ({pdf_attempt['reason']})"
                if publisher == "elsevier":
                    print(f"       ⚠ Elsevier 下载失败，请手动下载: https://doi.org/{doi}")

        existing_si = find_existing_si_asset(si_base)
        if existing_si:
            result["si_status"] = f"{STATUS_ALREADY_EXISTS} ({existing_si.name})"
            log_event("si", "exists_check", STATUS_ALREADY_EXISTS, str(existing_si), "")
        else:
            try:
                landing = article_url(doi, publisher)
                log_event("si", "article_open", "start", landing, "")
                try:
                    await page.goto(landing, wait_until="domcontentloaded",
                                    timeout=NAV_TIMEOUT)
                except PlaywrightError as e:
                    if "ERR_ABORTED" in str(e) and page.url and page.url != "about:blank":
                        log_event("si", "article_open", "aborted_continue", page.url, str(e)[:120])
                    else:
                        raise
                await page.wait_for_timeout(200)

                await handle_access_barrier(page, "si")

                si_links = await get_si_links(page, publisher, doi)
                log_event("si", "extract_links", "found" if si_links else STATUS_NOT_FOUND, page.url, ", ".join(si_links[:2]))
                if not si_links:
                    result["si_status"] = STATUS_NOT_FOUND
                    print("       SI: not found")
                else:
                    si_url = si_links[0]
                    print(f"       SI: {si_url[:80]}")
                    if is_non_pdf_asset_url(si_url):
                        si_attempt = await try_direct_asset(page, ctx, si_url, si_base, stage="si")
                    else:
                        si_attempt = await try_direct_pdf(page, ctx, si_url, si_base.with_suffix(".pdf"), stage="si", allow_navigation=True)
                    if si_attempt["state"] == STATUS_DOWNLOADED:
                        if si_attempt["reason"] == "non_pdf_asset_saved":
                            result["si_status"] = f"non_pdf_asset_saved ({si_attempt.get('asset_ext', '')}, {si_attempt['size_kb']} KB)"
                        else:
                            result["si_status"] = f"downloaded ({si_attempt['size_kb']} KB)"
                    elif si_attempt["state"] == STATUS_MANUAL:
                        if is_auto_mode():
                            retry_page = si_attempt.get("auto_retry_page") or page
                            scheduled_attempt = await schedule_auto_manual_retry(
                                ctx,
                                retry_page,
                                si_base.with_suffix(".pdf"),
                                "si",
                                si_attempt["reason"],
                                ref,
                            )
                            if retry_page is page:
                                keep_page = True
                            result["si_status"] = f"manual_pending ({manual_pending_reason(scheduled_attempt)})"
                        else:
                            keep_page = True
                            preserve_manual_page(page, "si", si_attempt["reason"])
                            result["si_status"] = f"manual_pending ({si_attempt['reason']})"
                    else:
                        result["si_status"] = f"failed ({si_attempt['reason']})"
            except ManualInterventionRequired as e:
                if is_auto_mode():
                    scheduled_attempt = await schedule_auto_manual_retry(
                        ctx,
                        page,
                        si_base.with_suffix(".pdf"),
                        "si",
                        e.reason,
                        ref,
                    )
                    keep_page = True
                    result["si_status"] = f"manual_pending ({manual_pending_reason(scheduled_attempt)})"
                else:
                    keep_page = True
                    preserve_manual_page(page, "si", e.reason)
                    result["si_status"] = f"manual_pending ({e.reason})"
            except Exception as e:
                result["si_status"] = f"failed ({str(e)[:80]})"
                print(f"       ✗ SI error: {str(e)[:60]}")
                log_event("si", "download", STATUS_FAILED, page.url, str(e)[:120])

        await asyncio.sleep(DELAY)
        return finalize_report_row(result)
    finally:
        RUN_CTX["current_ref"] = None
        if not keep_page:
            try:
                await page.close()
            except Exception:
                pass


# ── Manual queue hotpath ─────────────────────────────────────────────────────

def upsert_report_row(report: List[Dict[str, Any]], row: Dict[str, Any]):
    for i, old in enumerate(report):
        if old["id"] == row["id"]:
            report[i] = row
            return
    report.append(row)


async def flush_manual_queue(
    ctx: BrowserContext,
    refs: List[Dict[str, Any]],
    report: List[Dict[str, Any]],
    project_dir: Path,
    total: int,
    *,
    force: bool = False,
    trigger: str = "",
):
    clean_manual_pages()
    if not RUN_CTX["manual_pages"]:
        return
    if not force and RUN_CTX.get("manual_deferred"):
        return
    auto_retried_elsevier_ids: set[int] = set()

    while True:
        clean_manual_pages()
        if not RUN_CTX["manual_pages"]:
            RUN_CTX["manual_deferred"] = False
            return

        limit = manual_queue_limit()
        if not force and len(RUN_CTX["manual_pages"]) < limit:
            return

        pending_ids = {item["id"] for item in RUN_CTX["manual_pages"]}
        pending_refs = [r for r in refs if r["id"] in pending_ids]
        hot_elsevier = any((item.get("publisher") or "") == "elsevier" for item in RUN_CTX["manual_pages"])
        auto_retry_elsevier = (
            not force
            and hot_elsevier
            and should_auto_retry_elsevier_queue(RUN_CTX["manual_pages"], auto_retried_elsevier_ids)
        )

        if auto_retry_elsevier:
            print(f"\n{'='*60}")
            print(f"  ⚡ Elsevier hot session active — auto retrying {len(pending_refs)} ref(s) without prompting")
            print(f"{'='*60}")
            if trigger:
                print(f"  Trigger: {trigger}")
            for item in RUN_CTX["manual_pages"]:
                print(f"  [{item['id']:02d}] {item['label']}")
                print(f"       stage={item['stage']}  reason={item['reason']}")
                print(f"       url: {item['url'][:100]}")
            auto_retried_elsevier_ids.update(pending_ids)
            ans = ""
        else:
            print(f"\n{'='*60}")
            if force:
                print(f"  ⏸  {len(pending_refs)} refs still need manual attention:")
            elif hot_elsevier:
                print(f"  ⚡ {len(pending_refs)} manual refs ready for hot-session retry:")
            else:
                print(f"  ⏸  {len(pending_refs)} manual refs reached queue limit {limit}:")
            print(f"{'='*60}")
            if trigger:
                print(f"  Trigger: {trigger}")
            for item in RUN_CTX["manual_pages"]:
                print(f"  [{item['id']:02d}] {item['label']}")
                print(f"       stage={item['stage']}  reason={item['reason']}")
                print(f"       url: {item['url'][:100]}")
            print()
            print("  ► The pages above are open in Edge.")
            if hot_elsevier:
                print("  ► Elsevier session is still warm — solve any challenge now,")
                print("    then press Enter to retry immediately.")
            else:
                print("  ► Log in to any sites that require authentication,")
                print("    then press Enter to retry the current queue.")
            if force:
                print("  ► Press Ctrl+C (or type 'skip') to stop retrying and close later.")
            else:
                print("  ► Type 'skip' to defer this queue until the end of the run.")

            try:
                ans = await asyncio.to_thread(input, "\n  Ready? (Enter=retry  /  skip=defer): ")
            except (EOFError, KeyboardInterrupt):
                ans = "skip"

        if ans.strip().lower() in ("skip", "s", "q", "quit", "exit"):
            if force:
                print("  Skipping retry — browser will close after the run.")
            else:
                print("  Deferring current manual queue until the end of the run.")
                RUN_CTX["manual_deferred"] = True
            return

        RUN_CTX["manual_deferred"] = False
        if hot_elsevier:
            mark_elsevier_hot("manual_queue_retry")
        print(f"\n  Retrying {len(pending_refs)} pending refs with current session...")
        queued_manual_items = list(RUN_CTX["manual_pages"])
        set_manual_retry_pages(queued_manual_items)
        RUN_CTX["manual_pages"].clear()

        try:
            for ref in pending_refs:
                try:
                    ref_manual_items = [item for item in queued_manual_items if item.get("id") == ref["id"]]
                    res = await resume_ref_from_manual_pages(ctx, ref, project_dir, total, ref_manual_items)
                    if res is None:
                        res = await download_one(ctx, ref, project_dir, total)
                    merged = res
                    for old in report:
                        if old["id"] == ref["id"]:
                            merged = merge_report_rows(old, res)
                            break
                    upsert_report_row(report, merged)
                except PlaywrightError as e:
                    err_msg = str(e)[:60]
                    print(f"\n  ✗ Browser error at [{ref['id']:02d}]: {err_msg}")
        finally:
            clear_manual_retry_pages()

        clean_manual_pages()
        if not RUN_CTX["manual_pages"]:
            print("\n  ✓ All currently pending refs resolved.")
            return
        if not force and RUN_CTX.get("manual_deferred"):
            return


# ── Edge session lifecycle ────────────────────────────────────────────────────

async def launch_edge_context(pw) -> BrowserContext:
    """Launch the persistent browser context.

    Two backends are supported:
    - Default: Microsoft Edge persistent profile (the original path).
    - REF_DOWNLOADER_BROWSER=cloak: cloakbrowser stealth Chromium. Optional —
      requires `pip install cloakbrowser` (or its dev path on sys.path via
      CLOAKBROWSER_PYTHONPATH). Edge does NOT need to be closed for this backend.

    The `pw` arg is the playwright instance; cloakbrowser does not use it, but the
    function signature stays the same so the rest of the script doesn't care which
    backend it called.
    """
    if using_cloakbrowser():
        # Optional dev-path hook: lets users point at a local cloakbrowser checkout
        # without installing the package. Empty / unset → assume normal pip install.
        cloak_pythonpath = os.environ.get("CLOAKBROWSER_PYTHONPATH", "").strip()
        if cloak_pythonpath and cloak_pythonpath not in sys.path:
            sys.path.insert(0, cloak_pythonpath)
        try:
            from cloakbrowser import launch_persistent_context_async  # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError(
                "REF_DOWNLOADER_BROWSER=cloak requested but cloakbrowser is not importable. "
                "Install it with `pip install cloakbrowser`, or set CLOAKBROWSER_PYTHONPATH "
                f"to a local cloakbrowser source checkout. Original error: {e}"
            ) from e

        profile = Path(os.environ.get("REF_DOWNLOADER_CLOAK_PROFILE", CLOAKBROWSER_DEFAULT_PROFILE))
        profile.mkdir(parents=True, exist_ok=True)

        humanize = env_flag("REF_DOWNLOADER_CLOAK_HUMANIZE", default=True)
        human_preset = selected_cloak_human_preset()
        proxy = os.environ.get("REF_DOWNLOADER_CLOAK_PROXY") or None
        geoip = env_flag("REF_DOWNLOADER_CLOAK_GEOIP", default=bool(proxy))
        launch_args = ["--disable-blink-features=AutomationControlled"]

        return await launch_persistent_context_async(
            profile,
            headless=False,
            humanize=humanize,
            human_preset=human_preset,
            proxy=proxy,
            geoip=geoip,
            args=launch_args,
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )

    # Edge backend (default).
    launch_args = [
        "--profile-directory=Default",
        "--disable-blink-features=AutomationControlled",
    ]
    if env_flag("REF_DOWNLOADER_DISABLE_EXTENSIONS", default=False):
        launch_args.append("--disable-extensions")

    return await pw.chromium.launch_persistent_context(
        get_edge_user_data_dir(),
        channel="msedge",
        headless=False,
        accept_downloads=True,
        args=launch_args,
        viewport={"width": 1280, "height": 900},
    )


async def close_context_quietly(ctx: Optional[BrowserContext]):
    if not ctx:
        return
    try:
        await ctx.close()
    except Exception:
        pass


async def restart_edge_context(pw, ctx: Optional[BrowserContext], reason: str, ref: Optional[Dict[str, Any]] = None) -> BrowserContext:
    await cancel_auto_manual_retries(f"session_restart: {reason[:120]}")
    clear_manual_queue(f"session_restart: {reason[:120]}")
    await close_context_quietly(ctx)
    await asyncio.sleep(1.0)
    new_ctx = await launch_edge_context(pw)
    detail = f"reason={reason[:120]}"
    if ref:
        detail += f" | ref=[{ref['id']:02d}] {ref['label']}"
    log_event("ref", "session_restart", "restarted", "", detail)
    return new_ctx


# ── Input resolution ──────────────────────────────────────────────────────────

def resolve_input(arg: str):
    """Resolve CLI argument to (project_dir, validated_json_path)."""
    p = Path(arg)
    if p.suffix == ".json" and p.exists():
        return p.parent, p
    if p.is_dir() and (p / "refs_validated.json").exists():
        return p, p / "refs_validated.json"
    candidate = Path(arg)
    if (candidate / "refs_validated.json").exists():
        return candidate, candidate / "refs_validated.json"
    print(f"ERROR: Cannot find refs_validated.json in '{arg}'")
    print(f"  Run validate_refs.py first.")
    sys.exit(1)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    if len(sys.argv) < 2:
        print("Usage: python download_refs.py <project_name_or_path>")
        print("Example: python download_refs.py jacs.5c05017")
        sys.exit(1)

    cfg = load_config()
    init_institution_config(cfg.institution)

    project_dir, validated_path = resolve_input(sys.argv[1])

    with open(validated_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    refs = [r for r in data["references"] if r.get("status") == "verified"]
    total = len(refs)
    auto_mode = "--auto" in sys.argv
    run_dir = init_run_artifacts(project_dir, validated_path, total, auto_mode)

    print(f"Project:  {project_dir}")
    print(f"Input:    {validated_path}")
    print(f"To download: {total} verified refs")
    print(f"Run dir:  {run_dir}")
    print(f"Backend:  {selected_browser_backend()}")

    if using_cloakbrowser():
        cloak_profile = os.environ.get("REF_DOWNLOADER_CLOAK_PROFILE", CLOAKBROWSER_DEFAULT_PROFILE)
        print(f"Cloak profile:      {cloak_profile}")
        print(f"Cloak humanize:     {env_flag('REF_DOWNLOADER_CLOAK_HUMANIZE', default=True)}")
        print(f"Cloak human preset: {selected_cloak_human_preset()}")
        print()
        # No Edge-close prompt: cloakbrowser uses its own Chromium with a separate profile.
    else:
        print(f"Edge extensions: {'disabled' if env_flag('REF_DOWNLOADER_DISABLE_EXTENSIONS', default=False) else 'enabled'}")

        # Check Edge
        edge_user_data = get_edge_user_data_dir()
        edge_path = Path(edge_user_data)
        if not edge_path.exists():
            print(f"\nERROR: Edge user data not found at:\n  {edge_user_data}")
            sys.exit(1)

        print(f"\n{'='*60}")
        print("  Please close Microsoft Edge completely!")
        print("  (Playwright needs exclusive access to Edge profile)")
        print(f"{'='*60}")
        if not auto_mode:
            await asyncio.to_thread(input, "  Press Enter when Edge is closed...")
        else:
            print("  (auto mode — skipping confirmation)")
        print()

    report = []

    async with async_playwright() as pw:
        ctx = await launch_edge_context(pw)

        for ref in refs:
            session_restarts = 0
            session_last_error = ""

            if auto_mode:
                try:
                    await drain_due_auto_manual_retries(ctx, report, wait=False)
                except PlaywrightError as e:
                    err_msg = str(e)[:120]
                    if is_session_closed_error(e):
                        print("\n  ↻ Edge 会话在 auto manual retry 阶段关闭，正在重启后继续主循环...")
                        log_event("auto_manual_queue", "drain", "session_closed", "", err_msg)
                        ctx = await restart_edge_context(pw, ctx, err_msg, ref)
                    else:
                        raise

            while True:
                try:
                    res = await download_one(ctx, ref, project_dir, total)
                    report.append(
                        attach_session_restart_metadata(
                            res,
                            session_restarts=session_restarts,
                            session_last_error=session_last_error,
                        )
                    )
                    break
                except PlaywrightError as e:
                    err_msg = str(e)[:120]
                    if is_session_closed_error(e) and session_restarts < SESSION_RESTART_LIMIT_PER_REF:
                        session_restarts += 1
                        session_last_error = err_msg
                        print(f"\n  ↻ Edge 会话意外关闭，正在重启并重试当前条目 [{ref['id']:02d}]...")
                        log_event("ref", "download_one", "session_closed", "", err_msg)
                        ctx = await restart_edge_context(pw, ctx, err_msg, ref)
                        continue

                    print(f"\n  ✗ Browser error at [{ref['id']:02d}]: {err_msg[:60]}")
                    log_event("ref", "download_one", STATUS_FAILED, "", err_msg[:60])
                    report.append(
                        make_browser_error_row(
                            ref,
                            err_msg,
                            session_restarts=session_restarts,
                            session_last_error=session_last_error,
                        )
                    )
                    break

            if auto_mode:
                try:
                    await drain_due_auto_manual_retries(ctx, report, wait=False)
                except PlaywrightError as e:
                    err_msg = str(e)[:120]
                    if is_session_closed_error(e):
                        print("\n  ↻ Edge 会话在 auto manual retry 阶段关闭，正在重启后继续主循环...")
                        log_event("auto_manual_queue", "drain", "session_closed", "", err_msg)
                        ctx = await restart_edge_context(pw, ctx, err_msg, ref)
                    else:
                        raise

            if not auto_mode:
                try:
                    await flush_manual_queue(
                        ctx,
                        refs,
                        report,
                        project_dir,
                        total,
                        force=False,
                        trigger=f"after [{ref['id']:02d}] {ref['label']}",
                    )
                except PlaywrightError as e:
                    err_msg = str(e)[:120]
                    if is_session_closed_error(e):
                        print("\n  ↻ Edge 会话在 manual queue 阶段关闭，正在重启会话后继续主循环...")
                        log_event("manual_queue", "flush", "session_closed", "", err_msg)
                        ctx = await restart_edge_context(pw, ctx, err_msg, ref)
                        if report and report[-1]["id"] == ref["id"]:
                            report[-1] = attach_session_restart_metadata(
                                report[-1],
                                session_restarts=1,
                                session_last_error=err_msg,
                            )
                    else:
                        raise

        if not auto_mode:
            try:
                await flush_manual_queue(
                    ctx,
                    refs,
                    report,
                    project_dir,
                    total,
                    force=True,
                    trigger="final manual queue",
                )
            except PlaywrightError as e:
                err_msg = str(e)[:120]
                if is_session_closed_error(e):
                    print("\n  ↻ Edge 会话在最终 manual queue 阶段关闭，跳过最后的手动重试。")
                    log_event("manual_queue", "flush", "session_closed_final", "", err_msg)
                else:
                    raise

        if auto_mode:
            try:
                await drain_due_auto_manual_retries(ctx, report, wait=True)
            except PlaywrightError as e:
                err_msg = str(e)[:120]
                if is_session_closed_error(e):
                    print("\n  ↻ Edge 会话在最终 auto manual retry 阶段关闭，跳过剩余自动重试。")
                    log_event("auto_manual_queue", "drain", "session_closed_final", "", err_msg)
                else:
                    raise

        await close_context_quietly(ctx)

    # Write the final root report only after the run has closed cleanly. During interrupted
    # runs, the latest run dir plus the actual files on disk are the authoritative artifacts.
    report_path = project_dir / "download_report.csv"
    if report:
        sync_report_with_existing_files(report, project_dir)
        with open(report_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(report[0].keys()))
            w.writeheader()
            w.writerows(report)
        copy_report_to_run_dir(report_path)
        write_postmortem(report, project_dir, report_path)

    ok   = sum(1 for r in report if any(x in r["pdf_status"] for x in ("downloaded", "exists")))
    fail = sum(1 for r in report if "failed" in r["pdf_status"])
    manual = sum(1 for r in report if "manual_pending" in r["pdf_status"])
    ignored = sum(1 for r in report if "ignored" in r["pdf_status"])
    si   = sum(1 for r in report if any(x in r["si_status"] for x in ("downloaded", "exists", "non_pdf_asset_saved")))
    si_manual = sum(1 for r in report if "manual_pending" in r["si_status"])
    si_ignored = sum(1 for r in report if "ignored" in r["si_status"])

    print(f"\n{'='*60}")
    print(f"  Main PDFs      : {ok}/{total} ok, {fail} failed, {manual} manual, {ignored} ignored")
    print(f"  SI files       : {si}/{total} found, {si_manual} manual, {si_ignored} ignored")
    print(f"  Report         : {report_path}")
    print(f"  Run artifacts  : {run_dir}")
    print(f"{'='*60}")
    if fail:
        print(f"\nStill failing:")
        for r in report:
            if "failed" in r["pdf_status"]:
                print(f"  [{r['id']:02d}] {r['label']}: https://doi.org/{r['doi']}")

    if manual or si_manual:
        print("\nManual pending:")
        for r in report:
            if "manual_pending" in r["pdf_status"] or "manual_pending" in r["si_status"]:
                print(f"  [{r['id']:02d}] {r['label']}: pdf={r['pdf_status']} | si={r['si_status']}")


if __name__ == "__main__":
    asyncio.run(main())
