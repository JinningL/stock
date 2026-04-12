import argparse
import json
import math
import os
import re
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo


DEFAULT_SYMBOLS = ["QQQ", "TSLA", "CRCL"]
DEFAULT_LOOKBACK_MINUTES = 60
DEFAULT_THRESHOLD_PERCENT = 3.0
DEFAULT_SUMMARY_TIME = "16:05"
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 8000

DEFAULT_SYMBOL_THRESHOLDS = {
    "QQQ": 1.5,
    "TSLA": 4.0,
    "CRCL": 8.0,
}

POPULAR_SYMBOLS = [
    "AAPL",
    "AMZN",
    "BRK-B",
    "CRCL",
    "GOOGL",
    "META",
    "MSFT",
    "NVDA",
    "QQQ",
    "SPY",
    "TSLA",
]

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9^][A-Z0-9.\-^]{0,14}$")
MARKET_TZ = ZoneInfo(os.environ.get("MARKET_TIMEZONE", "America/New_York"))
STATE_PATH = Path(os.environ.get("STATE_FILE", ".state/monitor_state.json"))
HISTORY_PATH = Path(os.environ.get("HISTORY_FILE", "data/daily_history.json"))
REPORT_PATH = Path(os.environ.get("REPORT_FILE", "docs/index.html"))
CONFIG_PATH = Path(os.environ.get("CONFIG_FILE", "data/monitor_config.json"))


def get_env_or_default(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def load_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)


def deep_copy_json(data):
    return json.loads(json.dumps(data))


def default_threshold_for_symbol(symbol, fallback=DEFAULT_THRESHOLD_PERCENT):
    return float(DEFAULT_SYMBOL_THRESHOLDS.get(symbol, fallback))


def build_default_config(fallback_threshold=DEFAULT_THRESHOLD_PERCENT):
    return {
        "lookback_minutes": DEFAULT_LOOKBACK_MINUTES,
        "summary_time": DEFAULT_SUMMARY_TIME,
        "symbols": [
            {
                "symbol": symbol,
                "threshold": default_threshold_for_symbol(symbol, fallback_threshold),
            }
            for symbol in DEFAULT_SYMBOLS
        ],
    }


def normalize_symbol(value):
    symbol = str(value or "").strip().upper()
    return symbol if SYMBOL_PATTERN.fullmatch(symbol) else ""


def normalize_time_string(value, fallback):
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{2}:\d{2}", text):
        return fallback
    hour, minute = [int(part) for part in text.split(":", 1)]
    if hour > 23 or minute > 59:
        return fallback
    return f"{hour:02d}:{minute:02d}"


def coerce_positive_float(value, fallback):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    if number <= 0:
        return float(fallback)
    return round(number, 2)


def coerce_positive_int(value, fallback):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(fallback)
    if number <= 0:
        return int(fallback)
    return number


def parse_symbol_list(raw):
    symbols = []
    seen = set()
    for item in str(raw or "").split(","):
        symbol = normalize_symbol(item)
        if symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    return symbols


def parse_threshold_overrides(raw, fallback=DEFAULT_THRESHOLD_PERCENT):
    overrides = {}
    for item in str(raw or "").split(","):
        if ":" not in item:
            continue
        symbol, threshold = item.split(":", 1)
        symbol = normalize_symbol(symbol)
        if not symbol:
            continue
        overrides[symbol] = coerce_positive_float(threshold, default_threshold_for_symbol(symbol, fallback))
    return overrides


def build_symbol_entries(symbols, threshold_map=None, fallback_threshold=DEFAULT_THRESHOLD_PERCENT):
    threshold_map = threshold_map or {}
    entries = []
    seen = set()
    for raw_symbol in symbols:
        symbol = normalize_symbol(raw_symbol)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        threshold = threshold_map.get(symbol, default_threshold_for_symbol(symbol, fallback_threshold))
        entries.append({"symbol": symbol, "threshold": coerce_positive_float(threshold, default_threshold_for_symbol(symbol, fallback_threshold))})
    return entries


def normalize_config(raw_config, fallback_threshold=DEFAULT_THRESHOLD_PERCENT):
    base = build_default_config(fallback_threshold)
    if not isinstance(raw_config, dict):
        return base

    symbols_value = raw_config.get("symbols", [])
    threshold_map = {}
    raw_symbols = []
    if isinstance(symbols_value, list):
        for item in symbols_value:
            if isinstance(item, dict):
                symbol = normalize_symbol(item.get("symbol"))
                if not symbol:
                    continue
                raw_symbols.append(symbol)
                if symbol not in threshold_map:
                    threshold_map[symbol] = coerce_positive_float(
                        item.get("threshold"),
                        default_threshold_for_symbol(symbol, fallback_threshold),
                    )
            else:
                symbol = normalize_symbol(item)
                if not symbol:
                    continue
                raw_symbols.append(symbol)

    symbol_entries = build_symbol_entries(raw_symbols, threshold_map, fallback_threshold)
    if not symbol_entries:
        symbol_entries = base["symbols"]

    return {
        "lookback_minutes": coerce_positive_int(raw_config.get("lookback_minutes"), base["lookback_minutes"]),
        "summary_time": normalize_time_string(raw_config.get("summary_time"), base["summary_time"]),
        "symbols": symbol_entries,
    }


def load_config_file(create_if_missing=False):
    default_config = build_default_config()
    if not CONFIG_PATH.exists():
        if create_if_missing:
            save_json(CONFIG_PATH, default_config)
        return default_config
    return normalize_config(load_json(CONFIG_PATH, default_config))


