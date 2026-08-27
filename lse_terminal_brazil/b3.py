"""B3 (Brasil, Bolsa, Balcão) market data, straight from the exchange.

Brazil has no free vendor feed worth trusting, but B3 publishes its own
data and asks nothing for it: no key, no account, no vendor in the middle.
This provider reads exactly those public sources, so a Brazilian user gets
a working terminal on a fresh install with nothing to sign up for.

Two sources, split by what each one is actually good at:

* **COTAHIST** (``bvmf.bmfbovespa.com.br/InstDados/SerHist``) is the
  official end-of-day series for the cash segment, one fixed-width record
  per instrument per session, published as a daily, monthly and yearly
  ZIP. It carries a true open/high/low/close plus traded shares, and the
  yearly files go back to 1986. This is the daily history, and the daily
  file (a few hundred KB) doubles as the instrument catalog: whatever
  traded last session is what exists.
* **cotacao.b3.com.br** is the JSON feed behind B3's own quote pages: a
  delayed last price for any listed symbol, and the current session's
  minute-by-minute prints. This is the intraday path and the quote, and
  unlike COTAHIST it also answers for BM&F contracts (WIN, WDO) and the
  indices, which the cash-segment files do not cover.

What that split means for a caller, stated plainly rather than discovered:
daily bars exist only for instruments COTAHIST carries, intraday exists
only for the session in progress, and B3's public quote is delayed by
about 15 minutes. B3 publishes no intraday archive at all, so deep
intraday history is not something any free source here can give: it has
to be recorded as it happens, or imported from a file.

Everything downloaded is cached under ``<config_dir>/b3`` and parsed once
into parquet; nothing about the user travels the other way, because these
are plain file downloads with no session and no identity attached.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import numpy as np
import pandas as pd

from lse_terminal.contracts import CANDLE_COLUMNS, Instrument, NotSupported, Provider, Quote
from lse_terminal_brazil import _config as cfg

SERHIST_URL = "https://bvmf.bmfbovespa.com.br/InstDados/SerHist"
QUOTE_URL = "https://cotacao.b3.com.br/mds/api/v1"

# B3 sits at UTC-3 the whole year: Brazil abolished daylight saving in 2019,
# and the intraday path only ever asks about the session in progress, so a
# fixed offset is exact here and saves depending on a tz database that
# Windows does not ship.
BRT = timezone(timedelta(hours=-3))

_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "1d": 86400}

# CODBDI (COTAHIST positions 11-12) is the exchange's own instrument class,
# and it is the only field that separates a FII from an ETF from a BDR --
# the ticker suffix does not (HGLG11, BOVA11 and KLBN11 are a fund, an ETF
# and a unit). Mapped straight to the display-ready group label the
# Provider contract asks for; unlisted codes are the boards a chart cannot
# use (odd lots, forwards, exercises) and are dropped from the catalog.
_CODBDI_CATEGORY = {
    "02": "B3 Ações", "05": "B3 Ações", "06": "B3 Ações",
    "07": "B3 Ações", "08": "B3 Ações",
    "12": "B3 Fundos Imobiliários",
    "13": "B3 Fiagro",
    "14": "B3 ETFs",
    "34": "B3 BDRs", "35": "B3 BDRs", "36": "B3 BDRs",
    "10": "B3 Direitos e Recibos", "22": "B3 Direitos e Recibos",
    "74": "B3 Opções de compra", "78": "B3 Opções de compra",
    "75": "B3 Opções de venda", "82": "B3 Opções de venda",
}

# Sidebar order for an empty query. Options are excluded from that listing
# entirely: a single session lists ~15k of them against ~1.4k cash
# instruments, so they would bury everything else in a folder nobody scrolls.
# A typed query still reaches them.
_CATEGORY_ORDER = [
    "B3 Ações", "B3 ETFs", "B3 Fundos Imobiliários", "B3 Fiagro",
    "B3 BDRs", "B3 Direitos e Recibos",
]
_OPTION_CATEGORIES = ("B3 Opções de compra", "B3 Opções de venda")

# Futures and indices: not in COTAHIST (it is the cash segment), but B3's
# quote feed answers for them, so they are offered with the intraday and
# quote surfaces and no daily history. The month letters are the standard
# contract codes; the roll rules are B3's own (index futures expire on the
# Wednesday nearest the 15th of even months, FX futures on the first
# business day of every month).
_MONTH_CODE = "FGHJKMNQUVXZ"
_INDEX_SYMBOLS = [
    ("IBOV", "Índice Bovespa"),
    ("IBXX", "Índice Brasil 100"),
    ("SMLL", "Índice Small Cap"),
    ("IDIV", "Índice Dividendos"),
    ("IFIX", "Índice de Fundos Imobiliários"),
]
_FUTURES_ROOTS = [
    ("WIN", "Ibovespa Futuro Mini", "index"),
    ("IND", "Ibovespa Futuro", "index"),
    ("WDO", "Dólar Futuro Mini", "fx"),
    ("DOL", "Dólar Futuro", "fx"),
]
_FUTURES_CATEGORY = "B3 Futuros e Índices"


class B3Error(Exception):
    """A B3 source could not answer (network, or a file B3 has not published)."""


# ── COTAHIST parsing ────────────────────────────────────────────────────
# The layout is fixed at 245 characters plus CRLF, every record the same
# width, which is what makes the numpy view below legal: reshaping the raw
# bytes into a (rows, 247) grid turns every field into a column slice and
# parses a 4-million-record yearly file in about a second, where a Python
# loop over the same file takes minutes.
_RECLEN_DATA = 245
_PRICE_SCALE = 100.0


def _int_field(grid: np.ndarray, start: int, stop: int) -> np.ndarray:
    """Digits at [start:stop) of every record, as int64."""
    digits = grid[:, start:stop].astype(np.int64) - ord("0")
    # A blank field (all spaces) reads as -16 per column; clamping to zero
    # is right for every numeric field COTAHIST has, all of which are
    # zero-filled when absent.
    np.clip(digits, 0, 9, out=digits)
    weights = 10 ** np.arange(stop - start - 1, -1, -1, dtype=np.int64)
    return digits @ weights


def _text_field(grid: np.ndarray, start: int, stop: int) -> np.ndarray:
    block = np.ascontiguousarray(grid[:, start:stop])
    return np.char.strip(np.char.decode(block.view(f"S{stop - start}").ravel(), "latin-1"))


def _epoch_seconds(yyyymmdd: np.ndarray) -> np.ndarray:
    """COTAHIST's YYYYMMDD integers as epoch seconds at 00:00 UTC.

    Done with integer arithmetic (Howard Hinnant's days-from-civil) rather
    than through pandas datetimes: the conversion is exact, it is the same
    answer on every pandas version -- 2.x and 3.x disagree about the
    resolution a parsed datetime column carries, and a wrong guess there
    silently scales every timestamp -- and it is fast enough to run over a
    four-million-record yearly file.

    A daily bar is stamped at midnight UTC of its session date. B3 is at
    UTC-3 year round, so this is a date label rather than the session's
    opening instant, which is the convention every daily series here uses.
    """
    y = yyyymmdd // 10000
    m = (yyyymmdd // 100) % 100
    d = yyyymmdd % 100
    y = y - (m <= 2)                      # March-based year
    era = np.where(y >= 0, y, y - 399) // 400
    yoe = y - era * 400                   # [0, 399]
    doy = (153 * (m + np.where(m > 2, -3, 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return ((era * 146097 + doe - 719468) * 86400).astype("int64")


def parse_cotahist(raw: bytes) -> pd.DataFrame:
    """One COTAHIST TXT payload as a tidy frame, one row per instrument-session.

    Returns the columns the rest of this module needs: ``symbol``, ``ts``
    (session date at 00:00 UTC), OHLCV, and the classification fields
    (``codbdi``, ``name``, ``spec``, ``isin``). Header and trailer records
    are dropped; so is anything that is not a detail record.
    """
    reclen = raw.find(b"\n") + 1
    if reclen < _RECLEN_DATA:
        raise B3Error("COTAHIST payload is not fixed-width as expected")
    usable = len(raw) // reclen * reclen
    grid = np.frombuffer(raw[:usable], dtype=np.uint8).reshape(-1, reclen)
    # 0x30 0x31 == "01", the detail record; "00" is the header, "99" the trailer.
    detail = (grid[:, 0] == 0x30) & (grid[:, 1] == 0x31)
    grid = grid[detail]
    if not len(grid):
        return pd.DataFrame(columns=["symbol", "ts", *CANDLE_COLUMNS[1:],
                                     "codbdi", "name", "spec", "isin"])

    frame = pd.DataFrame({
        "symbol": _text_field(grid, 12, 24),
        "ts": _epoch_seconds(_int_field(grid, 2, 10)),
        "open": _int_field(grid, 56, 69) / _PRICE_SCALE,
        "high": _int_field(grid, 69, 82) / _PRICE_SCALE,
        "low": _int_field(grid, 82, 95) / _PRICE_SCALE,
        "close": _int_field(grid, 108, 121) / _PRICE_SCALE,
        # Traded shares/contracts, not the financial total: "volume" means
        # quantity everywhere else in the terminal, and matching that is what
        # lets a B3 chart's volume pane read like every other chart's.
        "volume": _int_field(grid, 152, 170).astype("float64"),
        "codbdi": _text_field(grid, 10, 12),
        "name": _text_field(grid, 27, 39),
        "spec": _text_field(grid, 39, 49),
        "isin": _text_field(grid, 230, 242),
    })
    # An auction that never printed leaves a zero-priced record behind. It is
    # not a bar, and left in it drags a chart's low to zero.
    return frame[frame["close"] > 0].reset_index(drop=True)


class B3Provider(Provider):
    name = "b3"
    title = "B3 (Brasil, Bolsa, Balcão)"
    timeframes = ["1m", "5m", "15m", "30m", "1h", "1d"]
    # The user's own free public source, not something the fleet directory
    # lists as a vendor, so /api/providers must never gate it behind one.
    is_custom = True

    def __init__(self, cache_dir: Path | None = None, fetch=None):
        # No network and no disk here: the registry builds every provider at
        # boot, and a source that phoned home on construction would make the
        # terminal's start time hostage to B3 being up.
        self._cache_dir = cache_dir
        self._fetch = fetch or _http_get
        self._lock = threading.Lock()
        self._catalog: tuple[float, list[Instrument]] | None = None

    # ── cache ───────────────────────────────────────────────────────────

    def cache_dir(self) -> Path:
        # Coerced rather than assumed: a caller passing a plain string is
        # the obvious thing to do, and without this it fails much later
        # inside a download as an AttributeError that reads like the source
        # is down.
        d = Path(self._cache_dir) if self._cache_dir else (cfg.config_dir() / "b3")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _cached_cotahist(self, kind: str, stamp: str) -> pd.DataFrame:
        """One COTAHIST file as a frame, downloaded and parsed at most once.

        ``kind`` is B3's own letter: D a single session, M a month, A a year.
        The parsed parquet is what gets kept, not the ZIP: a yearly file is
        90 MB of fixed-width text that reduces to a fraction of that as
        columns, and reparsing it on every chart would cost a second each time.
        """
        path = self.cache_dir() / f"cotahist_{kind}{stamp}.parquet"
        if path.exists():
            return pd.read_parquet(path)
        # A session B3 never published (a holiday) is a permanent answer, and
        # without recording it every daily request would spend a round trip
        # rediscovering that Carnival happened.
        absent = path.with_suffix(".absent")
        if absent.exists():
            raise B3Error(f"B3 published no COTAHIST_{kind}{stamp}")
        try:
            raw = self._fetch(f"{SERHIST_URL}/COTAHIST_{kind}{stamp}.ZIP")
        except Exception:
            if kind == "D" and self._settled(stamp):
                absent.touch()
            raise
        with zipfile.ZipFile(BytesIO(raw)) as z:
            # The member name is NOT stable across B3's history: recent
            # archives hold "COTAHIST_A2015.TXT", but the older ones hold
            # "COTAHIST.A2000" -- a dot, and no extension at all. Matching
            # on ".TXT" silently lost every year before that changeover.
            # The largest member is the data file under either convention,
            # and under any future one.
            members = [i for i in z.infolist() if not i.is_dir() and i.file_size]
            if not members:
                raise B3Error(f"COTAHIST_{kind}{stamp}.ZIP is empty")
            biggest = max(members, key=lambda i: i.file_size)
            frame = parse_cotahist(z.read(biggest))
        frame.to_parquet(path, index=False)
        return frame

    @staticmethod
    def _settled(stamp: str) -> bool:
        """True once a session date is old enough that its absence is final.

        B3 posts a session's file that evening, so a file missing for today
        or yesterday may simply not be written yet; three days on, missing
        means the market was closed.
        """
        try:
            day = datetime.strptime(stamp, "%d%m%Y").date()
        except ValueError:
            return False
        return (datetime.now(BRT).date() - day) > timedelta(days=3)

    # ── catalog ─────────────────────────────────────────────────────────

    def _latest_session(self) -> pd.DataFrame:
        """The most recent published daily file.

        B3 publishes a session's file that evening, so "today" is usually
        absent and so is every weekend and holiday. Walking back day by day
        finds the newest one that exists without needing a holiday calendar;
        the window is generous enough to clear Carnival week.
        """
        today = datetime.now(BRT).date()
        errors = []
        for back in range(0, 12):
            day = today - timedelta(days=back)
            if day.weekday() >= 5:
                continue
            stamp = day.strftime("%d%m%Y")
            try:
                frame = self._cached_cotahist("D", stamp)
            except (B3Error, OSError, ValueError) as e:
                # A session B3 has not published, a holiday, or the network
                # being unreachable. Anything else -- a TypeError, an
                # AttributeError -- is a bug in this code, and reporting it
                # as "the exchange published nothing" would send whoever
                # hits it looking in entirely the wrong place.
                errors.append(f"{day}: {e}")
                continue
            if len(frame):
                return frame
        raise B3Error("no COTAHIST daily file found in the last 12 days: "
                      + "; ".join(errors[:3]))

    def _catalog_rows(self) -> list[Instrument]:
        with self._lock:
            hit = self._catalog
            # One session's worth of listings cannot change until the next
            # session, so an hour is a short TTL, not a long one.
            if hit and time.time() - hit[0] < 3600:
                return hit[1]
        frame = self._latest_session()
        frame = frame.assign(category=frame["codbdi"].map(_CODBDI_CATEGORY))
        frame = frame[frame["category"].notna()]
        frame = frame.drop_duplicates(subset="symbol", keep="first")
        rows: list[Instrument] = []
        for category in [*_CATEGORY_ORDER, *_OPTION_CATEGORIES]:
            group = frame[frame["category"] == category].sort_values("symbol")
            for r in group.itertuples():
                klass = (r.spec or "").split()
                label = f"{r.name} {klass[0]}".strip() if klass else r.name
                rows.append(Instrument(
                    symbol=r.symbol, name=label, category=category,
                    provider=self.name,
                    meta={"isin": r.isin, "codbdi": r.codbdi}))
        rows.extend(self._derivative_rows())
        with self._lock:
            self._catalog = (time.time(), rows)
        return rows

    def _derivative_rows(self, today: date | None = None) -> list[Instrument]:
        """Front-month futures and the headline indices.

        These are not in COTAHIST at all, and they are the instruments most
        Brazilian systematic traders actually watch, so they are offered
        from the quote feed with the surfaces it can honestly serve: a
        delayed quote and the session in progress, no daily history.
        """
        today = today or datetime.now(BRT).date()
        rows = [Instrument(symbol=s, name=n, category=_FUTURES_CATEGORY,
                           provider=self.name, meta={"intraday_only": True})
                for s, n in _INDEX_SYMBOLS]
        for root, label, kind in _FUTURES_ROOTS:
            code = front_month(kind, today)
            rows.append(Instrument(
                symbol=f"{root}{code}", name=f"{label} ({code})",
                category=_FUTURES_CATEGORY, provider=self.name,
                meta={"intraday_only": True, "root": root}))
        return rows

    def search(self, query: str = "", limit: int = 50) -> list[Instrument]:
        q = query.strip().upper()
        rows = self._catalog_rows()
        if not q:
            rows = [i for i in rows if i.category not in _OPTION_CATEGORIES]
        else:
            rows = [i for i in rows if q in i.symbol.upper() or q in i.name.upper()]
        return rows[: max(1, int(limit))]

    # ── candles ─────────────────────────────────────────────────────────

    def candles(self, symbol: str, timeframe: str, limit: int = 500,
                start: str | None = None, end: str | None = None) -> pd.DataFrame:
        if timeframe not in _TF_SECONDS:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        sym = symbol.strip().upper()
        if timeframe == "1d":
            df = self._daily(sym, limit=limit, start=start, end=end)
        else:
            df = self._intraday(sym, timeframe)
            if start:
                df = df[df["ts"] >= int(pd.Timestamp(start, tz="UTC").timestamp())]
            if end:
                df = df[df["ts"] <= int(pd.Timestamp(end, tz="UTC").timestamp())]
        n = max(1, int(limit))
        # Same law as the rest of the terminal: a start-anchored query pages
        # forward from that date, everything else returns the newest N.
        df = df.head(n) if start else df.tail(n)
        return df[CANDLE_COLUMNS].reset_index(drop=True)

    # COTAHIST comes in three grains and they cost wildly different amounts:
    # a session is a few hundred KB, a month around 15 MB, a year around
    # 90 MB. Fetching the right grain is the difference between a chart that
    # appears and one that stalls, so a daily request is turned into the
    # cheapest set of files that covers its window rather than always
    # reaching for the year.
    _SESSIONS_PER_YEAR = 246
    # A past year is worth its single yearly file once this many of its
    # months are wanted; below that the monthly files add up to less.
    _MONTHS_BEFORE_YEARLY = 7
    # Likewise within a month: a handful of sessions is cheaper one file at
    # a time than pulling the whole month for the sake of two days of it.
    _DAYS_BEFORE_MONTHLY = 8

    def _window(self, limit: int, start: str | None, end: str | None) -> tuple[date, date]:
        """The calendar window a daily request covers.

        With no explicit start, ``limit`` counts trading sessions, so the
        calendar span is longer than the bar count: ~246 sessions a year,
        plus a fortnight of slack so a run of holidays cannot leave the
        request short of the bars it asked for.
        """
        last = pd.Timestamp(end).date() if end else datetime.now(BRT).date()
        if start:
            return pd.Timestamp(start).date(), last
        days = int(max(1, int(limit)) * 365 / self._SESSIONS_PER_YEAR) + 14
        return last - timedelta(days=days), last

    def file_plan(self, first: date, last: date,
                  today: date | None = None) -> list[tuple[str, str]]:
        """The COTAHIST files covering ``[first, last]``, at the cheapest grain.

        Returned as B3's own (kind, stamp) pairs: ``("A", "2024")``,
        ``("M", "032025")``, ``("D", "12082026")``. A month that has not
        finished is served by its individual sessions, because B3 writes the
        monthly file only once the month is over; the same rule one level up
        makes the running year a set of months rather than a yearly file.
        """
        today = today or datetime.now(BRT).date()
        plan: list[tuple[str, str]] = []
        by_year: dict[int, list[int]] = {}
        year, month = first.year, first.month
        while (year, month) <= (last.year, last.month):
            by_year.setdefault(year, []).append(month)
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)

        for y in sorted(by_year):
            wanted = by_year[y]
            if y < today.year and len(wanted) >= self._MONTHS_BEFORE_YEARLY:
                plan.append(("A", str(y)))
                continue
            for m in wanted:
                day = max(first, date(y, m, 1))
                stop = min(last, today, _month_end(y, m))
                sessions = [d for d in _days(day, stop) if d.weekday() < 5]
                # "Complete" means B3 has closed the book on that month; the
                # current month always has to be assembled from its sessions,
                # and so does any month the window barely clips.
                complete = (y, m) < (today.year, today.month)
                if complete and len(sessions) >= self._DAYS_BEFORE_MONTHLY:
                    plan.append(("M", f"{m:02d}{y}"))
                    continue
                plan.extend(("D", d.strftime("%d%m%Y")) for d in sessions)
        return plan

    def _daily(self, symbol: str, limit: int, start: str | None,
               end: str | None) -> pd.DataFrame:
        first, last = self._window(limit, start, end)
        frames, errors = [], []
        loaded = 0
        # A plan entry that 404s is not always a holiday. B3 stopped
        # publishing monthly files somewhere back in its history -- 2015 has
        # them, 2000 does not -- and the yearly archive is the only grain
        # that covers those years. Rather than hardcode a cutoff year that
        # is not documented anywhere and could move, a missing month or
        # session falls back to that year's yearly file, which is
        # self-correcting wherever the real boundary sits. Old yearly files
        # are also small (2000 is 6.7 MB against 2025's 89 MB), so this is
        # a cheap fallback for exactly the years that need it.
        queue = list(self.file_plan(first, last))
        planned = set(queue)
        while queue:
            kind, stamp = queue.pop(0)
            try:
                frame = self._cached_cotahist(kind, stamp)
            except Exception as e:
                errors.append(f"{kind}{stamp}: {e}")
                fallback = _yearly_fallback(kind, stamp)
                if fallback and fallback not in planned:
                    planned.add(fallback)
                    queue.append(fallback)
                continue
            loaded += 1
            hit = frame[frame["symbol"] == symbol]
            if len(hit):
                frames.append(hit)
        if not frames:
            if loaded:
                # Files came back fine; this instrument is simply not in
                # them, which is a coverage question, not a fetch failure.
                raise ValueError(
                    f"no B3 end-of-day history for {symbol!r} in "
                    f"{first}..{last}. COTAHIST covers the cash segment "
                    f"(ações, units, ETFs, FIIs, Fiagro, BDRs, opções); "
                    f"BM&F futures and the index levels are not in it at "
                    f"all, and are intraday-only on B3's free feed.")
            # Nothing loaded at all: say that, rather than blaming the
            # instrument for what is a download problem.
            raise ValueError(
                f"could not read any COTAHIST file covering {first}..{last} "
                f"for {symbol!r}"
                + (f" — first error: {errors[0]}" if errors else ""))
        out = pd.concat(frames, ignore_index=True)
        out = out.drop_duplicates(subset="ts", keep="last").sort_values("ts")
        if start:
            out = out[out["ts"] >= int(pd.Timestamp(start, tz="UTC").timestamp())]
        if end:
            out = out[out["ts"] <= int(pd.Timestamp(end, tz="UTC").timestamp())]
        return out

    def _intraday(self, symbol: str, timeframe: str) -> pd.DataFrame:
        """The session in progress, resampled from B3's minute prints.

        The feed gives a close per minute and nothing else, so a 1m bar is
        that print in all four OHLC slots and a wider bar is a real
        open/high/low/close built from the minutes inside it. Volume is not
        published on this feed and stays zero rather than being invented.
        """
        payload = self._quote_json(f"DailyFluctuationHistory/{symbol}")
        floor = payload.get("TradgFlr") or {}
        prints = ((floor.get("scty") or {}).get("lstQtn")) or []
        session = floor.get("date")
        if not prints or not session:
            raise ValueError(f"B3 published no intraday prints for {symbol!r} today")
        day = date.fromisoformat(session)
        rows = []
        for p in prints:
            hh, mm, ss = (int(x) for x in str(p["dtTm"]).split(":"))
            at = datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=BRT)
            rows.append((int(at.timestamp()), float(p["closPric"])))
        series = pd.DataFrame(rows, columns=["ts", "price"]).sort_values("ts")
        step = _TF_SECONDS[timeframe]
        bucket = series["ts"] // step * step
        bars = series.groupby(bucket)["price"].agg(["first", "max", "min", "last"])
        return pd.DataFrame({
            "ts": bars.index.astype("int64"),
            "open": bars["first"].to_numpy(),
            "high": bars["max"].to_numpy(),
            "low": bars["min"].to_numpy(),
            "close": bars["last"].to_numpy(),
            "volume": 0.0,
        }).reset_index(drop=True)

    # ── quote ───────────────────────────────────────────────────────────

    def _quote_json(self, path: str) -> dict:
        payload = json.loads(self._fetch(f"{QUOTE_URL}/{path}"))
        status = (payload.get("BizSts") or {}).get("cd")
        if status and status != "OK":
            raise ValueError((payload.get("BizSts") or {}).get("desc")
                             or f"B3 declined the request for {path}")
        return payload

    def quote(self, symbol: str) -> Quote:
        """Last price from B3's public feed, which is delayed about 15 minutes.

        No bid/ask: B3 does not publish the book on this feed, and the
        terminal's spread synthesizer only has trade prints to work from,
        so leaving them unset is the honest answer.
        """
        payload = self._quote_json(f"instrumentQuotation/{symbol.strip().upper()}")
        trades = payload.get("Trad") or []
        if not trades:
            raise NotSupported(f"B3 has no quote for {symbol!r}")
        q = ((trades[0].get("scty") or {}).get("SctyQtn")) or {}
        price = q.get("curPrc")
        if price is None:
            raise NotSupported(f"B3 has no last price for {symbol!r}")
        return Quote(symbol=symbol.strip().upper(), price=float(price),
                     ts=time.time())

    def configured(self) -> bool:
        # Nothing to configure: these are public files. A source that needs
        # only a working network connection is configured by definition.
        return True


def front_month(kind: str, today: date) -> str:
    """B3's contract code for the front month of a futures root.

    ``index`` follows the Ibovespa contracts (even months, expiring on the
    Wednesday nearest the 15th); ``fx`` follows the dollar contracts (every
    month, expiring on the first business day of the contract month, so the
    front month is simply the next one). The code is a month letter plus
    two digits of the year: WINV26 is October 2026.
    """
    if kind == "fx":
        year, month = today.year, today.month + 1
        if month > 12:
            year, month = year + 1, 1
        return f"{_MONTH_CODE[month - 1]}{year % 100:02d}"
    year, month = today.year, today.month
    for _ in range(14):
        if month % 2 == 0 and today <= _index_expiry(year, month):
            return f"{_MONTH_CODE[month - 1]}{year % 100:02d}"
        month += 1
        if month > 12:
            year, month = year + 1, 1
    raise ValueError(f"no front month found for {kind} at {today}")


def _yearly_fallback(kind: str, stamp: str) -> tuple[str, str] | None:
    """The yearly archive covering a monthly or daily stamp that was missing.

    ``("M", "012000") -> ("A", "2000")``; ``("D", "03012000") -> ("A", "2000")``.
    Yearly files are the one grain B3 has published for every year, so this
    is the grain to retry with. Returns None for a stamp already yearly, or
    one in the running year, whose archive B3 has not written yet.
    """
    if kind == "M":
        year = int(stamp[2:])
    elif kind == "D":
        year = int(stamp[4:])
    else:
        return None
    return None if year >= datetime.now(BRT).year else ("A", str(year))


def _month_end(year: int, month: int) -> date:
    return (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))


def _days(first: date, last: date):
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


def _index_expiry(year: int, month: int) -> date:
    """The Wednesday nearest the 15th, B3's expiry day for index futures."""
    fifteenth = date(year, month, 15)
    # weekday(): Monday is 0, so Wednesday is 2. Ties (the 15th falling on a
    # Saturday or Sunday) resolve to the earlier Wednesday, which is B3's rule.
    offset = (2 - fifteenth.weekday()) % 7
    later = fifteenth + timedelta(days=offset)
    earlier = later - timedelta(days=7)
    return earlier if (fifteenth - earlier) <= (later - fifteenth) else later


def _http_get(url: str, timeout: float = 60.0, attempts: int = 3) -> bytes:
    """One GET, retried, with the response length checked against the header.

    Separate from the providers so tests can hand them a stub instead. The
    retry is not defensive habit: a COTAHIST yearly archive is tens of
    megabytes (2025 is 89 MB) and a dropped connection part-way through
    yields a short body, which surfaces later as a corrupt-ZIP error with
    nothing pointing at the download. Comparing what arrived against
    Content-Length catches it where it happens, and a truncated read is
    exactly the kind of failure a second attempt fixes.
    """
    last = None
    for attempt in range(1, max(1, attempts) + 1):
        req = urllib.request.Request(url, headers={
            # B3's edge answers a bare urllib with a challenge page; a
            # browser User-Agent is what makes these public files
            # actually reachable.
            "User-Agent": "Mozilla/5.0 (compatible; lse-terminal)",
            "Accept": "*/*",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                declared = r.headers.get("Content-Length")
                body = r.read()
            if declared is not None and len(body) != int(declared):
                raise B3Error(
                    f"short read: got {len(body)} bytes of {declared}")
            return body
        except urllib.error.HTTPError:
            # A 404 is an answer -- the file does not exist -- and retrying
            # it just spends time to be told the same thing.
            raise
        except Exception as e:
            last = e
            if attempt == attempts:
                break
    raise B3Error(f"could not download {url}: {last}") from last
