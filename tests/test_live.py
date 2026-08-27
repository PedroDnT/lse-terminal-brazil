"""Live tests: the real B3 and Banco Central endpoints, over the network.

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

from lse_terminal_brazil import B3Provider, BcbProvider
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