def save_monitor_config(raw_config):
    config = normalize_config(raw_config)
    save_json(CONFIG_PATH, config)
    return config


def apply_env_overrides(config):
    result = deep_copy_json(config)
    fallback_threshold = coerce_positive_float(
        os.environ.get("ALERT_THRESHOLD_PERCENT"),
        DEFAULT_THRESHOLD_PERCENT,
    )
    existing_thresholds = {item["symbol"]: item["threshold"] for item in result["symbols"]}

    raw_symbols = os.environ.get("STOCK_SYMBOLS")
    if raw_symbols and raw_symbols.strip():
        symbols = parse_symbol_list(raw_symbols)
        merged_thresholds = {
            symbol: existing_thresholds.get(symbol, default_threshold_for_symbol(symbol, fallback_threshold))
            for symbol in symbols
        }
        result["symbols"] = build_symbol_entries(symbols, merged_thresholds, fallback_threshold)

    raw_thresholds = os.environ.get("ALERT_THRESHOLDS")
    if raw_thresholds and raw_thresholds.strip():
        overrides = parse_threshold_overrides(raw_thresholds, fallback_threshold)
        result["symbols"] = build_symbol_entries(
            [item["symbol"] for item in result["symbols"]],
            {
                item["symbol"]: overrides.get(item["symbol"], item["threshold"])
                for item in result["symbols"]
            },
            fallback_threshold,
        )

    result["lookback_minutes"] = coerce_positive_int(
        os.environ.get("ALERT_LOOKBACK_MINUTES"),
        result["lookback_minutes"],
    )
    result["summary_time"] = normalize_time_string(
        os.environ.get("SUMMARY_TIME"),
        result["summary_time"],
    )
    return result


def load_active_config():
    return apply_env_overrides(load_config_file(create_if_missing=True))


def get_symbols(config):
    return [item["symbol"] for item in config["symbols"]]


def get_threshold_map(config):
    return {item["symbol"]: float(item["threshold"]) for item in config["symbols"]}


def load_state():
    data = load_json(STATE_PATH, {"alerts_sent": {}, "summaries_sent": {}})
    data.setdefault("alerts_sent", {})
    data.setdefault("summaries_sent", {})
    return data


def save_state(state):
    save_json(STATE_PATH, state)


def prune_state(state, today_str):
    cutoff = datetime.strptime(today_str, "%Y-%m-%d").date() - timedelta(days=14)

    state["alerts_sent"] = {
        key: value
        for key, value in state.get("alerts_sent", {}).items()
        if datetime.strptime(value, "%Y-%m-%d").date() >= cutoff
    }
    state["summaries_sent"] = {
        key: value
        for key, value in state.get("summaries_sent", {}).items()
        if datetime.strptime(key, "%Y-%m-%d").date() >= cutoff
    }


def load_history():
    history = load_json(HISTORY_PATH, {"records": []})
    history.setdefault("records", [])
    return history


def save_history(history):
    save_json(HISTORY_PATH, history)


def safe_pct_change(current, baseline):
    if baseline == 0:
        return 0.0
    return ((current - baseline) / baseline) * 100


def format_signed(value):
    return f"{value:+.2f}"


def format_pct(value):
    return f"{value:+.2f}%"


def get_yfinance_module():
    try:
        import yfinance as yf  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("yfinance is required to fetch market data. Run pip install -r requirements.txt.") from exc
    return yf


def get_daily_snapshot(symbol):
    yf = get_yfinance_module()
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
    yf = get_yfinance_module()
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


def build_alert_email_text(symbol, intraday, threshold, lookback_minutes):
    direction = "上涨" if intraday["move_pct"] > 0 else "下跌"
    return "\n".join(
        [
            "股票提醒已触发",
            "",
            f"股票: {symbol}",
            f"方向: {direction}",
            f"当前价格: {intraday['latest_price']:.2f}",
            f"{lookback_minutes} 分钟前基准价: {intraday['baseline_price']:.2f}",
            f"{lookback_minutes} 分钟涨跌: {format_signed(intraday['move'])} ({format_pct(intraday['move_pct'])})",
            f"今日相对昨收: {format_signed(intraday['daily_change'])} ({format_pct(intraday['daily_change_pct'])})",
            f"你的提醒阈值: {threshold:.2f}%",
            f"触发时间: {intraday['latest_time']}",
            "",
            f"触发原因: {symbol} 在最近 {lookback_minutes} 分钟内的波动已经超过你设置的 {threshold:.2f}% 阈值。",
        ]
    )


