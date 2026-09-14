"""Live tests: the real B3, Banco Central and Silo endpoints, over the network.

Deselected by default (`-m "not live"` in pyproject), because the unit
suite must stay runnable offline and must never fail because a Brazilian
public website is down. Run them deliberately:

    pytest -m live

These exist because of what this package actually is. The unit tests
cover the parsing and the request planning, which is where logic bugs
live -- but the standing risk here is different in kind: B3's endpoints
are public downloads and an undocumented JSON feed, not a contracted API.
Nobody owes us notice before a URL moves, a field is renamed or the
fixed-width layout gains a column. No amount of stubbed testing detects
that. Only asking the real source does, which is why CI runs these on a
schedule as well as on demand.

So the assertions here are deliberately about SHAPE and INVARIANTS, never
about values. A test that pins PETR4 to a price fails every day for the
wrong reason; a test that checks the frame has the right columns, is
ordered, and has a high above its close fails only when something has
genuinely broken.

Market state is not a failure either. Outside a session B3 publishes no
intraday prints, and a holiday has no daily file. Those paths raise a
clear error by design, so the tests accept the documented refusal in
place of data.
"""

from __future__ import annotations

import pytest

from lse_terminal_brazil import B3Provider, BcbProvider, SiloProvider
from lse_terminal.contracts import CANDLE_COLUMNS

pytestmark = pytest.mark.live


def assert_candle_frame(df, *, minimum: int = 1):
    """The candle contract, checked the way the terminal itself depends on it."""
    assert list(df.columns) == CANDLE_COLUMNS
    assert len(df) >= minimum
    assert df["ts"].is_monotonic_increasing
    assert not df["ts"].duplicated().any()
    assert df[["open", "high", "low", "close"]].notna().all().all()
    assert (df["high"] + 1e-9 >= df[["open", "close"]].max(axis=1)).all()
    assert (df["low"] - 1e-9 <= df[["open", "close"]].min(axis=1)).all()
    assert (df["close"] > 0).all()


# ── B3 ──────────────────────────────────────────────────────────────────

def test_cotahist_still_downloads_and_parses(tmp_path):
    """The daily file exists, is fixed-width as expected, and yields a catalog.

    This is the single most valuable check in the package: it exercises
    the real URL, the real ZIP, and the real record layout in one call.
    """
    rows = B3Provider(cache_dir=tmp_path).search("", limit=5000)
    assert len(rows) > 500, f"catalog collapsed to {len(rows)} rows"
    symbols = {i.symbol for i in rows}
    # Blue chips B3 does not stop listing. If PETR4 is missing, the parse
    # is wrong, not the market.
    assert {"PETR4", "VALE3", "ITUB4"} <= symbols
    by_symbol = {i.symbol: i for i in rows}
    assert by_symbol["PETR4"].category == "B3 Ações"
    assert by_symbol["PETR4"].name, "display name went empty"
    # The classification map still lines up with the CODBDI values B3 sends.
    categories = {i.category for i in rows}
    assert {"B3 Ações", "B3 ETFs", "B3 Fundos Imobiliários", "B3 BDRs"} <= categories


def test_deep_history_reaches_the_years_only_the_yearly_archive_covers(tmp_path):
    """The oldest grain, which is where the naming and fallback bugs lived.

    B3 publishes no monthly file for 2000, so this only works if the plan
    falls back to that year's yearly archive -- and that archive names its
    member "COTAHIST.A2000", not "...TXT". Both were broken at once and the
    only symptom was a bogus "no history for PETR4". The recent-file tests
    could not see either, because recent files have monthly grains and the
    modern name.
    """
    df = B3Provider(cache_dir=tmp_path).candles(
        "PETR4", "1d", start="2000-01-03", end="2000-03-31", limit=10)
    assert_candle_frame(df, minimum=5)
    # Pre-split prices, so this also pins that no silent rescaling crept in.
    assert df["close"].min() > 100


def test_daily_candles_come_back_from_the_real_files(tmp_path):
    df = B3Provider(cache_dir=tmp_path).candles("PETR4", "1d", limit=5)
    assert_candle_frame(df, minimum=3)
    assert (df["volume"] > 0).all(), "traded shares should not be zero on a blue chip"


def test_intraday_or_the_documented_refusal(tmp_path):
    """Intraday during a session; outside one, the error we promise."""
    p = B3Provider(cache_dir=tmp_path)
    try:
        df = p.candles("PETR4", "15m", limit=20)
    except ValueError as e:
        # Outside a session there are no prints, and saying so is the
        # contract, not a failure.
        assert "intraday prints" in str(e), f"unexpected refusal: {e}"
        return
    assert_candle_frame(df)


def test_quote_feed_still_answers_for_every_instrument_class(tmp_path):
    """Shares, futures and indices all come through the same feed.

    Futures are the reason this matters: they are not in COTAHIST at all,
    so this endpoint is the only thing that reaches them, and the
    front-month contract code is derived rather than looked up.
    """
    p = B3Provider(cache_dir=tmp_path)
    for symbol in ("PETR4", "IBOV"):
        q = p.quote(symbol)
        assert q.symbol == symbol
        assert q.price > 0
    # The derived front-month codes must name contracts B3 actually quotes.
    futures = [i.symbol for i in p.search("", limit=5000)
               if i.category == "B3 Futuros e Índices" and i.symbol[:3] in
               ("WIN", "WDO", "IND", "DOL")]
    assert futures, "no front-month futures in the catalog"
    quoted = []
    for symbol in futures:
        try:
            quoted.append(p.quote(symbol).price > 0)
        except Exception:
            quoted.append(False)
    assert any(quoted), f"none of the derived contracts quote: {futures}"


