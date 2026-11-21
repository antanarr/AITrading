from decimal import Decimal

import pytest

from copybot.forward_market import (
    FUNDING_WINDOW_MS,
    TAKER_FEE_RATE,
    BookLevel,
    L2Book,
    PublicMarket,
    size_for_notional,
    sweep_ioc,
)
from copybot.research_models import HOUR_MS

D = Decimal


class Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class Client:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def post(self, url, *, json):
        self.requests.append((url, json))
        return Response(next(self.responses))


def l2_payload():
    return {
        "coin": "ETH",
        "time": 1_000,
        "levels": [
            [{"px": "100.00", "sz": "1.5"}, {"px": "99.90", "sz": "1.0"}],
            [
                {"px": "100.10", "sz": "1.005"},
                {"px": "100.20", "sz": "0.75"},
                {"px": "100.30", "sz": "0.5"},
            ],
        ],
    }


def test_entry_rounds_cap_and_lots_and_charges_every_partial_fill():
    client = Client(l2_payload())
    book = PublicMarket(client).book("ETH", now_ms=1_201)
    fill = sweep_ioc(book, buy=True, quantity=D("2.348"), size_decimals=2, limit_price=D("100.205"))
    assert (book.exchange_ms, book.received_ms) == (1_000, 1_201)
    assert fill.limit_price == D("100.20")
    assert fill.requested_quantity == D("2.34")
    assert fill.quantity == D("1.75")
    assert fill.remaining == D("0.59")
    assert [part.notional for part in fill.fills] == [D("100.10"), D("75.150")]
    assert fill.notional == D("175.250")
    assert fill.average_price == D("175.250") / D("1.75")
    assert fill.fee == D("175.250") * TAKER_FEE_RATE
    assert client.requests == [
        ("https://api.hyperliquid.xyz/info", {"type": "l2Book", "coin": "ETH"})
    ]


def test_sell_floor_rounds_up_and_uncapped_exit_only_consumes_visible_depth():
    book = PublicMarket(Client(l2_payload())).book("ETH", now_ms=1_201)
    order = sweep_ioc(book, buy=False, quantity=D("4"), size_decimals=2, limit_price=D("99.9001"))
    assert order.limit_price == D("99.901")
    assert order.quantity == D("1.50")
    assert order.remaining == D("2.50")
    exit_fill = sweep_ioc(book, buy=False, quantity=D("4"), size_decimals=2)
    assert exit_fill.limit_price is None
    assert exit_fill.quantity == D("2.50")
    assert exit_fill.notional == D("249.90")
    assert exit_fill.remaining == D("1.50")


def test_uncapped_exit_consumes_at_most_twenty_levels_and_no_fee_when_unfilled():
    bids = tuple(BookLevel(D(100 - i), D("1")) for i in range(20))
    asks = (BookLevel(D("101"), D("1")),)
    book = L2Book("ETH", 100, 120, bids, asks)
    fill = sweep_ioc(book, buy=False, quantity=D("25"), size_decimals=0)
    assert len(fill.fills) == 20
    assert fill.quantity == D("20")
    assert fill.remaining == D("5")
    blocked = sweep_ioc(book, buy=True, quantity=D("2"), size_decimals=0, limit_price=D("100"))
    assert blocked.average_price is None
    assert blocked.quantity == blocked.notional == blocked.fee == D("0")
    assert blocked.remaining == D("2")
    with pytest.raises(ValueError, match="up to 20"):
        L2Book("ETH", 100, 120, (*bids, BookLevel(D(80), D(1))), asks)


@pytest.mark.parametrize(("stamp", "expected"), [(11_001, "Stale"), (999, "timestamp")])
def test_book_rejects_stale_and_future_timestamps(stamp, expected):
    with pytest.raises(ValueError, match=expected):
        PublicMarket(Client(l2_payload())).book("ETH", now_ms=stamp)