def build_alert_email_html(symbol, intraday, threshold, lookback_minutes):
    tone_color = "#0f7b53" if intraday["move_pct"] > 0 else "#b54833"
    direction = "上涨" if intraday["move_pct"] > 0 else "下跌"
    return f"""<!doctype html>
<html lang="zh-CN">
  <body style="margin:0;padding:24px;background:#f7f3eb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1b2733;">
    <div style="max-width:640px;margin:0 auto;background:#fffdf9;border:1px solid #eadfce;border-radius:20px;padding:24px;">
      <p style="margin:0 0 8px;font-size:12px;letter-spacing:0.16em;text-transform:uppercase;color:#7a6d5c;">Stock Alert</p>
      <h1 style="margin:0 0 8px;font-size:30px;line-height:1.05;">{escape(symbol)} 触发提醒</h1>
      <p style="margin:0 0 20px;color:#5f6b75;font-size:15px;">最近 {lookback_minutes} 分钟 {direction}幅度已经超过你设置的阈值。</p>

      <div style="border-radius:18px;background:#f6efe2;padding:18px;margin-bottom:18px;">
        <p style="margin:0;color:#5f6b75;font-size:13px;">核心结论</p>
        <p style="margin:8px 0 0;font-size:28px;font-weight:700;color:{tone_color};">{format_pct(intraday['move_pct'])}</p>
        <p style="margin:6px 0 0;font-size:14px;color:#42505d;">当前价格 {intraday['latest_price']:.2f}，阈值 {threshold:.2f}%</p>
      </div>

      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #eadfce;color:#5f6b75;">当前价格</td>
          <td style="padding:10px 0;border-bottom:1px solid #eadfce;text-align:right;">{intraday['latest_price']:.2f}</td>
        </tr>
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #eadfce;color:#5f6b75;">基准价格</td>
          <td style="padding:10px 0;border-bottom:1px solid #eadfce;text-align:right;">{intraday['baseline_price']:.2f}</td>
        </tr>
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #eadfce;color:#5f6b75;">{lookback_minutes} 分钟涨跌</td>
          <td style="padding:10px 0;border-bottom:1px solid #eadfce;text-align:right;color:{tone_color};">{format_signed(intraday['move'])} ({format_pct(intraday['move_pct'])})</td>
        </tr>
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #eadfce;color:#5f6b75;">今日相对昨收</td>
          <td style="padding:10px 0;border-bottom:1px solid #eadfce;text-align:right;">{format_signed(intraday['daily_change'])} ({format_pct(intraday['daily_change_pct'])})</td>
        </tr>
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #eadfce;color:#5f6b75;">你的提醒阈值</td>
          <td style="padding:10px 0;border-bottom:1px solid #eadfce;text-align:right;">{threshold:.2f}%</td>
        </tr>
        <tr>
          <td style="padding:10px 0;color:#5f6b75;">触发时间</td>
          <td style="padding:10px 0;text-align:right;">{escape(intraday['latest_time'])}</td>
        </tr>
      </table>
    </div>
  </body>
</html>
"""


def build_summary_email_text(today_str, snapshots, threshold_map, config):
    lines = [
        f"每日股票收盘摘要 | {today_str}",
        "",
        f"盘中提醒规则: 最近 {config['lookback_minutes']} 分钟波动超过阈值就发邮件",
        f"每日摘要时间: {config['summary_time']}",
        "",
    ]
    for snapshot in snapshots:
        threshold = threshold_map.get(snapshot["symbol"], DEFAULT_THRESHOLD_PERCENT)
        lines.append(
            f"{snapshot['symbol']}: 收盘 {snapshot['last_close']:.2f} | 日涨跌 {format_signed(snapshot['change'])} ({format_pct(snapshot['change_pct'])}) | 盘中阈值 {threshold:.2f}%"
        )
    return "\n".join(lines)


def build_summary_email_html(today_str, snapshots, threshold_map, config):
    rows = []
    for snapshot in snapshots:
        tone_color = "#0f7b53" if snapshot["change"] > 0 else "#b54833" if snapshot["change"] < 0 else "#61707d"
        threshold = threshold_map.get(snapshot["symbol"], DEFAULT_THRESHOLD_PERCENT)
        rows.append(
            "<tr>"
            f"<td style=\"padding:12px 0;border-bottom:1px solid #eadfce;font-weight:600;\">{escape(snapshot['symbol'])}</td>"
            f"<td style=\"padding:12px 0;border-bottom:1px solid #eadfce;text-align:right;\">{snapshot['last_close']:.2f}</td>"
            f"<td style=\"padding:12px 0;border-bottom:1px solid #eadfce;text-align:right;color:{tone_color};\">{format_signed(snapshot['change'])}</td>"
            f"<td style=\"padding:12px 0;border-bottom:1px solid #eadfce;text-align:right;color:{tone_color};\">{format_pct(snapshot['change_pct'])}</td>"
            f"<td style=\"padding:12px 0;border-bottom:1px solid #eadfce;text-align:right;\">{threshold:.2f}%</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="zh-CN">
  <body style="margin:0;padding:24px;background:#f7f3eb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1b2733;">
    <div style="max-width:720px;margin:0 auto;background:#fffdf9;border:1px solid #eadfce;border-radius:20px;padding:24px;">
      <p style="margin:0 0 8px;font-size:12px;letter-spacing:0.16em;text-transform:uppercase;color:#7a6d5c;">Daily Summary</p>
      <h1 style="margin:0 0 8px;font-size:30px;line-height:1.05;">每日股票收盘摘要</h1>
      <p style="margin:0 0 18px;color:#5f6b75;font-size:15px;">日期 {today_str}。盘中提醒规则：最近 {config['lookback_minutes']} 分钟波动超过阈值就发邮件。</p>

      <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px;">
        <span style="padding:10px 14px;border:1px solid #eadfce;border-radius:999px;background:#faf5ec;font-size:14px;">股票数 {len(snapshots)}</span>
        <span style="padding:10px 14px;border:1px solid #eadfce;border-radius:999px;background:#faf5ec;font-size:14px;">摘要时间 {escape(config['summary_time'])}</span>
      </div>

      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <thead>
          <tr>
            <th style="padding:0 0 10px;text-align:left;color:#5f6b75;font-weight:600;">股票</th>
            <th style="padding:0 0 10px;text-align:right;color:#5f6b75;font-weight:600;">收盘</th>
            <th style="padding:0 0 10px;text-align:right;color:#5f6b75;font-weight:600;">涨跌</th>
            <th style="padding:0 0 10px;text-align:right;color:#5f6b75;font-weight:600;">涨跌幅</th>
            <th style="padding:0 0 10px;text-align:right;color:#5f6b75;font-weight:600;">盘中阈值</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows)}
        </tbody>
      </table>
    </div>
  </body>
