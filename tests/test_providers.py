"""The Brazilian sources: B3's own files and the central bank's series.

Packaged copy of the suite: same tests, importing from this distribution
rather than from a provider vendored into the terminal itself.

Every test here runs offline. Each provider takes its HTTP getter as a
constructor argument for exactly this reason, so the suite exercises the
parsing and the request planning -- which is where the bugs live -- without
depending on B3 or the Banco Central being up.

The COTAHIST fixture is a dozen real records from a real session (a share,
a unit, an ETF, a FII, a Fiagro, a BDR, an odd lot and two options), kept
whole so the fixed-width offsets are tested against the layout B3 actually
publishes rather than one hand-typed to match the parser.
"""

from __future__ import annotations

import io
import json
import tempfile
import zipfile
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from lse_terminal_brazil.b3 import (B3Provider, _epoch_seconds, front_month,
                                       parse_cotahist)
from lse_terminal_brazil.bcb import BcbProvider, _to_candles as bcb_candles
from lse_terminal.contracts import CANDLE_COLUMNS
from lse_terminal.testing import check_provider

FIXTURE = Path(__file__).parent / "data" / "cotahist_sample.txt"


def cotahist_zip() -> bytes:
    """The fixture wrapped exactly as B3 serves it: one TXT inside a ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("COTAHIST_D25082026.TXT", FIXTURE.read_bytes())
    return buf.getvalue()


class StubFetch:
    """A fetch that answers from a canned map and records what was asked for."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float = 0) -> bytes:
        self.calls.append(url)
        for fragment, body in self.answers.items():
            if fragment in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise FileNotFoundError(url)


# ── COTAHIST parsing ────────────────────────────────────────────────────

def test_epoch_seconds_matches_calendar():
    import datetime as dt
    for yyyymmdd in (19860102, 19991231, 20000229, 20260825):
        want = int(dt.datetime.strptime(str(yyyymmdd), "%Y%m%d")
                   .replace(tzinfo=dt.timezone.utc).timestamp())
        assert int(_epoch_seconds(np.array([yyyymmdd]))[0]) == want


def test_parse_cotahist_reads_the_published_layout():
    frame = parse_cotahist(FIXTURE.read_bytes())
    # Header and trailer records are not instruments.
    assert set(frame["symbol"]) == {
        "PETR4", "VALE3", "BOVA11", "HGLG11", "ROXO34", "KLBN11", "LAFI11",
        "GGBR4F", "ALOSI334", "ASAIV770"}
    petr = frame[frame["symbol"] == "PETR4"].iloc[0]
    # The published 2026-08-25 session for PETR4, to the cent.
    assert (petr["open"], petr["high"], petr["low"], petr["close"]) == (
        41.18, 41.91, 41.01, 41.35)
    assert petr["volume"] == 85_920_800          # shares, not reais
    assert petr["ts"] == 1_787_616_000           # 2026-08-25T00:00:00Z
    assert petr["isin"] == "BRPETRACNPR6"
    assert petr["name"] == "PETROBRAS"
    assert petr["codbdi"] == "02"


def test_parse_cotahist_rejects_a_payload_that_is_not_fixed_width():
    # Better to say the file is not what we think it is than to reshape
    # arbitrary bytes into a grid and hand back nonsense prices.
    from lse_terminal_brazil.b3 import B3Error
    with pytest.raises(B3Error):
        parse_cotahist(b"")
    with pytest.raises(B3Error):
        parse_cotahist(b"not a cotahist file\n")


def test_parse_cotahist_keeps_only_priced_records():
    # A record that never printed carries a zero close; left in, it drags a
    # chart's low to the axis.
    lines = FIXTURE.read_bytes().split(b"\r\n")
    zeroed = lines[1][:56] + b"0" * 65 + lines[1][121:]
    frame = parse_cotahist(b"\r\n".join([lines[0], zeroed, lines[2]]) + b"\r\n")
    assert list(frame["symbol"]) == ["VALE3"]


