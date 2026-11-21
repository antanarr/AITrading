from datetime import date

import pytest

from copybot.binance_regime_archive import (
    END,
    START,
    Settlement,
    _funding_url,
    epoch_hour,
    load_binance_regime,
    monthly_funding_start,
    verify_settlements,
)
from copybot.research_models import HOUR_MS


def event(hour, cadence, delay=0):
    return Settlement(hour * HOUR_MS + delay, 0.0001, cadence)


def test_sparse_funding_passes_only_on_observed_schedule_and_preserves_time():
    result = verify_settlements(
        [event(100, 8), event(108, 8), event(116, None, delay=2)],
        100,
        124,
    )
    assert set(result) == {100, 108, 116}
    assert result[116].time_ms == 116 * HOUR_MS + 2
    assert result[116].rate == 0.0001


def test_declared_cadence_change_can_join_the_venue_schedule():
    assert set(
        verify_settlements(
            [event(100, 4), event(104, 4), event(112, 8), event(120, None)], 100, 128
        )
    ) == {100, 104, 112, 120}


@pytest.mark.parametrize(
    ("sequence", "reason"),
    [
        ([event(108, 8), event(116, 8)], "starts too late"),
        ([event(100, 8), event(116, 8)], "Unexplained funding cadence"),
        ([event(100, 8), event(108, 8)], "stops before"),
        ([event(100, 8), event(108, 8, delay=8_000), event(116, 8)], "Invalid funding observation"),
    ],
)
def test_missing_or_misaligned_events_fail_closed(sequence, reason):
    with pytest.raises(ValueError, match=reason):
        verify_settlements(sequence, 100, 124)


def test_provider_shards_and_rest_window_do_not_cross_cutoff(tmp_path):
    shards = list(monthly_funding_start("ETHUSDT", START, END))
    assert shards[-1].start == date(2026, 4, 1)
    assert shards[-1].end == date(2026, 5, 1)
    assert len(shards) == 28
    stop = epoch_hour(END) * HOUR_MS
    assert f"endTime={stop - 1}" in _funding_url("ETHUSDT", shards[-1].end, END)
    with pytest.raises(ValueError, match="independent Binance window"):
        load_binance_regime(tmp_path, epoch_hour(START), epoch_hour(END) + 1)
