import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import main


class ConfigTests(unittest.TestCase):
    def test_normalize_config_cleans_symbols_and_thresholds(self):
        config = main.normalize_config(
            {
                "lookback_minutes": "45",
                "summary_time": "16:15",
                "symbols": [
                    {"symbol": " aapl ", "threshold": "2.5"},
                    {"symbol": "AAPL", "threshold": "9.9"},
                    "tsla",
                    {"symbol": "msft", "threshold": "-1"},
                    {"symbol": "bad symbol", "threshold": "2"},
                ],
            }
        )

        self.assertEqual(config["lookback_minutes"], 45)
        self.assertEqual(config["summary_time"], "16:15")
        self.assertEqual(
            config["symbols"],
            [
                {"symbol": "AAPL", "threshold": 2.5},
                {"symbol": "TSLA", "threshold": 4.0},
                {"symbol": "MSFT", "threshold": 3.0},
            ],
        )

    def test_load_active_config_applies_env_overrides(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "monitor_config.json"
            main.save_json(
                config_path,
                {
                    "lookback_minutes": 60,
                    "summary_time": "16:05",
                    "symbols": [
                        {"symbol": "QQQ", "threshold": 1.5},
                        {"symbol": "TSLA", "threshold": 4.0},
                    ],
                },
            )

            with patch.object(main, "CONFIG_PATH", config_path), patch.dict(
                os.environ,
                {
                    "STOCK_SYMBOLS": "AAPL,TSLA",
                    "ALERT_THRESHOLDS": "AAPL:1.2",
                    "ALERT_THRESHOLD_PERCENT": "2.4",
                    "ALERT_LOOKBACK_MINUTES": "30",
                    "SUMMARY_TIME": "15:50",
                },
                clear=False,
            ):
                active = main.load_active_config()

        self.assertEqual(active["lookback_minutes"], 30)
        self.assertEqual(active["summary_time"], "15:50")
        self.assertEqual(
            active["symbols"],
            [
                {"symbol": "AAPL", "threshold": 1.2},
                {"symbol": "TSLA", "threshold": 4.0},
            ],
        )


class RenderingTests(unittest.TestCase):
    def test_render_report_contains_dashboard_controls(self):
        config = {
            "lookback_minutes": 30,
            "summary_time": "15:50",
            "symbols": [{"symbol": "AAPL", "threshold": 1.2}],
        }
        history = {
            "records": [
                {
                    "date": "2026-04-10",
                    "symbol": "AAPL",
                    "close": 200.0,
                    "previous_close": 198.0,
                    "change": 2.0,
                    "change_pct": 1.01,
                }
            ]
        }

        html = main.render_report(config, history, datetime(2026, 4, 10, 16, 5, tzinfo=main.MARKET_TZ))

        self.assertIn("Configure tracked symbols and alert thresholds in the browser", html)
        self.assertIn("Threshold 1.20%", html)
        self.assertIn("python main.py serve", html)
        self.assertIn("AAPL", html)

    def test_alert_email_builders_include_key_fields(self):
        intraday = {
            "latest_price": 210.55,
            "baseline_price": 203.0,
            "move": 7.55,
            "move_pct": 3.72,
            "latest_time": "2026-04-10 14:30 EDT",
            "daily_change": 4.3,
            "daily_change_pct": 2.09,
        }

        text_body = main.build_alert_email_text("TSLA", intraday, 3.5, 60)
        html_body = main.build_alert_email_html("TSLA", intraday, 3.5, 60)

        self.assertIn("Stock alert triggered", text_body)
        self.assertIn("TSLA", text_body)
        self.assertIn("3.50%", text_body)
        self.assertIn("TSLA", html_body)
        self.assertIn("Your alert threshold", html_body)
        self.assertIn("2026-04-10 14:30 EDT", html_body)


if __name__ == "__main__":
    unittest.main()
