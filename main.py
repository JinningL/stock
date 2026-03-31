import json
import math
import os
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf


DEFAULT_SYMBOLS = ["QQQ", "TSLA", "CRCL"]
DEFAULT_LOOKBACK_MINUTES = 60
DEFAULT_THRESHOLD_PERCENT = 3.0
DEFAULT_SUMMARY_TIME = "16:05"
MARKET_TZ = ZoneInfo(os.environ.get("MARKET_TIMEZONE", "America/New_York"))
STATE_PATH = Path(os.environ.get("STATE_FILE", ".state/monitor_state.json"))
HISTORY_PATH = Path(os.environ.get("HISTORY_FILE", "data/daily_history.json"))
REPORT_PATH = Path(os.environ.get("REPORT_FILE", "docs/index.html"))

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


def load_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=True, indent=2, sort_keys=True)


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


def safe_pct_change(current, baseline):
    if baseline == 0:
        return 0.0
    return ((current - baseline) / baseline) * 100


def format_signed(value):
    return f"{value:+.2f}"


def format_pct(value):
    return f"{value:+.2f}%"


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
    direction = "up" if intraday["move_pct"] > 0 else "down"
    return [
        f"{symbol} moved sharply {direction} in the last {lookback_minutes} minutes",
        f"Latest price: {intraday['latest_price']}",
        f"{lookback_minutes}-minute move: {format_signed(intraday['move'])} ({format_pct(intraday['move_pct'])})",
        f"Day change vs previous close: {format_signed(intraday['daily_change'])} ({format_pct(intraday['daily_change_pct'])})",
        f"Alert threshold: {threshold:.2f}%",
        f"Data time: {intraday['latest_time']}",
    ]


def build_summary_lines(today_str, daily_snapshots):
    lines = [f"{today_str} Daily Close Summary", ""]
    for snapshot in daily_snapshots:
        lines.append(
            f"{snapshot['symbol']}: Close {snapshot['last_close']:.2f} | Day change {format_signed(snapshot['change'])} ({format_pct(snapshot['change_pct'])})"
        )
    return lines


def load_history():
    history = load_json(HISTORY_PATH, {"records": []})
    history.setdefault("records", [])
    return history


def save_history(history):
    save_json(HISTORY_PATH, history)


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


