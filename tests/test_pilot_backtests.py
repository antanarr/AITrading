from datetime import date

from copybot.meme_study import day_hour
from copybot.pilot_backtests import calendar_windows, delay_execution
from copybot.research_models import OrderPlan, TradePlan


def test_execution_delay_preserves_the_signal_and_shifts_channel_exits():
    order = OrderPlan("SOL", 1, 100, 2, 2, None, 240, 0, "ioc")
    plan = TradePlan((order,), "daily")
    exits = []

    def exit_signal(bet, submitted, current):
        exits.append((bet, submitted, current))
        return "channel" if current == 124 else None

    delayed, callback = delay_execution({100: [plan], 199: [plan]}, exit_signal, 200, 1)

    assert delayed == {101: [plan]}
    assert callback(plan, 101, 124) is None
    assert callback(plan, 101, 125) == "channel"
    assert exits == [(plan, 100, 123), (plan, 100, 124)]


def test_quarterly_resets_are_contiguous_and_end_at_the_exclusive_cutoff():
    d = date.fromisoformat
    start, end = d("2024-10-01"), d("2025-01-23")
    assert calendar_windows(start, end, 3) == {
        "reset_2024-10": (day_hour(start), day_hour(d("2025-01-01"))),
        "reset_2025-01": (day_hour(d("2025-01-01")), day_hour(end)),
    }
