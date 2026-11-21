from datetime import date
from math import exp

import pytest

from copybot import residual_reversal_study as study


def test_factor_removes_the_candidate_and_uses_prior_turnover():
    panels = {
        **{f"PEER{i}": {5: study.Daily(1, 1, 1, 2_000_000, 24)} for i in range(8)},
        "TARGET": {5: study.Daily(1, 1, 1, 2_000_000, 24)},
        "ILLIQUID": {5: study.Daily(1, 1, 1, 200_000, 24)},
    }
    returns = {coin: {6: 0.01} for coin in panels}
    returns["TARGET"][6] = 0.3
    returns["ILLIQUID"][6] = 0.8
    factor = study.factor_returns(panels, returns)
    assert "ILLIQUID" not in factor[6][1]
    assert study.factor_for("TARGET", *factor[6]) == pytest.approx(0.01)


def test_fitted_beta_excludes_shock_returns_and_future():
    enter = 100
    candle = 10.0
    reg, factor, days = {}, {}, {}
    for day in range(enter - 33, enter):
        broad = 0.025 if day % 2 else -0.025
        small_noise = 0.002 if day % 3 else -0.002
        asset_ret = 0.001 + 1.2 * broad + small_noise
        if day >= enter - 3:
            asset_ret -= 0.05
        candle *= exp(asset_ret)
        reg[day] = asset_ret
        factor[day] = (
            8 * broad + asset_ret,
            {
                "ALT": asset_ret,
                **{f"PEER{i}": broad for i in range(8)},
            },
        )
        days[day] = study.Daily(candle, 1.01 * candle, 0.99 * candle, 2_000_000, 24)

    original = study.signal("ALT", enter, days, reg, factor)
    assert original.z_score < -10
    assert original.beta == pytest.approx(1.2, abs=0.05)
    assert original.turnover_ratio == 1.0
    days[enter] = study.Daily(5000, 5000, 5000, 100_000_000, 24)
    reg[enter] = 10
    factor[enter] = 10, {}
    assert study.signal("ALT", enter, days, reg, factor) == original


def test_volume_gate_and_pair_weights_follow_known_betas():
    low = study.Candidate("ONE", -2.5, 0.7, 1.0, 10, 1)
    high = study.Candidate("TWO", 2.5, 1.4, 2.0, 20, 1)
    alternative = study.Candidate("THREE", -1.8, 1.1, 2.0, 10, 1)
    assert study.choose([low, high, alternative], False) == (low, high)
    assert study.choose([low, high, alternative], True) == (alternative, high)
    one, two = study.order_pair((low, high), "residual").legs
    assert (one.side, two.side) == (1, -1)
    assert one.weight * low.beta == pytest.approx(two.weight * high.beta)
    assert one.weight + two.weight == pytest.approx(1.0)
    assert one.hold_hours == 72


def test_refuses_reserve_before_reading_any_archive(tmp_path):
    with pytest.raises(ValueError, match="exclusive of Reserve"):
        study.run(tmp_path / "unavailable", study.START, study.VALIDATION, date(2026, 11, 22))
