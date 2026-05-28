#!/usr/bin/env python3
"""
Script 2: Validate DOIs via Crossref API, enrich metadata, generate labels.

Usage:
  python validate_refs.py <project_name>
  python validate_refs.py <path/to/refs_raw.json>

Example:
  python validate_refs.py jacs.5c05017

Output:
  {project_dir}/refs_validated.json

Incremental: skips refs already marked "verified", only re-checks "failed"/"pending".
"""

import sys
import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

from _config import load_config, user_agent_from, warn_if_placeholder_mailto

API_BASE = "https://api.crossref.org/works"
RATE_LIMIT_DELAY = 0.35  # seconds between API calls (Crossref polite pool)

# DOI prefix → publisher key
PUBLISHER_MAP = {
    "10.1038": "nature",
    "10.1021": "acs",
    "10.1126": "science",
    "10.1016": "elsevier",
    "10.1002": "wiley",
    "10.1039": "rsc",
    "10.1007": "springer",
    "10.1073": "pnas",
    "10.1149": "ecs",
    "10.1088": "iop",
    "10.1103": "aps",
    "10.1146": "annualreviews",
    "10.1080": "tandfonline",
    # AIP Publishing: Applied Physics Letters, Journal of Applied Physics, etc.
    "10.1063": "aip",
    # AVS (American Vacuum Society): Journal of Vacuum Science & Technology A/B
    # IMPORTANT: must come before journal-name matching to avoid "science" false-positive
    "10.1116": "avs",
    # IEEE Xplore
    "10.1109": "ieee",
    # Japanese Journal of Applied Physics (JJAP, now IOP-hosted)
    "10.1143": "iop",
    # IBM Journal of Research and Development (now Springer-hosted)
    "10.1147": "springer",
    # OSA / Optica Publishing Group
    "10.1364": "osa",
    # Korean Physical Society
    "10.3938": "kps",
    # Beilstein Journals
    "10.3762": "beilstein",
    # CCS Chemistry (Chinese Chemical Society — Cloudflare-protected; needs cloak backend)
    "10.31635": "ccs",
}

# Journal name fragments → publisher overrides (more specific than DOI prefix)
JOURNAL_PUBLISHER_MAP = {
    "nature": "nature",
    "nat ": "nature",
    "science": "science",
    "sci adv": "science",
    "sci. adv": "science",
    "acs ": "acs",
    "j. am. chem. soc": "acs",
    "nano lett": "acs",
    "j. phys. chem": "acs",
    "angew": "wiley",
    "adv. mater": "wiley",
    "adv mater": "wiley",
    "chemsuschem": "wiley",
    "pnas": "pnas",
    "proc. natl. acad": "pnas",
    "electrochem": "ecs",
    "j. membr": "elsevier",
    "j. power sources": "elsevier",
    "matter": "elsevier",
    "iop": "iop",
    "beilstein": "beilstein",
    "ccs chemistry": "ccs",
}

# Short journal name mapping for labels
JOURNAL_SHORT = {
    "nature": "Nature",
    "nature energy": "NatEnergy",
    "nature catalysis": "NatCatal",
    "nature communications": "NatCommun",
    "nature materials": "NatMater",
    "nature biotechnology": "NatBiotechnol",
    "nature chemistry": "NatChem",
    "nature nanotechnology": "NatNano",
    "science": "Science",
    "science advances": "SciAdv",
    "journal of the american chemical society": "JACS",
    "acs catalysis": "ACSCatal",
    "acs nano": "ACSNano",
    "nano letters": "NanoLett",
    "the journal of physical chemistry b": "JPhysChemB",
    "the journal of physical chemistry c": "JPhysChemC",
    "the journal of physical chemistry letters": "JPhysChemLett",
    "acs applied energy materials": "ACSApplEnergy",
    "acs applied materials & interfaces": "ACSApplMater",
    "advanced materials": "AdvMater",
    "angewandte chemie": "AngewChem",
    "angewandte chemie international edition": "AngewChem",
    "chemsuschem": "ChemSusChem",
    "journal of membrane science": "JMembrSci",
    "journal of power sources": "JPowerSources",
    "journal of the electrochemical society": "JElectrochemSoc",
    "proceedings of the national academy of sciences": "PNAS",
    "journal of materials chemistry a": "JMaterChemA",
    "journal of applied electrochemistry": "JApplElectrochem",
    "matter": "Matter",
    "beilstein journal of nanotechnology": "BJON",
}


