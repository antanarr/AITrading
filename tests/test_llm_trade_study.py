import pytest

from copybot.llm_trade_study import (
    matched_random_plans,
    policy_plans,
    score_one,
    side_matched_random_plans,
    validated,
)
from copybot.research_models import OrderPlan, TradePlan


def plan(coin, side):
    return TradePlan((OrderPlan(coin, side, 100, 2, 2, None, 240, 0, "ioc"),), "strategy")


def rating(key, score, veto=False):
    return {"id": key, "score": score, "veto": veto, "reason": "mixed"}


def test_veto_and_rank_only_change_the_allowed_parts_of_baseline():
    first, second, third = plan("SOL", 1), plan("ETH", -1), plan("HYPE", 1)
    baseline = {1: [first, second, third]}
    ids = {1: {"C2": first, "C1": second, "C3": third}}
    ratings = {
        1: {
            "C1": rating("C1", 90, veto=True),
            "C2": rating("C2", 50),
            "C3": rating("C3", 80),
        },
    }
    result = policy_plans(baseline, ids, ratings)
    assert result["veto"][1] == [first, third]
    assert result["rank"][1] == [second, third, first]
    assert result["veto_and_rank"][1] == [third, first]
    assert result["narrow_veto"][1] == baseline[1]
    ratings[1]["C1"] = rating("C1", 35, veto=True)
    assert policy_plans(baseline, ids, ratings)["narrow_veto"][1] == [first, third]
    assert baseline[1] == [first, second, third]


def test_model_response_rejects_missing_unknown_and_duplicate_candidates():
    keys = ["a", "b"]
    for rows in (
        [rating("a", 50)],
        [rating("a", 50), rating("c", 50)],
        [rating("a", 50), rating("a", 60)],
        [rating("a", 101), rating("b", 50)],
    ):
        with pytest.raises(ValueError):
            validated({"assessments": rows}, keys)


def test_cached_responses_are_bound_to_the_prompt_inputs_and_model(tmp_path):
    invocations = []

    def scorer(observations):
        invocations.append(observations)
        return {"assessments": [rating("a", 50)]}, {"input_tokens": 100}

    observations = {"candidates": [{"id": "a"}]}
    for model, expected_cached in (("model-a", False), ("model-a", True), ("model-b", False)):
        rows, usage, cached = score_one("test", model, scorer, tmp_path, observations, ["a"])
        assert rows["a"]["score"] == 50
        assert usage["input_tokens"] == 100
        assert cached is expected_cached
    assert len(invocations) == 2


def test_random_controls_match_activity_and_keep_baseline_priority():
    a, b, c = plan("SOL", 1), plan("ETH", -1), plan("HYPE", 1)
    plans = {1: [a, b, c], 2: [b, c], 3: [a, c]}
    accepted = {1: [a, b], 2: [], 3: [c]}
    same_day = matched_random_plans(plans, accepted, 1)
    same_side = side_matched_random_plans(plans, accepted, 1)
    assert [len(rows) for rows in same_day.values()] == [2, 0, 1]
    for result in (same_day, same_side):
        for hour, rows in result.items():
            assert rows == [plan for plan in plans[hour] if plan in rows]
    assert sorted(plan.legs[0].side for rows in same_side.values() for plan in rows) == [-1, 1, 1]
