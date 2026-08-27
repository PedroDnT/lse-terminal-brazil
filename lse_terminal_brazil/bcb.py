"""Brazilian macro series from the Banco Central do Brasil.

A Brazilian strategy is not written against price alone. The risk-free
leg of every backtest here is the CDI, position sizing answers to the
Selic, and anything with an offshore leg answers to the PTAX dollar. The
central bank publishes all of it through the SGS (Sistema Gerenciador de
Séries Temporais) open API: no key, no registration, no rate limit worth
the name, and series that run back to the 1990s.

Each series arrives as a (date, value) stream, which this provider
presents as candles with the value in all four OHLC slots. That is not a
disguise: a policy rate has one number per print, and putting it in the
candle shape is what lets the chart, the indicator stack and the
backtester read it with no special case. Volume is zero because a rate
has none.

Frequency belongs to the series, not to the request. ``timeframes`` is
``["1d"]`` because that is the finest grain any of these have; a monthly
series asked for on the daily timeframe returns its monthly prints,
stamped at the first of the month, and simply draws as a sparser line.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from lse_terminal.contracts import CANDLE_COLUMNS, Instrument, Provider, Quote
from lse_terminal_brazil import _config as cfg

SGS_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados"

# SGS refuses a daily series over a window wider than ten years, so any
# longer request is walked in chunks and stitched back together.
_MAX_WINDOW = timedelta(days=365 * 10)

# How long a fetched series is reused before the tail is refreshed. These
# print once a day at the very fastest, and most are monthly.
_TTL_S = 6 * 3600

# The catalog. Each row is (symbol, SGS code, display name, frequency),
# grouped under the category heading the sidebar renders as a folder. Kept
# deliberately short: these are the series a Brazilian strategy actually
# reads, not everything the SGS holds (tens of thousands of series). The
# frequency is what turns a bar count into a calendar window, and daily and
# monthly series differ by a factor of twenty there.
_SERIES: dict[str, list[tuple[str, int, str, str]]] = {
    "Brasil — Juros": [
        ("BCB:SELIC.META", 432, "Meta Selic definida pelo Copom (% a.a.)", "D"),
        ("BCB:SELIC", 1178, "Selic anualizada base 252 (% a.a.)", "D"),
        ("BCB:SELIC.DIA", 11, "Selic diária (% a.d.)", "D"),
        ("BCB:SELIC.MES", 4390, "Selic acumulada no mês (%)", "M"),
        ("BCB:CDI", 4389, "CDI anualizado base 252 (% a.a.)", "D"),
        ("BCB:CDI.DIA", 12, "CDI diário (% a.d.)", "D"),
    ],
    "Brasil — Câmbio": [
        ("BCB:USDBRL", 1, "Dólar dos EUA (venda) — PTAX", "D"),
        ("BCB:EURBRL", 21619, "Euro (venda) — PTAX", "D"),
    ],
    "Brasil — Inflação": [
        ("BCB:IPCA.MES", 433, "IPCA — variação mensal (%)", "M"),
        ("BCB:IPCA.12M", 13522, "IPCA — acumulado em 12 meses (%)", "M"),
        ("BCB:IGPM.MES", 189, "IGP-M — variação mensal (%)", "M"),
    ],
    "Brasil — Atividade": [
        ("BCB:IBCBR", 24364, "IBC-Br — índice de atividade econômica", "M"),
        ("BCB:PIB.MENSAL", 4380, "PIB mensal — valores correntes (R$ milhões)", "M"),
        ("BCB:DESEMPREGO", 24369, "Taxa de desocupação — PNAD Contínua (%)", "M"),
    ],
}

_BY_SYMBOL = {sym: (code, name, category, freq)
              for category, rows in _SERIES.items()
              for sym, code, name, freq in rows}

# Calendar days a single print covers, by frequency. Used to turn the
# caller's bar count into a window to ask the SGS for.
_DAYS_PER_PRINT = {"D": 365 / 252, "M": 31}

# The oldest date worth asking about: no SGS series here predates it.
_EPOCH = date(1986, 1, 1)


class BcbProvider(Provider):
    name = "bcb"
    title = "Banco Central do Brasil (macro)"
    # The finest grain the SGS publishes. Monthly series answer here too and
    # simply return monthly prints; see the module docstring.
    timeframes = ["1d"]
    # A free public API the user reaches directly, so the fleet directory
    # never decides whether it is listed.
    is_custom = True

    def __init__(self, cache_dir: Path | None = None, fetch=None):
        self._cache_dir = cache_dir
        self._fetch = fetch or _http_get
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, pd.DataFrame]] = {}

    def cache_dir(self) -> Path:
        d = self._cache_dir or (cfg.config_dir() / "bcb")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def search(self, query: str = "", limit: int = 50) -> list[Instrument]:
        q = query.strip().upper()
        out = [Instrument(symbol=sym, name=name, category=category,
                          provider=self.name, meta={"sgs": code, "freq": freq})
               for category, rows in _SERIES.items()
               for sym, code, name, freq in rows
               if not q or q in sym.upper() or q in name.upper()]
        return out[: max(1, int(limit))]

    def candles(self, symbol: str, timeframe: str, limit: int = 500,
                start: str | None = None, end: str | None = None) -> pd.DataFrame:
        if timeframe not in self.timeframes:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        sym = symbol.strip().upper()
        if sym not in _BY_SYMBOL:
            raise ValueError(f"unknown BCB series: {symbol}")
        df = self._series(sym, *_window(sym, limit, start, end))
        if start:
            df = df[df["ts"] >= int(pd.Timestamp(start, tz="UTC").timestamp())]
        if end:
            df = df[df["ts"] <= int(pd.Timestamp(end, tz="UTC").timestamp())]
        n = max(1, int(limit))
        df = df.head(n) if start else df.tail(n)
        return df[CANDLE_COLUMNS].reset_index(drop=True)

    def quote(self, symbol: str) -> Quote:
        # A handful of bars rather than one: the window a bar count implies
        # is padded by days, not weeks, and a single-print ask over a long
        # weekend or a run of holidays would come back empty.
        df = self.candles(symbol, "1d", limit=5)
        if not len(df):
            raise ValueError(f"no prints for {symbol}")
        last = df.iloc[-1]
        return Quote(symbol=symbol.strip().upper(), price=float(last["close"]),
                     ts=float(last["ts"]))

    # ── fetching ────────────────────────────────────────────────────────

    def _series(self, symbol: str, first: date, last: date) -> pd.DataFrame:
        """The series over ``[first, last]``, fetching only what is missing.

        Every SGS request costs about twenty seconds whatever its width --
        the cost is the query, not the bytes -- so the two things that
        matter are asking for as few windows as possible and never asking
        twice. The whole series as fetched so far lives in one parquet on
        disk, alongside a note of how far back it reaches; a request inside
        that range and fresh enough is served without touching the network,
        and one that reaches further back extends it in place.
        """
        with self._lock:
            hit = self._cache.get(symbol)
            if hit and time.time() - hit[0] < _TTL_S and _covers(hit[1], first):
                return hit[1]
        cached, covered_from, fetched_at = self._read_cache(symbol)
        fresh = time.time() - fetched_at < _TTL_S
        if cached is not None and fresh and covered_from is not None and covered_from <= first:
            frame = cached
        else:
            # One fetch covering both what is missing at the front and
            # everything since: a second request for the tail would cost as
            # much as the first.
            want_from = min(first, covered_from) if covered_from else first
            rows: list[dict] = []
            code = _BY_SYMBOL[symbol][0]
            window_start = want_from
            today = date.today()
            while window_start <= today:
                window_end = min(today, window_start + _MAX_WINDOW)
                rows.extend(self._fetch_window(code, window_start, window_end))
                window_start = window_end + timedelta(days=1)
            frame = _to_candles(rows)
            if cached is not None and len(cached):
                frame = _merge(cached, frame)
            self._write_cache(symbol, frame, want_from)
        with self._lock:
            self._cache[symbol] = (time.time(), frame)
        return frame

    # ── disk cache ──────────────────────────────────────────────────────

    def _paths(self, symbol: str) -> tuple[Path, Path]:
        slug = symbol.replace(":", "_").replace(".", "_").lower()
        d = self.cache_dir()
        return d / f"{slug}.parquet", d / f"{slug}.json"

    def _read_cache(self, symbol: str) -> tuple[pd.DataFrame | None, date | None, float]:
        data, meta = self._paths(symbol)
        try:
            frame = pd.read_parquet(data)
            note = json.loads(meta.read_text())
            return frame, date.fromisoformat(note["from"]), float(note["fetched"])
        except Exception:
            # A missing or unreadable cache is simply a cold start.
            return None, None, 0.0

    def _write_cache(self, symbol: str, frame: pd.DataFrame, covered_from: date) -> None:
        data, meta = self._paths(symbol)
        try:
            frame.to_parquet(data, index=False)
            meta.write_text(json.dumps({"from": covered_from.isoformat(),
                                        "fetched": time.time()}))
        except Exception:
            # A cache that cannot be written is a slow provider, not a
            # broken one; the frame in hand is still the right answer.
            pass

    def _fetch_window(self, code: int, first: date, last: date) -> list[dict]:
        url = (f"{SGS_URL.format(code=code)}?formato=json"
               f"&dataInicial={first.strftime('%d/%m/%Y')}"
               f"&dataFinal={last.strftime('%d/%m/%Y')}")
        raw = self._fetch(url)
        if not raw.strip():
            # SGS answers an empty body for a window entirely before the
            # series began, which is a gap rather than an error.
            return []
        payload = json.loads(raw)
        return payload if isinstance(payload, list) else []


def _window(symbol: str, limit: int, start: str | None,
            end: str | None) -> tuple[date, date]:
    """The calendar window a request covers, from its bar count or its dates."""
    last = pd.Timestamp(end).date() if end else date.today()
    if start:
        return pd.Timestamp(start).date(), last
    freq = _BY_SYMBOL[symbol][3]
    span = timedelta(days=int(max(1, int(limit)) * _DAYS_PER_PRINT[freq]) + 7)
    return max(_EPOCH, last - span), last


def _covers(frame: pd.DataFrame, first: date) -> bool:
    """True when ``frame`` already reaches back to ``first``."""
    if not len(frame):
        return False
    epoch = int(pd.Timestamp(first, tz="UTC").timestamp())
    return int(frame["ts"].iloc[0]) <= epoch


def _merge(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Two fetches of one series, newer prints winning on a shared date."""
    both = pd.concat([old, new], ignore_index=True)
    both = both.drop_duplicates(subset="ts", keep="last").sort_values("ts")
    return both.reset_index(drop=True)


def _to_candles(rows: list[dict]) -> pd.DataFrame:
    """SGS ``[{"data": "DD/MM/YYYY", "valor": "1.23"}]`` as the candle frame."""
    if not rows:
        return pd.DataFrame(columns=CANDLE_COLUMNS)
    frame = pd.DataFrame(rows)
    when = pd.to_datetime(frame["data"], format="%d/%m/%Y", utc=True)
    value = pd.to_numeric(frame["valor"].astype(str).str.replace(",", ".",
                                                                regex=False),
                          errors="coerce")
    out = pd.DataFrame({
        # Seconds, computed through a fixed-unit cast so the answer does not
        # depend on which resolution the installed pandas parsed into.
        "ts": when.dt.tz_convert("UTC").dt.tz_localize(None)
                  .values.astype("datetime64[s]").astype("int64"),
        "open": value, "high": value, "low": value, "close": value,
        # A rate has no volume, and inventing one would put a number in a
        # pane that means nothing.
        "volume": 0.0,
    })
    out = out[out["close"].notna()]
    # A revised print reissues the same date; the later row is the revision.
    out = out.drop_duplicates(subset="ts", keep="last").sort_values("ts")
    return out.reset_index(drop=True)


def _http_get(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "lse-terminal",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()
