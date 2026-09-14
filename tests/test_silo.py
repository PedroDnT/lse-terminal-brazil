"""The Silo provider: symbols, paging past the row cap, and the factor.

Offline, like the rest of the unit suite. ``SiloProvider`` takes its HTTP
getter as a constructor argument for exactly this reason, so what is
exercised here is the request planning and the arithmetic -- which is where
the bugs live -- with no dependency on Silo being up.

Three things get most of the attention, because all three are silent when
wrong:

* **The row cap.** PostgREST truncates every response at ``db-max-rows``
  and says so only in ``Content-Range``. A series cut short at 1,000 rows
  looks exactly like a complete one, so the stub here deliberately enforces
  the cap and the tests check that a longer history still arrives whole.
* **The quotation factor.** A paper quoted per lot of a thousand reports a
  close a thousand times its unit price. Nothing about the number looks
  wrong, and a chart of it is simply off by three decimal places.
* **The key.** It must come from the caller's own environment or config and
  never from this package, and its absence must read as "not configured"
  rather than as a failure.
"""

from __future__ import annotations

import json
import re

import pytest

from lse_terminal.contracts import CANDLE_COLUMNS
from lse_terminal.testing import check_provider
from lse_terminal_brazil.silo import (FUND_PREFIX, SiloError, SiloProvider,
                                      _fund_frame, _has_more, _http_json,
                                      _looks_like_ticker, _parse_fund_symbol,
                                      _ticker_frame)

KEY = "test-key"

COVERAGE = [
    {"dataset": "quotes", "as_of": "2026-09-11",
     "complete_through": "2026-09-11", "source": "b3_cotahist"},
    {"dataset": "fund_nav", "as_of": "2026-12-31",
     "complete_through": "2026-08-01", "source": "cvm"},
]


def quote_row(ticker="PETR4", day="2026-09-11", close=49.0, factor=1,
              asset_class="equity", **extra):
    """One row shaped as Silo's quotes view actually returns it."""
    row = {"ticker": ticker, "trade_date": day, "short_name": "PETROBRAS",
           "spec": "PN      N2", "asset_class": asset_class,
           "open": close - 0.5, "high": close + 0.12, "low": close - 0.91,
           "close": close, "volume": 1_343_979_511.0, "quantity": 27_608_600,
           "quotation_factor": factor, "bid": close, "ask": close + 0.01}
    row.update(extra)
    return row


class StubSilo:
    """A stand-in for Silo that enforces the same row cap the real one does.

    Only the behaviours the provider actually depends on are modelled, but
    those are modelled properly: ``Range`` is honoured, the response is
    truncated at ``max_rows``, and ``Content-Range`` reports what was
    served. Without the truncation the paging tests would pass against a
    provider that never paged.
    """

    def __init__(self, rows=None, rpc=None, max_rows=1000, total=None):
        self.rows = rows or []
        self.rpc = rpc or {}
        self.max_rows = max_rows
        # "*" is what PostgREST sends when counting was not asked for, which
        # is the default and therefore the case worth defaulting to here.
        self.total = total
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method, url, headers, body, timeout=30.0):
        self.calls.append((method, url, dict(headers)))
        if not headers.get("apikey"):
            raise AssertionError("request sent with no apikey header")
        if method == "POST":
            function = url.rsplit("/", 1)[-1]
            payload = json.loads(body) if body else {}
            answer = self.rpc.get(function)
            if answer is None:
                raise AssertionError(f"unexpected rpc: {function}")
            rows = answer(payload) if callable(answer) else answer
            return rows, {"Content-Range": f"0-{max(0, len(rows) - 1)}/*"}

        rows = _apply_query(self.rows, url)
        first, last = _requested_range(headers)
        window = rows[first:last + 1][: self.max_rows]
        total = self.total if self.total is not None else "*"
        served = f"{first}-{first + len(window) - 1}" if window else "*"
        return window, {"Content-Range": f"{served}/{total}"}


def _requested_range(headers) -> tuple[int, int]:
    raw = headers.get("Range", "0-999")
    first, _, last = raw.partition("-")
    return int(first), int(last)


