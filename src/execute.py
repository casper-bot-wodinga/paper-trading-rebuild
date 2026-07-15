"""
Alpaca trade execution wrapper for the dashboard.

Provides AlpacaExecutor — used by leaderboard_api.py to pull live portfolio
data from Alpaca's paper trading API when recent DB snapshots are stale.
"""

from alpaca.trading.client import TradingClient


class AlpacaExecutor:
    """Lightweight wrapper around Alpaca's TradingClient for paper trading."""

    BASE_URL = "https://paper-api.alpaca.markets"

    def __init__(self, api_key: str, secret_key: str, company: str):
        self.api_key = api_key
        self.secret_key = secret_key
        self.company = company
        self.client = TradingClient(api_key, secret_key, paper=True)

    def get_account_value(self) -> dict | None:
        """
        Return current account snapshot.

        Returns a dict with:
            portfolio_value (float) — total equity (cash + positions)
            cash           (float) — cash balance
            buying_power   (float) — available buying power
            _source        (str)   — always "alpaca_live"

        Returns None if the Alpaca call fails.
        """
        try:
            acct = self.client.get_account()
            return {
                "portfolio_value": float(acct.equity),
                "cash":            float(acct.cash),
                "buying_power":    float(acct.buying_power),
                "_source":         "alpaca_live",
            }
        except Exception:
            return None