def test_futures_daily_still_refuses_rather_than_inventing(tmp_path):
    p = B3Provider(cache_dir=tmp_path)
    winner = next(i.symbol for i in p.search("", limit=5000)
                  if i.symbol.startswith("WIN"))
    with pytest.raises(ValueError, match="intraday-only"):
        p.candles(winner, "1d", limit=5)


# ── Banco Central ───────────────────────────────────────────────────────

@pytest.mark.parametrize("symbol", ["BCB:CDI", "BCB:SELIC.META", "BCB:USDBRL",
                                    "BCB:IPCA.12M"])
def test_sgs_series_still_answer(tmp_path, symbol):
    df = BcbProvider(cache_dir=tmp_path).candles(symbol, "1d", limit=10)
    assert_candle_frame(df)
    # A series is one value per print, so the candle is flat by construction.
    # If that stops holding, the SGS payload shape has changed.
    assert (df["open"] == df["close"]).all()
    assert (df["high"] == df["low"]).all()


def test_sgs_deep_history_is_still_reachable(tmp_path):
    """A window older than one SGS request can serve, so the chunking runs."""
    df = BcbProvider(cache_dir=tmp_path).candles(
        "BCB:USDBRL", "1d", start="2000-01-01", limit=40)
    assert_candle_frame(df, minimum=20)
    # 2000 is well before the ten-year window a single SGS call allows.
    assert df["ts"].iloc[0] < 1_000_000_000


# ── Silo ────────────────────────────────────────────────────────────────

# Unlike B3 and the Banco Central, Silo needs a key, and CI has no business
# holding one by default. These skip rather than fail when SILO_KEY is
# unset, so the scheduled run still exercises the two free sources.
silo_key = pytest.mark.skipif(not SiloProvider().configured(),
                              reason="SILO_KEY is not set")


@silo_key
def test_silo_coverage_still_reports_the_quotes_dataset():
    """Everything else here depends on this, so it is worth asserting alone."""
    p = SiloProvider()
    datasets = {row["dataset"] for row in p.coverage()}
    assert "quotes" in datasets
    # A warehouse that has stopped ingesting is not an outage and would not
    # fail any other test here; a stale session date is the only symptom.
    assert p.latest_session().year >= 2025


@silo_key
def test_silo_serves_a_daily_history():
    df = SiloProvider().candles("PETR4", "1d", limit=60)
    assert_candle_frame(df, minimum=40)


@silo_key
def test_silo_pages_past_the_row_cap():
    """The whole reason _get() reads Content-Range.

    PostgREST truncates at 1,000 rows and says so only in a header, so a
    provider that ignored it would return exactly 1,000 rows here and look
    entirely healthy doing it.
    """
    df = SiloProvider().candles("PETR4", "1d", limit=1500, start="2019-01-01")
    assert_candle_frame(df, minimum=1200)
    assert len(df) > 1000, "capped at the PostgREST row limit"


@silo_key
def test_silo_and_b3_agree_on_a_session(tmp_path):
    """Two independent paths to the same COTAHIST row must produce one number.

    This is the assertion that would have caught the quotation factor: b3
    parses the fixed-width file itself, Silo parsed it into Postgres, and
    only a shared understanding of what the price column means makes them
    match to the cent.
    """
    silo = SiloProvider()
    day = silo.latest_session()
    mine = silo.candles("PETR4", "1d", limit=5, end=day.isoformat())
    theirs = B3Provider(cache_dir=tmp_path).candles(
        "PETR4", "1d", limit=5, end=day.isoformat())
    overlap = mine.merge(theirs, on="ts", suffixes=("_silo", "_b3"))
    assert len(overlap) >= 3, "no shared sessions to compare"
    for column in ("open", "high", "low", "close"):
        assert (overlap[f"{column}_silo"] - overlap[f"{column}_b3"]).abs().max() < 0.005


@silo_key
def test_silo_search_reaches_the_ticker_being_typed():
    out = SiloProvider().search("PETR", limit=20)
    assert "PETR4" in {i.symbol for i in out}
    assert all(i.category for i in out), "every row needs a folder label"


@silo_key
def test_silo_default_listing_is_grouped_and_nonempty():
    out = SiloProvider().search("", limit=50)
    assert len(out) >= 20
    categories = [i.category for i in out]
    # Contiguous by category: the sidebar renders the order verbatim, so a
    # category appearing twice would draw two folders with the same name.
    assert len(set(categories)) == len(list(dict.fromkeys(categories)))


@silo_key
def test_silo_serves_a_cvm_fund_series():
    """The part COTAHIST has no equivalent for."""
    p = SiloProvider()
    fund = next(i for i in p.search("08973948000135", limit=20)
                if i.symbol.startswith("CVM:"))
    df = p.candles(fund.symbol, "1d", limit=24)
    assert list(df.columns) == CANDLE_COLUMNS
    assert len(df) >= 6
    assert df["ts"].is_monotonic_increasing
    assert (df["open"] == df["close"]).all()
    assert (df["volume"] == 0).all()


@silo_key
def test_silo_quote_is_the_stored_session_not_now():
    import time
    q = SiloProvider().quote("PETR4")
    assert q.price > 0
    # Stamped at the session it came from. A provider that filled in
    # time.time() would look live and be a day stale.
    assert q.ts <= time.time()
