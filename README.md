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
  tells you the command.

Three sources then appear in MARKETS. Two of them need no account at all:
B3 and the Banco Central publish their data for free, and this package
reads exactly those public sources with no vendor in the middle. The third,
`silo`, wants a key and stays quietly unconfigured until it has one.

| Source | Key | What it serves |
| --- | --- | --- |
| `b3` | none | Official end-of-day history for the cash segment, back to 1986; the current session minute by minute; delayed quotes for shares, futures and the indices |
| `bcb` | none | Selic, CDI, IPCA, IGP-M, PTAX, IBC-Br and the rest, back to the 1990s |
| `silo` | yours | The same B3 history from 2019, but in a request rather than a download; plus CVM monthly statistics for every Brazilian fund |

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

## `silo` — the same B3 history, plus the funds

[Silo](https://octo-98895abd.mintlify.site/) is a warehouse that has
already done the parsing `b3` does on your machine: COTAHIST loaded into
Postgres and served over PostgREST. That changes the economics of deep
history. Ten years of one ticker through `b3` means ten yearly archives —
around 400 MB of fixed-width text parsed to find the 2,500 rows you wanted,
and the first such chart takes minutes. Through `silo` it is a row filter:
two HTTP requests, well under a second, cold.

It also carries something no exchange file does. Brazilian funds report
monthly to the **CVM**, not to B3, so COTAHIST has no equivalent of any of
this — net assets, quota, unit-holders, subscriptions, redemptions,
delinquency, yield, for every FI, FII, FIDC, FIP and Fiagro in the country.

Funds are addressed by CNPJ, with a suffix choosing which number:

```
CVM:08973948000135                 patrimônio líquido
CVM:08973948000135.COTA            valor da cota
CVM:08973948000135.COTISTAS        número de cotistas
CVM:08973948000135.CAPTACAO        captação no mês
CVM:08973948000135.RESGATE         resgates no mês
CVM:08973948000135.RENDIMENTO      rendimento mensal (FII)
CVM:08973948000135.INADIMPLENCIA   carteira inadimplente (FIDC, Fiagro)
CVM:08973948000135.ATIVOS          ativo total
```

Search for a fund by name or CNPJ and it offers those variants directly, so
there is nothing to memorise. They are monthly series, drawn as a sparser
line on the daily timeframe — the same arrangement `bcb` uses.

### The key

No credential ships in this package. Silo publishes a shared testing key in
its own documentation, which is fine for trying the API by hand and wrong
to bake into an installed terminal: it is one anonymous credential shared
by every reader of that page. Bring your own:

```
export SILO_KEY=...
# or
echo '{"key": "..."}' > ~/.config/lse-terminal/silo.json
```

`SILO_URL` points the provider at a different deployment. Until a key is
set, `silo` reports itself unconfigured — the terminal shows it as needing
setup rather than as broken, and `b3` and `bcb` are unaffected.

### Why it is a separate provider and not a fast path inside `b3`

Because which one answered changes what the data means. Silo's quote
history begins in **2019**; COTAHIST's begins in **1986**. A provider that
silently fell back between them would hand you a chart whose start date
depended on a network condition. Keeping them apart means a request to `b3`
is always the exchange's own file and a request to `silo` is always the
warehouse, and the two are checked against each other: a live test charts
PETR4 through both paths and asserts they agree to the cent.

They also agree on the awkward part. A handful of papers are quoted per lot
of a thousand — FNAM11's published close of `0.16` is the price of a
thousand units, and only `0.00016` reconciles against the session's own
published financial volume. Both providers divide by the quotation factor,
so a paper charts identically whichever one served it.

## What these sources do not have

Stated up front rather than discovered from an empty chart.

* **No live feed.** None of the three implements `stream()`, so the
  terminal's live socket and its watchlist price poll do not apply to them.
  A chart shows the session as of when it was fetched.
* **B3's public quote is delayed** (B3 publishes it on ~15-minute delay) and
  carries no book, so there is no bid and no ask.
* **No intraday archive.** B3 publishes none, so yesterday's minutes cannot
  be fetched from anywhere here. An intraday series has to be recorded as it
  prints, or imported into MY DATA from a file. Only the daily series has
  history.
* **No daily history for futures.** COTAHIST is the cash segment, so `WINV26`
  and friends have quotes and today's intraday but no daily series. Asking
  for one says so rather than returning something invented.
* **The first deep history request is slow through `b3`** — a yearly
  COTAHIST archive is about 90 MB. Once per year of history, then it is on
  disk. `silo` is the answer if you want that history now instead.
* **`silo` starts in 2019 and has no intraday either.** It stores sessions
  and monthly fund reports; its quote is the last stored close with the
  session's own closing bid and ask, which is honest but a day old. For a
  number from today, `b3`'s quote is the ~15-minute-delayed one.
* **Nothing is adjusted for corporate actions**, from any of these sources,
  because COTAHIST is not: a split shows up as a gap.

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
GET /api/candles?provider=silo&symbol=PETR4&timeframe=1d&start=2019-01-01&limit=2000
GET /api/candles?provider=silo&symbol=CVM:08973948000135.COTISTAS&timeframe=1d&limit=96
```

## How it plugs in

Through the terminal's own extension point — no fork and no patch. The
package declares:

```toml
[project.entry-points."lse_terminal.providers"]
b3 = "lse_terminal_brazil:B3Provider"
bcb = "lse_terminal_brazil:BcbProvider"
silo = "lse_terminal_brazil:SiloProvider"
```

and the terminal's registry walks that group at start. All three implement
`lse_terminal.contracts.Provider` and pass the terminal's own
`check_provider` compliance harness.

## Tests

```
pip install -e ".[dev]"
pytest tests/           # 84 offline tests
pytest tests/ -m live   # 19 more, against the real B3, BCB and Silo
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
the market moved. CI runs them weekly and on demand. The Silo half needs
`SILO_KEY` and skips without it, so a fork with no key still monitors the
two free sources instead of going red on a credential it never had.

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
open API. Use it however you like, including commercially. **CVM fund
statistics** are the same kind of thing: mandatory regulatory filings the
CVM publishes openly.

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
* **`silo` does not change that.** Its quotes are COTAHIST; the same terms
  attach to the numbers however they reach you. What licence Silo itself
  holds to redistribute them is Silo's question to answer, and worth asking
  before you build on it — this package is a client, and the key is yours.

This is a plain reading of published terms by someone who is not a lawyer,
and not legal advice. If you are going commercial, have Brazilian counsel
look at it — and note that a market-data licence is the normal route, not
an obstacle to route around.

MIT (the code).
