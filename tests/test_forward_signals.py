from decimal import Decimal

import pytest

from copybot.forward_signals import (
    DAY_MS,
    MAX_LAG_MS,
    RANK_HOURS,
    ForwardDataError,
    SignalLoader,
)
from copybot.research_models import DAY_HOURS, HOUR_MS

HOUR = 50_000 // DAY_HOURS * DAY_HOURS
BOUNDARY = HOUR * HOUR_MS


class Clock:
    def __init__(self, stamp=BOUNDARY):
        self.stamp = stamp
        self.elapsed = 0.0

    def now(self):
        return self.stamp + int(self.elapsed * 1000)

    def monotonic(self):
        return self.elapsed

    def sleep(self, delay):
        self.elapsed += delay


class Venue:
    def __init__(self, coins):
        self.coins = coins
        self.recent_listings = {}
        self.gap = None
        self.closed_price = {}
        self.calls = []
        self.day_liquidity_failure = set()

    def info(self, kind, **payload):
        self.calls.append((kind, payload))
        if kind == "metaAndAssetCtxs":
            return [
                {
                    "universe": [
                        {
                            "name": coin,
                            "szDecimals": 3,
                            "maxLeverage": 20,
                            "isDelisted": coin == "DELISTED",
                        }
                        for coin in self.coins
                    ]
                },
                [{"markPx": "100", "midPx": "99"} for coin in self.coins],
            ]
        request = payload["req"]
        coin, interval = request["coin"], request["interval"]
        step = HOUR_MS if interval == "1h" else DAY_MS
        first = max(request["startTime"], self.recent_listings.get(coin, 0))
        rows = []
        for stamp in range(first, request["endTime"] + 1, step):
            if (coin, stamp) == self.gap:
                continue
            price = self.closed_price.get(coin, 100) if stamp + step == BOUNDARY else 100
            volume = self.coins[coin]
            if coin in self.day_liquidity_failure and stamp >= BOUNDARY - 16 * HOUR_MS:
                volume = 0
            rows.append(
                {
                    "s": coin,
                    "i": interval,
                    "t": stamp,
                    "T": stamp + step - 1,
                    "o": str(price),
                    "h": str(price + 1),
                    "l": str(price - 1),
                    "c": str(price),
                    "v": str(volume),
                }
            )
        # A venue response can contain the just-opened candle. Its values must stay unread.
        return [{"t": BOUNDARY, "s": "WRONG", "c": "future"}, *reversed(rows)]


def loader(venue, clock):
    return SignalLoader(venue.info, now_ms=clock.now, monotonic=clock.monotonic, sleep=clock.sleep)


def test_top_20_ranking_keeps_non_signals_and_tracks_open_symbols_outside_roster():
    coins = {f"C{n:02}": 1000 + 100 * n for n in range(21)}
    coins |= {"BTC": 10_000, "wBTC": 10_000, "dex:COIN": 10_000, "DELISTED": 10_000}
    venue, clock = Venue(coins), Clock()
    venue.closed_price = {"C20": 110, "C00": 90}
    result = loader(venue, clock).load(HOUR, open_symbols=["C00"])
    assert result.complete
    assert len(result.roster) == 20 and result.roster[:2] == ("C20", "C19")
    assert set(result.decisions) == set(result.roster) | {"C00"}
    entry = result.decisions["C20"]
    assert (entry.rank, entry.market.decimals, entry.market.max_leverage) == (1, 3, 20)
    assert entry.plan.side == 1
    assert entry.plan.limit == pytest.approx(110.44)
    assert entry.plan.stop_atr == 2 and entry.plan.hold_hours == 240
    assert entry.atr == Decimal(19) / 7
    assert entry.sma50 == Decimal(100) and entry.entry_high == Decimal(101)
    assert result.decisions["C19"].plan is None and result.decisions["C19"].status == "ready"
    assert result.decisions["C00"].rank is None
    assert result.decisions["C00"].plan is None and result.decisions["C00"].exit_long is True
    assert all(
        call[1].get("req", {}).get("coin") not in {"BTC", "wBTC", "dex:COIN", "DELISTED"}
        for call in venue.calls
    )
    assert clock.elapsed > 60
    assert all(call[1].get("req", {}).get("endTime", 0) < BOUNDARY for call in venue.calls)


def test_unknown_ranking_gap_disables_all_entries_but_explicit_listing_is_excluded():
    venue = Venue({"ETH": 3000, "HYPE": 3000, "NEW": 100_000, "QUIET": 100_000})
    venue.closed_price["ETH"] = 110
    venue.recent_listings["NEW"] = BOUNDARY - 10 * DAY_MS
    venue.day_liquidity_failure.add("QUIET")
    venue.gap = ("HYPE", BOUNDARY - 5 * DAY_MS)
    result = loader(venue, Clock()).load(HOUR)
    assert not result.complete
    assert result.problems == {"HYPE": "Incomplete 30-day hourly ranking history"}
    assert result.exclusions["NEW"] == "new_listing_with_less_than_30_days"
    assert result.exclusions["QUIET"] == "fails_closed_24h_liquidity"
    assert result.decisions["ETH"].plan is None