def test_book_rejects_crossed_levels_and_market_identity_mismatch():
    malformed = l2_payload()
    malformed["levels"][0][1]["px"] = "100.20"
    with pytest.raises(ValueError, match="Crossed, locked or unsorted"):
        PublicMarket(Client(malformed)).book("ETH", now_ms=1_001)
    with pytest.raises(ValueError, match="wrong market"):
        PublicMarket(Client(l2_payload())).book("SOL", now_ms=1_001)


def test_market_context_records_request_receipt_and_omits_invalid_native_rows():
    data = [
        {
            "universe": [
                {"name": "ETH", "szDecimals": 2, "maxLeverage": 20},
                {"name": "BTC", "szDecimals": 3},
                {"name": "SOL", "szDecimals": 2, "isDelisted": True},
                {"name": "ADA", "szDecimals": 0},
                {"name": "xyz:X", "szDecimals": 1},
            ]
        },
        [
            {"markPx": "100.001", "oraclePx": "99.999"},
            {"markPx": "12000", "oraclePx": "12000"},
            None,
            {"markPx": "NaN", "oraclePx": "1"},
            {"markPx": "10", "oraclePx": "10"},
        ],
    ]
    clock = iter((1_000, 1_015))
    client = Client(data)
    contexts = PublicMarket(client, clock=clock.__next__).contexts()
    assert tuple(contexts) == ("ETH",)
    assert contexts["ETH"].mark == D("100.001")
    assert contexts["ETH"].oracle == D("99.999")
    assert contexts["ETH"].max_leverage == 20
    assert (contexts["ETH"].requested_ms, contexts["ETH"].received_ms) == (1_000, 1_015)
    assert client.requests[0][1] == {"type": "metaAndAssetCtxs"}


def test_funding_pages_exact_bounds_without_synthesizing_missing_events():
    first = {"coin": "ETH", "time": HOUR_MS, "fundingRate": "-0.0001"}
    next_page = {
        "coin": "ETH",
        "time": FUNDING_WINDOW_MS,
        "fundingRate": "0.00002",
        "premium": "0.00001",
    }
    client = Client([first, first], [next_page])
    rows = PublicMarket(client).funding("ETH", 0, FUNDING_WINDOW_MS)
    assert [(event.timestamp_ms, event.rate, event.premium) for event in rows] == [
        (HOUR_MS, D("-0.0001"), None),
        (FUNDING_WINDOW_MS, D("0.00002"), D("0.00001")),
    ]
    assert [(req["startTime"], req["endTime"]) for _, req in client.requests] == [
        (0, FUNDING_WINDOW_MS - 1),
        (FUNDING_WINDOW_MS, FUNDING_WINDOW_MS),
    ]


def test_private_and_non_native_requests_fail_before_http():
    client = Client()
    feed = PublicMarket(client)
    with pytest.raises(ValueError, match="public market endpoints"):
        feed.info("userState")
    with pytest.raises(ValueError, match="public market endpoints"):
        feed.info("l2Book", coin="ETH", type="userState")
    for symbol in ("BTC", "xbt", "spot/USDC", "@2", "xyz:ABC"):
        with pytest.raises(ValueError, match="native perpetual"):
            feed.book(symbol)
    assert not client.requests


def test_forward_candle_endpoint_accepts_only_explicit_native_hourly_and_daily_bounds():
    client = Client([], [])
    feed = PublicMarket(client)
    request = {"coin": "ETH", "interval": "1h", "startTime": 10, "endTime": 100}
    assert feed.info("candleSnapshot", req=request) == []
    assert feed.info("candleSnapshot", req={**request, "interval": "1d"}) == []
    for overrides in (
        {"coin": "BTC"},
        {"interval": "15m"},
        {"startTime": 0},
        {"startTime": True},
        {"endTime": 1},
        {"endTime": "100"},
        {"user": "wallet-address"},
    ):
        with pytest.raises(ValueError):
            feed.info("candleSnapshot", req={**request, **overrides})
    assert len(client.requests) == 2


def test_size_for_notional_has_no_one_lot_overrun_at_the_division_boundary():
    assert size_for_notional(D("10"), D("3.3333333333333333333333333334"), 0) == D(2)
    assert size_for_notional(D("99.999"), D("33.33"), 2) == D("3.00")