def _apply_query(rows, url):
    """The slice of ``rows`` the query string asks for: filters, then order."""
    query = url.partition("?")[2]
    out = list(rows)
    for part in query.split("&"):
        key, _, value = part.partition("=")
        value = value.replace("%3A", ":")
        if key == "ticker" and value.startswith("eq."):
            out = [r for r in out if r["ticker"] == value[3:]]
        elif key == "ticker" and value.startswith("like."):
            pattern = "^" + re.escape(value[5:]).replace(r"\*", ".*") + "$"
            out = [r for r in out if re.match(pattern, r["ticker"])]
        elif key == "trade_date" and value.startswith("eq."):
            out = [r for r in out if r["trade_date"] == value[3:]]
        elif key == "trade_date" and value.startswith("gte."):
            out = [r for r in out if r["trade_date"] >= value[4:]]
        elif key == "trade_date" and value.startswith("lte."):
            out = [r for r in out if r["trade_date"] <= value[4:]]
        elif key == "order" and value == "trade_date.desc":
            out.sort(key=lambda r: r["trade_date"], reverse=True)
        elif key == "order" and value == "trade_date.asc":
            out.sort(key=lambda r: r["trade_date"])
        elif key == "order" and value == "volume.desc":
            out.sort(key=lambda r: r.get("volume") or 0, reverse=True)
    return out


def sessions(n, ticker="PETR4", start_year=2019, end=None):
    """``n`` consecutive dated rows, ascending, with a walking close.

    ``end`` anchors the last row to a given date instead of the first row to
    a year, which is what a test needs when the provider will look up the
    latest session and filter on it.
    """
    import datetime as dt
    day = (dt.date.fromisoformat(end) - dt.timedelta(days=n - 1)
           if end else dt.date(start_year, 1, 2))
    out = []
    for i in range(n):
        out.append(quote_row(ticker=ticker, day=day.isoformat(),
                             close=20.0 + i * 0.01))
        day += dt.timedelta(days=1)
    return out


def provider(**kw):
    return SiloProvider(key=KEY, **kw)


# ── configuration ───────────────────────────────────────────────────────

def test_unconfigured_without_a_key(monkeypatch, tmp_path):
    monkeypatch.delenv("SILO_KEY", raising=False)
    monkeypatch.setenv("LSE_TERMINAL_CONFIG_DIR", str(tmp_path))
    assert SiloProvider(fetch=StubSilo()).configured() is False


def test_key_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("SILO_KEY", "from-env")
    monkeypatch.setenv("LSE_TERMINAL_CONFIG_DIR", str(tmp_path))
    p = SiloProvider(fetch=StubSilo())
    assert p.configured() is True
    assert p.key() == "from-env"


def test_key_from_config_file(monkeypatch, tmp_path):
    monkeypatch.delenv("SILO_KEY", raising=False)
    monkeypatch.setenv("LSE_TERMINAL_CONFIG_DIR", str(tmp_path))
    (tmp_path / "silo.json").write_text(json.dumps({"key": "from-file"}))
    assert SiloProvider(fetch=StubSilo()).key() == "from-file"


def test_environment_beats_the_config_file(monkeypatch, tmp_path):
    monkeypatch.setenv("SILO_KEY", "from-env")
    monkeypatch.setenv("LSE_TERMINAL_CONFIG_DIR", str(tmp_path))
    (tmp_path / "silo.json").write_text(json.dumps({"key": "from-file"}))
    assert SiloProvider(fetch=StubSilo()).key() == "from-env"


def test_unreadable_config_is_unconfigured_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.delenv("SILO_KEY", raising=False)
    monkeypatch.setenv("LSE_TERMINAL_CONFIG_DIR", str(tmp_path))
    (tmp_path / "silo.json").write_text("this is not json")
    assert SiloProvider(fetch=StubSilo()).configured() is False


def test_missing_key_names_both_places_to_put_one(monkeypatch, tmp_path):
    monkeypatch.delenv("SILO_KEY", raising=False)
    monkeypatch.setenv("LSE_TERMINAL_CONFIG_DIR", str(tmp_path))
    p = SiloProvider(fetch=StubSilo(rpc={"coverage": COVERAGE}))
    with pytest.raises(SiloError) as e:
        p.coverage()
    assert "SILO_KEY" in str(e.value) and "silo.json" in str(e.value)


def test_no_credential_ships_in_the_package():
    """The published Silo key is a shared testing credential. Not ours to bundle."""
    from pathlib import Path
    import lse_terminal_brazil
    root = Path(lse_terminal_brazil.__file__).parent
    for path in root.rglob("*.py"):
        assert "sb_publishable_" not in path.read_text(), path
        assert "sb_secret_" not in path.read_text(), path


# ── the row cap ─────────────────────────────────────────────────────────

