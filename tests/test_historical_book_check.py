from decimal import Decimal

import pytest

from copybot.forward_market import BookLevel, L2Book
from copybot.historical_book_check import entry_probe, first_book, read_capture

D = Decimal


def book(exchange, received, price="99.60", depth="0.1"):
    return L2Book(
        "WLD",
        exchange,
        received,
        (BookLevel(D(price), D(depth)),),
        (BookLevel(D("100.50"), D("1")),),
    )


def test_quote_selection_respects_arrival_wait_and_source_age():
    previous = book(39_000, 39_999)
    stale = book(29_000, 40_000)
    current = book(40_500, 41_000)
    later = book(50_000, 51_000)
    assert first_book([previous, stale, current, later], 40_000) == current
    assert first_book([previous, stale, later], 40_000) is None


def test_short_case_uses_40_bps_floor_and_partial_depth_for_each_leverage():
    blocked = entry_probe(book(800, 1_000, "99.59"), -1, D(100), D(1), 2, 1)
    assert blocked["status"] == "unfilled"
    assert blocked["limit"] == D("99.60")
    assert blocked["fee"] == 0
    shallow = book(800, 1_000, "99.60")
    one = entry_probe(shallow, -1, D(100), D(1), 2, 1)
    three = entry_probe(shallow, -1, D(100), D(1), 2, 3)
    assert one["status"] == three["status"] == "partial"
    assert one["filled"] == three["filled"] == D("0.10")
    assert three["requested"] > one["requested"]
    assert one["fee"] == three["fee"] == D("99.60") * D("0.1") * D("0.00045")


def test_archive_cutoff_is_enforced_before_loading_the_payload(tmp_path):
    with pytest.raises(ValueError, match="Development archive cutoff"):
        read_capture(
            tmp_path,
            {
                "file": "unused.log.gz",
                "window_start": "2026-11-21T00:00:00+00:00",
                "window_end_exclusive": "2026-11-21T00:08:00+00:00",
            },
            "WLD",
        )