</html>
"""


def send_email(subject, text_body, html_body=None):
    sender = os.environ.get("EMAIL")
    password = os.environ.get("PASSWORD")
    receiver = os.environ.get("RECEIVER", sender or "")
    if not sender or not password:
        raise RuntimeError("EMAIL and PASSWORD must be configured before sending mail")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = receiver
    msg.set_content(text_body, subtype="plain", charset="utf-8")
    if html_body:
        msg.add_alternative(html_body, subtype="html", charset="utf-8")

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


def should_send_summary(now_market_tz, summary_time):
    if now_market_tz.weekday() >= 5:
        return False

    summary_hour, summary_minute = [int(part) for part in summary_time.split(":", 1)]
    summary_dt = now_market_tz.replace(hour=summary_hour, minute=summary_minute, second=0, microsecond=0)
    return now_market_tz >= summary_dt


def upsert_history_records(history, snapshots):
    changed = False
    record_map = {(item["date"], item["symbol"]): item for item in history["records"]}
    for snapshot in snapshots:
        key = (snapshot["latest_date"], snapshot["symbol"])
        record = {
            "date": snapshot["latest_date"],
            "symbol": snapshot["symbol"],
            "close": snapshot["last_close"],
            "previous_close": snapshot["previous_close"],
            "change": snapshot["change"],
            "change_pct": snapshot["change_pct"],
        }
        if record_map.get(key) != record:
            record_map[key] = record
            changed = True

    history["records"] = sorted(record_map.values(), key=lambda item: (item["date"], item["symbol"]))
    return changed


def build_sparkline(points, width=220, height=72):
    if not points:
        return ""
    if len(points) == 1:
        value = points[0]
        points = [value, value]

    min_value = min(points)
    max_value = max(points)
    spread = max(max_value - min_value, 0.01)
    step_x = width / (len(points) - 1)

    coords = []
    for index, value in enumerate(points):
        x = index * step_x
        y = height - ((value - min_value) / spread) * height
        coords.append(f"{x:.1f},{y:.1f}")
    return " ".join(coords)


def build_symbol_sections(config, history):
    sections = []
    records = history["records"]
    threshold_map = get_threshold_map(config)

    for symbol in get_symbols(config):
        rows = [item for item in records if item["symbol"] == symbol]
        threshold_label = f"{threshold_map.get(symbol, DEFAULT_THRESHOLD_PERCENT):.2f}%"
        if not rows:
            sections.append(
                {
                    "symbol": symbol,
                    "threshold": threshold_label,
                    "last_close": "--",
                    "change": "--",
                    "change_pct": "--",
                    "last_date": "还没有收盘数据",
                    "sparkline": "",
                    "tone": "flat",
                    "table_rows": '<tr><td colspan="4">还没有历史记录。</td></tr>',
                }
            )
            continue

        latest = rows[-1]
        sparkline = build_sparkline([row["close"] for row in rows[-20:]])
        table_rows = []
        for row in reversed(rows[-8:]):
            tone = "up" if row["change"] > 0 else "down" if row["change"] < 0 else "flat"
            table_rows.append(
                "<tr>"
                f"<td>{escape(row['date'])}</td>"
                f"<td>{row['close']:.2f}</td>"
                f"<td class=\"{tone}\">{format_signed(row['change'])}</td>"
                f"<td class=\"{tone}\">{format_pct(row['change_pct'])}</td>"
                "</tr>"
            )

        sections.append(
            {
                "symbol": symbol,
                "threshold": threshold_label,
                "last_close": f"{latest['close']:.2f}",
                "change": format_signed(latest["change"]),
                "change_pct": format_pct(latest["change_pct"]),
                "last_date": latest["date"],
                "sparkline": sparkline,
                "tone": "up" if latest["change"] > 0 else "down" if latest["change"] < 0 else "flat",
                "table_rows": "".join(table_rows),
            }
        )

    return sections


def render_report(config, history, generated_at):
    sections = build_symbol_sections(config, history)
    symbols = get_symbols(config)
    datalist_options = "".join(f"<option value=\"{escape(symbol)}\"></option>" for symbol in POPULAR_SYMBOLS)
    last_updated = generated_at.strftime("%Y-%m-%d %H:%M %Z")
    config_json = json.dumps(config, ensure_ascii=True)
    cards = []
    tables = []

    for section in sections:
        sparkline_block = (
            f'<svg viewBox="0 0 220 72" class="sparkline" preserveAspectRatio="none"><polyline points="{section["sparkline"]}" /></svg>'
            if section.get("sparkline")
            else '<div class="empty-chart">等待下一次收盘数据</div>'
        )
        cards.append(
            f"""
            <article class="card {section['tone']}">
              <div class="card-top">
                <div>
                  <p class="eyebrow">Watchlist</p>
                  <h3>{escape(section['symbol'])}</h3>
                </div>
                <div class="mini-pills">
                  <span class="mini-pill">阈值 {escape(section['threshold'])}</span>
                  <span class="mini-pill">{escape(section['last_date'])}</span>
                </div>
              </div>
              <div class="price-row">
                <div>
                  <p class="metric-label">最近收盘</p>
                  <p class="price">{section['last_close']}</p>
                </div>
                <div class="delta-block">
                  <p class="metric-label">当日涨跌</p>
                  <p class="delta">{section['change']}</p>
                  <p class="delta-pct">{section['change_pct']}</p>
                </div>
              </div>
              {sparkline_block}
            </article>
            """
        )
        tables.append(
            f"""
            <section class="table-card">
              <div class="table-head">
                <div>
                  <p class="eyebrow">Recent closes</p>
                  <h3>{escape(section['symbol'])}</h3>
                </div>
                <div class="mini-pills">
                  <span class="mini-pill">阈值 {escape(section['threshold'])}</span>
                  <span class="mini-pill">{escape(section['last_date'])}</span>
                </div>
              </div>
              <table>
                <thead>
                  <tr>
                    <th>日期</th>
                    <th>收盘</th>
                    <th>涨跌</th>
                    <th>涨跌幅</th>
                  </tr>
                </thead>
                <tbody>
                  {section['table_rows']}
                </tbody>
              </table>
            </section>
            """
        )

    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>股票提醒面板</title>
    <style>
      :root {{
        --bg: #f6f1e8;
        --panel: rgba(255, 252, 247, 0.88);
        --ink: #18242f;
        --muted: #6a737d;
        --line: rgba(24, 36, 47, 0.12);
        --up: #0f7b53;
        --down: #b54833;
        --flat: #61707d;
        --accent: #d7e7d3;
        --warm: #f1e1c8;
        --info: #e8f0fb;
        --shadow: 0 18px 48px rgba(24, 36, 47, 0.12);
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: "Avenir Next", "Segoe UI", sans-serif;
        color: var(--ink);
        background:
          radial-gradient(circle at top left, rgba(215, 231, 211, 0.9), transparent 38%),
          radial-gradient(circle at top right, rgba(228, 209, 182, 0.65), transparent 34%),
          linear-gradient(180deg, #fbf7f0 0%, var(--bg) 100%);
      }}
      .shell {{
        width: min(1160px, calc(100% - 32px));
        margin: 0 auto;
        padding: 42px 0 72px;
      }}
      h1, h2, h3, p {{ margin: 0; }}
      .hero {{
        display: grid;
        gap: 16px;
        margin-bottom: 24px;
      }}
      .eyebrow {{
        margin: 0 0 8px;
        font-size: 12px;
        letter-spacing: 0.18em;
        text-transform: uppercase;
        color: var(--muted);
      }}
      h1 {{
        font-family: Georgia, "Times New Roman", serif;
        font-size: clamp(2.4rem, 5vw, 4.8rem);
        line-height: 0.95;
        letter-spacing: -0.05em;
        max-width: 10ch;
      }}
      .subtitle {{
        max-width: 58rem;
        color: var(--muted);
        font-size: 1.04rem;
        line-height: 1.65;
      }}
      .status-bar {{
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
      }}
      .pill, .mini-pill {{
        border: 1px solid var(--line);
        border-radius: 999px;
        background: rgba(255, 255, 255, 0.68);
        padding: 10px 14px;
        font-size: 0.92rem;
      }}
      .mini-pills {{
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        justify-content: flex-end;
      }}
      .mini-pill {{
        padding: 8px 10px;
        font-size: 0.82rem;
      }}
      .config-panel, .card, .table-card {{
        background: var(--panel);
        border: 1px solid rgba(255, 255, 255, 0.84);
        border-radius: 24px;
        box-shadow: var(--shadow);
        backdrop-filter: blur(12px);
      }}
      .config-panel {{
        padding: 24px;
        margin: 24px 0 28px;
      }}
      .config-head {{
        display: grid;
        gap: 10px;
      }}
      .section-title {{
        font-size: 1.7rem;
        letter-spacing: -0.03em;
      }}
      .notice {{
        border-radius: 16px;
        padding: 14px 16px;
        font-size: 0.95rem;
        line-height: 1.55;
        border: 1px solid var(--line);
        background: rgba(255, 255, 255, 0.62);
      }}
      .notice.live {{ background: var(--accent); }}
      .notice.warn {{ background: #f8ead7; }}
      .notice.error {{ background: #f7dbd6; }}
      .form-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 14px;
        margin: 18px 0 16px;
      }}
      label {{
        display: grid;
        gap: 8px;
        font-size: 0.93rem;
        color: var(--muted);
      }}
      input, button {{
        font: inherit;
      }}
      input {{
        width: 100%;
        padding: 12px 14px;
        border-radius: 14px;
        border: 1px solid var(--line);
        background: rgba(255, 255, 255, 0.85);
        color: var(--ink);
      }}
      input:focus {{
        outline: 2px solid rgba(24, 36, 47, 0.15);
        outline-offset: 0;
      }}
      .symbol-list {{
        display: grid;
        gap: 12px;
      }}
      .symbol-row {{
        display: grid;
        grid-template-columns: minmax(140px, 1.2fr) minmax(140px, 1fr) auto;
        gap: 10px;
        align-items: end;
        padding: 14px;
        border-radius: 18px;
        background: rgba(255, 255, 255, 0.62);
        border: 1px solid var(--line);
      }}
      .symbol-row-title {{
        font-size: 0.8rem;
        text-transform: uppercase;
        letter-spacing: 0.12em;
        color: var(--muted);
      }}
      .button-row {{
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
        margin-top: 16px;
      }}
      button {{
        border: none;
        border-radius: 999px;
        padding: 11px 16px;
        cursor: pointer;
        background: var(--ink);
        color: white;
      }}
      button.secondary {{
        background: rgba(24, 36, 47, 0.08);
        color: var(--ink);
      }}
      button.ghost {{
        background: transparent;
        border: 1px solid var(--line);
        color: var(--muted);
      }}
      .tips {{
        margin-top: 14px;
        color: var(--muted);
        font-size: 0.92rem;
        line-height: 1.6;
      }}
      .grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
        gap: 16px;
        margin: 28px 0 20px;
      }}
      .card {{
        padding: 22px;
      }}
      .card-top, .table-head {{
        display: flex;
        justify-content: space-between;
        gap: 12px;
        align-items: start;
      }}
      .price-row {{
        display: flex;
        justify-content: space-between;
        gap: 14px;
        align-items: end;
        margin: 18px 0 14px;
      }}
      .metric-label {{
        color: var(--muted);
        font-size: 0.88rem;
        margin-bottom: 6px;
      }}
      .price {{
        font-family: Georgia, "Times New Roman", serif;
        font-size: 2.4rem;
        letter-spacing: -0.05em;
      }}
      .delta-block {{
        text-align: right;
        color: inherit;
      }}
      .delta {{
        font-size: 1.45rem;
        font-weight: 700;
      }}
      .delta-pct {{
        margin-top: 2px;
      }}
      .card.up, .up {{ color: var(--up); }}
      .card.down, .down {{ color: var(--down); }}
      .card.flat, .flat {{ color: var(--flat); }}
      .sparkline {{
        width: 100%;
        height: 72px;
        margin-top: 10px;
      }}
      .sparkline polyline {{
        fill: none;
        stroke: currentColor;
        stroke-width: 3;
        stroke-linecap: round;
        stroke-linejoin: round;
      }}
      .empty-chart {{
        height: 72px;
        margin-top: 10px;
        border-radius: 16px;
        border: 1px dashed var(--line);
        display: grid;
        place-items: center;
        color: var(--muted);
        font-size: 0.92rem;
      }}
      .table-stack {{
        display: grid;
        gap: 16px;
        margin-top: 12px;
      }}
      .table-card {{
        padding: 20px;
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
        margin-top: 14px;
      }}
      th, td {{
        padding: 12px 0;
        text-align: left;
        border-bottom: 1px solid var(--line);
        font-size: 0.95rem;
      }}
      th {{
        color: var(--muted);
        font-weight: 600;
      }}
      tbody tr:last-child td {{
        border-bottom: none;
      }}
      .footer {{
        margin-top: 28px;
        color: var(--muted);
        font-size: 0.92rem;
        line-height: 1.7;
      }}
      code {{
        font-family: "SFMono-Regular", "Menlo", monospace;
        font-size: 0.9em;
      }}
      @media (max-width: 760px) {{
        .shell {{
          width: min(100% - 20px, 1160px);
          padding-top: 28px;
        }}
        .price-row, .card-top, .table-head {{
          display: block;
        }}
        .delta-block {{
          text-align: left;
          margin-top: 12px;
        }}
        .symbol-row {{
          grid-template-columns: 1fr;
        }}
      }}
    </style>
  </head>
  <body>
    <main class="shell">
      <section class="hero">
        <p class="eyebrow">Stock Monitor</p>
        <h1>选股票、调阈值，再看每天收盘表现。</h1>
        <p class="subtitle">这个页面现在不仅展示收盘历史，也能作为你的提醒配置面板。用 <code>python main.py serve</code> 打开时，你可以直接在网页里修改股票列表、每只股票的提醒阈值、盘中回看分钟数和每日摘要时间。</p>
        <div class="status-bar">
          <div class="pill">股票: {escape(", ".join(symbols))}</div>
          <div class="pill">盘中回看: {config['lookback_minutes']} 分钟</div>
          <div class="pill">摘要时间: {escape(config['summary_time'])}</div>
          <div class="pill">历史记录: {len(history['records'])}</div>
          <div class="pill">生成时间: {escape(last_updated)}</div>
        </div>
      </section>

      <section class="config-panel">
        <div class="config-head">
          <p class="eyebrow">Alert Settings</p>
          <h2 class="section-title">在网页里配置监控股票和提醒幅度</h2>
          <p class="subtitle">提醒规则很直接：系统会比较最近一段时间内的价格波动，只要绝对涨跌幅超过你设置的阈值，就发送邮件。每个方向每天只发一次，避免刷屏。</p>
          <div id="config-status" class="notice">正在加载当前设置...</div>
        </div>

        <form id="config-form">
          <div class="form-grid">
            <label>
              <span>盘中回看分钟数</span>
              <input id="lookback-minutes" name="lookback_minutes" type="number" min="1" step="1" required>
            </label>
            <label>
              <span>每日摘要时间</span>
              <input id="summary-time" name="summary_time" type="time" required>
            </label>
          </div>

          <div id="symbol-list" class="symbol-list"></div>
          <datalist id="popular-symbols">{datalist_options}</datalist>

          <div class="button-row">
            <button type="button" id="add-symbol">新增股票</button>
            <button type="submit" id="save-config">保存设置</button>
            <button type="button" id="reset-config" class="secondary">恢复已保存</button>
          </div>
          <p class="tips">股票代码示例：<code>QQQ</code>、<code>TSLA</code>、<code>AAPL</code>、<code>BRK-B</code>。阈值填百分比数字，例如 <code>2.5</code> 表示波动超过 2.5% 就提醒。</p>
        </form>
      </section>

      <section class="grid">
        {''.join(cards)}
      </section>

      <section class="table-stack">
        {''.join(tables)}
      </section>

      <p class="footer">静态报告页会展示当前配置和收盘历史；真正保存配置需要本地运行 <code>python main.py serve</code>。定时监控脚本仍然使用 <code>python main.py</code>，并会优先读取 <code>{escape(str(CONFIG_PATH))}</code>，同时兼容原有环境变量覆盖。</p>
    </main>

    <script>
      const INITIAL_CONFIG = {config_json};

      function cloneConfig(config) {{
        return JSON.parse(JSON.stringify(config));
      }}

      function setStatus(message, tone) {{
        const status = document.getElementById("config-status");
        status.textContent = message;
        status.className = "notice" + (tone ? " " + tone : "");
      }}

      function makeRow(entry) {{
        const row = document.createElement("div");
        row.className = "symbol-row";
        row.innerHTML = `
          <label>
            <span class="symbol-row-title">股票代码</span>
            <input class="symbol-input" list="popular-symbols" maxlength="15" placeholder="例如 AAPL" value="${{entry.symbol || ""}}" required>
          </label>
          <label>
            <span class="symbol-row-title">提醒阈值 (%)</span>
            <input class="threshold-input" type="number" min="0.1" step="0.1" placeholder="例如 2.5" value="${{entry.threshold || ""}}" required>
          </label>
          <button type="button" class="ghost remove-symbol">删除</button>
        `;
        row.querySelector(".remove-symbol").addEventListener("click", () => {{
          row.remove();
        }});
        return row;
      }}

      function renderSymbolRows(config) {{
        const list = document.getElementById("symbol-list");
        list.innerHTML = "";
        config.symbols.forEach((entry) => list.appendChild(makeRow(entry)));
      }}

      function populateForm(config) {{
        document.getElementById("lookback-minutes").value = config.lookback_minutes;
        document.getElementById("summary-time").value = config.summary_time;
        renderSymbolRows(config);
      }}

      function readForm() {{
        const rows = Array.from(document.querySelectorAll(".symbol-row"));
        const symbols = rows
          .map((row) => {{
            const symbol = row.querySelector(".symbol-input").value.trim().toUpperCase();
            const threshold = Number(row.querySelector(".threshold-input").value);
            return {{ symbol, threshold }};
          }})
          .filter((item) => item.symbol);

        if (!symbols.length) {{
          throw new Error("至少保留一只股票。");
        }}

        const seen = new Set();
        for (const item of symbols) {{
          if (!/^[A-Z0-9^][A-Z0-9.\\-^]{{0,14}}$/.test(item.symbol)) {{
            throw new Error("股票代码格式不对，请只使用字母、数字、点或横线。");
          }}
          if (seen.has(item.symbol)) {{
            throw new Error("股票代码不能重复。");
          }}
          if (!Number.isFinite(item.threshold) || item.threshold <= 0) {{
            throw new Error(`请给 ${{item.symbol}} 填一个大于 0 的阈值。`);
          }}
          seen.add(item.symbol);
        }}

        return {{
          lookback_minutes: Number(document.getElementById("lookback-minutes").value),
          summary_time: document.getElementById("summary-time").value,
          symbols,
        }};
      }}

      async function tryLoadServerConfig() {{
        const response = await fetch("/api/config", {{ headers: {{ "Accept": "application/json" }} }});
        if (!response.ok) {{
          throw new Error("config fetch failed");
        }}
        const payload = await response.json();
        return payload.config;
      }}

      let savedConfig = cloneConfig(INITIAL_CONFIG);
      let liveMode = false;

      document.getElementById("add-symbol").addEventListener("click", () => {{
        document.getElementById("symbol-list").appendChild(makeRow({{ symbol: "", threshold: 2.0 }}));
      }});

      document.getElementById("reset-config").addEventListener("click", () => {{
        populateForm(savedConfig);
        setStatus(
          liveMode
            ? "已恢复到当前保存的服务器配置。"
            : "已恢复到页面里最后一次加载的配置。",
          liveMode ? "live" : ""
        );
      }});

      document.getElementById("config-form").addEventListener("submit", async (event) => {{
        event.preventDefault();
        let nextConfig;
        try {{
          nextConfig = readForm();
        }} catch (error) {{
          setStatus(error.message, "error");
          return;
        }}

        localStorage.setItem("stock-monitor-draft-config", JSON.stringify(nextConfig));

        try {{
          const response = await fetch("/api/config", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify(nextConfig),
          }});
          if (!response.ok) {{
            throw new Error("save failed");
          }}
          const payload = await response.json();
          savedConfig = payload.config;
          liveMode = true;
          localStorage.removeItem("stock-monitor-draft-config");
          populateForm(savedConfig);
          setStatus("设置已保存到 data/monitor_config.json。刷新页面后，新的股票和阈值会用于后续提醒。", "live");
          window.setTimeout(() => window.location.reload(), 700);
        }} catch (error) {{
          setStatus("当前页面是静态模式，设置只保存在这个浏览器里做预览。要真正保存，请运行 python main.py serve。", "warn");
        }}
      }});

      (async () => {{
        const draft = localStorage.getItem("stock-monitor-draft-config");
        if (draft) {{
          try {{
            savedConfig = JSON.parse(draft);
          }} catch (error) {{
            localStorage.removeItem("stock-monitor-draft-config");
          }}
        }}

        try {{
          savedConfig = await tryLoadServerConfig();
          liveMode = true;
          setStatus("当前为本地可写模式。保存后会直接写入 data/monitor_config.json。", "live");
        }} catch (error) {{
          if (draft) {{
            setStatus("当前是静态报告页。你上次在浏览器里改过的设置已经恢复，但还没有真正保存到项目里。", "warn");
          }} else {{
            setStatus("当前是静态报告页。你可以先在这里预览设置；要真正保存，请在项目目录运行 python main.py serve。", "warn");
          }}
        }}

        populateForm(savedConfig);
      }})();
    </script>
  </body>
</html>
"""


