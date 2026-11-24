from collections import Counter
from dataclasses import replace

import pytest

from copybot.funding_pair_study import (
    Candidate,
    Market,
    build_plans,
    funding_forecast,
    matched_pairs,
    pair_plan,
)
from copybot.research_data import ARCHIVE_END_HOUR
from copybot.research_models import HOUR_MS, Funding, MarketHistory


def test_funding_forecast_requires_two_persistent_closed_windows():
    hour = 1_000
    settlements = {
        stamp: Funding(stamp * HOUR_MS + 10, -0.0003 if stamp >= hour - 24 else -0.0002)
        for stamp in range(hour - 72, hour)
    }
    history = MarketHistory("SOL", 2, False, None, {}, settlements)
    assert funding_forecast(history, hour) == pytest.approx(-0.0002)

    in_progress = replace(history, funding={**settlements, hour: Funding(hour * HOUR_MS, 1.0)})
    assert funding_forecast(in_progress, hour) == pytest.approx(-0.0002)

    not_published = replace(
        history, funding={**settlements, hour - 1: Funding(hour * HOUR_MS + 1, -0.003)}
    )
    assert funding_forecast(not_published, hour) is None

    changed_direction = replace(
        history,
        funding={
            stamp: replace(row, rate=-row.rate) if stamp >= hour - 24 else row
            for stamp, row in settlements.items()
        },
    )
    assert funding_forecast(changed_direction, hour) is None


def test_pair_hedges_beta_and_requires_carry_to_cover_stressed_costs():
    returns = tuple(((index % 5) - 2) * 0.01 for index in range(30))
    long = Candidate(Market("SOL", 100, returns, 0.04, -0.0003), 1.2)
    short = Candidate(Market("ETH", 200, returns, 0.04, 0.0002), 0.8)

    counts = Counter()
    pairs = matched_pairs([long, short], 3, counts)
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.correlation == pytest.approx(1)
    assert pair.net_carry_bps > 10
    assert pair.long_weight * long.beta == pytest.approx((1 - pair.long_weight) * short.beta)
    legs = pair_plan(pair, 3).legs
    assert [leg.side for leg in legs] == [1, -1]
    assert [leg.hold_hours for leg in legs] == [72, 72]
    assert sum(leg.weight for leg in legs) == pytest.approx(1)

    weak = [
        replace(long, market=replace(long.market, funding=-0.00001)),
        replace(short, market=replace(short.market, funding=0.00001)),
    ]
    assert matched_pairs(weak, 7, Counter()) == []

    uncorrelated = replace(short, market=replace(short.market, returns=returns[::-1]))
    assert matched_pairs([long, uncorrelated], 7, Counter()) == []


def test_development_cutoff_stays_exclusive():
    with pytest.raises(ValueError, match="Development"):
        build_plans({}, ARCHIVE_END_HOUR - 24, ARCHIVE_END_HOUR + 1, 0)


def test_allowing_one_payer_still_enforces_the_net_carry_hurdle():
    returns = tuple(((index % 5) - 2) * 0.01 for index in range(30))
    long = Candidate(Market("SOL", 100, returns, 0.04, 0.00002), 1.0)
    short = Candidate(Market("ETH", 200, returns, 0.04, 0.00020), 1.0)
    assert matched_pairs([long, short], 7, Counter()) == []
    pair = matched_pairs([long, short], 7, Counter(), allow_paying_leg=True)[0]
    assert pair.long == long and pair.short == short
    assert pair.net_carry_bps == pytest.approx(106.2)