def detect_publisher(doi: str, journal: str = "") -> str:
    """Determine publisher from DOI prefix (primary) and journal name (fallback).
    DOI prefix is more reliable — journal name matching is only used for unknowns."""
    # DOI prefix is the most reliable signal
    prefix = doi.split("/")[0] if "/" in doi else ""
    pub = PUBLISHER_MAP.get(prefix)
    if pub:
        return pub
    # Fall back to journal name for unknown prefixes
    jl = journal.lower()
    for frag, pub in JOURNAL_PUBLISHER_MAP.items():
        if frag in jl:
            return pub
    return "unknown"


def shorten_journal(journal: str) -> str:
    """Convert full journal name to short label form."""
    jl = journal.lower().strip()
    # Exact match first
    if jl in JOURNAL_SHORT:
        return JOURNAL_SHORT[jl]
    # Partial match
    for full, short in JOURNAL_SHORT.items():
        if full in jl or jl in full:
            return short
    # Fallback: take first letters of each word
    words = journal.split()
    if len(words) <= 2:
        return journal.replace(" ", "").replace(".", "")[:15]
    return "".join(w[0].upper() for w in words if w[0].isalpha())[:10]


def make_label(first_author: str, year: int, journal: str) -> str:
    """Generate label like 'Lee2016_NatEnergy'."""
    # Clean author name
    author = first_author.strip().split(",")[0].split(" ")[-1]  # last name
    author = author.replace("-", "").replace("'", "")
    if not author or author == "?":
        author = "Unknown"
    j_short = shorten_journal(journal)
    return f"{author}{year}_{j_short}"


