import json
from dataclasses import asdict
from datetime import date

import pytest

from copybot import directional_context as context


def write_updates(path, updates):
    path.write_text("\n".join(json.dumps(asdict(row)) for row in updates))


def test_join_applies_only_available_updates_and_never_resurrects_stale_values(tmp_path):
    feed = tmp_path / "context.jsonl"
    source = "public feed"
    write_updates(
        feed,
        (
            context.Update(14, 20, source, {"macro_rates": 25}),
            context.Update(9, 25, source, {"news_tone": -4}),
            context.Update(11, 13, source, {"news_tone": 5}),
            context.Update(17, 25, source, {"macro_rates": 50}),
        ),
    )
    result = context.load_context(feed, 10, 17)
    assert result[10] == {"news_tone": -4}
    assert result[11] == result[12] == {"news_tone": 5}
    assert result[13] == {}
    assert result[14] == result[16] == {"macro_rates": 25}


def test_reserve_refused_before_file_read(tmp_path):
    cutoff = context.eastern_hour(context.DEVELOPMENT_END)
    with pytest.raises(ValueError, match="cutoff"):
        context.load_context(tmp_path / "unavailable.jsonl", cutoff - 1, cutoff)


def test_join_rejects_duplicated_conflicting_update_and_btc_features(tmp_path):
    feed = tmp_path / "context.jsonl"
    unsigned = context.Update(10, 11, "first", {"macro_risk": 4})
    write_updates(feed, [unsigned, context.Update(10, 12, "second", {"macro_risk": 5})])
    with pytest.raises(ValueError, match="Duplicate context update"):
        context.load_context(feed, 10, 11)
    write_updates(feed, [context.Update(10, 11, "first", {"news_btc_fear": 2})])
    with pytest.raises(ValueError, match="Invalid context feature"):
        context.load_context(feed, 10, 11)


def test_market_lags_and_previous_session_change_survive_calendar_gaps():
    friday, monday = date(2026, 7, 10), date(2026, 7, 13)
    rates = context.market_updates("source", [(friday, 4.25), (monday, 4.15)], "rates", 2, True)
    assert rates[0].available_hour == context.eastern_hour(date(2026, 7, 12))
    assert rates[1].available_hour == context.eastern_hour(date(2026, 7, 15))
    assert rates[1].features["macro_rates_change_1d"] == pytest.approx(-0.10)
    assert "macro_rates_change_5d" not in rates[1].features


def test_news_day_bucket_disallows_same_day_and_uses_dst_offsets():
    day = date(2026, 11, 1)
    update = next(context.speech_updates([(day, "source", 0.5, True)], day, date(2026, 11, 2)))
    assert update.available_hour - context.eastern_hour(day) == 25
    assert update.features == {
        "news_fed_speeches_1d": 1,
        "news_fed_speeches_7d": 1,
        "news_fed_digital_asset_speeches_7d": 1,
        "news_fed_speech_tone_7d": 0.5,
    }


def test_official_article_body_excludes_bitcoin_passages():
    html = """
    <p>A separate Bitcoin paragraph shows that we do not use material outside the selected article.</p>
    <p class="article__time">September 16, 2026</p>
    <p>The Fed published its official statement on American economic conditions and its outlook for inflation.</p>
    <p>Digital fintech Bitcoin demand is reportedly strong but that text will be excluded from these features.</p>
    <p>Another passage examines the broader market response and conditions in the United States today.</p>
    """
    body = context.article_body(html)
    assert "inflation" in body
    assert "conditions in the United States" in body
    assert not context.BITCOIN.search(body)


def test_cftc_archive_excludes_btc_titles_and_reads_older_pages(monkeypatch):
    def headline(stamp, release, title):
        return f'<time datetime="{stamp}">date</time><a href="/PressRoom/PressReleases/{release}-26">{title}</a>'

    page_one = headline(
        "2026-05-30T18:00:00Z", 200, "CFTC charges executives with crypto asset fraud"
    )
    page_two = headline("2026-05-29T18:00:00Z", 199, "CFTC press on Bitcoin digital assets")
    page_two += headline("2026-05-28T18:00:00Z", 198, "CFTC rulemaking on stablecoins")
    page_two += headline("2026-05-26T18:00:00Z", 197, "CFTC rulemaking on digital assets")
    monkeypatch.setattr(
        context, "fetch_text", lambda url: page_one if url.endswith("=0") else page_two
    )
    assert context.cftc_records(date(2026, 5, 27), date(2026, 6, 1)) == [
        (date(2026, 5, 28), False),
        (date(2026, 5, 30), True),
    ]
