# stock

GitHub Actions stock monitor that fetches prices from Yahoo Finance, sends email alerts, stores close history, and renders a report page in `docs/index.html`.

The project now supports a local dashboard for editing the watchlist and alert thresholds in the browser. Those settings are saved in `data/monitor_config.json`.

## Required GitHub Secrets

- `EMAIL`: Gmail address used to send the email
- `PASSWORD`: Gmail app password for that account
- `RECEIVER`: Optional recipient email address. If omitted, email is sent to `EMAIL`

## Optional GitHub Secrets

These still work as runtime overrides and take precedence over `data/monitor_config.json`:

- `STOCK_SYMBOLS`: Comma-separated tickers such as `QQQ,TSLA,AAPL`
- `ALERT_THRESHOLDS`: Per-symbol thresholds such as `QQQ:1.5,TSLA:4,AAPL:2.2`
- `ALERT_THRESHOLD_PERCENT`: Fallback threshold for symbols not listed in `ALERT_THRESHOLDS`
- `ALERT_LOOKBACK_MINUTES`: Lookback window for intraday alerts
- `SUMMARY_TIME`: Market-local time for the daily summary, for example `16:05`

## Config File

The main configuration lives in `data/monitor_config.json`.

```json
{
  "lookback_minutes": 60,
  "summary_time": "16:05",
  "symbols": [
    { "symbol": "QQQ", "threshold": 1.5 },
    { "symbol": "TSLA", "threshold": 4.0 }
  ]
}
```

Each symbol has its own alert threshold. If the absolute move over the lookback window exceeds that threshold, the script sends an email alert.

## Local Usage

Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run the monitor:

```bash
export EMAIL="your_email@gmail.com"
export PASSWORD="your_app_password"
export RECEIVER="your_email@gmail.com"
python main.py
```

Render the report without fetching fresh market data:

```bash
python main.py render
```

Start the local dashboard for editing symbols and thresholds in the browser:

```bash
python main.py serve
```

Then open `http://127.0.0.1:8000`.

## Workflow

- Scheduled every 15 minutes on weekdays during the U.S. market session window
- Sends one intraday alert per symbol and direction per day
- Sends one daily close summary after the configured summary time
- Stores close history in `data/daily_history.json`
- Writes the report page to `docs/index.html`
- Commits generated report updates back to the repository

## Report Page

- `docs/index.html` shows the watchlist, alert thresholds, recent close history, and a browser-based settings form
- On GitHub Pages the form is preview-only because the site is static
- In local `serve` mode the form saves directly to `data/monitor_config.json`
