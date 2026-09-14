"""Silo: a warehouse view of B3 and the CVM, behind one key.

This is the third source in the package and the only one that needs
configuring, so the first thing to say is why it exists at all when ``b3``
already reads the exchange's own files for free.

``b3`` fetches COTAHIST: the authoritative end-of-day archive, published as
fixed-width text in daily, monthly and yearly ZIPs. It is complete back to
1986 and costs nothing, but the grain is the file. Ten years of one ticker
means ten yearly archives -- around 400 MB of text parsed to find the 2,500
rows that were wanted. The first such chart takes minutes. Afterwards it is
cached and instant, which is the bargain that provider makes.

Silo has already done that parsing. It keeps COTAHIST in Postgres and
serves it over PostgREST, so ten years of one ticker is a row filter: two
HTTP requests, well under a second, on a cold start. It also carries
something COTAHIST simply does not have -- the CVM's monthly fund
statistics, for every FI, FII, FIDC, FIP and Fiagro in Brazil: net assets,
quota, unit-holders, subscriptions, redemptions, delinquency, yield. No
exchange file contains those, because funds report them to the regulator
rather than to the exchange.

So the split is: ``b3`` is the free, complete, slow-on-first-read archive
and needs nothing; ``silo`` is fast and reaches the fund data, and needs a
key. They are deliberately separate providers rather than one with a
fallback, because which one answered changes what the data means -- Silo's
quotes history begins in 2019, COTAHIST's in 1986.

Configuring it
--------------

Set ``SILO_KEY``, or write ``{"key": "..."}`` to ``<config_dir>/silo.json``.
Without one, :meth:`SiloProvider.configured` returns False and the terminal
shows the source as needing setup rather than as broken. ``SILO_URL``
points the provider at a different deployment; it defaults to the public
one.

The key is never read from this file or any other default: no credential
ships in this package. Silo publishes a shared testing key in its own docs,
which is fine for trying the API by hand and wrong to bake into an
installed terminal -- it is one anonymous credential shared by every reader
of that page.

What the numbers mean
---------------------

Prices are divided by the published quotation factor, exactly as ``b3``
does, so the two providers chart a paper identically. That matters for the
handful of papers quoted per lot of a thousand: FNAM11's stored close of
0.16 is the price of a thousand units, and only 0.00016 reconciles against
the session's own published financial volume. Silo exposes the same
quantity as ``close_unit``; this provider derives it for the full OHLC bar
rather than the close alone.

Nothing here is adjusted for corporate actions, because COTAHIST is not: a
split shows up as a gap. Fund metrics are monthly and arrive on the
``1d`` timeframe as a sparser line, the same arrangement ``bcb`` uses for
monthly macro series.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

import pandas as pd

from lse_terminal.contracts import CANDLE_COLUMNS, Instrument, NotSupported, Provider, Quote
from lse_terminal_brazil import _config as cfg

# The public deployment. Not a secret -- a PostgREST base URL is useless
# without a key -- and overridable for anyone running their own.
SILO_URL = "https://zcjbtpxuhdekpwcxmepn.supabase.co/rest/v1"

# PostgREST truncates every response at its project-wide `db-max-rows`,
# which is 1000 here, and does it silently: a series cut short is
# indistinguishable from a complete one by its contents. Only the
# Content-Range header says which happened, so every read goes through
# _get(), which reads that header and keeps asking.
_PAGE = 1000

# How long the default instrument listing is reused. It is one session's
# most-traded papers, so it changes once a day at most.
_CATALOG_TTL_S = 6 * 3600

# Silo's asset_class vocabulary mapped to display-ready folder labels.
# Prefixed, because a terminal with both providers installed lists B3's
# "Ações" and Silo's side by side and they do not cover the same years.
_CATEGORY = {
    "equity": "Silo — Ações",
    "unit": "Silo — Units",
    "fund_quota": "Silo — ETFs e Fundos listados",
    "bdr": "Silo — BDRs",
    "index": "Silo — Índices",
    "right": "Silo — Direitos e Recibos",
    "bonus": "Silo — Direitos e Recibos",
    "cash_security": "Silo — Outros papéis",
    "derivative": "Silo — Derivativos",
}
_FUND_CATEGORY = "Silo — Fundos (CVM)"

# Order the sidebar renders those folders in for an empty query.
_CATEGORY_ORDER = [
    "Silo — Ações", "Silo — ETFs e Fundos listados", "Silo — Units",
    "Silo — BDRs", "Silo — Índices", "Silo — Direitos e Recibos",
    "Silo — Outros papéis", "Silo — Derivativos",
]

# A fund reports several numbers a month and a candle holds one, so the
# symbol carries which: CVM:<cnpj> is net assets, CVM:<cnpj>.COTISTAS the
# unit-holder count, and so on. Suffixes are Portuguese because the people
# reading a CVM series are; each maps to the column fund_nav returns.
FUND_PREFIX = "CVM:"
_FUND_METRICS: dict[str, tuple[str, str]] = {
    "": ("nav", "patrimônio líquido (R$)"),
    "PL": ("nav", "patrimônio líquido (R$)"),
    "COTA": ("quota", "valor da cota"),
    "COTISTAS": ("quotaholders", "número de cotistas"),
    "CAPTACAO": ("inflows", "captação no mês (R$)"),
    "RESGATE": ("redemptions", "resgates no mês (R$)"),
    "INADIMPLENCIA": ("delinquency", "carteira inadimplente (R$)"),
    "RENDIMENTO": ("monthly_yield", "rendimento mensal (%)"),
    "ATIVOS": ("assets", "ativo total (R$)"),
}
# The order the variants are offered in when a query resolves to one fund.
_FUND_SUFFIX_ORDER = ["", "COTA", "COTISTAS", "CAPTACAO", "RESGATE",
                      "RENDIMENTO", "INADIMPLENCIA", "ATIVOS"]


class SiloError(Exception):
    """Silo could not answer: no key, a rejected key, or the service down."""


class SiloProvider(Provider):
    name = "silo"
    title = "Silo (B3 + CVM)"
    # Silo stores end-of-day rows and monthly fund reports; there is no
    # intraday grain to offer. Monthly series answer here too and simply
    # return monthly prints, as in the bcb provider.
    timeframes = ["1d"]
    # Reached directly with the user's own key, so the fleet directory has
    # no say in whether it is listed.
    is_custom = True

    def __init__(self, url: str | None = None, key: str | None = None, fetch=None):
        # No network and no disk here: the registry builds every provider at
        # boot, and a source that authenticated on construction would make
        # the terminal's start time hostage to Silo being up.
        self._url = (url or os.environ.get("SILO_URL") or SILO_URL).rstrip("/")
        self._key_override = key
        self._fetch = fetch or _http_json
        self._lock = threading.Lock()
        self._catalog: tuple[float, list[Instrument]] | None = None
        self._session: tuple[float, date] | None = None

    # ── configuration ───────────────────────────────────────────────────

    def key(self) -> str:
        """The caller's Silo key, or "" when none is configured.

        Three places, most explicit first: the constructor, ``SILO_KEY``,
        and ``<config_dir>/silo.json``. Re-read on each call rather than
        captured at construction, so a user who writes the file while the
        terminal is running does not have to restart it.
        """
        if self._key_override:
            return self._key_override
        env = os.environ.get("SILO_KEY", "").strip()
        if env:
            return env
        try:
            note = json.loads((cfg.config_dir() / "silo.json").read_text())
        except (OSError, ValueError):
            # No file, or one that is not JSON: unconfigured, not broken.
            return ""
        return str(note.get("key") or "").strip()

    def configured(self) -> bool:
        return bool(self.key())

    # ── search ──────────────────────────────────────────────────────────

    def search(self, query: str = "", limit: int = 50) -> list[Instrument]:
        n = max(1, int(limit))
        q = query.strip()
        if not q:
            return self._default_listing()[:n]

        out: list[Instrument] = []
        seen: set[str] = set()

        # Ticker matches first, and they have to be asked for separately.
        # Silo's lookup ranks by name across ~80k CVM funds, so typing
        # "PETR" returns twenty funds called PETRA before it reaches PETR4.
        # A prefix query against the last session's papers puts the thing
        # the user is typing at the top, where it belongs.
        if _looks_like_ticker(q):
            for inst in self._ticker_prefix(q.upper(), n):
                if inst.symbol not in seen:
                    seen.add(inst.symbol)
                    out.append(inst)

        for row in self._rpc("lookup", {"p_query": q}):
            inst = _instrument_from_lookup(row, self.name)
            if inst is None or inst.symbol in seen:
                continue
            seen.add(inst.symbol)
            out.append(inst)

        # One fund and nothing else means the user named it, so offer the
        # other numbers it reports rather than leaving them to be guessed
        # from the documentation.
        funds = [i for i in out if i.symbol.startswith(FUND_PREFIX)]
        if len(out) == 1 and len(funds) == 1:
            out = _fund_variants(funds[0], self.name)
        return out[:n]

    def _default_listing(self) -> list[Instrument]:
        """The last session's papers, busiest first, grouped by class.

        An empty query wants "a sensible default list", and for a Brazilian
        cash market that is what actually traded yesterday in order of
        money: PETR4, BOVA11, VALE3, ITUB4. Ordering by volume also does
        the filtering for free -- the long tail of papers that print once a
        month sorts itself to the bottom.
        """
        with self._lock:
            hit = self._catalog
        if hit and time.time() - hit[0] < _CATALOG_TTL_S:
            return hit[1]

        last = self.latest_session()
        rows = self._get("quotes", {
            "trade_date": f"eq.{last.isoformat()}",
            "order": "volume.desc",
            "select": "ticker,short_name,asset_class,spec",
        }, cap=600)
        by_category: dict[str, list[Instrument]] = {}
        for row in rows:
            category = _CATEGORY.get(row.get("asset_class") or "")
            if not category:
                continue
            by_category.setdefault(category, []).append(Instrument(
                symbol=row["ticker"],
                name=_clean(row.get("short_name")) or row["ticker"],
                category=category, provider=self.name,
                meta={"asset_class": row.get("asset_class"),
                      "spec": _clean(row.get("spec"))},
            ))
        # Contiguous by category, in the sidebar order this module declares:
        # the UI renders what arrives, verbatim.
        out = [inst for category in _CATEGORY_ORDER
               for inst in by_category.get(category, [])]
        with self._lock:
            self._catalog = (time.time(), out)
        return out

    def _ticker_prefix(self, prefix: str, limit: int) -> list[Instrument]:
        """Papers whose ticker starts with ``prefix``, from the last session."""
        rows = self._get("quotes", {
            "trade_date": f"eq.{self.latest_session().isoformat()}",
            "ticker": f"like.{prefix}*",
            "order": "volume.desc",
            "select": "ticker,short_name,asset_class,spec",
        }, cap=max(1, limit))
        return [Instrument(
            symbol=row["ticker"],
            name=_clean(row.get("short_name")) or row["ticker"],
            category=_CATEGORY.get(row.get("asset_class") or "",
                                   "Silo — Outros papéis"),
            provider=self.name,
            meta={"asset_class": row.get("asset_class"),
                  "spec": _clean(row.get("spec"))},
        ) for row in rows]

    # ── candles ─────────────────────────────────────────────────────────

    def candles(self, symbol: str, timeframe: str, limit: int = 500,
                start: str | None = None, end: str | None = None) -> pd.DataFrame:
        if timeframe not in self.timeframes:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        sym = symbol.strip().upper()
        n = max(1, int(limit))
        if sym.startswith(FUND_PREFIX):
            df = self._fund_candles(sym, n, start, end)
        else:
            df = self._ticker_candles(sym, n, start, end)
        # Same law as the rest of the terminal: a start-anchored query pages
        # forward from that date, everything else returns the newest N.
        df = df.head(n) if start else df.tail(n)
        return df[CANDLE_COLUMNS].reset_index(drop=True)

    def _ticker_candles(self, ticker: str, limit: int, start: str | None,
                        end: str | None) -> pd.DataFrame:
        # A list of pairs rather than a dict: a bounded range puts two
        # filters on trade_date, which PostgREST reads as AND and a dict
        # cannot hold.
        pairs = [
            ("ticker", f"eq.{ticker}"),
            ("select", "trade_date,open,high,low,close,volume,quotation_factor"),
        ]
        if start:
            pairs.append(("trade_date", f"gte.{_iso(start)}"))
        if end:
            pairs.append(("trade_date", f"lte.{_iso(end)}"))
        # Ascending from an explicit start, because that is the direction
        # the caller is paging. Otherwise newest first, so a bar count is
        # one page rather than a walk through the history to reach its tail.
        pairs.append(("order", "trade_date.asc" if start else "trade_date.desc"))
        return _ticker_frame(self._get("quotes", pairs, cap=limit))

    def _fund_candles(self, symbol: str, limit: int, start: str | None,
                      end: str | None) -> pd.DataFrame:
        cnpj, metric, _ = _parse_fund_symbol(symbol)
        rows = self._rpc("fund_nav", {"p_cnpj": cnpj})
        frame = _fund_frame(rows, metric)
        # Filtered here rather than in the request: a fund's entire monthly
        # history since 2019 is under a hundred rows, so there is nothing to
        # save by asking the server to narrow it, and fund_nav is an RPC --
        # which, unlike the views, PostgREST will not page.
        if start:
            frame = frame[frame["ts"] >= _epoch_seconds_scalar(start)]
        if end:
            frame = frame[frame["ts"] <= _epoch_seconds_scalar(end)]
        return frame

    # ── quote ───────────────────────────────────────────────────────────

    def quote(self, symbol: str) -> Quote:
        """The last stored session's close, with its published bid and ask.

        End of day, not live: Silo's quotes come from COTAHIST, which B3
        publishes after the close. The bid and ask are the session's own
        closing best bid and offer, so the spread is real but as old as the
        price. For an intraday number use the ``b3`` provider, whose quote
        comes from B3's public feed about fifteen minutes behind.
        """
        sym = symbol.strip().upper()
        if sym.startswith(FUND_PREFIX):
            # A fund reports monthly and has no quote in any useful sense.
            df = self.candles(sym, "1d", limit=1)
            if not len(df):
                raise NotSupported(f"Silo has no prints for {symbol!r}")
            last = df.iloc[-1]
            return Quote(symbol=sym, price=float(last["close"]),
                         ts=float(last["ts"]))
        rows = self._rpc("quote_latest", {"p_ticker": sym})
        if not rows:
            raise NotSupported(f"Silo has no quote for {symbol!r}")
        row = rows[0]
        factor = _factor(row.get("quotation_factor"))
        close = row.get("close")
        if close is None:
            raise NotSupported(f"Silo has no last price for {symbol!r}")
        return Quote(
            symbol=sym, price=float(close) / factor,
            ts=float(_epoch_seconds_scalar(row["trade_date"])),
            bid=_opt_price(row.get("bid"), factor),
            ask=_opt_price(row.get("ask"), factor),
        )

    # ── coverage ────────────────────────────────────────────────────────

    def coverage(self) -> list[dict]:
        """What each dataset is complete through, as Silo reports it."""
        return self._rpc("coverage", {})

    def latest_session(self) -> date:
        """The most recent session Silo holds quotes for.

        Read from ``coverage`` rather than assumed from the calendar: a
        weekend, a Brazilian holiday and a warehouse that has not ingested
        yesterday yet are three different things that all look like "today
        has no rows", and only the first two are predictable.

        Cached for the same interval as the catalog, because every search
        needs it and a new session appears once a day.
        """
        with self._lock:
            hit = self._session
        if hit and time.time() - hit[0] < _CATALOG_TTL_S:
            return hit[1]
        for row in self.coverage():
            if row.get("dataset") == "quotes":
                stamp = row.get("complete_through") or row.get("as_of")
                if stamp:
                    day = pd.Timestamp(stamp).date()
                    with self._lock:
                        self._session = (time.time(), day)
                    return day
        raise SiloError("Silo reported no coverage for the quotes dataset")

    # ── transport ───────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        key = self.key()
        if not key:
            raise SiloError(
                "Silo needs an API key. Set SILO_KEY in the environment, or "
                f"write {{\"key\": \"...\"}} to {cfg.config_dir() / 'silo.json'}."
            )
        return {"apikey": key, "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "lse-terminal-brazil"}

    def _rpc(self, function: str, body: dict) -> list[dict]:
        """One PostgREST function call.

        Deliberately unpaged. PostgREST applies Range to a view, not to a
        function's result set, so an RPC that would exceed the row cap is
        simply truncated and there is no second page to ask for. Every RPC
        used here returns well under the cap -- a fund's whole monthly
        history is about ninety rows, lookup caps itself at twenty -- and
        anything that would not is read from the views through _get().
        """
        rows, _ = self._fetch("POST", f"{self._url}/rpc/{function}",
                              self._headers(), json.dumps(body).encode())
        if isinstance(rows, dict):
            return [rows]
        return rows if isinstance(rows, list) else []

    def _get(self, view: str, params, cap: int) -> list[dict]:
        """Up to ``cap`` rows from a view, paged past the server's row cap.

        ``params`` is a dict or a list of pairs; the pair form is what lets
        a bounded date range put two filters on ``trade_date``.
        """
        query = urllib.parse.urlencode(params, safe="*,.:")
        url = f"{self._url}/{view}?{query}"
        out: list[dict] = []
        offset = 0
        while len(out) < cap:
            want = min(_PAGE, cap - len(out))
            headers = self._headers()
            headers["Range-Unit"] = "items"
            headers["Range"] = f"{offset}-{offset + want - 1}"
            rows, response_headers = self._fetch("GET", url, headers, None)
            if not isinstance(rows, list) or not rows:
                break
            out.extend(rows)
            offset += len(rows)
            # A short page is the end of the data, whatever the header says.
            if len(rows) < want or not _has_more(response_headers, offset):
                break
        return out[:cap]


# ── frames ──────────────────────────────────────────────────────────────

def _ticker_frame(rows: list[dict]) -> pd.DataFrame:
    """Silo's quote rows as the candle frame, prices per single unit."""
    if not rows:
        return pd.DataFrame(columns=CANDLE_COLUMNS)
    frame = pd.DataFrame(rows)
    # The quotation factor divides the price and not the money: `volume` is
    # already the session's financial volume in reais, so dividing it too
    # would understate a lot-quoted paper's turnover by a thousand.
    scale = frame["quotation_factor"].map(_factor)
    out = pd.DataFrame({
        "ts": _epoch_seconds(frame["trade_date"]),
        "open": pd.to_numeric(frame["open"], errors="coerce") / scale,
        "high": pd.to_numeric(frame["high"], errors="coerce") / scale,
        "low": pd.to_numeric(frame["low"], errors="coerce") / scale,
        "close": pd.to_numeric(frame["close"], errors="coerce") / scale,
        "volume": pd.to_numeric(frame["volume"], errors="coerce").fillna(0.0),
    })
    out = out[out["close"].notna()]
    out = out.drop_duplicates(subset="ts", keep="last").sort_values("ts")
    return out.reset_index(drop=True)


