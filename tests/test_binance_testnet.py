import os
import unittest
from unittest import mock
from pathlib import Path

from bot.config import Config
from bot.execution.executor import Executor
from bot.execution.risk_manager import RiskManager
from bot.main import format_account_summary


class _DummyStore:
    def __init__(self, balance: float = 0.0) -> None:
        self._balance = balance

    def get_account_balance(self) -> float:
        return self._balance


class _DummyStateMachine:
    def create(self, *args, **kwargs) -> None:
        return None

    def transition(self, *args, **kwargs) -> None:
        return None


class _DummyKillSwitch:
    is_active = False

    async def trigger(self, *args, **kwargs) -> None:
        return None


class _DummyRiskStore:
    def __init__(self) -> None:
        self._ticker = {"symbol": "BTCUSDT", "price": 100.0}

    def get_daily_pnl(self):
        return 0.0, 0.0

    def get_weekly_pnl(self) -> float:
        return 0.0

    def get_peak_balance(self):
        return 1000.0

    def get_open_live_positions(self):
        return []

    def get_ticker(self, symbol: str):
        return self._ticker if symbol == "BTCUSDT" else None


class BinanceTestnetConfigTests(unittest.TestCase):
    def test_testnet_uses_testnet_rest_and_ws_endpoints(self) -> None:
        with mock.patch.dict(os.environ, {"BINANCE_TESTNET": "true"}, clear=False):
            config = Config()

        self.assertEqual(config.binance_rest_base, "https://testnet.binancefuture.com")
        self.assertEqual(config.binance_ws_base, "wss://stream.binancefuture.com")


class ExecutorBalanceParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_balance_falls_back_to_top_level_total_wallet_balance(self) -> None:
        store = _DummyStore(balance=17.0)
        executor = Executor(
            config=Config(),
            store=store,
            state_machine=_DummyStateMachine(),
            kill_switch=_DummyKillSwitch(),
        )

        async def fake_signed_get(path: str, params: dict) -> dict:
            self.assertEqual(path, "/fapi/v2/account")
            self.assertEqual(params, {})
            return {"totalWalletBalance": "123.45", "assets": []}

        executor._signed_get = fake_signed_get  # type: ignore[method-assign]

        balance = await executor.get_account_balance()

        self.assertEqual(balance, 123.45)

    async def test_account_snapshot_reads_wallet_and_available_balance(self) -> None:
        store = _DummyStore(balance=17.0)
        executor = Executor(
            config=Config(),
            store=store,
            state_machine=_DummyStateMachine(),
            kill_switch=_DummyKillSwitch(),
        )

        async def fake_signed_get(path: str, params: dict) -> dict:
            self.assertEqual(path, "/fapi/v2/account")
            return {
                "assets": [
                    {
                        "asset": "USDT",
                        "walletBalance": "250.0",
                        "availableBalance": "80.5",
                        "marginBalance": "255.2",
                    }
                ]
            }

        executor._signed_get = fake_signed_get  # type: ignore[method-assign]

        snapshot = await executor.get_account_snapshot()

        self.assertEqual(
            snapshot,
            {
                "wallet_balance": 250.0,
                "available_balance": 80.5,
                "margin_balance": 255.2,
            },
        )


class RiskManagerAvailableBalanceTests(unittest.TestCase):
    def test_position_sizing_uses_available_balance_but_keeps_total_for_limits(self) -> None:
        store = _DummyRiskStore()
        risk_manager = RiskManager(store)
        risk_manager.check_consecutive_losses = lambda strategy: 0  # type: ignore[method-assign]

        signal = mock.Mock()
        signal.symbol = "BTCUSDT"
        signal.strategy = "demo"
        signal.action = "BUY"
        signal.sl = 99.0

        result = risk_manager.check(
            signal,
            account_balance=1000.0,
            available_balance=100.0,
            sl_pct=0.01,
        )

        self.assertTrue(result.passed)
        self.assertAlmostEqual(result.position_size, 1.0)


class AccountSummaryFormatTests(unittest.TestCase):
    def test_account_summary_includes_total_available_and_margin(self) -> None:
        summary = format_account_summary(
            wallet_balance=1000.0,
            available_balance=123.45,
            margin_balance=1100.0,
        )

        self.assertIn("총잔고: `1000.00 USDT`", summary)
        self.assertIn("가용잔고: `123.45 USDT`", summary)
        self.assertIn("마진잔고: `1100.00 USDT`", summary)


class DashboardBalanceDisplayTests(unittest.TestCase):
    def test_dashboard_header_mentions_margin_balance(self) -> None:
        template = Path("dashboard/templates/index.html").read_text(encoding="utf-8")
        script = Path("dashboard/static/js/dashboard.js").read_text(encoding="utf-8")

        self.assertIn("Margin", template)
        self.assertIn("margin_balance", script)
        self.assertIn("Margin $", script)


if __name__ == "__main__":
    unittest.main()
