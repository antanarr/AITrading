from datetime import UTC, date, datetime, timedelta
from xml.sax.saxutils import escape

import pytest

from copybot import directional_media as media


def rss(rows):
    items = []
    for title, publisher, published in rows:
        items.append(
            f"<item><title>{escape(title)} - {escape(publisher)}</title>"
            f"<source>{escape(publisher)}</source><pubDate>{published}</pubDate></item>"
        )
    return f"<rss><channel>{''.join(items)}</channel></rss>"


def test_rss_keeps_bounded_publisher_headlines_and_excludes_btc():
    document = rss(
        [
            ("Meme coin rallies", "Publisher", "Mon, 01 Jun 2026 10:45:00 GMT"),
            ("Memecoin and WBTC surge", "Publisher", "Mon, 01 Jun 2026 14:45:00 GMT"),
            ("Fear & Greed around meme coins", "Publisher", "Mon, 01 Jun 2026 14:50:00 GMT"),
            ("Memecoins fall", "Publisher", "Tue, 02 Jun 2026 00:00:00 GMT"),
        ]
    )
    expected = media.Headline(
        datetime(2026, 6, 1, 10, 45, tzinfo=UTC), "Publisher", "Meme coin rallies"
    )
    rows, total = media.rss_headlines(document, date(2026, 6, 1), date(2026, 6, 2))
    assert total == 4
    assert rows == [expected]
    assert expected.tone == 1


def test_article_is_unavailable_until_24_hour_delay_and_expires_from_windows():
    row = media.Headline(datetime(2026, 6, 1, 10, 45, tzinfo=UTC), "Publisher", "Memecoin fraud")
    updates = {
        update.available_hour: update.features
        for update in media.media_updates([row], date(2026, 6, 1), date(2026, 6, 6))
    }
    hour = media.datetime_hour(datetime(2026, 6, 2, 11, tzinfo=UTC))
    assert updates[hour - 1]["news_media_memecoin_headlines_24h"] == 0
    assert "news_media_memecoin_headline_tone_72h" not in updates[hour - 1]
    assert updates[hour]["news_media_memecoin_headlines_24h"] == 1
    assert updates[hour]["news_media_memecoin_headline_tone_72h"] == -1
    assert updates[hour + 24]["news_media_memecoin_headlines_24h"] == 0
    assert updates[hour + 24]["news_media_memecoin_headlines_72h"] == 1
    assert updates[hour + 72]["news_media_memecoin_headlines_72h"] == 0


def test_syndicated_titles_dedup_and_collection_limits(monkeypatch):
    start = date(2026, 6, 1)
    document = rss(
        [
            ("Meme coins rally!", "Later Publisher", "Mon, 01 Jun 2026 11:00:00 GMT"),
            ("Meme coins rally", "First Publisher", "Mon, 01 Jun 2026 10:00:00 GMT"),
        ]
    )
    calls = []

    def fetch(url):
        calls.append(url)
        return document

    monkeypatch.setattr(media, "fetch_text", fetch)
    rows, queries, empty, saturated = media.collect(start, start + timedelta(days=8))
    assert [row.publisher for row in rows] == ["First Publisher"]
    assert (queries, empty, saturated) == (2, 1, 0)
    assert len(calls) == 2
    with pytest.raises(ValueError, match="weekly queries"):
        media.collect(start, start + timedelta(weeks=media.MAX_QUERIES + 1))
    assert len(calls) == 2
