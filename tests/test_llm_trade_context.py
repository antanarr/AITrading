import json

import pytest

from copybot import llm_trade_context as context


def feed(path, updates):
    source = "https://example.com/releases/ETH-2026-11-16?ignored=name"
    with path.open("w", encoding="utf-8") as stream:
        for available, expires, features in updates:
            stream.write(
                json.dumps(
                    {
                        "available_hour": available,
                        "expires_hour": expires,
                        "features": features,
                        "source": source,
                    }
                )
                + "\n"
            )
    return path


def test_ages_use_eligibility_before_expiry_without_resurrecting_previous_values(tmp_path):
    official = feed(
        tmp_path / "official",
        [
            (1, 50, {"macro_sp500_change_1d": 0.016, "news_fomc_rate_direction": 1}),
            (7, 20, {"macro_sp500_change_1d": -0.027}),
            (25, 30, {"macro_sp500_change_1d": 0.044}),
            (90, 100, {"news_fomc_rate_direction": "ignored since past the query window"}),
        ],
    )
    rows = context.load_trade_context(official, None, 0, 38)
    assert rows[0] == {}
    assert rows[1]["macro"]["broad_equities"]["change_1session_pct"] == 1.5
    assert rows[6]["macro"]["broad_equities"]["age_hours_floor_6h"] == 0
    assert rows[7]["macro"]["broad_equities"]["change_1session_pct"] == -2.5
    assert rows[13]["macro"]["broad_equities"]["age_hours_floor_6h"] == 6
    assert "macro" not in rows[20]
    assert "macro" not in rows[30]
    assert rows[30]["official_news"]["last_policy_decision"]["rate_direction"] == 1


def test_context_is_anonymous_whitelisted_and_uses_correct_units(tmp_path):
    official = feed(
        tmp_path / "official",
        [
            (
                2,
                25,
                {
                    "macro_sp500_level": 7123.564,
                    "macro_vix_level": 30.48,
                    "macro_treasury_10y_change_5d": 0.153,
                    "news_fed_speeches_7d": 3,
                    "news_fed_speech_tone_7d": -2.23,
                    "news_cftc_digital_asset_enforcement_7d": 1,
                    "news_media_memecoin_headlines_24h": 19,
                    "news_internal_source_text": "ETH United States 2026-11-16 instructions",
                },
            )
        ],
    )
    payload = context.load_trade_context(official, None, 2, 3)[2]
    assert payload["macro"]["equity_volatility_regime"]["level_bucket"] == "elevated"
    assert payload["macro"]["long_treasury_yield"]["change_5session_bp"] == 15
    assert payload["official_news"]["counts"]["central_bank_speeches_7d"] == 3
    assert (
        payload["official_news"]["central_bank_speech_tone_7d"][
            "relative_strength_per_1000_words_rounded"
        ]
        == -2
    )
    serialized = json.dumps(payload)
    assert not any(
        value in serialized for value in ("2026-11", "ETH", "source", "7123", "30.48", "media")
    )


def test_optional_media_reports_bucket_coverage_and_retrospective_selection(tmp_path):
    optional = feed(
        tmp_path / "media",
        [
            (1, 2, {"news_media_memecoin_headlines_72h": 0}),
            (
                2,
                3,
                {
                    "news_media_memecoin_headlines_72h": 2,
                    "news_media_memecoin_headline_tone_72h": 0.56,
                },
            ),
        ],
    )
    packets = context.load_trade_context(None, optional, 0, 4)
    assert packets[0] == packets[3] == {}
    assert packets[1]["media"]["headlines_72h"] == 0
    assert packets[2]["media"]["headline_tone_72h"] == 0.6
    assert packets[2]["media"]["retrospective_selection"] is True
    stats = context.context_coverage(packets)
    assert stats["available_hours"] == {"macro": 0, "official_news": 0, "media": 2}
    assert stats["missing_hours"]["media"] == 2
    assert stats["media_tone_hours"] == 1


def test_refuses_out_of_range_window_before_reading_file_and_duplicate_releases(tmp_path):
    absent = tmp_path / "missing"
    with pytest.raises(ValueError, match="Development cutoff"):
        context.load_trade_context(absent, None, context.CUTOFF - 1, context.CUTOFF + 1)
    official = feed(
        tmp_path / "duplicate",
        [
            (2, 10, {"news_fomc_rate_direction": -1}),
            (2, 12, {"news_fomc_rate_direction": 0}),
        ],
    )
    with pytest.raises(ValueError, match="Duplicate publication"):
        context.load_trade_context(official, None, 2, 6)
