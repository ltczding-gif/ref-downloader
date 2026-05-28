"""Offline tests for validate_refs.py — DOI prefix → publisher classification."""

from validate_refs import detect_publisher


def test_detect_publisher_by_prefix():
    """DOI prefix is the primary signal."""
    assert detect_publisher("10.1021/jacs.5c05017") == "acs"
    assert detect_publisher("10.1038/nature12345") == "nature"
    assert detect_publisher("10.1016/j.jpowsour.2018.01.068") == "elsevier"
    assert detect_publisher("10.1002/adma.201234567") == "wiley"
    assert detect_publisher("10.1149/1.3546038") == "ecs"
    assert detect_publisher("10.1063/1.1234567") == "aip"
    # CCS Chemistry (Chinese Chemical Society) — added in v0.4
    assert detect_publisher("10.31635/ccschem.024.202404123") == "ccs"


def test_detect_publisher_falls_back_to_journal_for_unknown_prefix():
    """When the DOI prefix isn't in PUBLISHER_MAP, the journal name is consulted."""
    # Unknown prefix + recognizable journal name → falls back to journal
    assert detect_publisher("10.99999/unknown", journal="Nature Communications") == "nature"
    # Unknown prefix + no journal → unknown
    assert detect_publisher("10.99999/unknown") == "unknown"
    # Unknown prefix + unrecognized journal → unknown
    assert detect_publisher("10.99999/unknown", journal="Made-Up Journal Name") == "unknown"
    # Unknown prefix + CCS Chemistry journal name → ccs
    assert detect_publisher("10.99999/unknown", journal="CCS Chemistry") == "ccs"
