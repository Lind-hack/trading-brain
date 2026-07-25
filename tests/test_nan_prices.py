"""NaN-quote handling — the failure mode that silently poisons the ledger.

yfinance returns NaN for a halted symbol or a bad session. NaN slips past `is None`, makes
every stop comparison False, and serialises into PORTFOLIO.json as a bare `NaN` token that is
not valid JSON. These tests pin the behaviour at each layer that can see a price.
"""
import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import supabase
from brain.portfolio import Portfolio, usable_price

NAN = float("nan")


@pytest.fixture
def pf(tmp_path):
    return Portfolio(path=tmp_path / "PORTFOLIO.json")


def _buy(pf, ticker, entry, weight=10.0):
    return pf.apply_action(
        {"action": "BUY", "ticker": ticker, "entry": entry, "stop": None,
         "target_weight_pct": weight, "reason": "test"},
        prices={ticker: entry})


# -- usable_price ---------------------------------------------------------------

@pytest.mark.parametrize("bad", [None, NAN, float("inf"), float("-inf"), 0, -5, "abc"])
def test_unusable_prices_rejected(bad):
    assert usable_price(bad) is False


@pytest.mark.parametrize("good", [1, 0.01, 142.37])
def test_real_prices_accepted(good):
    assert usable_price(good) is True


# -- the ledger -----------------------------------------------------------------

def test_nan_quote_does_not_mark_the_position(pf):
    assert _buy(pf, "NVDA", entry=100)[0]
    pf.mark_to_market({"NVDA": 110})
    exits = pf.mark_to_market({"NVDA": NAN})
    assert exits == []
    pos = pf.state["positions"]["NVDA"]
    assert pos["last"] == 110            # held at the last good quote, not overwritten
    assert math.isfinite(pos["high_water"])


def test_nan_quote_keeps_equity_finite_and_json_valid(pf, tmp_path):
    assert _buy(pf, "AAPL", entry=100)[0]
    eq = pf.equity({"AAPL": NAN})
    assert math.isfinite(eq)
    pf.save()
    # strict JSON: json.loads rejects the bare NaN token Python would otherwise emit
    text = (tmp_path / "PORTFOLIO.json").read_text(encoding="utf-8")
    json.loads(text, parse_constant=_reject)


def _reject(token):
    raise AssertionError(f"non-JSON constant {token!r} written to PORTFOLIO.json")


def test_nan_quote_cannot_fire_a_stop(pf):
    """A NaN must never be read as a -100% move."""
    assert _buy(pf, "TSLA", entry=100)[0]
    assert pf.mark_to_market({"TSLA": NAN}) == []
    assert "TSLA" in pf.state["positions"]


def test_summary_survives_a_nan_quote(pf):
    assert _buy(pf, "MSFT", entry=100)[0]
    s = pf.summary({"MSFT": NAN})
    assert math.isfinite(s["equity"])
    assert math.isfinite(s["open_positions"][0]["pnl_pct"])


# -- the dashboard payload ------------------------------------------------------

def test_jsonable_scrubs_non_finite_everywhere():
    payload = {"equity": NAN, "positions": [{"pnl_pct": float("inf"), "ticker": "NVDA"}],
               "nested": {"deep": [1.5, float("-inf")]}, "ok": 3.3, "text": "fine"}
    clean = supabase._jsonable(payload)
    assert clean["equity"] is None
    assert clean["positions"][0]["pnl_pct"] is None
    assert clean["positions"][0]["ticker"] == "NVDA"
    assert clean["nested"]["deep"] == [1.5, None]
    assert clean["ok"] == 3.3
    assert clean["text"] == "fine"
    json.dumps(clean, allow_nan=False)   # what requests does; raises on any leftover NaN