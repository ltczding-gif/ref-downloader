"""Config loader for ref-downloader.

Resolution order (highest priority last — later overrides earlier):
    1. config.example.toml (committed defaults)
    2. config.local.toml (gitignored, user-specific)
    3. REF_DOWNLOADER_CONFIG env var (path to alternate TOML)
    4. explicit_path arg (typically from --config CLI)
    5. Per-field env vars (REF_DOWNLOADER_MAILTO, _ZOTERO_DB, _EDGE_PROFILE, _DISABLE_EXTENSIONS)

Used by run_ref_downloader.py, extract_refs.py, validate_refs.py, download_refs.py.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import List, Optional

# Scripts live in <skill>/scripts/. Config files (config.example.toml,
# config.local.toml) live one level up at the skill root so users don't
# have to descend into scripts/ to edit them.
_SKILL_DIR = Path(__file__).resolve().parent.parent
EXAMPLE_TOML = _SKILL_DIR / "config.example.toml"
LOCAL_TOML = _SKILL_DIR / "config.local.toml"
PLACEHOLDER_MAILTO = "your.email@example.com"

# Single source of truth for the User-Agent app version. Keep in sync with
# CHANGELOG.md when releasing.
__version__ = "0.4.1"
APP_NAME = "RefDownloader"


@dataclass
class CrossrefConfig:
    mailto: str = PLACEHOLDER_MAILTO


@dataclass
class ZoteroConfig:
    db_path: str = ""


@dataclass
class BrowserConfig:
    edge_profile_dir: str = ""
    disable_extensions: bool = False


@dataclass
class InstitutionConfig:
    auth_hosts: List[str] = field(default_factory=list)
    auth_url_fragments: List[str] = field(default_factory=list)
    auth_page_titles: List[str] = field(default_factory=list)
    auth_loading_titles: List[str] = field(default_factory=list)
    ignored_access_dois: List[str] = field(default_factory=list)


@dataclass
class UserConfig:
    # DOIs the user has personally verified to have no SI material. Refs with
    # these DOIs get `si_status=not_applicable (verified_no_si)` instead of
    # `not_found` — avoids the warning noise + repeated probing on re-runs.
    verified_no_si_dois: List[str] = field(default_factory=list)


@dataclass
class Config:
    crossref: CrossrefConfig = field(default_factory=CrossrefConfig)
    zotero: ZoteroConfig = field(default_factory=ZoteroConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    institution: InstitutionConfig = field(default_factory=InstitutionConfig)
    user: UserConfig = field(default_factory=UserConfig)
    source_files: List[str] = field(default_factory=list)


def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        print(
            f"ERROR: Failed to parse TOML config file:\n  {path}\n  {e}",
            file=sys.stderr,
        )
        sys.exit(2)


def _merge_dict(base: dict, overlay: dict) -> dict:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _merge_dict(dict(base[key]), value)
        else:
            base[key] = value
    return base


def _coerce_section(data: dict, key: str) -> dict:
    """Return data[key] if it is a dict, else empty dict + warn on stderr."""
    value = data.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        print(
            f"WARNING: config section [{key}] is {type(value).__name__}, expected a table; ignoring.",
            file=sys.stderr,
        )
        return {}
    return value


def _coerce_str_list(section: dict, key: str, section_name: str) -> List[str]:
    """Pull section[key] as a list of strings; drop non-strings + warn."""
    raw = section.get(key, []) or []
    if not isinstance(raw, list):
        print(
            f"WARNING: config field [{section_name}].{key} is {type(raw).__name__}, expected a list; ignoring.",
            file=sys.stderr,
        )
        return []
    out: List[str] = []
    dropped = 0
    for item in raw:
        if isinstance(item, str):
            out.append(item)
        else:
            dropped += 1
    if dropped:
        print(
            f"WARNING: config field [{section_name}].{key} had {dropped} non-string entries; ignored.",
            file=sys.stderr,
        )
    return out


def _build_from_dict(data: dict, source_files: List[str]) -> Config:
    crossref = _coerce_section(data, "crossref")
    zotero = _coerce_section(data, "zotero")
    browser = _coerce_section(data, "browser")
    institution = _coerce_section(data, "institution")
    user = _coerce_section(data, "user")
    return Config(
        crossref=CrossrefConfig(
            mailto=str(crossref.get("mailto", PLACEHOLDER_MAILTO)),
        ),
        zotero=ZoteroConfig(
            db_path=str(zotero.get("db_path", "")),
        ),
        browser=BrowserConfig(
            edge_profile_dir=str(browser.get("edge_profile_dir", "")),
            disable_extensions=bool(browser.get("disable_extensions", False)),
        ),
        institution=InstitutionConfig(
            auth_hosts=_coerce_str_list(institution, "auth_hosts", "institution"),
            auth_url_fragments=_coerce_str_list(institution, "auth_url_fragments", "institution"),
            auth_page_titles=_coerce_str_list(institution, "auth_page_titles", "institution"),
            auth_loading_titles=_coerce_str_list(institution, "auth_loading_titles", "institution"),
            ignored_access_dois=_coerce_str_list(institution, "ignored_access_dois", "institution"),
        ),
        user=UserConfig(
            verified_no_si_dois=_coerce_str_list(user, "verified_no_si_dois", "user"),
        ),
        source_files=source_files,
    )


def _apply_env_overrides(cfg: Config) -> Config:
    mailto = os.environ.get("REF_DOWNLOADER_MAILTO")
    zotero = os.environ.get("REF_DOWNLOADER_ZOTERO_DB")
    edge = os.environ.get("REF_DOWNLOADER_EDGE_PROFILE")
    disable_ext = os.environ.get("REF_DOWNLOADER_DISABLE_EXTENSIONS")

    if mailto:
        cfg = replace(cfg, crossref=replace(cfg.crossref, mailto=mailto))
    if zotero is not None:
        cfg = replace(cfg, zotero=replace(cfg.zotero, db_path=zotero))
    if edge is not None:
        cfg = replace(cfg, browser=replace(cfg.browser, edge_profile_dir=edge))
    if disable_ext is not None:
        flag = disable_ext.strip().lower() in ("1", "true", "yes", "on")
        cfg = replace(cfg, browser=replace(cfg.browser, disable_extensions=flag))
    return cfg


def load_config(explicit_path: Optional[Path] = None) -> Config:
    """Load config from TOML files + env vars; return a Config dataclass.

    explicit_path: if provided (e.g. from --config CLI arg), takes priority over
    REF_DOWNLOADER_CONFIG env var, which takes priority over config.local.toml.
    """
    chain: List[Path] = []
    chain.append(EXAMPLE_TOML)
    if LOCAL_TOML.exists():
        chain.append(LOCAL_TOML)

    env_path = os.environ.get("REF_DOWNLOADER_CONFIG")
    if env_path:
        chain.append(Path(env_path).expanduser())
    if explicit_path:
        chain.append(explicit_path.expanduser())

    merged: dict = {}
    used: List[str] = []
    for path in chain:
        if not path.exists():
            continue
        data = _load_toml(path)
        merged = _merge_dict(merged, data)
        used.append(str(path))

    cfg = _build_from_dict(merged, used)
    cfg = _apply_env_overrides(cfg)
    return cfg


def user_agent_from(cfg: Config, app: Optional[str] = None) -> str:
    """Build the Crossref polite-pool User-Agent string.

    Default app token is `<APP_NAME>/<__version__>`. Pass an override only when
    a script wants to add a stage-specific suffix.
    """
    if app is None:
        app = f"{APP_NAME}/{__version__}"
    return f"{app} (mailto:{cfg.crossref.mailto})"


def warn_if_placeholder_mailto(cfg: Config) -> None:
    if cfg.crossref.mailto == PLACEHOLDER_MAILTO:
        print(
            "WARNING: crossref.mailto is the placeholder. "
            "Edit config.local.toml (copy from config.example.toml) or set "
            "REF_DOWNLOADER_MAILTO to enter the Crossref polite pool.",
            file=sys.stderr,
        )