def test_history_longer_than_the_row_cap_arrives_whole():
    """1,900 sessions through a 1,000-row cap is two pages, not one truncated."""
    stub = StubSilo(rows=sessions(1900), rpc={"coverage": COVERAGE})
    df = provider(fetch=stub).candles("PETR4", "1d", limit=1900)
    assert len(df) == 1900
    assert df["ts"].is_monotonic_increasing
    gets = [c for c in stub.calls if c[0] == "GET"]
    assert len(gets) == 2, "a 1,900-row history should cost exactly two pages"


def test_paging_stops_when_the_server_reports_the_total():
    """``0-999/1500`` says where the data ends; asking again would waste a trip."""
    stub = StubSilo(rows=sessions(1500), rpc={"coverage": COVERAGE}, total=1500)
    df = provider(fetch=stub).candles("PETR4", "1d", limit=5000)
    assert len(df) == 1500
    assert len([c for c in stub.calls if c[0] == "GET"]) == 2


def test_a_short_page_ends_the_walk():
    """With ``/*`` for a total, a page under the window is the end of the data."""
    stub = StubSilo(rows=sessions(300), rpc={"coverage": COVERAGE})
    provider(fetch=stub).candles("PETR4", "1d", limit=1000)
    assert len([c for c in stub.calls if c[0] == "GET"]) == 1


def test_a_bar_count_reads_the_newest_bars_in_one_page():
    stub = StubSilo(rows=sessions(1900), rpc={"coverage": COVERAGE})
    df = provider(fetch=stub).candles("PETR4", "1d", limit=200)
    assert len(df) == 200
    assert len([c for c in stub.calls if c[0] == "GET"]) == 1
    # Newest 200 of 1,900, still handed back oldest-first.
    assert df["close"].iloc[-1] == pytest.approx(20.0 + 1899 * 0.01)
    assert df["ts"].is_monotonic_increasing


def test_has_more_reads_the_header_not_the_rows():
    assert _has_more({"Content-Range": "0-999/1917"}, 1000) is True
    assert _has_more({"Content-Range": "0-999/1000"}, 1000) is False
    # PostgREST's casing is not guaranteed, and an unknown total is not
    # a reason to assume the data ended.
    assert _has_more({"content-range": "0-999/*"}, 1000) is True
    assert _has_more({}, 1000) is True


# ── prices ──────────────────────────────────────────────────────────────

def test_prices_are_divided_by_the_quotation_factor():
    """FNAM11: a stored close of 0.16 per lot is 0.00016 per unit.

    The direction is not a convention to pick -- it is checked against the
    session's own published financial volume, which only reconciles with
    the divided price. See the module docstring in silo.py.
    """
    rows = [quote_row(ticker="FNAM11", day="2019-12-30", close=0.16, factor=1000)]
    df = _ticker_frame(rows)
    assert df["close"].iloc[0] == pytest.approx(0.00016)
    assert df["open"].iloc[0] == pytest.approx((0.16 - 0.5) / 1000)


def test_volume_is_money_and_keeps_its_scale():
    """The factor divides the price, not the turnover."""
    rows = [quote_row(ticker="FNAM11", close=0.16, factor=1000,
                      volume=185_917.58)]
    assert _ticker_frame(rows)["volume"].iloc[0] == pytest.approx(185_917.58)


def test_a_missing_or_absurd_factor_falls_back_to_one():
    for factor in (None, 0, -1, "", "nonsense"):
        rows = [quote_row(close=49.0, factor=factor)]
        assert _ticker_frame(rows)["close"].iloc[0] == pytest.approx(49.0)


def test_candles_frame_matches_the_contract():
    df = provider(fetch=StubSilo(rows=sessions(30), rpc={"coverage": COVERAGE})
                  ).candles("PETR4", "1d", limit=30)
    assert list(df.columns) == CANDLE_COLUMNS
    assert df["ts"].dtype.kind == "i"
    assert not df["ts"].duplicated().any()
    # Epoch SECONDS: a 2019 date is ~1.5e9, and a pandas that parsed to
    # microseconds would put it three orders of magnitude out.
    assert 1_500_000_000 < int(df["ts"].iloc[0]) < 2_000_000_000


def test_an_unknown_ticker_returns_an_empty_frame_not_an_error():
    df = provider(fetch=StubSilo(rows=sessions(10), rpc={"coverage": COVERAGE})
                  ).candles("ZZZZ9", "1d", limit=10)
    assert list(df.columns) == CANDLE_COLUMNS
    assert len(df) == 0


