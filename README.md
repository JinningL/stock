# stock

Simple GitHub Actions job that fetches a stock price from Yahoo Finance and emails it.

## Required GitHub Secrets

- `EMAIL`: Gmail address used to send the email
- `PASSWORD`: Gmail app password for that account
- `RECEIVER`: Optional recipient email address. If omitted, email is sent to `EMAIL`
- `STOCK_SYMBOL`: Optional stock ticker such as `AAPL`, `TSLA`, or `NVDA`

## Workflow

- Scheduled for `13:35 UTC` on weekdays
- On March 30, 2026, that is `9:35 AM` in Toronto / New York time
- You can also run it manually from the Actions tab with `workflow_dispatch`

## Local Run

```bash
export EMAIL="your_email@gmail.com"
export PASSWORD="your_app_password"
export RECEIVER="your_email@gmail.com"
export STOCK_SYMBOL="AAPL"
python main.py
```
