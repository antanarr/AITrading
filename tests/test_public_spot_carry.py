import pytest

from copybot import public_spot_carry as carry
from copybot.research_models import HOUR_MS, Bar, Funding

OPEN = carry.utc_hour("2026-09-26")


def pair():
    identity = carry.Identity("kBONK", "UBONK", "reviewed", 1000, "source", True)
    return carry.Pair(identity, "@194", 0, 0, 10)


def history():
    asset = pair()
    hours = range(OPEN - carry.WEEK, OPEN + 3 * carry.WEEK + 1)
    spot = {hour: Bar(0.01, 0.0102, 0.0099, 0.01, 100_000_000) for hour in hours}
    perp = {hour: Bar(10, 10.2, 9.95, 10, 100_000) for hour in hours}
    funding = {hour: Funding(hour * HOUR_MS + 30, 0.0001) for hour in hours}
    return carry.History(asset, spot, perp, funding, {})


def test_census_joins_reviewed_token_on_canonical_quote_and_unit_issuer():
    token = pair().identity
    tokens = [
        (0, "USDC", carry.USDC_TOKEN_ID),
        (1, "UBONK", token.token_id),
        (2, "BONK", "impostor-symbol"),
        (3, "USDC", "impostor-usdc"),
        (4, "UBTC", "reviewed-btc-wrapper"),
    ]
    spot = {
        "tokens": [
            {"index": index, "name": symbol, "tokenId": token_id, "szDecimals": 2}
            for index, symbol, token_id in tokens
        ],
        "universe": [
            {"tokens": indices, "name": f"@{index}"}
            for index, indices in enumerate(((1, 0), (2, 0), (1, 3), (4, 0)))
        ],
    }
    native = {
        "universe": [
            {"name": name, "szDecimals": 1, "maxLeverage": 10}
            for name in ("kBONK", "BTC", "test:kBONK")
        ]
    }
    pairs, exclusions = carry.discover(
        spot, native, {token.token_id: carry.UNIT_DEPLOYER}, (token,)
    )
    assert pairs["kBONK"].spot_id == "@0"
    assert pairs["kBONK"].identity.units_per_contract == 1000
    assert [item["reason"] for item in exclusions] == [
        "underlying_identity_unverified",
        "noncanonical_or_non_usdc_quote",
    ]
    pairs, exclusions = carry.discover(spot, native, {}, (token,))
    assert not pairs
    assert exclusions[0]["reason"] == "unit_issuer_unverified"


def test_bounded_candles_exclude_end_and_drop_conflicted_hours():
    def row(hour, close):
        return {
            "s": "@1",
            "i": "1h",
            "t": hour * HOUR_MS,
            "T": (hour + 1) * HOUR_MS - 1,
            "o": "1",
            "h": "4",
            "l": "1",
            "c": close,
            "v": "100",
        }

    parsed, errors = carry.parsed_rows(
        [row(OPEN, "1"), row(OPEN, "2"), row(OPEN + 1, "3"), row(OPEN + 2, "bad")],
        "@1",
        OPEN,
        OPEN + 2,
    )
    assert set(parsed) == {OPEN + 1}
    assert errors == 1
    bounds = carry.candles_body("@1", OPEN, OPEN + 2)["req"]
    assert bounds["endTime"] == (OPEN + 2) * HOUR_MS - 1


def test_equal_underlying_hedge_books_both_fees_slippage_and_settlement_clock():
    market = history()
    market.funding[OPEN] = Funding(OPEN * HOUR_MS + 25, 0.03)
    exit_hour = OPEN + carry.WEEK
    market.funding[exit_hour] = Funding(exit_hour * HOUR_MS + 25, -0.01)
    trade, problem = carry.execute(market, OPEN, exit_hour, 500.0, carry.PROFILES["base"])
    assert problem is None
    assert trade["spot"]["units"] == trade["perp"]["contracts"] * 1000
    assert trade["capital_committed_at_entry_usdc"] <= 475
    assert trade["spot"]["units"] * trade["spot"]["entry_px"] > 200
    assert trade["basis_pnl_usdc"] == 0
    expected_funding = trade["spot"]["units"] * 0.01 * ((carry.WEEK - 1) * 0.0001 - 0.01)
    assert trade["funding_pnl_usdc"] == pytest.approx(expected_funding)
    assert trade["fees_usdc"] > 0
    assert trade["slippage_usdc"] > 0
    assert trade["net_usdc"] == pytest.approx(
        expected_funding - trade["fees_usdc"] - trade["slippage_usdc"]
    )
    assert trade["entry_utc_ms"] == OPEN * HOUR_MS
    assert trade["exit_utc_ms"] == exit_hour * HOUR_MS


def test_replay_resizes_after_settlement_and_excludes_locked_profit():
    book = {"kBONK": history()}
    end = OPEN + 3 * carry.WEEK
    regular = carry.replay(book, OPEN, end, 500, "base", adaptive=True)
    reserve = carry.replay(book, OPEN, end, 500, "base", adaptive=True, profit_reserve=0.25)
    assert regular["completed_trades"] == reserve["completed_trades"] == 2
    one, two = reserve["trades"]
    assert two["account_equity_before_usdc"] == pytest.approx(one["account_equity_after_usdc"])
    assert two["locked_profit_before_usdc"] == pytest.approx(one["net_usdc"] / 4)
    assert two["available_entry_equity_usdc"] < regular["trades"][1]["available_entry_equity_usdc"]
    assert reserve["final_locked_profit_usdc"] > one["locked_profit_after_usdc"]


def test_missing_future_price_does_not_change_past_ranking_or_silently_flatten():
    market = history()
    score, problem = carry.expected_carry(market, OPEN, 500.0, carry.PROFILES["base"])
    assert problem is None and score > 0
    del market.spot[OPEN + 1]
    score_after, _ = carry.expected_carry(market, OPEN, 500.0, carry.PROFILES["base"])
    assert score_after == score
    report = carry.replay(
        {"kBONK": market},
        OPEN,
        OPEN + carry.WEEK + 1,
        500,
        "base",
        adaptive=True,
    )
    assert report["completed_trades"] == 0
    assert report["diagnostics"]["unreplayable_selected_windows"] == 1
    assert report["diagnostics"]["missing_held_prices"] == 1