def test_an_intraday_timeframe_is_refused():
    p = provider(fetch=StubSilo(rows=sessions(5), rpc={"coverage": COVERAGE}))
    with pytest.raises(ValueError, match="timeframe"):
        p.candles("PETR4", "5m", limit=10)


# ── date windows ────────────────────────────────────────────────────────

def test_a_bounded_window_sends_both_bounds():
    stub = StubSilo(rows=sessions(400), rpc={"coverage": COVERAGE})
    df = provider(fetch=stub).candles("PETR4", "1d", limit=500,
                                      start="2019-03-01", end="2019-03-31")
    url = [c[1] for c in stub.calls if c[0] == "GET"][0]
    assert "trade_date=gte.2019-03-01" in url
    assert "trade_date=lte.2019-03-31" in url
    assert "order=trade_date.asc" in url
    assert len(df) == 31


def test_a_start_pages_forward_and_a_bar_count_pages_back():
    """The terminal's own law, and the two directions differ."""
    stub = StubSilo(rows=sessions(400), rpc={"coverage": COVERAGE})
    p = provider(fetch=stub)
    forward = p.candles("PETR4", "1d", limit=5, start="2019-01-02")
    assert forward["close"].iloc[0] == pytest.approx(20.0)
    backward = p.candles("PETR4", "1d", limit=5)
    assert backward["close"].iloc[-1] == pytest.approx(20.0 + 399 * 0.01)


# ── search ──────────────────────────────────────────────────────────────

def test_empty_query_lists_the_last_session_busiest_first():
    rows = [quote_row(ticker="PETR4", volume=3.0),
            quote_row(ticker="BOVA11", volume=2.0, asset_class="fund_quota"),
            quote_row(ticker="VALE3", volume=1.0)]
    out = provider(fetch=StubSilo(rows=rows, rpc={"coverage": COVERAGE})).search("")
    assert [i.symbol for i in out] == ["PETR4", "VALE3", "BOVA11"]
    # Contiguous by category, in the module's declared order: the sidebar
    # renders what arrives, verbatim.
    assert [i.category for i in out] == [
        "Silo — Ações", "Silo — Ações", "Silo — ETFs e Fundos listados"]


def test_the_default_listing_is_fetched_once():
    stub = StubSilo(rows=[quote_row()], rpc={"coverage": COVERAGE})
    p = provider(fetch=stub)
    p.search("")
    p.search("")
    assert len([c for c in stub.calls if c[0] == "GET"]) == 1


def test_a_listing_skips_classes_with_no_folder():
    rows = [quote_row(ticker="PETR4"),
            quote_row(ticker="WEIRD1", asset_class="something_new")]
    out = provider(fetch=StubSilo(rows=rows, rpc={"coverage": COVERAGE})).search("")
    assert [i.symbol for i in out] == ["PETR4"]


def test_a_ticker_query_beats_lookups_name_ranking():
    """Typing PETR must reach PETR4, not twenty funds called PETRA.

    lookup ranks by name across ~80k CVM funds, so on its own it buries the
    ticker. This is the whole reason the ticker prefix is a separate query.
    """
    funds = [{"id": f"1234567800019{i}", "id_type": "cnpj", "asset_class": "fi",
              "name": f"PETRA FUNDO {i}", "isin": None,
              "cnpj": f"1234567800019{i}", "tickers": None} for i in range(5)]
    stub = StubSilo(rows=[quote_row(ticker="PETR4"), quote_row(ticker="PETR3")],
                    rpc={"coverage": COVERAGE, "lookup": funds})
    out = provider(fetch=stub).search("PETR")
    assert out[0].symbol == "PETR4"
    assert {i.symbol for i in out[:2]} == {"PETR4", "PETR3"}
    assert any(i.symbol.startswith(FUND_PREFIX) for i in out)


def test_a_fund_name_query_does_not_ask_for_tickers():
    """Spaces mean it is not a ticker, so the prefix query is skipped."""
    fund = [{"id": "08973948000135", "id_type": "cnpj", "asset_class": "fi",
             "name": "BB ACOES SETOR FINANCEIRO", "isin": None,
             "cnpj": "08973948000135", "tickers": None}]
    stub = StubSilo(rows=[quote_row()], rpc={"coverage": COVERAGE, "lookup": fund})
    provider(fetch=stub).search("BB ACOES")
    assert not [c for c in stub.calls if c[0] == "GET"]


