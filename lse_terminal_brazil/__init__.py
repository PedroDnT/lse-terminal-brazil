"""Brazilian market data for LSE Terminal: B3 and the Banco Central.

Install this alongside the terminal and both sources appear in MARKETS on
the next start -- no fork, no patch, no configuration. The terminal
discovers them through the ``lse_terminal.providers`` entry-point group
declared in this package's pyproject.

Two of the three need no account at all: ``b3`` reads the exchange's own
public files and ``bcb`` the central bank's open series API. ``silo`` is
the exception -- it wants a key, and in return serves the same B3 history
in a fraction of the time and adds the CVM's monthly fund statistics, which
no exchange file carries. Without a key it reports itself unconfigured and
the other two are unaffected. What each covers, and what it does not, is in
each module's docstring and in the README.
"""

try:
    from lse_terminal_brazil.b3 import B3Provider
    from lse_terminal_brazil.bcb import BcbProvider
    from lse_terminal_brazil.silo import SiloProvider
except ModuleNotFoundError as e:  # pragma: no cover - install-shape problem
    # Both shapes matter: no terminal at all (e.name == "lse_terminal") and
    # the PyPI placeholder, which imports but has no submodules
    # (e.name == "lse_terminal.contracts").
    if e.name != "lse_terminal" and not (e.name or "").startswith("lse_terminal."):
        raise
    # The host is a prerequisite, not a pip dependency: the `lse-terminal`
    # distribution on PyPI is a placeholder whose wheel ships an empty
    # package, so pip cannot be asked to provide the real one. Say so,
    # rather than leaving a bare ModuleNotFoundError on a submodule nobody
    # has heard of.
    raise ImportError(
        "lse-terminal-brazil needs the LSE Terminal itself, and could not "
        f"import {e.name}. The `lse-terminal` package on PyPI is only a "
        "name placeholder (its wheel is empty), so install the terminal "
        "from source instead:\n\n"
        "    pip install git+https://github.com/londonstrategicedge/lse-terminal\n\n"
        "then install this package into that same environment."
    ) from e

__all__ = ["B3Provider", "BcbProvider", "SiloProvider"]
__version__ = "0.1.0"