def write_report(config, history, generated_at):
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(render_report(config, history, generated_at), encoding="utf-8")


def refresh_history_for_report(config, history, now_market_tz):
    updated_snapshots = []
    for symbol in get_symbols(config):
        try:
            snapshot = get_daily_snapshot(symbol)
        except Exception as exc:
            print(f"[WARN] report refresh failed for {symbol}: {exc}")
            continue

        if snapshot["latest_date"] > now_market_tz.date().isoformat():
            continue
        updated_snapshots.append(snapshot)

    if updated_snapshots:
        upsert_history_records(history, updated_snapshots)


def maybe_send_intraday_alerts(config, state, now_market_tz, today_str):
    if not should_run_intraday(now_market_tz):
        print("Outside market hours for intraday alerts")
        return

    lookback_minutes = config["lookback_minutes"]
    threshold_map = get_threshold_map(config)
    for symbol in get_symbols(config):
        threshold = threshold_map.get(symbol, DEFAULT_THRESHOLD_PERCENT)
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

        subject = f"股票提醒 | {symbol} {format_pct(intraday['move_pct'])} | 超过阈值 {threshold:.2f}%"
        text_body = build_alert_email_text(symbol, intraday, threshold, lookback_minutes)
        html_body = build_alert_email_html(symbol, intraday, threshold, lookback_minutes)
        try:
            send_email(subject, text_body, html_body)
        except Exception as exc:
            print(f"[WARN] failed to send alert mail for {symbol}: {exc}")
            continue

        state["alerts_sent"][alert_key] = today_str
        print(f"[ALERT] sent intraday alert for {symbol}: {intraday['move_pct']:+.2f}%")


