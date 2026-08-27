# lse-terminal-brazil

Brazilian market data for [LSE Terminal](https://github.com/londonstrategicedge/lse-terminal),
read straight from the institutions that publish it.

```
pip install git+https://github.com/PedroDnT/lse-terminal-brazil
```

Install it into whatever environment runs the terminal — the same one
`lset` starts from, since that is the interpreter whose entry points get
walked. Then restart the terminal.

Two install notes, both about packages that are not on PyPI in usable form:

* **This package** is not published yet, hence the git URL. `pip install
  lse-terminal-brazil` is the intended form once it is.
* **The terminal itself** must be installed from source. The `lse-terminal`
  distribution on PyPI is a name placeholder whose wheel contains an empty
  package, so it will not satisfy this plugin — which is why the plugin does
  not declare it as a dependency (pip would call the requirement met and
  leave you with a broken import). Install the host with
  `pip install git+https://github.com/londonstrategicedge/lse-terminal`, or
  from a checkout. If it is missing, importing this package says so and
  tells you the command. Two sources appear in MARKETS. There is nothing to
configure and no account to open — B3 and the Banco Central both publish
this data for free, and this package reads exactly those public sources
with no vendor in the middle.

| Source | Key | What it serves |
| --- | --- | --- |
| `b3` | none | Official end-of-day history for the cash segment, back to 1986; the current session minute by minute; delayed quotes for shares, futures and the indices |
| `bcb` | none | Selic, CDI, IPCA, IGP-M, PTAX, IBC-Br and the rest, back to the 1990s |

## `b3` — the exchange's own files

**COTAHIST** (`bvmf.bmfbovespa.com.br/InstDados/SerHist`) is B3's official
end-of-day series: one fixed-width record per instrument per session,
published as daily, monthly and yearly ZIPs, with a true open/high/low/close
and traded quantity. Daily bars come from here, and the most recent daily
file also builds the instrument catalog — ações, units, ETFs, FIIs, Fiagro,
BDRs, and options on a typed query.

**`cotacao.b3.com.br`** is the JSON feed behind B3's own quote pages: a last
price for any listed symbol and the current session's minute prints. Unlike
COTAHIST it answers for BM&F contracts and the indices, so `WIN`, `IND`,
`WDO`, `DOL` and `IBOV` are reachable — front-month contract codes are
derived from B3's own roll rules, so they always name the live contract.

Downloads cache under `~/.config/lse-terminal/b3` as parquet. A daily
request fetches only the grain it needs: a fortnight of bars pulls
individual sessions, a year pulls the yearly archive.

## `bcb` — the central bank's series

The SGS open API, no key. Fourteen series under juros, câmbio, inflação and
atividade — including the CDI, which is the risk-free leg of essentially
every Brazilian backtest.

Each series is served as candles with the value in all four OHLC slots. Not
a disguise: a policy rate has one number per print, and the candle shape is
what lets the chart, the indicator stack and the backtester read it with no
special case. Volume is zero because a rate has none.

## What these sources do not have

Stated up front rather than discovered from an empty chart.

* **No live feed.** Neither source implements `stream()`, so the terminal's
  live socket and its watchlist price poll do not apply to them. A chart
  shows the session as of when it was fetched.
* **B3's public quote is delayed** (B3 publishes it on ~15-minute delay) and
  carries no book, so there is no bid and no ask.
* **No intraday archive.** B3 publishes none, so yesterday's minutes cannot
  be fetched from anywhere here. An intraday series has to be recorded as it
  prints, or imported into MY DATA from a file. Only the daily series has
  history.
* **No daily history for futures.** COTAHIST is the cash segment, so `WINV26`
  and friends have quotes and today's intraday but no daily series. Asking
  for one says so rather than returning something invented.
* **The first deep history request is slow** — a yearly COTAHIST archive is
  about 90 MB. Once per year of history, then it is on disk.

Real-time B3 prices come from a broker connection or a paid feed, not from
anything free. See the terminal's Brue Connect adapters for the execution
side.

## Using it

Pick the source in MARKETS and chart a symbol, or address it by provider
name:

```
GET /api/instruments?provider=b3&query=PETR
GET /api/candles?provider=b3&symbol=PETR4&timeframe=1d&limit=500
GET /api/candles?provider=bcb&symbol=BCB:CDI&timeframe=1d&limit=2000
```

## How it plugs in

Through the terminal's own extension point — no fork and no patch. The
package declares:

```toml
[project.entry-points."lse_terminal.providers"]
b3 = "lse_terminal_brazil:B3Provider"
bcb = "lse_terminal_brazil:BcbProvider"
```

and the terminal's registry walks that group at start. Both providers
implement `lse_terminal.contracts.Provider` and pass the terminal's own
`check_provider` compliance harness.

## Tests

```
pip install -e ".[dev]"
pytest tests/           # 28 offline tests
pytest tests/ -m live   # 10 more, against the real B3 and BCB endpoints
```

**Offline (default).** Each provider takes its HTTP getter as a constructor
argument, so the suite exercises the parsing and the request planning
without depending on B3 or the Banco Central being up. The COTAHIST fixture
is a dozen real records from a real session, kept whole so the fixed-width
offsets are tested against the layout B3 actually publishes.

**Live (opt-in).** Everything here reads a public download or an
undocumented JSON feed, so the standing risk is not a logic bug — it is B3
moving a URL, renaming a field, or adding a column to the fixed-width
record, with no notice owed to anyone. No stubbed test sees that coming.
These ask the real sources, and assert on shape and invariants rather than
on prices, so they fail when something has genuinely broken and not because
the market moved. CI runs them weekly and on demand.

CI also asserts the thing the suite itself cannot: that a stock terminal
actually *discovers* these providers through the entry point. Tests that
import the providers directly would pass even with that broken.

The offline suite runs across Python 3.10–3.13 and both pandas 2.x and 3.x.
That axis is deliberate: the two disagree about the resolution a parsed
datetime column carries, which would silently rescale every timestamp. The
providers sidestep it by doing date arithmetic on integers, and the matrix
is what keeps that true rather than merely intended.

## Terms — read this before building a product on it

The MIT licence covers this code. It does not cover the data, and the two
sources are on completely different footing.

**Banco Central (SGS)** is Brazilian government open data, published as an
open API. Use it however you like, including commercially.

**B3 is not open data.** Its [Termos de Uso](https://www.b3.com.br/pt_br/termos-de-uso-e-protecao-de-dados/termos-de-uso/)
say visitors may use the data on its pages *"para uso exclusivamente
pessoal"* — for exclusively personal use — and expressly prohibit
*"distribuição, redistribuição, transferência, transmissão, retransmissão,
licença, sublicença, locação, empréstimo, venda, revenda, recirculação,
reformatação, publicação, prestação de serviços autônomos de difusão de
dados"*, along with using the data to construct any kind of index. B3 sells
market data commercially (UP2DATA, vendor licensing), so this is a live
commercial interest, not boilerplate.

In practice that means:

* **Running the terminal on your own machine, for your own trading — fine.**
  That is the personal use the terms describe, and it is what this package
  is for. Every request goes out from the user's own machine; nothing is
  relayed through a server here.
* **Serving B3 data to other people is not.** A hosted API, a paid product,
  a public dashboard, a data feed for clients, or an index built from these
  numbers all land on the prohibited list. That needs a market-data
  agreement with B3, not this package.

This is a plain reading of published terms by someone who is not a lawyer,
and not legal advice. If you are going commercial, have Brazilian counsel
look at it — and note that a market-data licence is the normal route, not
an obstacle to route around.

MIT (the code).
