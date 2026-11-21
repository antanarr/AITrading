from datetime import date

import pytest

from copybot import public_carry as carry
from copybot.research_models import HOUR_MS, Bar, Funding, MarketHistory


def pair(coin="DOGE", *, gap=None):
    native = MarketHistory(
        coin,
        1,
        True,
        None,
        {hour: Bar(100, 100, 100, 100, 1000) for hour in range(6) if hour != gap},
        {hour: Funding(hour * HOUR_MS, 0) for hour in range(6)},
    )
    return carry.build_pair(
        native,
        carry.binance_venue(coin),
        {hour: carry.Price(100, 100, 1000) for hour in range(6)},
        [carry.settlement(hour * HOUR_MS + 2, 0.001, 1, 100.0) for hour in range(6)],
    )


def test_public_multipliers_are_consistent_and_btc_is_refused():
    assert carry.binance_venue("kNEIRO") == carry.Venue("NEIROUSDT", 1000)
    assert carry.binance_venue("kPEPE") == carry.Venue("1000PEPEUSDT", 1)
    assert carry.binance_venue("DOGE") == carry.Venue("DOGEUSDT", 1)
    with pytest.raises(ValueError, match="Excluded"):
        carry.binance_venue("WBTC")


def test_funding_quote_respects_both_the_publication_clock_and_interval():
    market = pair()
    assert market.binance_funding.last(3).time_ms == 2 * HOUR_MS + 2
    quote = carry.quote_at(market, 3, carry.VARIANTS[0])
    interval_quote = carry.quote_at(market, 3, carry.VARIANTS[2])
    assert quote.advantage == pytest.approx(0.003)
    assert interval_quote.advantage == pytest.approx(0.024)


def test_same_size_hedge_uses_one_long_one_short_and_interior_funding():
    market = pair()
    result = carry.simulate({market.coin: market}, 1, 5, carry.VARIANTS[0], "base", 500)
    assert result["count"] == 1
    trade = result["trades"][0]
    assert trade["entered_ms"] == HOUR_MS
    assert trade["exited_ms"] == 4 * HOUR_MS
    assert trade["reason"] == "period_end"
    assert [leg["side"] for leg in trade["legs"]] == [1, -1]
    assert [leg["quantity"] for leg in trade["legs"]] == [1, 1]
    assert trade["funding"] == pytest.approx(0.2)
    assert trade["fees"] == pytest.approx(0.19)
    assert trade["slippage"] == pytest.approx(0.16)
    assert result["net"] == pytest.approx(-0.15)


def test_executed_size_requires_independent_venue_collateral_and_reports_gaps():
    markets = {coin: pair(coin) for coin in ("DOGE", "TRUMP")}
    full = carry.simulate(markets, 1, 5, carry.VARIANTS[0], "base", 500)
    lean = carry.simulate(markets, 1, 5, carry.VARIANTS[0], "base", 300)
    assert [trade["legs"][0]["notional"] for trade in full["trades"]] == [100, 50]
    assert [trade["legs"][0]["notional"] for trade in lean["trades"]] == [60, 30]
    broken = carry.simulate({"DOGE": pair(gap=2)}, 1, 5, carry.VARIANTS[0], "base", 500)
    assert broken["count"] == 0
    assert broken["unresolved_exits"] == 1
    assert broken["unresolved_positions"][0]["first_gap_ms"] == 2 * HOUR_MS
    assert broken["hourly_drawdown"] == 0


def test_optional_basis_entry_gate_charges_both_venues_before_trading():
    market = pair()
    prices_equal = carry.quote_at(market, 3, carry.VARIANTS[0])
    entry, reason = carry.entry_candidate(
        market, prices_equal, (250, 250), carry.VARIANTS[1], carry.SLIPPAGE["base"]
    )
    assert entry is None
    assert reason == "negative_entry_basis"


def test_entry_quantity_scales_with_per_venue_available_margin_and_minimum():
    market = pair()
    quote = carry.quote_at(market, 3, carry.VARIANTS[0])
    entry, _ = carry.entry_candidate(
        market, quote, (250, 43), carry.VARIANTS[0], carry.SLIPPAGE["base"]
    )
    assert entry[1] == 0.4
    assert entry[3] == pytest.approx(58.6)
    rejected, reason = carry.entry_candidate(
        market, quote, (23.50, 23.50), carry.VARIANTS[0], carry.SLIPPAGE["base"]
    )
    assert rejected is None
    assert reason == "below_modeled_minimum_notional"


def test_archive_guard_checks_future_dates_before_reading_any_paths(tmp_path):
    with pytest.raises(ValueError, match="cutoff"):
        carry.load_binance(
            carry.binance_venue("DOGE"), tmp_path / "missing", date(2026, 10, 1), date(2026, 11, 22)
        )


def test_complete_funding_mode_filters_native_history_before_external_requests(
    tmp_path, monkeypatch
):
    market = pair()
    native = {coin: pair(coin).native for coin in ("DOGE", "TRUMP")}
    metadata = {
        "markets": {
            "DOGE": {"missing_funding_for_hourly": 0},
            "TRUMP": {"missing_funding_for_hourly": 1},
        }
    }

    def verify_pairs(histories, *_):
        assert list(histories) == ["DOGE"]
        return {"DOGE": market}, {}

    monkeypatch.setattr(carry, "load_archive", lambda *_: (native, metadata))
    monkeypatch.setattr(carry, "archive_pairs", verify_pairs)
    result = carry.run(
        tmp_path,
        tmp_path / "cache",
        date(2026, 5, 25),
        date(2026, 5, 26),
        date(2026, 5, 27),
        complete_native_funding=True,
    )
    assert result["excluded_for_incomplete_native_funding"] == ["TRUMP"]
    assert result["native_archive_coverage"] == metadata
