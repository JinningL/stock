# stock

GitHub Actions stock monitor that fetches prices from Yahoo Finance and sends email alerts.

It also generates a simple static report page at `docs/index.html` using saved daily close history from `data/daily_history.json`.

## Required GitHub Secrets

- `EMAIL`: Gmail address used to send the email
- `PASSWORD`: Gmail app password for that account
- `RECEIVER`: Optional recipient email address. If omitted, email is sent to `EMAIL`
- `STOCK_SYMBOLS`: Optional comma-separated tickers such as `QQQ,TSLA,CRCL`

## Optional GitHub Secrets

- `ALERT_THRESHOLDS`: Per-symbol alert threshold percentages, for example `QQQ:1.5,TSLA:4,CRCL:8`
- `ALERT_THRESHOLD_PERCENT`: Fallback threshold percentage for symbols not listed in `ALERT_THRESHOLDS`
- `ALERT_LOOKBACK_MINUTES`: Lookback window for intraday alerts. Default is `60`
- `SUMMARY_TIME`: Market-local time to send the daily close summary. Default is `16:05`

## Workflow

- Scheduled every 15 minutes on weekdays during the broad U.S. market session window
- Sends an intraday alert when a symbol moves sharply within the configured lookback window
- Sends one daily close summary after the configured summary time
- Uses a cached `.state/monitor_state.json` file so the same alert is not re-sent every run
- Stores close-history rows in `data/daily_history.json`
- Generates a simple report page in `docs/index.html`
- Commits report updates back to the repository automatically
- You can also run it manually from the Actions tab with `workflow_dispatch`

## Report Page

- The static report is written to `docs/index.html`
- If you enable GitHub Pages for the `docs/` folder on `main`, you can open it as a webpage
- The page shows the latest close, day change, and recent close history for each tracked symbol

## Default behavior

If you do not set any symbol or threshold secrets, the script defaults to:

- Symbols: `QQQ,TSLA,CRCL`
- Intraday lookback: `60` minutes
- Thresholds: `QQQ 1.5%`, `TSLA 4%`, `CRCL 8%`
- Summary time: `16:05` in `America/New_York`

## Local Run

```bash
export EMAIL="your_email@gmail.com"
export PASSWORD="your_app_password"
export RECEIVER="your_email@gmail.com"
export STOCK_SYMBOLS="QQQ,TSLA,CRCL"
export ALERT_THRESHOLDS="QQQ:1.5,TSLA:4,CRCL:8"
export ALERT_LOOKBACK_MINUTES="60"
export SUMMARY_TIME="16:05"
python main.py
```