def test_verified_illiquid_market_does_not_block_an_unrelated_breakout():
    venue = Venue({"ETH": 3000, "QUIET": 100})
    venue.closed_price["ETH"] = 110
    venue.gap = ("QUIET", BOUNDARY - 5 * DAY_MS)
    result = loader(venue, Clock()).load(HOUR)
    assert result.complete and result.roster == ("ETH",)
    assert result.exclusions["QUIET"] == "fails_closed_24h_liquidity"
    assert result.decisions["ETH"].plan.side == 1
    assert (
        sum(
            kind == "candleSnapshot" and payload["req"]["coin"] == "QUIET"
            for kind, payload in venue.calls
        )
        == 1
    )


def test_missing_recent_candle_cannot_be_used_to_prove_low_turnover():
    venue = Venue({"ETH": 3000, "QUIET": 100})
    venue.closed_price["ETH"] = 110
    venue.gap = ("QUIET", BOUNDARY - HOUR_MS)
    result = loader(venue, Clock()).load(HOUR)
    assert not result.complete
    assert result.problems == {"QUIET": "Incomplete 30-day hourly ranking history"}
    assert result.decisions["ETH"].plan is None


def test_short_transient_data_gap_is_retried_using_only_closed_candles():
    venue, clock = Venue({"ETH": 3000, "HYPE": 3000}), Clock()
    venue.closed_price["ETH"] = 110
    gap = BOUNDARY - HOUR_MS
    venue.gap = ("HYPE", gap)

    def transient_gap(kind, **payload):
        rows = venue.info(kind, **payload)
        if kind == "candleSnapshot" and payload["req"]["coin"] == "HYPE":
            venue.gap = None
        return rows

    scanner = SignalLoader(
        transient_gap, now_ms=clock.now, monotonic=clock.monotonic, sleep=clock.sleep
    )
    result = scanner.load(HOUR)
    assert result.complete
    assert result.decisions["ETH"].plan.side == 1
    assert (
        "candleSnapshot",
        {
            "req": {
                "coin": "HYPE",
                "interval": "1h",
                "startTime": gap,
                "endTime": BOUNDARY - 1,
            }
        },
    ) in venue.calls


def test_ranked_new_listing_has_no_entry_when_50_prior_days_are_missing():
    venue = Venue({"ETH": 3000})
    venue.closed_price["ETH"] = 110
    venue.recent_listings["ETH"] = BOUNDARY - 40 * DAY_MS
    result = loader(venue, Clock()).load(HOUR)
    assert result.complete and result.roster == ("ETH",)
    assert result.decisions["ETH"].status == "insufficient_50_day_history"
    assert result.decisions["ETH"].plan is None
    assert result.decisions["ETH"].exit_short is True


def test_prior_census_prevents_current_additions_from_switching_roster():
    venue, clock = Venue({"ETH": 3000}), Clock(BOUNDARY - HOUR_MS // 2)
    scanner = loader(venue, clock)
    prior = scanner.capture_universe()
    venue.coins["HYPE"] = 1_000_000
    clock.stamp = BOUNDARY
    result = scanner.load(HOUR, prior_universe=prior)
    assert result.complete and result.roster == ("ETH",)
    assert result.census_basis == "pre_boundary_snapshot"
    assert result.exclusions["HYPE"] == "not_active_in_pre_boundary_census"
    assert venue.calls[2][1]["req"]["startTime"] == BOUNDARY - RANK_HOURS * HOUR_MS


def test_decision_window_rejects_past_future_and_overrun_entries():
    venue = Venue({"ETH": 3000})
    for stamp in (BOUNDARY - 1, BOUNDARY + MAX_LAG_MS + 1):
        with pytest.raises(ForwardDataError, match="15 minutes"):
            loader(venue, Clock(stamp)).load(HOUR)
    assert venue.calls == []
    clock = Clock(BOUNDARY + MAX_LAG_MS - 100)
    result = loader(venue, clock).load(HOUR)
    assert not result.complete and not result.roster
    assert result.problems == {"ETH": "Forward decision deadline exceeded"}


def test_last_census_controls_executability_without_reranking():
    venue, clock = Venue({"ETH": 3000}), Clock()
    venue.closed_price["ETH"] = 110

    def changed_meta(kind, **payload):
        raw = venue.info(kind, **payload)
        if kind == "metaAndAssetCtxs" and len(venue.calls) > 1:
            raw[0]["universe"][0].update(isDelisted=True, maxLeverage=5)
        return raw

    scanner = SignalLoader(
        changed_meta, now_ms=clock.now, monotonic=clock.monotonic, sleep=clock.sleep
    )
    result = scanner.load(HOUR)
    assert result.complete and result.roster == ("ETH",)
    row = result.decisions["ETH"]
    assert row.plan is None and not row.market.active and row.status == "inactive"
    assert row.market.max_leverage == 5