def test_one_fund_match_offers_the_series_it_reports():
    fund = [{"id": "08973948000135", "id_type": "cnpj", "asset_class": "fi",
             "name": "BB ACOES SETOR FINANCEIRO", "isin": None,
             "cnpj": "08973948000135", "tickers": None}]
    stub = StubSilo(rows=[], rpc={"coverage": COVERAGE, "lookup": fund})
    out = provider(fetch=stub).search("08973948000135")
    assert out[0].symbol == "CVM:08973948000135"
    assert "CVM:08973948000135.COTISTAS" in {i.symbol for i in out}
    assert all(i.category == "Silo — Fundos (CVM)" for i in out)


def test_search_respects_its_limit():
    rows = [quote_row(ticker=f"TICK{i}", volume=float(i)) for i in range(40)]
    out = provider(fetch=StubSilo(rows=rows, rpc={"coverage": COVERAGE})).search("", limit=7)
    assert len(out) == 7


def test_looks_like_ticker():
    assert _looks_like_ticker("PETR4")
    assert _looks_like_ticker("BOVA11")
    assert not _looks_like_ticker("BB ACOES")          # a name
    assert not _looks_like_ticker("08973948000135")    # a CNPJ
    assert not _looks_like_ticker("")


# ── funds ───────────────────────────────────────────────────────────────

FUND_ROWS = [
    {"cnpj": "00071477000168", "period": "2026-06-01", "entity_type": "fi",
     "nav": 15_510_848_680.95, "quota": 9.778624, "quotaholders": 145_509,
     "delinquency": None, "monthly_yield": None,
     "inflows": 27_890_781_471.65, "redemptions": 29_919_859_473.73,
     "assets": None},
    {"cnpj": "00071477000168", "period": "2026-07-01", "entity_type": "fi",
     "nav": 15_600_000_000.0, "quota": 9.9, "quotaholders": 146_000,
     "delinquency": None, "monthly_yield": None,
     "inflows": 1.0, "redemptions": 2.0, "assets": None},
]


@pytest.mark.parametrize("suffix,column,expected", [
    ("", "nav", 15_600_000_000.0),
    ("PL", "nav", 15_600_000_000.0),
    ("COTA", "quota", 9.9),
    ("COTISTAS", "quotaholders", 146_000),
    ("CAPTACAO", "inflows", 1.0),
    ("RESGATE", "redemptions", 2.0),
])
def test_a_fund_suffix_selects_its_column(suffix, column, expected):
    symbol = "CVM:00071477000168" + (f".{suffix}" if suffix else "")
    cnpj, metric, _ = _parse_fund_symbol(symbol)
    assert (cnpj, metric) == ("00071477000168", column)
    stub = StubSilo(rpc={"coverage": COVERAGE, "fund_nav": FUND_ROWS})
    df = provider(fetch=stub).candles(symbol, "1d", limit=12)
    assert df["close"].iloc[-1] == pytest.approx(expected)


def test_a_fund_series_is_flat_candles_with_no_volume():
    df = _fund_frame(FUND_ROWS, "nav")
    assert (df["open"] == df["close"]).all()
    assert (df["high"] == df["low"]).all()
    assert (df["volume"] == 0).all()
    assert df["ts"].is_monotonic_increasing


def test_a_metric_the_fund_does_not_report_is_empty_not_wrong():
    """A FI publishes no delinquency; that is an absence, not a zero."""
    df = _fund_frame(FUND_ROWS, "delinquency")
    assert list(df.columns) == CANDLE_COLUMNS
    assert len(df) == 0


def test_an_unknown_fund_suffix_says_which_ones_exist():
    with pytest.raises(ValueError) as e:
        _parse_fund_symbol("CVM:00071477000168.PROFIT")
    assert "COTISTAS" in str(e.value)


def test_a_punctuated_cnpj_is_accepted():
    cnpj, metric, _ = _parse_fund_symbol("CVM:00.071.477/0001-68.COTA")
    assert (cnpj, metric) == ("00071477000168", "quota")


def test_a_fund_window_is_filtered_without_a_second_request():
    """fund_nav is an RPC, and PostgREST will not page one. Filter locally."""
    stub = StubSilo(rpc={"coverage": COVERAGE, "fund_nav": FUND_ROWS})
    df = provider(fetch=stub).candles("CVM:00071477000168", "1d", limit=12,
                                      start="2026-07-01")
    assert len(df) == 1
    assert len(stub.calls) == 1


# ── quotes ──────────────────────────────────────────────────────────────

