from dataclasses import replace

import pytest

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


def _trend_history(
    coin: str, first: int, end: int, change: float, *, is_meme: bool = True, listing=None
) -> MarketHistory:
    bars = {hour: _bar(100 * (1 + change) ** (hour - first)) for hour in range(first, end)}
    return MarketHistory(coin, 2, is_meme, listing, bars, {})


def test_basket_compounds_prior_rosters_and_is_warmed_at_the_partition_start():
    decision = 300
    members = [_trend_history(f"MEME_{index}", 0, decision, index / 1000) for index in range(1, 6)]
    outgoing = members[-1]
    members[-1] = replace(
        outgoing,
        bars={**outgoing.bars, decision - 1: replace(outgoing.bars[decision - 1], volume=0)},
    )
    excluded = [
        _trend_history("PERP:BTC", 0, decision, 0.012),
        _trend_history("NON_MEME", 0, decision, 0.012, is_meme=False),
    ]
    newcomer = _trend_history("NEW", decision - 24, decision, 0.02, listing=decision - 24)
    young = _trend_history("YOUNG", decision - 13, decision, 0.02, listing=decision - 13)
    histories = {history.coin: history for history in (*members, *excluded, newcomer, young)}
    frames = build_frames(histories, decision, decision + 1)
    assert set(frames) == {decision}
    assert "NEW" in frames[decision] and outgoing.coin not in frames[decision]
    expected = {window: 1.003**window - 1 for window in (72, 168)}
    for frame in frames[decision].values():
        assert frame.basket_trends == pytest.approx(expected)

    changed = {
        coin: replace(history, bars={**history.bars, decision: _bar(4000)})
        for coin, history in histories.items()
    }
    extended = build_frames(changed, decision - 10, decision + 2)
    assert young.coin in extended[decision - 1]
    assert extended[decision] == frames[decision]

    execution_histories = {members[0].coin: members[0]}
    assert (
        build_frames(execution_histories, decision, decision + 1)[decision][
            members[0].coin
        ].basket_trends
        == {}
    )
    execution_frames = build_frames(
        execution_histories, decision, decision + 1, basket_histories=histories
    )
    assert execution_frames[decision] == {members[0].coin: frames[decision][members[0].coin]}


def test_basket_missing_price_cuts_the_chain_at_its_decision():
    decision = 300
    histories = {
        f"MEME_{index}": _trend_history(f"MEME_{index}", 0, decision, 0.001) for index in range(6)
    }
    history = histories["MEME_5"]
    cut = decision - 100
    histories[history.coin] = replace(
        history, bars={hour: bar for hour, bar in history.bars.items() if hour != cut}
    )
    frames = build_frames(histories, cut, decision + 1)
    coin = "MEME_0"
    assert set(frames[cut][coin].basket_trends) == {72, 168}
    assert frames[cut + 1][coin].basket_trends == {}
    assert frames[decision][coin].basket_trends == pytest.approx({72: 1.001**72 - 1})


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


@pytest.mark.parametrize("family", ("funding-6h", "dispersion-4h"))
@pytest.mark.parametrize("side", (-1, 1))
def test_basket_trend_requires_both_windows_to_align_with_the_trade(family, side):
    ordinary = [_frame(f"MEME_{index}") for index in range(4)]
    if side == 1:
        outlier = _frame(
            bar=Bar(99.8, 102, 98.2, 101.7, 450),
            previous=Bar(101.1, 101.2, 98.0, 100.0, 250),
            vwap12=102.4,
            funding_bps=(-0.65,) * 6,
            returns={4: -0.03},
        )
        if family == "funding-6h":
            outlier = replace(outlier, vwap12=100.5)
            ordinary = []
    else:
        outlier = _frame(returns={4: 0.029})
        if family == "funding-6h":
            ordinary = []

    control = _ideas(family, *ordinary, outlier)[0].legs[0]
    assert control.side == side
    gated = f"{family}-basket-trend"
    aligned = replace(outlier, basket_trends={72: side * 0.05, 168: side * 0.10})
    assert _ideas(gated, *ordinary, aligned)[0].legs[0] == control
    for basket_trends in (
        {72: side * 0.05},
        {72: side * 0.05, 168: -side * 0.10},
        {72: 0, 168: side * 0.10},
    ):
        assert _ideas(gated, *ordinary, replace(outlier, basket_trends=basket_trends)) == []


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


@pytest.mark.parametrize("direction,label", ((1, "momentum"), (-1, "reversion")))
def test_ioc_pairs_use_window_ranks_and_price_capped_equal_weight_legs(direction, label):
    scores = (-0.4, -0.24, 0, 0.24, 0.4)
    basket = [
        _frame(f"MEME_{i}", returns={24: -score, 72: score}) for i, score in enumerate(scores)
    ]
    market = {frame.coin: frame for frame in basket}
    clock = f"pair-clock-ioc-{label}"
    assert _ideas(clock, *basket, hour=121) == []
    clock_plan = _ideas(clock, *basket)[0]
    assert [(leg.coin, leg.side) for leg in clock_plan.legs] == [
        ("MEME_0", direction),
        ("MEME_4", -direction),
    ]

    # The extreme scores alone clear the threshold here. A two-by-two spread does not.
    marginal = {
        frame.coin: replace(frame, returns={72: score})
        for frame, score in zip(basket, (-0.15, 0, 0, 0, 0.15), strict=True)
    }
    frames = {118: marginal, 119: marginal, 120: market, 121: market}
    dispersion = ideas(frames, MENU[f"pair-dispersion-ioc-{label}"])
    assert set(dispersion) == {120}
    assert [(leg.coin, leg.side) for leg in dispersion[120][0].legs] == [
        ("MEME_4", direction),
        ("MEME_3", direction),
        ("MEME_0", -direction),
        ("MEME_1", -direction),
    ]
    for leg in (*clock_plan.legs, *dispersion[120][0].legs):
        assert leg.mode == "ioc" and leg.ttl_hours == 0 and leg.weight == 1
        quote = market[leg.coin].bar.close
        assert leg.limit == pytest.approx(quote * (1 + leg.side * 0.001))


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
    for ratio in (1.5, 2.5):
        fixed = ideas(frames, MENU[f"compression-12h-maker-{ratio}r"])[decision][0].legs[0]
        assert fixed == replace(order, target_atr=order.stop_atr * ratio, trailing_atr=None)

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
    fixed = _ideas("regime-24h-maker-2.5r", *ordinary, leader)[0].legs[0]
    assert fixed == replace(order, target_atr=order.stop_atr * 2.5)
    volatile = [replace(frame, vol_ratio=2.0) for frame in (*ordinary, leader)]
    assert _ideas("regime-24h", *volatile) == []
    mixed = [replace(frame, returns={24: -0.04}) for frame in ordinary[:2]]
    assert _ideas("regime-24h", *mixed, *ordinary[2:], leader) == []