def test_classification_covers_every_board_in_the_fixture():
    fetch = StubFetch({"COTAHIST_D": cotahist_zip()})
    p = B3Provider(cache_dir=Path(tempfile.mkdtemp()), fetch=fetch)
    by_symbol = {i.symbol: i.category for i in p.search("", limit=500)}
    assert by_symbol["PETR4"] == "B3 Ações"
    assert by_symbol["KLBN11"] == "B3 Ações"       # a unit trades on the share board
    assert by_symbol["BOVA11"] == "B3 ETFs"
    assert by_symbol["HGLG11"] == "B3 Fundos Imobiliários"
    assert by_symbol["LAFI11"] == "B3 Fiagro"
    assert by_symbol["ROXO34"] == "B3 BDRs"
    # Odd lots are the same instrument on a second board, so they are not a
    # second catalog row; options exist but are kept out of the default list.
    assert "GGBR4F" not in by_symbol
    assert "ALOSI334" not in by_symbol
    calls = [i.symbol for i in p.search("ALOSI", limit=10)]
    assert calls == ["ALOSI334"]
    assert p.search("ALOSI", limit=10)[0].category == "B3 Opções de compra"
    assert p.search("ASAIV770")[0].category == "B3 Opções de venda"


def test_catalog_is_grouped_contiguously_in_sidebar_order():
    # The Provider contract says the UI renders this order verbatim, one
    # folder per category, so a category may not appear twice.
    fetch = StubFetch({"COTAHIST_D": cotahist_zip()})
    p = B3Provider(cache_dir=Path(tempfile.mkdtemp()), fetch=fetch)
    seen, order = set(), []
    for row in p.search("", limit=500):
        if not order or order[-1] != row.category:
            assert row.category not in seen, f"{row.category} appears twice"
            order.append(row.category)
            seen.add(row.category)
    assert order[0] == "B3 Ações"
    assert order[-1] == "B3 Futuros e Índices"


def test_display_name_keeps_the_share_class_and_drops_the_bookkeeping():
    fetch = StubFetch({"COTAHIST_D": cotahist_zip()})
    p = B3Provider(cache_dir=Path(tempfile.mkdtemp()), fetch=fetch)
    names = {i.symbol: i.name for i in p.search("", limit=500)}
    # ESPECI is "PN  EDJ N2": the class is the label, the listing-segment
    # flags are not.
    assert names["PETR4"] == "PETROBRAS PN"
    assert names["VALE3"] == "VALE ON"
    assert names["KLBN11"] == "KLABIN S/A UNT"


def test_daily_candles_come_from_the_planned_files():
    fetch = StubFetch({"COTAHIST_D": cotahist_zip()})
    p = B3Provider(cache_dir=Path(tempfile.mkdtemp()), fetch=fetch)
    df = p.candles("PETR4", "1d", limit=5)
    assert list(df.columns) == CANDLE_COLUMNS
    assert len(df) == 1
    assert float(df["close"].iloc[0]) == 41.35


def test_daily_candles_explain_themselves_when_a_symbol_has_no_history():
    fetch = StubFetch({"COTAHIST_D": cotahist_zip()})
    p = B3Provider(cache_dir=Path(tempfile.mkdtemp()), fetch=fetch)
    with pytest.raises(ValueError, match="intraday-only"):
        p.candles("WINV26", "1d", limit=5)


def test_a_session_b3_never_published_is_remembered_as_absent():
    # Carnival is not a network failure, and rediscovering it on every chart
    # would spend a round trip a day forever.
    calls = {"n": 0}

    def fetch(url, timeout=0):
        if "COTAHIST_D01012020" in url:
            calls["n"] += 1
            raise FileNotFoundError(url)
        return cotahist_zip()

    p = B3Provider(cache_dir=Path(tempfile.mkdtemp()), fetch=fetch)
    for _ in range(3):
        with pytest.raises(Exception):
            p._cached_cotahist("D", "01012020")
    assert calls["n"] == 1


# ── request planning ────────────────────────────────────────────────────

def test_file_plan_uses_the_cheapest_grain_for_the_window():
    p = B3Provider(cache_dir=Path(tempfile.mkdtemp()), fetch=StubFetch({}))
    today = date(2026, 8, 26)
    # A window inside the running month is sessions, never a monthly file:
    # B3 writes the month only once it is over.
    plan = p.file_plan(date(2026, 8, 20), today, today=today)
    assert {kind for kind, _ in plan} == {"D"}
    assert ("D", "22082026") not in plan          # a Saturday
    assert ("D", "24082026") in plan
    # Full past years collapse into one yearly file.
    assert p.file_plan(date(2020, 1, 1), date(2021, 12, 31), today=today) == [
        ("A", "2020"), ("A", "2021")]
    # A month the window barely clips is cheaper session by session.
    plan = p.file_plan(date(2022, 11, 25), date(2022, 12, 20), today=today)
    assert [kind for kind, _ in plan][:4] == ["D", "D", "D", "D"]
    assert ("M", "122022") in plan


