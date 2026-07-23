"""Unit tests for the read-only line-item audit's reverse-transformation logic.

These lock down that the audit reproduces the pipeline's store-time and display-time
math exactly, so a genuine MISMATCH means a real pipeline bug, not an audit artifact.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import audit_lineitems as A  # noqa: E402


def test_reproduce_scaled_plain():
    # Revenue printed in millions, unit 1e6, no sign rules -> stored raw dollars.
    assert A._reproduce(14437.2, 1_000_000.0, "TotalRevenue", "Revenues") == 14437.2 * 1_000_000.0


def test_reproduce_per_share_no_scale():
    # EPS is a NO_SCALE_KEY: value used as printed regardless of unit.
    assert A._reproduce(4.41, 1_000_000.0, "BasicEPS", "Basic net earnings per share") == 4.41


def test_reproduce_abs_key():
    # CostOfRevenue is an _ABS_VALUE_KEY: negative printed becomes positive magnitude.
    assert A._reproduce(-500.0, 1000.0, "CostOfRevenue", "Cost of sales") == 500.0 * 1000.0


def test_reproduce_negate_key():
    # MinorityInterests is stored negated.
    assert A._reproduce(3.0, 1_000_000.0, "MinorityInterests", "Non-controlling interests") == -3.0 * 1_000_000.0


def test_reproduce_pure_loss_flip():
    # A positive printed number under a pure "loss" caption flips negative.
    v = A._reproduce(7121.0, 1000.0, "NetIncome", "Loss and comprehensive loss for the year")
    assert v == -7121.0 * 1000.0


def test_reproduce_income_caption_not_flipped():
    # "Net earnings" is not a pure-loss caption -> sign preserved.
    v = A._reproduce(681.4, 1_000_000.0, "NetIncome", "Net earnings")
    assert v == 681.4 * 1_000_000.0

    # Mixed "income (loss)" caption also must NOT flip.
    v2 = A._reproduce(50.0, 1_000_000.0, "NetIncome", "Net income (loss) for the year")
    assert v2 == 50.0 * 1_000_000.0


def test_sheet_value_millions_and_display_negate():
    # scale 'm' divides by 1e6; TaxProvision is negated only at display.
    assert A._sheet_value("TotalRevenue", 14437.2 * 1_000_000.0) == 14437.2
    assert A._sheet_value("TaxProvision", 191.9 * 1_000_000.0) == -191.9
    # EPS renders raw (no /1e6).
    assert A._sheet_value("BasicEPS", 4.41, scale="raw") == 4.41


def test_num_forms():
    forms = A._num_forms(14437.2)
    assert "14,437.2" in forms and "14437.2" in forms
    forms2 = A._num_forms(378.0)
    assert "378" in forms2


def test_face_sign_positive_and_parenthesized():
    assert A._face_sign("Revenues (note 7) 14,437.2 11,932.9", 14437.2) == 1
    assert A._face_sign("Net financing expense (1,234) prior", 1234.0) == -1
    # absent -> None (advisory, not a hard fail)
    assert A._face_sign("nothing relevant here", 999.0) is None


def test_alias_hit_face_requires_number():
    # A line with the concept AND a number is a hit; a bare heading is not.
    aliases = A._aliases_for("Goodwill")
    with_num = A._numbered_norm_lines("Goodwill (note 20) 7,155.8")
    assert with_num, "line with a number should survive the number filter"
    hit = A._face_alias_hit(aliases, with_num)
    assert hit is not None and hit[1] in ("exact", "fuzzy")
    # a bare heading (no number) is filtered out before matching
    assert A._numbered_norm_lines("Goodwill") == []


def _run():
    import types
    g = dict(globals())
    for name, fn in g.items():
        if name.startswith("test_") and isinstance(fn, types.FunctionType):
            fn()
            print("ok", name)
    print("all audit-tool tests passed")


if __name__ == "__main__":
    _run()