def _fund_frame(rows: list[dict], metric: str) -> pd.DataFrame:
    """One CVM metric as flat candles, the value in all four OHLC slots.

    Not a disguise: a monthly report has one number per period, and putting
    it in the candle shape is what lets the chart, the indicator stack and
    the backtester read it with no special case. Volume is zero because a
    fund's net assets have none.
    """
    if not rows:
        return pd.DataFrame(columns=CANDLE_COLUMNS)
    frame = pd.DataFrame(rows)
    if metric not in frame.columns:
        return pd.DataFrame(columns=CANDLE_COLUMNS)
    value = pd.to_numeric(frame[metric], errors="coerce")
    out = pd.DataFrame({
        "ts": _epoch_seconds(frame["period"]),
        "open": value, "high": value, "low": value, "close": value,
        "volume": 0.0,
    })
    out = out[out["close"].notna()]
    # A restated month reissues the same period; the later row is the
    # restatement.
    out = out.drop_duplicates(subset="ts", keep="last").sort_values("ts")
    return out.reset_index(drop=True)


# ── symbols ─────────────────────────────────────────────────────────────

def _parse_fund_symbol(symbol: str) -> tuple[str, str, str]:
    """``CVM:<cnpj>[.SUFFIX]`` as its CNPJ, fund_nav column and label.

    Split on the LAST dot and only when what follows it is letters,
    because a Brazilian pasting a CNPJ pastes it punctuated --
    ``00.071.477/0001-68`` -- and splitting on the first dot would read
    that as the fund ``00`` reporting a metric called ``071``.
    """
    rest = symbol[len(FUND_PREFIX):]
    head, dot, tail = rest.rpartition(".")
    if dot and tail.isalpha():
        cnpj, suffix = head, tail
    else:
        cnpj, suffix = rest, ""
    cnpj = "".join(c for c in cnpj if c.isdigit())
    if not cnpj:
        raise ValueError(f"not a CNPJ: {symbol}")
    try:
        metric, label = _FUND_METRICS[suffix]
    except KeyError:
        known = ", ".join(s for s in _FUND_SUFFIX_ORDER if s)
        raise ValueError(
            f"unknown fund metric {suffix!r} in {symbol}; known: {known}"
        ) from None
    return cnpj, metric, label