def test_window_turns_a_bar_count_into_trading_sessions():
    p = B3Provider(cache_dir=Path(tempfile.mkdtemp()), fetch=StubFetch({}))
    first, last = p._window(500, None, "2026-08-26")
    # 500 sessions is a bit over two calendar years, not 500 days.
    assert 720 <= (last - first).days <= 780
    assert p._window(10, "2024-01-01", "2024-06-30")[0] == date(2024, 1, 1)


def test_front_month_follows_b3_contract_rules():
    # Index futures expire on the Wednesday nearest the 15th of even months,
    # so on 26 August 2026 the August contract is long gone and October is
    # the front month; dollar futures roll monthly, so September is.
    assert front_month("index", date(2026, 8, 26)) == "V26"
    assert front_month("fx", date(2026, 8, 26)) == "U26"
    # The day of expiry is still the front month; the day after is not.
    assert front_month("index", date(2026, 8, 12)) == "Q26"
    assert front_month("index", date(2026, 8, 13)) == "V26"
    # December rolls the year over.
    assert front_month("fx", date(2026, 12, 5)) == "F27"


# ── B3 intraday and quotes ──────────────────────────────────────────────

INTRADAY = json.dumps({
    "BizSts": {"cd": "OK"},
    "TradgFlr": {"date": "2026-08-26", "scty": {"lstQtn": [
        {"closPric": 41.13, "dtTm": "10:03:00"},
        {"closPric": 41.10, "dtTm": "10:04:00"},
        {"closPric": 41.27, "dtTm": "10:06:00"},
        {"closPric": 41.20, "dtTm": "10:21:00"},
    ]}},
}).encode()

QUOTATION = json.dumps({
    "BizSts": {"cd": "OK"},
    "Trad": [{"scty": {"symb": "PETR4", "SctyQtn": {
        "opngPric": 41.14, "minPric": 40.97, "maxPric": 42.27,
        "curPrc": 41.42}}}],
}).encode()


def b3_intraday_provider() -> B3Provider:
    return B3Provider(cache_dir=Path(tempfile.mkdtemp()), fetch=StubFetch({
        "DailyFluctuationHistory": INTRADAY,
        "instrumentQuotation": QUOTATION,
        "COTAHIST_D": cotahist_zip(),
    }))


def test_intraday_minute_bars_are_the_print_itself():
    df = b3_intraday_provider().candles("PETR4", "1m", limit=10)
    assert list(df.columns) == CANDLE_COLUMNS
    assert len(df) == 4
    first = df.iloc[0]
    # B3's intraday feed publishes a close per minute and nothing else, so a
    # 1m bar is that one price four times over rather than an invented range.
    assert first["open"] == first["high"] == first["low"] == first["close"] == 41.13
    # 10:03 in São Paulo is 13:03 UTC; B3 has no daylight saving.
    assert int(first["ts"]) == 1_787_749_380
    assert (df["volume"] == 0).all()      # the feed publishes none


def test_intraday_wider_bars_are_real_ohlc_from_the_minutes_inside():
    df = b3_intraday_provider().candles("PETR4", "15m", limit=10)
    assert len(df) == 2                   # 10:00-10:15 and 10:15-10:30
    bar = df.iloc[0]
    assert (bar["open"], bar["high"], bar["low"], bar["close"]) == (
        41.13, 41.27, 41.10, 41.27)


def test_intraday_says_so_when_b3_has_published_no_prints():
    p = B3Provider(cache_dir=Path(tempfile.mkdtemp()), fetch=StubFetch({
        "DailyFluctuationHistory": json.dumps(
            {"BizSts": {"cd": "OK"}, "TradgFlr": {}}).encode()}))
    with pytest.raises(ValueError, match="no intraday prints"):
        p.candles("PETR4", "5m", limit=10)


def test_quote_reads_the_last_price_and_leaves_the_book_alone():
    q = b3_intraday_provider().quote("petr4")
    assert q.symbol == "PETR4"
    assert q.price == 41.42
    # B3 does not publish a book on this feed; inventing one would be a lie
    # the ticket would act on.
    assert q.bid is None and q.ask is None


def test_b3_declining_a_symbol_is_an_error_not_an_empty_chart():
    p = B3Provider(cache_dir=Path(tempfile.mkdtemp()), fetch=StubFetch({
        "instrumentQuotation": json.dumps(
            {"BizSts": {"cd": "NOK", "desc": "Quotation not available."}}).encode()}))
    with pytest.raises(ValueError, match="not available"):
        p.quote("NOPE1")


def test_b3_passes_the_compliance_harness():
    assert check_provider(b3_intraday_provider()) == []


