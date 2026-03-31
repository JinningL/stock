import json
import math
import os
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf


DEFAULT_SYMBOLS = ["QQQ", "TSLA", "CRCL"]
DEFAULT_LOOKBACK_MINUTES = 60
DEFAULT_THRESHOLD_PERCENT = 3.0
DEFAULT_SUMMARY_TIME = "16:05"
MARKET_TZ = ZoneInfo(os.environ.get("MARKET_TIMEZONE", "America/New_York"))
STATE_PATH = Path(os.environ.get("STATE_FILE", ".state/monitor_state.json"))

# Sensible defaults for the user's current watchlist. These can be overridden
# with ALERT_THRESHOLDS, for example: QQQ:1.5,TSLA:4,CRCL:8
DEFAULT_SYMBOL_THRESHOLDS = {
    "QQQ": 1.5,
    "TSLA": 4.0,
    "CRCL": 8.0,
}


def get_env_or_default(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def parse_symbols():
    raw = get_env_or_default("STOCK_SYMBOLS", ",".join(DEFAULT_SYMBOLS))
    symbols = []
    for item in raw.split(","):
        symbol = item.strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        raise ValueError("No stock symbols configured")
    return symbols


def parse_thresholds():
    thresholds = dict(DEFAULT_SYMBOL_THRESHOLDS)
    raw = get_env_or_default("ALERT_THRESHOLDS", "")
    if not raw:
        return thresholds

    for item in raw.split(","):
        if ":" not in item:
            continue
        symbol, percent = item.split(":", 1)
        symbol = symbol.strip().upper()
        percent = percent.strip()
        if not symbol or not percent:
            continue
        thresholds[symbol] = float(percent)

    return thresholds


def get_threshold_for(symbol, thresholds):
    fallback = float(get_env_or_default("ALERT_THRESHOLD_PERCENT", str(DEFAULT_THRESHOLD_PERCENT)))
    return float(thresholds.get(symbol, fallback))


def load_state():
    if not STATE_PATH.exists():
        return {"alerts_sent": {}, "summaries_sent": {}}

    with STATE_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    data.setdefault("alerts_sent", {})
    data.setdefault("summaries_sent", {})
    return data


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=True, indent=2, sort_keys=True)


def prune_state(state, today_str):
    cutoff = datetime.strptime(today_str, "%Y-%m-%d").date() - timedelta(days=14)

    alerts = {
        key: value
        for key, value in state.get("alerts_sent", {}).items()
        if datetime.strptime(value, "%Y-%m-%d").date() >= cutoff
    }
    summaries = {
        key: value
        for key, value in state.get("summaries_sent", {}).items()
        if datetime.strptime(key, "%Y-%m-%d").date() >= cutoff
    }

    state["alerts_sent"] = alerts
    state["summaries_sent"] = summaries


def safe_pct_change(current, baseline):
    if baseline == 0:
        return 0.0
    return ((current - baseline) / baseline) * 100


def get_daily_snapshot(symbol):
    history = yf.Ticker(symbol).history(period="10d", interval="1d", auto_adjust=False)
    if history.empty or len(history.index) < 2:
        raise ValueError(f"No daily price data returned for symbol: {symbol}")

    history = history.dropna(subset=["Close"])
    latest = history.iloc[-1]
    previous = history.iloc[-2]
    last_close = float(latest["Close"])
    prev_close = float(previous["Close"])
    change = last_close - prev_close
    change_pct = safe_pct_change(last_close, prev_close)

    latest_index = history.index[-1]
    latest_date = latest_index.date() if hasattr(latest_index, "date") else latest_index

    return {
        "symbol": symbol,
        "last_close": round(last_close, 2),
        "previous_close": round(prev_close, 2),
        "change": round(change, 2),
        "change_pct": round(change_pct, 2),
        "latest_date": str(latest_date),
    }


def get_intraday_snapshot(symbol, now_market_tz, lookback_minutes):
    history = yf.Ticker(symbol).history(period="5d", interval="15m", auto_adjust=False, prepost=False)
    if history.empty:
        raise ValueError(f"No intraday price data returned for symbol: {symbol}")

    history = history.dropna(subset=["Close"])
    if history.empty:
        raise ValueError(f"No usable intraday rows returned for symbol: {symbol}")

    if history.index.tz is None:
        history.index = history.index.tz_localize("UTC").tz_convert(MARKET_TZ)
    else:
        history.index = history.index.tz_convert(MARKET_TZ)

    today_rows = history[history.index.date == now_market_tz.date()]
    if today_rows.empty:
        raise ValueError(f"No intraday rows for today on symbol: {symbol}")

    latest_idx = today_rows.index[-1]
    latest_close = float(today_rows.iloc[-1]["Close"])
    lookback_cutoff = latest_idx - timedelta(minutes=lookback_minutes)
    baseline_rows = today_rows[today_rows.index <= lookback_cutoff]
    if baseline_rows.empty:
        baseline_rows = today_rows.iloc[:1]

    baseline_close = float(baseline_rows.iloc[-1]["Close"])
    move = latest_close - baseline_close
    move_pct = safe_pct_change(latest_close, baseline_close)

    daily = get_daily_snapshot(symbol)
    return {
        "symbol": symbol,
        "latest_price": round(latest_close, 2),
        "baseline_price": round(baseline_close, 2),
        "move": round(move, 2),
        "move_pct": round(move_pct, 2),
        "latest_time": latest_idx.strftime("%Y-%m-%d %H:%M %Z"),
        "daily_change": daily["change"],
        "daily_change_pct": daily["change_pct"],
    }