def maybe_send_daily_summary(config, state, now_market_tz, today_str):
    if not should_send_summary(now_market_tz, config["summary_time"]):
        print("Too early for daily summary")
        return []

    if state["summaries_sent"].get(today_str):
        print(f"[INFO] summary already sent for {today_str}")
        return []

    snapshots = []
    for symbol in get_symbols(config):
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
        return []

    threshold_map = get_threshold_map(config)
    subject = f"每日股票收盘摘要 | {today_str}"
    text_body = build_summary_email_text(today_str, snapshots, threshold_map, config)
    html_body = build_summary_email_html(today_str, snapshots, threshold_map, config)
    try:
        send_email(subject, text_body, html_body)
    except Exception as exc:
        print(f"[WARN] failed to send daily summary: {exc}")
        return snapshots

    state["summaries_sent"][today_str] = True
    print(f"[SUMMARY] sent daily summary for {today_str}")
    return snapshots


def write_report_from_disk(generated_at=None, active=False):
    generated_at = generated_at or datetime.now(MARKET_TZ)
    config = load_active_config() if active else load_config_file(create_if_missing=True)
    history = load_history()
    write_report(config, history, generated_at)
    return config, history


def run_monitor():
    now_market_tz = datetime.now(MARKET_TZ)
    today_str = now_market_tz.date().isoformat()
    config = load_active_config()
    state = load_state()
    history = load_history()
    prune_state(state, today_str)

    print(
        "Running stock monitor at "
        f"{now_market_tz.isoformat()} for {', '.join(get_symbols(config))} "
        f"(lookback {config['lookback_minutes']}m)"
    )
    maybe_send_intraday_alerts(config, state, now_market_tz, today_str)
    summary_snapshots = maybe_send_daily_summary(config, state, now_market_tz, today_str)
    if summary_snapshots:
        upsert_history_records(history, summary_snapshots)
    else:
        refresh_history_for_report(config, history, now_market_tz)

    save_state(state)
    save_history(history)
    write_report(config, history, now_market_tz)
    print(f"[REPORT] wrote {REPORT_PATH}")


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "StockMonitor/2.0"

    def log_message(self, fmt, *args):
        print(f"[WEB] {self.address_string()} - {fmt % args}")

    def _send_json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html, status=HTTPStatus.OK):
        data = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            config = load_config_file(create_if_missing=True)
            history = load_history()
            html = render_report(config, history, datetime.now(MARKET_TZ))
            self._send_html(html)
            return

        if self.path == "/api/config":
            self._send_json({"config": load_config_file(create_if_missing=True)})
            return

        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if self.path != "/api/config":
            self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length)
            payload = json.loads(raw_body.decode("utf-8") or "{}")
            config = save_monitor_config(payload)
            history = load_history()
            write_report(config, history, datetime.now(MARKET_TZ))
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self._send_json({"ok": True, "config": config})


def serve_dashboard(host, port):
    load_config_file(create_if_missing=True)
    write_report_from_disk()
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    print("Open the page above to edit stocks and thresholds.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server.")
    finally:
        server.server_close()


def parse_args():
    parser = argparse.ArgumentParser(description="Stock monitor and dashboard")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("monitor", help="Run stock checks and send emails")
    subparsers.add_parser("render", help="Render docs/index.html using saved history/config")

    serve_parser = subparsers.add_parser("serve", help="Start the local dashboard for editing config")
    serve_parser.add_argument("--host", default=DEFAULT_SERVER_HOST, help="Host to bind the dashboard server")
    serve_parser.add_argument("--port", default=DEFAULT_SERVER_PORT, type=int, help="Port to bind the dashboard server")

    args = parser.parse_args()
    if not args.command:
        args.command = "monitor"
    return args


def main():
    args = parse_args()
    if args.command == "serve":
        serve_dashboard(args.host, args.port)
        return

    if args.command == "render":
        write_report_from_disk(generated_at=datetime.now(MARKET_TZ), active=False)
        print(f"[REPORT] wrote {REPORT_PATH}")
        return

    run_monitor()


if __name__ == "__main__":
    main()
