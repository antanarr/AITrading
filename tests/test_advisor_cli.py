import asyncio
import json

import httpx
import pytest

from copybot.__main__ import main
from copybot.advisor import Advisor
from copybot.sources import SourceError
from webhook import app

CONTEXT = {"wallet": "w", "recent_loss_count": 0, "closed_trade_count": 0}


@pytest.mark.parametrize(
    "content",
    [
        '{"multiplier": 2, "reason": "increase"}',
        '{"multiplier": 0.5, "reason": ""}',
        '{"multiplier": 0.5, "reason": "ok", "override_limits": true}',
        "invalid-json",
    ],
)
def test_invalid_ai_never_falls_back_to_permission(monkeypatch, content):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("COPYBOT_OPENAI_MODEL", "test-model")

    def handler(req):
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(SourceError, match="invalid assessment"):
            Advisor("openai", 60, c).assess(CONTEXT, 100)


def test_ai_assessment_cache_expires(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("COPYBOT_OPENAI_MODEL", "test-model")
    requests = []

    def handler(req):
        requests.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"multiplier": 0.5, "reason": "Reduce exposure"}'},
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        advisor = Advisor("openai", 60, c)
        assert str(advisor.assess(CONTEXT, 100).multiplier) == "0.5"
        advisor.assess(CONTEXT, 159)
        assert len(requests) == 1
        advisor.assess(CONTEXT, 160)
    assert len(requests) == 2
    assert requests[0]["response_format"]["json_schema"]["strict"] is True


def test_demo_needs_no_credentials_and_keeps_traders_separate(capsys):
    assert main(["demo"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "paper"
    assert len(report["recent_decisions"]) == 4
    assert len(report["positions"]) == 1
    assert report["positions"][0]["wallet"] == "demo-trader-a"


def test_discovery_import_merges_without_enabling_trading(capsys):
    assert main(["discover", "--import", "examples/candidates.json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result) == 2
    assert result[0]["sources"] == ["birdeye", "gmgn"]


def test_live_flag_is_unavailable():
    with pytest.raises(SystemExit) as err:
        main(["--live", "watch"])
    assert err.value.code == 2


def test_legacy_webhook_cannot_trade_even_without_authentication():
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}"}

    async def send(event):
        sent.append(event)

    asyncio.run(app({"type": "http", "path": "/signal"}, receive, send))
    assert sent[0]["status"] == 410