def send_email(subject, body):
    sender = os.environ["EMAIL"]
    password = os.environ["PASSWORD"]
    receiver = os.environ.get("RECEIVER", sender)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = receiver

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)


def should_run_intraday(now_market_tz):
    if now_market_tz.weekday() >= 5:
        return False
    start = now_market_tz.replace(hour=9, minute=30, second=0, microsecond=0)
    end = now_market_tz.replace(hour=16, minute=0, second=0, microsecond=0)
    return start <= now_market_tz <= end


def should_send_summary(now_market_tz):
    if now_market_tz.weekday() >= 5:
        return False

    summary_hour, summary_minute = [int(part) for part in get_env_or_default("SUMMARY_TIME", DEFAULT_SUMMARY_TIME).split(":", 1)]
    summary_time = now_market_tz.replace(hour=summary_hour, minute=summary_minute, second=0, microsecond=0)
    return now_market_tz >= summary_time


def build_alert_lines(symbol, intraday, threshold, lookback_minutes):
    direction = "上涨" if intraday["move_pct"] > 0 else "下跌"
    return [
        f"{symbol} 在最近 {lookback_minutes} 分钟{direction}较多",
        f"最新价格: {intraday['latest_price']}",
        f"{lookback_minutes} 分钟变化: {intraday['move']:+.2f} ({intraday['move_pct']:+.2f}%)",
        f"今日相对昨收: {intraday['daily_change']:+.2f} ({intraday['daily_change_pct']:+.2f}%)",
        f"提醒阈值: {threshold:.2f}%",
        f"数据时间: {intraday['latest_time']}",
    ]


def build_summary_lines(today_str, daily_snapshots):
    lines = [f"{today_str} 收盘总结", ""]
    for snapshot in daily_snapshots:
        lines.append(
            f"{snapshot['symbol']}: 收盘 {snapshot['last_close']:.2f} | 日变动 {snapshot['change']:+.2f} ({snapshot['change_pct']:+.2f}%)"
        )
    return lines


def maybe_send_intraday_alerts(symbols, thresholds, state, now_market_tz, today_str):
    if not should_run_intraday(now_market_tz):
        print("Outside market hours for intraday alerts")
        return

    lookback_minutes = int(get_env_or_default("ALERT_LOOKBACK_MINUTES", str(DEFAULT_LOOKBACK_MINUTES)))
    for symbol in symbols:
        threshold = get_threshold_for(symbol, thresholds)
        try:
            intraday = get_intraday_snapshot(symbol, now_market_tz, lookback_minutes)
        except Exception as exc:
            print(f"[WARN] intraday check failed for {symbol}: {exc}")
            continue

        if math.fabs(intraday["move_pct"]) < threshold:
            print(f"[INFO] {symbol} move {intraday['move_pct']:+.2f}% below threshold {threshold:.2f}%")
            continue

        direction = "up" if intraday["move_pct"] > 0 else "down"
        alert_key = f"{today_str}:{symbol}:{direction}"
        if state["alerts_sent"].get(alert_key) == today_str:
            print(f"[INFO] alert already sent for {symbol} {direction} on {today_str}")
            continue

        subject = f"Stock Alert - {symbol} {intraday['move_pct']:+.2f}% in {lookback_minutes}m"
        body = "\n".join(build_alert_lines(symbol, intraday, threshold, lookback_minutes))
        send_email(subject, body)
        state["alerts_sent"][alert_key] = today_str
        print(f"[ALERT] sent intraday alert for {symbol}: {intraday['move_pct']:+.2f}%")


def maybe_send_daily_summary(symbols, state, now_market_tz, today_str):
    if not should_send_summary(now_market_tz):
        print("Too early for daily summary")
        return

    if state["summaries_sent"].get(today_str):
        print(f"[INFO] summary already sent for {today_str}")
        return

    snapshots = []
    for symbol in symbols:
        try:
            snapshot = get_daily_snapshot(symbol)
        except Exception as exc:
            print(f"[WARN] daily summary failed for {symbol}: {exc}")
            continue

        if snapshot["latest_date"] != today_str:
            print(f"[INFO] skipping summary row for {symbol}, latest market date is {snapshot['latest_date']}")
            continue

        snapshots.append(snapshot)

    if not snapshots:
        print("[INFO] no daily summary rows available yet")
        return

    subject = f"Daily Stock Summary - {today_str}"
    body = "\n".join(build_summary_lines(today_str, snapshots))
    send_email(subject, body)
    state["summaries_sent"][today_str] = True
    print(f"[SUMMARY] sent daily summary for {today_str}")


def main():
    now_market_tz = datetime.now(MARKET_TZ)
    today_str = now_market_tz.date().isoformat()
    symbols = parse_symbols()
    thresholds = parse_thresholds()
    state = load_state()
    prune_state(state, today_str)

    print(f"Running stock monitor at {now_market_tz.isoformat()} for {', '.join(symbols)}")
    maybe_send_intraday_alerts(symbols, thresholds, state, now_market_tz, today_str)
    maybe_send_daily_summary(symbols, state, now_market_tz, today_str)
    save_state(state)


if __name__ == "__main__":
    main()
