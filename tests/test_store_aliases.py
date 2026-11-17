"""Store code replacements: NLD -> NEG, KGP -> MKG, applied wherever a code is resolved (2026-10-27)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import masters as m  # noqa: E402


def _with_aliases(monkeypatch, raw_result):
    monkeypatch.setattr(m, "_ALIASES", {"NLD": "NEG", "KGP": "MKG"})
    monkeypatch.setattr(m, "_resolve_store_unaliased", lambda *a, **k: raw_result)


def test_a_mid_mapped_to_an_old_code_resolves_to_the_new_one(monkeypatch):
    _with_aliases(monkeypatch, m.ResolveResult("paytm_mid", "X", "KGP", "resolved"))
    r = m.resolve_store(None, "paytm_mid", "X", "2026-09-10")
    assert r.store == "MKG" and r.status == "resolved"


def test_a_code_that_is_not_replaced_is_untouched(monkeypatch):
    _with_aliases(monkeypatch, m.ResolveResult("paytm_mid", "X", "ABD", "resolved"))
    assert m.resolve_store(None, "paytm_mid", "X", "2026-09-10").store == "ABD"


def test_an_identifier_mapped_to_an_old_and_its_new_code_is_no_longer_ambiguous(monkeypatch):
    _with_aliases(monkeypatch, m.ResolveResult("axis_mid", "X", None, "store_ambiguous", ("NLD", "NEG")))
    r = m.resolve_store(None, "axis_mid", "X", "2026-09-10")
    assert r.store == "NEG" and r.status == "resolved"


def test_a_genuine_ambiguity_between_two_different_stores_stays_ambiguous(monkeypatch):
    _with_aliases(monkeypatch, m.ResolveResult("axis_mid", "X", None, "store_ambiguous", ("NLD", "ABD")))
    r = m.resolve_store(None, "axis_mid", "X", "2026-09-10")
    assert r.status == "store_ambiguous" and r.candidates == ("ABD", "NEG")


def test_canonical_store(monkeypatch):
    monkeypatch.setattr(m, "_ALIASES", {"NLD": "NEG"})
    assert m.canonical_store("NLD") == "NEG" and m.canonical_store("NEG") == "NEG" and m.canonical_store(None) is None
