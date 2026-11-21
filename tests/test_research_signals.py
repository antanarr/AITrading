from dataclasses import replace

from copybot.research_models import HOUR_MS, Bar, Funding, MarketHistory
from copybot.research_signals import PriceRange, SignalFrame, build_frames, ideas, variants

MENU = {variant.name: variant for variant in variants()}


def _bar(price: float, width: float = 1.0, volume: float = 1000.0) -> Bar:
    return Bar(price, price + width, price - width, price, volume)


def _frame(coin: str = "MEME_A", **changes) -> SignalFrame:
    base = SignalFrame(
        coin=coin,
        is_meme=True,
        age=None,
        bar=Bar(100.8, 102.7, 99.7, 100.4, 450),
        previous=Bar(100.2, 102.8, 99.9, 101.1, 250),
        atr=1.0,
        turnover=1_500_000,
        volume_multiple=1.8,
        vwap12=99.4,
        vwap24=99.0,
        previous_vwap12=99.5,
        previous_vwap24=99.2,
        prior_ranges={6: PriceRange(97.5, 102.8), 12: PriceRange(96, 103.3)},
        returns={4: 0.004, 12: 0.01, 24: 0.04, 48: 0.06, 72: 0.07},
        hourly_vol=0.01,
        vol_ratio=1.2,
        funding_bps=(0.60,) * 6,
        compression_percentiles={},
        listing_peak=None,
    )
    return replace(base, **changes)


def _ideas(name: str, *frames: SignalFrame, hour: int = 120):
    return ideas({hour: {frame.coin: frame for frame in frames}}, MENU[name]).get(hour, [])


def test_decision_uses_closed_bars_and_strictly_earlier_funding_timestamps():
    decision = 120
    bars = {hour: _bar(100 + hour % 3) for hour in range(70, decision)}
    observed = {hour: Funding(hour * HOUR_MS, 0.00004) for hour in range(decision - 6, decision)}
    history = MarketHistory("MEME_A", 0, True, None, bars, observed)
    frames = build_frames({history.coin: history}, decision, decision + 1)
    changed = replace(
        history,
        bars={**bars, decision: _bar(4000), decision + 1: _bar(1)},
        funding={
            **observed,
            # A misleading mapping key must have no effect on when this event is observable.
            -1: Funding(decision * HOUR_MS, -0.5),
            decision + 1: Funding((decision + 1) * HOUR_MS, 0.5),
        },
    )
    assert frames == build_frames({history.coin: changed}, decision, decision + 1)
    assert frames[decision][history.coin].bar == bars[decision - 1]
    assert frames[decision][history.coin].funding_bps == (0.40,) * 6


def test_eligibility_excludes_btc_sparse_hours_and_unverified_young_listings():
    decision = 120
    old_bars = {hour: _bar(100) for hour in range(90, decision)}
    young_bars = {hour: old_bars[hour] for hour in range(108, decision)}
    histories = {
        "PERP:BTC": MarketHistory("PERP:BTC", 0, True, None, old_bars, {}),
        "MATURE": MarketHistory("MATURE", 0, True, None, old_bars, {}),
        "VERIFIED": MarketHistory("VERIFIED", 0, False, 108, young_bars, {}),
        "UNVERIFIED": MarketHistory("UNVERIFIED", 0, False, None, young_bars, {}),
        "FUTURE": MarketHistory("FUTURE", 0, False, 121, old_bars, {}),
        "GAP": MarketHistory(
            "GAP", 0, True, 90, {hour: bar for hour, bar in old_bars.items() if hour != 111}, {}
        ),
        "SPARSE": MarketHistory(
            "SPARSE",
            0,
            True,
            None,
            {hour: _bar(100, volume=10_000 if hour in {110, 119} else 0) for hour in old_bars},
            {},
        ),
    }
    frames = build_frames(histories, decision, decision + 1)
    assert set(frames[decision]) == {"MATURE", "VERIFIED"}
    assert frames[decision]["VERIFIED"].age == 12
    assert frames[decision]["VERIFIED"].turnover == 1_200_000


def test_funding_exhaustion_fades_the_bounce_and_squeeze_reclaims_a_higher_low():
    short = _frame()
    assert _ideas("funding-6h", short)[0].legs[0].side == -1
    assert _ideas("funding-6h", replace(short, funding_bps=(0.6,) * 5)) == []
    assert _ideas("funding-squeeze-long", short) == []

    long = replace(
        short,
        bar=Bar(99.8, 102, 99.5, 101.7, 450),
        previous=Bar(101.1, 101.2, 98.8, 100.0, 250),
        vwap12=100.5,
        funding_bps=(-0.65,) * 6,
    )
    squeeze = _ideas("funding-squeeze-long", long)[0].legs[0]
    assert squeeze.side == 1
    assert squeeze.limit < long.bar.close
    assert squeeze.target_atr is None
    assert squeeze.trailing_atr == 1.8
    assert _ideas("funding-squeeze-long", replace(long, volume_multiple=1.0)) == []