def _instrument_from_lookup(row: dict, provider: str) -> Instrument | None:
    """One ``lookup`` row as an Instrument, or None for a shape we don't chart."""
    kind = row.get("id_type")
    if kind == "cnpj":
        cnpj = "".join(c for c in str(row.get("cnpj") or row.get("id") or "")
                       if c.isdigit())
        if not cnpj:
            return None
        return Instrument(
            symbol=f"{FUND_PREFIX}{cnpj}",
            name=_clean(row.get("name")) or cnpj,
            category=_FUND_CATEGORY, provider=provider,
            meta={"cnpj": cnpj, "asset_class": row.get("asset_class"),
                  "metric": "nav"},
        )
    symbol = str(row.get("id") or "").strip().upper()
    if not symbol:
        return None
    return Instrument(
        symbol=symbol, name=_clean(row.get("name")) or symbol,
        category=_CATEGORY.get(row.get("asset_class") or "",
                               "Silo — Outros papéis"),
        provider=provider,
        meta={"asset_class": row.get("asset_class"), "isin": row.get("isin")},
    )


def _fund_variants(fund: Instrument, provider: str) -> list[Instrument]:
    """One fund as the several series it reports, net assets first."""
    cnpj = fund.meta.get("cnpj", "")
    out = []
    for suffix in _FUND_SUFFIX_ORDER:
        metric, label = _FUND_METRICS[suffix]
        symbol = f"{FUND_PREFIX}{cnpj}" + (f".{suffix}" if suffix else "")
        out.append(Instrument(
            symbol=symbol, name=f"{fund.name} — {label}",
            category=_FUND_CATEGORY, provider=provider,
            meta={**fund.meta, "metric": metric},
        ))
    return out


