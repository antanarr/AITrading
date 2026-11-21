from dataclasses import replace

from copybot.liquid_trend_leverage import policy_leverage
from copybot.research_models import OrderPlan


def test_mixed_leverage_uses_strength_and_reduces_crowded_exposure():
    caps = {"SOL": 20, "DOGE": 3}
    strong = OrderPlan("SOL", 1, 100, 2, 2, None, 240, 0, "ioc", signal_strength=0.5)
    weak = replace(strong, signal_strength=0.49)
    limited = replace(strong, coin="DOGE")
    assert policy_leverage("mixed", caps, strong, 2, 0) == 5
    assert policy_leverage("mixed", caps, weak, 2, 0) == 3
    assert policy_leverage("mixed", caps, limited, 2, 0) == 3
    assert policy_leverage("mixed", caps, strong, 3, 0) == 1
    assert policy_leverage("mixed", caps, strong, 2, 1) == 1
