"""Audit D-008: text colours the theme puts on a tint or white must meet WCAG AA (4.5:1 for normal text).

The amber 'Receivable' figure was #B07B18 on #FBF3E1 (3.34:1); status pills and tile sub-lines in good / warn / bad were 3.3-4.5:1. The plain
--good/--warn/--bad stay for bars and borders (3:1 is enough there); text uses the darker --*-ink tokens."""
import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "app" / "assets" / "theme.css").read_text(encoding="utf-8")
ROOT = CSS[CSS.index(":root"):CSS.index("}", CSS.index(":root"))]
TOKENS = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", ROOT))


def _lum(h):
    h = h.lstrip("#")
    r, g, b = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _contrast(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


TEXT_ON = [("ink", "surface"), ("ink", "bg"), ("ink-soft", "surface"), ("ink-soft", "bg"), ("ink-soft", "surface-alt"), ("accent", "surface"),
           ("accent", "accent-tint"), ("good-ink", "good-tint"), ("good-ink", "surface"), ("warn-ink", "warn-tint"), ("warn-ink", "surface"),
           ("bad-ink", "bad-tint"), ("bad-ink", "surface"), ("bad", "surface")]


def test_every_text_pair_meets_aa():
    bad = [(t, b, round(_contrast(TOKENS[t], TOKENS[b]), 2)) for t, b in TEXT_ON if _contrast(TOKENS[t], TOKENS[b]) < 4.5]
    assert not bad, f"text pairs under 4.5:1: {bad}"


def test_the_rules_that_put_text_on_a_tint_use_the_ink_tokens():
    for rule, token in [(".mop-status--warn", "warn-ink"), (".mop-status--good", "good-ink"), (".mop-status--bad", "bad-ink"),
                        (".mop-tile-sub--warn", "warn-ink"), (".mop-matrix-cell--warn .mop-matrix-figure", "warn-ink")]:
        m = re.search(re.escape(rule) + r"\s*\{[^}]*color:\s*var\(--([a-z-]+)\)", CSS)
        assert m and m.group(1) == token, f"{rule} should use --{token}, uses {m.group(1) if m else None}"


def test_no_button_rule_makes_a_touch_target_smaller_than_24px():
    """WCAG 2.2 (2.5.8): a pointer target is at least 24x24 CSS px. The two compact +/- rules were 24x22."""
    for m in re.finditer(r"button\s*\{([^}]*)\}", CSS):
        body = m.group(1)
        for prop in ("min-height", "height", "min-width", "width"):
            for v in re.findall(r"(?<![a-z-])" + prop + r":\s*(\d+)px", body):
                assert int(v) >= 24, f"{prop}: {v}px in a button rule: {body.strip()[:80]}"
