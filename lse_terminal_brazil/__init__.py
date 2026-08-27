"""Brazilian market data for LSE Terminal: B3 and the Banco Central.

Install this alongside the terminal and both sources appear in MARKETS on
the next start -- no fork, no patch, no configuration. The terminal
discovers them through the ``lse_terminal.providers`` entry-point group
declared in this package's pyproject.

Neither source needs an account. ``b3`` reads the exchange's own public
files; ``bcb`` reads the central bank's open series API. What each covers,
and what it does not, is in each module's docstring and in the README.
"""

from lse_terminal_brazil.b3 import B3Provider
from lse_terminal_brazil.bcb import BcbProvider

__all__ = ["B3Provider", "BcbProvider"]
__version__ = "0.1.0"