def _looks_like_ticker(query: str) -> bool:
    """True for something that could be typed at a B3 ticker box.

    Short and alphanumeric: PETR, PETR4, BOVA11. A CNPJ is all digits and a
    fund name has spaces, so both fall through to ``lookup`` alone.
    """
    q = query.strip()
    return bool(q) and len(q) <= 8 and q.isalnum() and not q.isdigit()


# ── helpers ─────────────────────────────────────────────────────────────

def _factor(value) -> float:
    """The quotation factor as a divisor, guarding the meaningless values."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 1.0
    return f if f > 0 else 1.0


def _opt_price(value, factor: float) -> float | None:
    """A bid or ask per single unit, or None when the session published none."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    # COTAHIST writes zero for a side with no order at the close, which is
    # an absence rather than a price of nothing.
    return price / factor if price > 0 else None


def _clean(value) -> str:
    """A text column as a display string: B3 pads its fields with spaces."""
    return "" if value is None else " ".join(str(value).split())


def _epoch_seconds(series: pd.Series) -> pd.Series:
    """ISO dates as integer epoch seconds, UTC.

    Cast through a fixed unit rather than letting pandas choose: 2.x parses
    to nanoseconds and 3.x to microseconds, so `.astype("int64")` on the
    parsed values alone returns numbers a million apart depending on which
    version is installed.
    """
    when = pd.to_datetime(series, format="%Y-%m-%d", utc=True, errors="coerce")
    return (when.dt.tz_convert("UTC").dt.tz_localize(None)
                .values.astype("datetime64[s]").astype("int64"))