def test_a_quote_is_the_last_session_close_per_unit():
    latest = [quote_row(ticker="FNAM11", day="2019-12-30", close=0.16,
                        factor=1000, bid=0.15, ask=0.17)]
    q = provider(fetch=StubSilo(rpc={"coverage": COVERAGE, "quote_latest": latest})
                 ).quote("fnam11")
    assert q.symbol == "FNAM11"
    assert q.price == pytest.approx(0.00016)
    assert q.bid == pytest.approx(0.00015)
    assert q.ask == pytest.approx(0.00017)
    # Stamped at the session it came from, not at now: it is a day old and
    # saying otherwise would make a stale price look live.
    assert q.ts == pytest.approx(1_577_664_000)


def test_a_zero_bid_is_an_absence_not_a_price():
    latest = [quote_row(bid=0, ask=0)]
    q = provider(fetch=StubSilo(rpc={"coverage": COVERAGE, "quote_latest": latest})
                 ).quote("PETR4")
    assert q.bid is None and q.ask is None


def test_no_quote_raises_not_supported():
    from lse_terminal.contracts import NotSupported
    p = provider(fetch=StubSilo(rpc={"coverage": COVERAGE, "quote_latest": []}))
    with pytest.raises(NotSupported):
        p.quote("ZZZZ9")


# ── transport ───────────────────────────────────────────────────────────

def test_the_latest_session_comes_from_coverage_not_the_calendar():
    """A weekend, a holiday and an ingest that has not run all look alike."""
    stub = StubSilo(rows=[quote_row()], rpc={"coverage": COVERAGE})
    import datetime as dt
    assert provider(fetch=stub).latest_session() == dt.date(2026, 9, 11)


def test_coverage_without_a_quotes_row_is_an_error_not_a_guess():
    stub = StubSilo(rpc={"coverage": [{"dataset": "funds", "as_of": "2026-08-01"}]})
    with pytest.raises(SiloError, match="coverage"):
        provider(fetch=stub).latest_session()


def test_a_rejected_key_says_so_rather_than_reporting_an_outage(monkeypatch):
    """Exercised against the real transport, which is where translation lives."""
    import io
    import urllib.error
    import urllib.request

    def refuse(req, timeout=0):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", {},
            io.BytesIO(b'{"message":"Invalid API key"}'))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(SiloError) as e:
        _http_json("GET", "https://silo.example/quotes", {"apikey": KEY}, None)
    assert "SILO_KEY" in str(e.value) and "401" in str(e.value)
    assert "Invalid API key" in str(e.value)


def test_an_outage_is_reported_as_one(monkeypatch):
    import urllib.error
    import urllib.request

    def down(req, timeout=0):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", down)
    with pytest.raises(SiloError, match="unreachable"):
        _http_json("GET", "https://silo.example/quotes", {"apikey": KEY}, None)


def test_a_custom_url_is_honoured():
    stub = StubSilo(rows=[quote_row()], rpc={"coverage": COVERAGE})
    SiloProvider(url="https://silo.example/rest/v1/", key=KEY,
                 fetch=stub).coverage()
    assert stub.calls[0][1] == "https://silo.example/rest/v1/rpc/coverage"


def test_the_provider_is_marked_custom():
    """/api/providers must not gate a source the user reaches with their own key."""
    assert SiloProvider.is_custom is True
    assert SiloProvider.name == "silo"


def test_construction_touches_nothing():
    """The registry builds every provider at boot; none may phone home."""
    def explode(*a, **k):
        raise AssertionError("constructed provider made a request")

    SiloProvider(fetch=explode)


def test_provider_contract():
    # The default listing reads the session `coverage` reports, so the
    # history has to actually run up to it -- a fixture that stops in 2019
    # would leave search("") empty and prove nothing about the provider.
    rows = sessions(120, end="2026-09-11") + [
        quote_row(ticker="BOVA11", day="2026-09-11", asset_class="fund_quota",
                  volume=1.0)]
    problems = check_provider(
        provider(fetch=StubSilo(rows=rows, rpc={"coverage": COVERAGE})),
        symbol="PETR4", timeframe="1d")
    assert problems == []


def test_the_latest_session_is_asked_for_once_per_search_burst():
    """Every search needs it and it changes once a day; one RPC covers both."""
    stub = StubSilo(rows=[quote_row(ticker="PETR4"), quote_row(ticker="PETR3")],
                    rpc={"coverage": COVERAGE, "lookup": []})
    p = provider(fetch=stub)
    p.search("PETR")
    p.search("VALE")
    coverage_calls = [c for c in stub.calls
                      if c[0] == "POST" and c[1].endswith("/coverage")]
    assert len(coverage_calls) == 1