def build_symbol_sections(symbols, history):
    sections = []
    records = history["records"]
    for symbol in symbols:
        rows = [item for item in records if item["symbol"] == symbol]
        if not rows:
            sections.append(
                {
                    "symbol": symbol,
                    "last_close": "--",
                    "change": "--",
                    "change_pct": "--",
                    "last_date": "No close data yet",
                    "sparkline": "",
                    "table_rows": '<tr><td colspan="4">No historical rows yet.</td></tr>',
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


def render_report(symbols, history, generated_at):
    sections = build_symbol_sections(symbols, history)
    last_updated = generated_at.strftime("%Y-%m-%d %H:%M %Z")
    cards = []
    tables = []

    for section in sections:
        sparkline_block = (
            f'<svg viewBox="0 0 220 72" class="sparkline" preserveAspectRatio="none"><polyline points="{section["sparkline"]}" /></svg>'
            if section.get("sparkline")
            else '<div class="empty-chart">Waiting for close data</div>'
        )
        tone = section.get("tone", "flat")
        cards.append(
            f"""
            <article class="card">
              <div class="card-top">
                <div>
                  <p class="eyebrow">Watchlist</p>
                  <h2>{escape(section['symbol'])}</h2>
                </div>
                <p class="stamp">{escape(section['last_date'])}</p>
              </div>
              <div class="price-row">
                <div>
                  <p class="metric-label">Last close</p>
                  <p class="price">{section['last_close']}</p>
                </div>
                <div class="delta-block {tone}">
                  <p class="metric-label">Day move</p>
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
                <p class="stamp">{escape(section['last_date'])}</p>
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Date</th>
                    <th>Close</th>
                    <th>Change</th>
                    <th>Change %</th>
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
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Stock Monitor Report</title>
    <style>
      :root {{
        --bg: #f6f1e8;
        --panel: rgba(255, 252, 247, 0.85);
        --ink: #18242f;
        --muted: #6a737d;
        --line: rgba(24, 36, 47, 0.12);
        --up: #0f7b53;
        --down: #b54833;
        --flat: #61707d;
        --accent: #d7e7d3;
        --shadow: 0 18px 48px rgba(24, 36, 47, 0.12);
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: Georgia, "Times New Roman", serif;
        color: var(--ink);
        background:
          radial-gradient(circle at top left, rgba(215, 231, 211, 0.9), transparent 38%),
          radial-gradient(circle at top right, rgba(228, 209, 182, 0.65), transparent 34%),
          linear-gradient(180deg, #fbf7f0 0%, var(--bg) 100%);
      }}
      .shell {{
        width: min(1120px, calc(100% - 32px));
        margin: 0 auto;
        padding: 48px 0 72px;
      }}
      .hero {{
        display: grid;
        gap: 16px;
        margin-bottom: 28px;
      }}
      .eyebrow {{
        margin: 0 0 8px;
        font-size: 12px;
        letter-spacing: 0.18em;
        text-transform: uppercase;
        color: var(--muted);
      }}
      h1, h2, h3, p {{ margin: 0; }}
      h1 {{
        font-size: clamp(2.3rem, 5vw, 4.8rem);
        line-height: 0.95;
        letter-spacing: -0.04em;
        max-width: 10ch;
      }}
      .subtitle {{
        max-width: 52rem;
        color: var(--muted);
        font-size: 1.04rem;
        line-height: 1.6;
      }}
      .status-bar {{
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        margin-top: 10px;
      }}
      .pill {{
        border: 1px solid var(--line);
        border-radius: 999px;
        background: rgba(255, 255, 255, 0.6);
        padding: 10px 14px;
        font-size: 0.92rem;
      }}
      .grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
        gap: 16px;
        margin: 28px 0 20px;
      }}
      .card, .table-card {{
        background: var(--panel);
        border: 1px solid rgba(255, 255, 255, 0.8);
        border-radius: 24px;
        box-shadow: var(--shadow);
        backdrop-filter: blur(12px);
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
      .stamp {{
        color: var(--muted);
        font-size: 0.9rem;
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
        font-size: 2.4rem;
        letter-spacing: -0.05em;
      }}
      .delta-block {{
        text-align: right;
      }}
      .delta {{
        font-size: 1.45rem;
        font-weight: 700;
      }}
      .delta-pct {{
        margin-top: 2px;
        color: inherit;
      }}
      .up {{ color: var(--up); }}
      .down {{ color: var(--down); }}
      .flat {{ color: var(--flat); }}
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
        font-family: "Avenir Next", "Segoe UI", sans-serif;
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
      }}
      @media (max-width: 640px) {{
        .shell {{
          width: min(100% - 20px, 1120px);
          padding-top: 28px;
        }}
        .price-row, .card-top, .table-head {{
          display: block;
        }}
        .delta-block {{
          text-align: left;
          margin-top: 12px;
        }}
      }}
    </style>
  </head>
  <body>
    <main class="shell">
      <section class="hero">
        <p class="eyebrow">GitHub Stock Monitor</p>
        <h1>Daily close report for your watchlist.</h1>
        <p class="subtitle">This page is regenerated by GitHub Actions. It stores close history for QQQ, TSLA, and CRCL and gives you a quick dashboard plus the most recent close rows for each symbol.</p>
        <div class="status-bar">
          <div class="pill">Symbols: {escape(", ".join(symbols))}</div>
          <div class="pill">Records: {len(history['records'])}</div>
          <div class="pill">Generated: {escape(last_updated)}</div>
        </div>
      </section>

      <section class="grid">
        {''.join(cards)}
      </section>

      <section class="table-stack">
        {''.join(tables)}
      </section>

      <p class="footer">The report updates after scheduled workflow runs. Intraday alerts are still emailed separately; this page focuses on close data history.</p>
    </main>
  </body>
</html>
"""


def write_report(symbols, history, generated_at):
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(render_report(symbols, history, generated_at), encoding="utf-8")


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
        return []

    if state["summaries_sent"].get(today_str):
        print(f"[INFO] summary already sent for {today_str}")
        return []

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
        return []

    subject = f"Daily Stock Summary - {today_str}"
    body = "\n".join(build_summary_lines(today_str, snapshots))
    send_email(subject, body)
    state["summaries_sent"][today_str] = True
    print(f"[SUMMARY] sent daily summary for {today_str}")
    return snapshots


def refresh_history_for_report(symbols, history, now_market_tz):
    updated_snapshots = []
    for symbol in symbols:
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


def main():
    now_market_tz = datetime.now(MARKET_TZ)
    today_str = now_market_tz.date().isoformat()
    symbols = parse_symbols()
    thresholds = parse_thresholds()
    state = load_state()
    history = load_history()
    prune_state(state, today_str)

    print(f"Running stock monitor at {now_market_tz.isoformat()} for {', '.join(symbols)}")
    maybe_send_intraday_alerts(symbols, thresholds, state, now_market_tz, today_str)
    summary_snapshots = maybe_send_daily_summary(symbols, state, now_market_tz, today_str)
    if summary_snapshots:
        upsert_history_records(history, summary_snapshots)
    else:
        refresh_history_for_report(symbols, history, now_market_tz)

    save_state(state)
    save_history(history)
    write_report(symbols, history, now_market_tz)
    print(f"[REPORT] wrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