def test_listing_fade_and_washout_use_different_legs_and_verified_ages():
    fade = _frame(
        is_meme=False,
        age=12,
        listing_peak=110,
        bar=Bar(101.1, 101.4, 98.0, 98.7, 450),
    )
    assert _ideas("listing-6h", fade)[0].legs[0].side == -1
    for age in (None, 11, 337):
        assert _ideas("listing-6h", replace(fade, age=age)) == []
    assert _ideas("listing-washout-long", fade) == []

    washout = replace(
        fade,
        age=336,
        listing_peak=135,
        bar=Bar(99.9, 102.0, 99.3, 101.7, 450),
        previous=Bar(99.4, 100.6, 98.0, 100.0, 250),
        prior_ranges={6: PriceRange(97.5, 100.7)},
        funding_bps=(-0.65,) * 6,
    )
    order = _ideas("listing-washout-long", washout)[0].legs[0]
    assert order.side == 1 and order.trailing_atr == 2
    assert _ideas("listing-6h", washout) == []
    assert _ideas("listing-washout-long", replace(washout, listing_peak=None)) == []


def test_dispersion_fades_outlier_into_vwap_with_exhaustion_and_funding():
    ordinary = [_frame(f"MEME_{index}") for index in range(4)]
    outlier = _frame(returns={4: 0.029})
    order = _ideas("dispersion-4h", *ordinary, outlier)[0].legs[0]
    assert (order.coin, order.side, order.mode) == (outlier.coin, -1, "maker")
    assert order.target_price == outlier.vwap12
    assert _ideas("dispersion-4h", *ordinary, replace(outlier, funding_bps=(-0.5,) * 6)) == []
    no_wick = replace(outlier, bar=replace(outlier.bar, high=outlier.bar.open + 0.1))
    assert _ideas("dispersion-4h", *ordinary, no_wick) == []
    assert _ideas("dispersion-4h", *ordinary, replace(no_wick, volume_multiple=2.3))
    assert _ideas("dispersion-4h", *ordinary, replace(outlier, volume_multiple=1.0))
    assert _ideas("dispersion-4h", *ordinary, replace(outlier, returns={4: 0.025})) == []
    assert _ideas("dispersion-4h", *ordinary[1:], outlier) == []


def test_pair_uses_equal_plan_weights_and_clock_or_dispersion_trigger():
    scores = (-0.2, -0.02, 0, 0.02, 0.2)
    basket = [_frame(f"MEME_{i}", returns={24: score, 48: score}) for i, score in enumerate(scores)]
    assert _ideas("pair-clock", *basket, hour=121) == []
    pair = _ideas("pair-clock", *basket)[0]
    assert [(order.coin, order.side, order.weight) for order in pair.legs] == [
        ("MEME_4", 1, 1.0),
        ("MEME_0", -1, 1.0),
    ]

    quiet = {frame.coin: replace(frame, returns={48: 0}) for frame in basket}
    wide = {frame.coin: frame for frame in basket}
    frames = {119: quiet, 120: wide, 121: wide}
    assert ideas(frames, MENU["pair-dispersion"]) == {120: [replace(pair, label="pair-dispersion")]}


def test_compression_breakout_retests_bound_and_applies_variant_exit_settings():
    decision = 400
    bars = {hour: _bar(100 if hour % 2 else 101, width=2) for hour in range(100, 375)}
    bars.update({hour: _bar(100, width=0.25) for hour in range(375, 399)})
    bars[399] = Bar(100, 103, 99.9, 102.6, 2500)
    history = MarketHistory("MEME_A", 0, True, None, bars, {})
    frames = build_frames({history.coin: history}, decision, decision + 1)
    variant = MENU["compression-12h"]
    order = ideas(frames, variant)[decision][0].legs[0]
    assert order.limit == 100.25
    assert order.target_atr is None and order.trailing_atr == 2.0 and order.ttl_hours == 2
    taker = ideas(frames, MENU["compression-12h-taker"])[decision][0].legs[0]
    assert taker == replace(order, mode="ioc", limit=102.6 * 1.0015, ttl_hours=0)

    retest = replace(
        variant,
        settings=variant.settings + (("hold_hours", 36), ("trailing_atr", 2.5), ("stop_atr", 1.5)),
    )
    assert ideas(frames, retest)[decision][0].legs[0] == replace(
        order, hold_hours=36, trailing_atr=2.5, stop_atr=1.5
    )
    truncated = replace(history, bars={hour: bar for hour, bar in bars.items() if hour >= 250})
    assert ideas(build_frames({history.coin: truncated}, decision, decision + 1), variant) == {}


def test_regime_breadth_and_volatility_gate_maker_leadership_pullback():
    ordinary = [_frame(f"MEME_{index}") for index in range(4)]
    leader = _frame(returns={24: 0.12, 72: 0.19})
    order = _ideas("regime-24h", *ordinary, leader)[0].legs[0]
    assert order.coin == leader.coin and order.side == 1
    assert order.limit == leader.vwap12 < leader.bar.close
    volatile = [replace(frame, vol_ratio=2.0) for frame in (*ordinary, leader)]
    assert _ideas("regime-24h", *volatile) == []
    mixed = [replace(frame, returns={24: -0.04}) for frame in ordinary[:2]]
    assert _ideas("regime-24h", *mixed, *ordinary[2:], leader) == []
