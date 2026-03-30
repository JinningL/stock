import yfinance as yf
import smtplib
from email.mime.text import MIMEText
import os


def get_price(symbol):
    stock = yf.Ticker(symbol)
    history = stock.history(period="5d")
    if history.empty:
        raise ValueError(f"No price data returned for symbol: {symbol}")

    return round(float(history["Close"].iloc[-1]), 2)


def send_email(symbol, price):
    sender = os.environ["EMAIL"]
    password = os.environ["PASSWORD"]
    receiver = os.environ.get("RECEIVER", sender)

    msg = MIMEText(f"{symbol} 当前价格: {price}")
    msg["Subject"] = f"Daily Stock Report - {symbol}"
    msg["From"] = sender
    msg["To"] = receiver

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)


def main():
    symbol = os.environ.get("STOCK_SYMBOL", "AAPL").strip().upper()
    price = get_price(symbol)
    print(symbol, price)
    send_email(symbol, price)


if __name__ == "__main__":
    main()