def fetch_metadata(doi: str, user_agent: str) -> dict:
    """Fetch full metadata for a DOI from Crossref. Returns {} on failure."""
    url = f"{API_BASE}/{urllib.request.quote(doi, safe='')}"
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["message"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise
    except Exception:
        return {}


def extract_year(msg: dict) -> int:
    """Extract publication year from Crossref metadata."""
    for key in ("published-print", "published-online", "created"):
        dp = msg.get(key, {})
        if isinstance(dp, dict) and "date-parts" in dp:
            parts = dp["date-parts"]
            if parts and parts[0] and parts[0][0]:
                return int(parts[0][0])
    return 0


def validate_one(ref: dict, user_agent: str) -> dict:
    """Validate a single reference and enrich with metadata."""
    doi = ref.get("doi")
    ref_id = ref["id"]

    if not doi:
        return {
            "id": ref_id,
            "doi": None,
            "status": "no_doi",
            "label": f"Ref{ref_id:02d}_NoDOI",
            "title": ref.get("unstructured", "")[:100],
            "authors": ref.get("author", ""),
            "year": 0,
            "journal": "",
            "publisher": "unknown",
            "error": "No DOI available in Crossref reference data",
        }

    msg = fetch_metadata(doi, user_agent)

    if not msg:
        return {
            "id": ref_id,
            "doi": doi,
            "status": "failed",
            "label": f"Ref{ref_id:02d}_UnverifiedDOI",
            "title": "",
            "authors": "",
            "year": 0,
            "journal": "",
            "publisher": detect_publisher(doi),
            "error": f"DOI not found in Crossref: {doi}",
        }

    # Extract fields
    title = msg.get("title", [""])[0]
    authors_list = msg.get("author", [])
    first_author = authors_list[0].get("family", "Unknown") if authors_list else "Unknown"
    all_authors = ", ".join(
        a.get("family", "") + " " + a.get("given", "")
        for a in authors_list[:5]
    )
    if len(authors_list) > 5:
        all_authors += f" ... (+{len(authors_list)-5} more)"

    year = extract_year(msg)
    journal = msg.get("container-title", [""])[0] if msg.get("container-title") else ""
    publisher = detect_publisher(doi, journal)
    label = make_label(first_author, year, journal)

    return {
        "id": ref_id,
        "doi": doi,
        "status": "verified",
        "label": label,
        "title": title,
        "authors": all_authors,
        "year": year,
        "journal": journal,
        "publisher": publisher,
    }


def resolve_input(arg: str):
    """Resolve CLI argument to (project_dir, raw_json_path, validated_json_path)."""
    p = Path(arg)

    # If it's a direct path to refs_raw.json
    if p.suffix == ".json" and p.exists():
        project_dir = p.parent
        return project_dir, p, project_dir / "refs_validated.json"

    # If it's a project directory name
    if p.is_dir():
        raw = p / "refs_raw.json"
        if raw.exists():
            return p, raw, p / "refs_validated.json"

    # Try as project name in current directory
    project_dir = Path(arg)
    raw = project_dir / "refs_raw.json"
    if raw.exists():
        return project_dir, raw, project_dir / "refs_validated.json"

    print(f"ERROR: Cannot find refs_raw.json in '{arg}'")
    print(f"  Tried: {p}, {project_dir / 'refs_raw.json'}")
    sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print("Usage: python validate_refs.py <project_name_or_path>")
        print("Example: python validate_refs.py jacs.5c05017")
        sys.exit(1)

    cfg = load_config()
    warn_if_placeholder_mailto(cfg)
    user_agent = user_agent_from(cfg)

    project_dir, raw_path, validated_path = resolve_input(sys.argv[1])

    print(f"Project: {project_dir}")
    print(f"Input:   {raw_path}")
    print(f"Output:  {validated_path}\n")

    # Load raw refs
    with open(raw_path, "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    parent_doi = raw_data["parent_doi"]
    parent_title = raw_data.get("parent_title", "")
    raw_refs = raw_data["references"]

    print(f"Parent: {parent_title}")
    print(f"DOI:    {parent_doi}")
    print(f"Refs:   {len(raw_refs)}\n")

    # Load existing validated data for incremental mode
    existing = {}
    if validated_path.exists():
        with open(validated_path, "r", encoding="utf-8") as f:
            prev = json.load(f)
        for r in prev.get("references", []):
            if r.get("status") == "verified":
                existing[r["id"]] = r
        print(f"Incremental: {len(existing)} already verified, skipping.\n")

    # Validate each ref
    results = []
    verified = 0
    failed = 0
    skipped = 0
    no_doi = 0

    for ref in raw_refs:
        ref_id = ref["id"]

        # Incremental: skip already verified
        if ref_id in existing:
            results.append(existing[ref_id])
            skipped += 1
            continue

        result = validate_one(ref, user_agent)
        results.append(result)

        status = result["status"]
        if status == "verified":
            verified += 1
            print(f"  [{ref_id:2d}] ✓ {result['label']}  |  {result['journal']}")
        elif status == "no_doi":
            no_doi += 1
            print(f"  [{ref_id:2d}] ⚠ NO DOI  |  {result.get('title', '')[:60]}")
        else:
            failed += 1
            print(f"  [{ref_id:2d}] ✗ FAILED  |  {result.get('error', '')}")

        time.sleep(RATE_LIMIT_DELAY)

    # Sort by id
    results.sort(key=lambda r: r["id"])

    # Summary
    total_verified = verified + skipped
    total = len(results)

    output = {
        "parent_doi": parent_doi,
        "parent_title": parent_title,
        "validated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "total": total,
            "verified": total_verified,
            "failed": failed,
            "no_doi": no_doi,
        },
        "references": results,
    }

    with open(validated_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"  Total:    {total}")
    print(f"  Verified: {total_verified}  ({skipped} cached + {verified} new)")
    print(f"  Failed:   {failed}")
    print(f"  No DOI:   {no_doi}")
    print(f"{'='*60}")
    print(f"\n✓ Saved to {validated_path}")

    if failed:
        print(f"\n⚠ {failed} refs could not be verified:")
        for r in results:
            if r["status"] == "failed":
                print(f"  [{r['id']:2d}] {r.get('doi', '?')} — {r.get('error', '')}")

    print(f"\n  Next step: python download_refs.py {project_dir.name}")


if __name__ == "__main__":
    main()