def test_b3_rejects_a_timeframe_it_does_not_serve():
    with pytest.raises(ValueError):
        b3_intraday_provider().candles("PETR4", "1w", limit=10)


# ── Banco Central ───────────────────────────────────────────────────────

SGS_ROWS = json.dumps([
    {"data": "21/08/2026", "valor": "13.90"},
    {"data": "24/08/2026", "valor": "13.90"},
    {"data": "25/08/2026", "valor": "13.65"},
]).encode()


def bcb_provider(body: bytes = SGS_ROWS) -> BcbProvider:
    return BcbProvider(cache_dir=Path(tempfile.mkdtemp()),
                       fetch=StubFetch({"bcdata.sgs": body}))


def test_bcb_series_become_flat_candles():
    df = bcb_provider().candles("BCB:CDI", "1d", limit=10)
    assert list(df.columns) == CANDLE_COLUMNS
    assert len(df) == 3
    row = df.iloc[-1]
    # One number per print, so it fills every OHLC slot. A rate has no volume.
    assert row["open"] == row["high"] == row["low"] == row["close"] == 13.65
    assert row["ts"] == 1_787_616_000          # 2026-08-25T00:00:00Z
    assert (df["volume"] == 0).all()


def test_bcb_revisions_replace_the_earlier_print_for_that_date():
    body = json.dumps([{"data": "25/08/2026", "valor": "13.65"},
                       {"data": "25/08/2026", "valor": "13.70"}]).encode()
    df = bcb_provider(body).candles("BCB:CDI", "1d", limit=10)
    assert len(df) == 1
    assert float(df["close"].iloc[0]) == 13.70


def test_bcb_windows_a_daily_series_by_bar_count_not_by_decades():
    p = bcb_provider()
    p.candles("BCB:CDI", "1d", limit=30)
    asked = p._fetch.calls
    # 30 daily prints is weeks, so one request, not four decades in ten-year
    # chunks -- each SGS window costs about twenty seconds whatever its width.
    assert len(asked) == 1
    assert "dataInicial=" in asked[0]


def test_bcb_monthly_series_asks_for_a_proportionally_longer_window():
    p = bcb_provider()
    p.candles("BCB:IPCA.MES", "1d", limit=24)
    first = p._fetch.calls[0]
    # 24 monthly prints is two years of calendar, not a month.
    assert "dataInicial=" in first


def test_bcb_reuses_the_disk_cache_across_processes():
    cache = Path(tempfile.mkdtemp())
    fetch = StubFetch({"bcdata.sgs": SGS_ROWS})
    BcbProvider(cache_dir=cache, fetch=fetch).candles("BCB:CDI", "1d", limit=10)
    before = len(fetch.calls)
    # A second provider over the same directory is what a restart looks like.
    again = BcbProvider(cache_dir=cache, fetch=fetch)
    assert len(again.candles("BCB:CDI", "1d", limit=10)) == 3
    assert len(fetch.calls) == before


def test_bcb_unknown_series_is_named_as_such():
    with pytest.raises(ValueError, match="unknown BCB series"):
        bcb_provider().candles("BCB:NOPE", "1d")


def test_bcb_empty_window_is_a_gap_not_a_crash():
    assert list(bcb_candles([]).columns) == CANDLE_COLUMNS


def test_bcb_passes_the_compliance_harness():
    assert check_provider(bcb_provider()) == []


# ── plugin robustness ───────────────────────────────────────────────────

def test_cache_dir_accepts_a_plain_string():
    """A string path is the obvious thing for a caller to pass.

    Without coercion it survives construction and fails much later inside
    a download, as an AttributeError that the caller sees reported as "B3
    published no file" -- sending them to check the exchange instead of
    their own call.
    """
    for provider in (B3Provider(cache_dir=tempfile.mkdtemp()),
                     BcbProvider(cache_dir=tempfile.mkdtemp())):
        assert isinstance(provider.cache_dir(), Path)
        assert provider.cache_dir().is_dir()


def test_a_bug_is_not_reported_as_the_exchange_being_quiet():
    """Our own errors must not be laundered into "no file published".

    _latest_session walks back over days and treats a failure as a
    holiday. That is right for a missing file and wrong for a TypeError,
    which is a bug here and has to surface as one.
    """
    def broken_fetch(url, timeout=0):
        raise TypeError("this is a bug in our own code, not a holiday")

    p = B3Provider(cache_dir=Path(tempfile.mkdtemp()), fetch=broken_fetch)
    with pytest.raises(TypeError):
        p.search("")