def _epoch_seconds_scalar(value) -> int:
    return int(pd.Timestamp(pd.Timestamp(value).date(), tz="UTC").timestamp())


def _has_more(headers: dict, fetched: int) -> bool:
    """Whether a Content-Range says rows remain past what has been read.

    PostgREST answers ``0-999/1917`` when it knows the total and ``0-999/*``
    when it does not, and the second is the common case because counting is
    opt-in. So "the last page was short of the cap" is the reliable signal;
    the total is used only when it is actually there.
    """
    raw = ""
    for name, value in headers.items():
        if name.lower() == "content-range":
            raw = str(value)
            break
    _, _, total = raw.partition("/")
    total = total.strip()
    if total.isdigit():
        return fetched < int(total)
    return True


def _iso(value) -> str:
    """A caller's date, however written, as the YYYY-MM-DD PostgREST wants."""
    return pd.Timestamp(value).date().isoformat()


def _http_json(method: str, url: str, headers: dict, body: bytes | None,
               timeout: float = 30.0) -> tuple[list | dict, dict]:
    """One request, returning the decoded body and the response headers.

    The headers come back because the row cap is only visible there; a
    caller that dropped them could not tell a complete series from a
    truncated one. Separate from the provider so tests can hand it a stub.
    """
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            response_headers = dict(r.headers)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = (json.loads(e.read()) or {}).get("message", "")
        except Exception:
            pass
        if e.code in (401, 403):
            raise SiloError(
                f"Silo rejected the key ({e.code}"
                f"{': ' + detail if detail else ''}). Check SILO_KEY."
            ) from e
        raise SiloError(
            f"Silo returned {e.code}{': ' + detail if detail else ''}") from e
    except urllib.error.URLError as e:
        raise SiloError(f"Silo is unreachable: {e.reason}") from e
    return (json.loads(raw or "null") or []), response_headers
