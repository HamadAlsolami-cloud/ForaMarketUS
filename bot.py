
import os
import re
import json
import time
import hashlib
import logging
import requests
import feedparser

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


# ==================================================
# FORAS MARKET US
# ==================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

STATE_FILE = "alert_state.json"

FINNHUB_URL = "https://finnhub.io/api/v1"

NASDAQ_HALT_URL = (
    "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
)

YAHOO_SCREENER_URL = (
    "https://query1.finance.yahoo.com/v1/finance/"
    "screener/predefined/saved"
)

YAHOO_CHART_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/"
)

NY_TIMEZONE = ZoneInfo("America/New_York")

# ==================================================
# SETTINGS
# ==================================================

TOP_GAINERS_COUNT = 10

MAX_SCAN_SYMBOLS = 20

MAX_NEWS_SYMBOLS = 10

PRICE_CHANGE_LIMIT = 5.0

MOMENTUM_LIMIT = 2.5

# Nasdaq / NYSE / NYSE American only
ALLOWED_EXCHANGES = {
    "NMS",
    "NGM",
    "NCM",
    "NYQ",
    "ASE"
}

# لا يوجد حالياً مصدر موثوق لبيانات Time & Sales وLevel 2
WHALE_DATA_AVAILABLE = False

HEADERS = {
    "User-Agent": "Mozilla/5.0 ForasMarketUS/4.0"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
session.headers.update(HEADERS)


# ==================================================
# TIME
# ==================================================

def now_ny():
    return datetime.now(NY_TIMEZONE)


def ny_time_text():
    return now_ny().strftime("%Y-%m-%d %I:%M:%S %p ET")


def is_market_open():

    now = now_ny()

    if now.weekday() >= 5:
        return False

    current_time = now.time()

    market_open = datetime.strptime(
        "09:30", "%H:%M"
    ).time()

    market_close = datetime.strptime(
        "16:00", "%H:%M"
    ).time()

    return market_open <= current_time < market_close


# ==================================================
# STATE / DUPLICATE PROTECTION
# ==================================================

def load_state():

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            data = json.load(file)

            if isinstance(data, dict):
                data.setdefault("sent", [])
                return data

    except Exception:
        pass

    return {"sent": []}


state = load_state()


def save_state():

    try:
        state["sent"] = state.get("sent", [])[-5000:]

        with open(
            STATE_FILE,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                state,
                file,
                ensure_ascii=False,
                indent=2
            )

    except Exception as error:
        logging.error(f"State save error: {error}")


def make_id(value):

    return hashlib.sha256(
        str(value).encode("utf-8")
    ).hexdigest()


def already_sent(alert_id):

    return alert_id in state.get("sent", [])


def remember(alert_id):

    state.setdefault("sent", []).append(alert_id)
    save_state()


# ==================================================
# TELEGRAM
# ==================================================

def send_telegram(message):

    if not TOKEN or not CHAT_ID:
        logging.error("Telegram configuration missing.")
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TOKEN}/sendMessage"
    )

    for attempt in range(5):

        try:

            response = session.post(
                url,
                data={
                    "chat_id": CHAT_ID,
                    "text": message,
                    "disable_web_page_preview": True
                },
                timeout=25
            )

            if response.status_code == 429:

                try:
                    wait_time = int(
                        response.json()
                        .get("parameters", {})
                        .get("retry_after", 10)
                    )
                except Exception:
                    wait_time = 10

                logging.warning(
                    f"Telegram rate limit: {wait_time}"
                )

                time.sleep(wait_time + 1)
                continue

            response.raise_for_status()

            result = response.json()

            if result.get("ok"):
                time.sleep(1.2)
                return True

            logging.error(f"Telegram error: {result}")
            return False

        except Exception as error:

            logging.error(
                f"Telegram sending error: {error}"
            )

            time.sleep(3)

    return False


# ==================================================
# ARABIC TRANSLATION
# ==================================================

def translate_chunk(text):

    try:

        response = session.get(
            "https://translate.googleapis.com/translate_a/single",
            params={
                "client": "gtx",
                "sl": "auto",
                "tl": "ar",
                "dt": "t",
                "q": text
            },
            timeout=20
        )

        response.raise_for_status()

        data = response.json()

        return "".join(
            part[0]
            for part in data[0]
            if part and part[0]
        )

    except Exception as error:

        logging.warning(
            f"Translation error: {error}"
        )

        return text


def translate_arabic(text):

    if not text:
        return ""

    # تقسيم النص الطويل حتى لا يتجاوز حد الطلب
    chunks = [
        text[index:index + 2500]
        for index in range(0, len(text), 2500)
    ]

    translated_chunks = []

    for chunk in chunks:

        translated_chunks.append(
            translate_chunk(chunk)
        )

        time.sleep(0.2)

    return "".join(translated_chunks)


# ==================================================
# FINNHUB
# ==================================================

def finnhub_request(endpoint, params=None):

    if not FINNHUB_API_KEY:
        return None

    request_params = dict(params or {})
    request_params["token"] = FINNHUB_API_KEY

    try:

        response = session.get(
            f"{FINNHUB_URL}/{endpoint}",
            params=request_params,
            timeout=20
        )

        response.raise_for_status()

        return response.json()

    except Exception as error:

        logging.warning(
            f"Finnhub {endpoint} error: {error}"
        )

        return None


# ==================================================
# STOCK PRICE
# ==================================================

def get_stock_price(symbol):

    quote = finnhub_request(
        "quote",
        {"symbol": symbol}
    )

    if not isinstance(quote, dict):
        return None

    try:

        price = float(quote.get("c", 0))

        if price > 0:
            return price

    except (ValueError, TypeError):
        pass

    return None


def format_price(price):

    if price is None:
        return "غير متاح"

    return f"${price:.4f}"


# ==================================================
# YAHOO MARKET MOVERS
# ==================================================

def get_market_movers():

    params = {
        "formatted": "false",
        "scrIds": "day_gainers",
        "count": 100,
        "start": 0
    }

    try:

        response = session.get(
            YAHOO_SCREENER_URL,
            params=params,
            timeout=25
        )

        response.raise_for_status()

        data = response.json()

        results = (
            data.get("finance", {})
            .get("result", [])
        )

        if not results:
            return []

        quotes = results[0].get("quotes", [])

        movers = []

        for item in quotes:

            symbol = str(
                item.get("symbol", "")
            ).upper()

            exchange = str(
                item.get("exchange", "")
            ).upper()

            quote_type = str(
                item.get("quoteType", "")
            ).upper()

            if not symbol:
                continue

            if exchange not in ALLOWED_EXCHANGES:
                continue

            if quote_type != "EQUITY":
                continue

            try:

                price = float(
                    item.get("regularMarketPrice", 0)
                )

                change = float(
                    item.get(
                        "regularMarketChangePercent",
                        0
                    )
                )

            except (ValueError, TypeError):
                continue

            if price <= 0:
                continue

            movers.append({
                "symbol": symbol,
                "price": price,
                "change": change,
                "exchange": exchange
            })

        movers.sort(
            key=lambda item: item["change"],
            reverse=True
        )

        return movers[:100]

    except Exception as error:

        logging.error(
            f"Yahoo market movers error: {error}"
        )

        return []


# ==================================================
# TOP GAINERS
# ==================================================

def process_top_gainers(movers):

    if not movers:
        logging.warning("No market movers available.")
        return 0

    top = movers[:TOP_GAINERS_COUNT]

    lines = []

    for index, item in enumerate(top, start=1):

        symbol = item["symbol"]
        price = item["price"]
        change = item["change"]

        emoji = "🟢" if change >= 0 else "🔴"

        lines.append(
            f"{index}. {emoji} "
            f"{symbol} ${price:.4f} "
            f"({change:+.2f}%)"
        )

    message = (
        "🏆 أعلى الأسهم الرابحة\n\n"
        + "\n".join(lines)
        + f"\n\n🕒 {ny_time_text()}"
        + "\nالمصدر: Yahoo Finance"
    )

    report_id = make_id(
        "GAINERS|"
        + now_ny().strftime("%Y-%m-%d-%H")
    )

    if already_sent(report_id):
        return 0

    if send_telegram(message):

        remember(report_id)
        return 1

    return 0


# ==================================================
# PRICE MOVEMENT
# ==================================================

def process_price_moves(movers):

    count = 0

    today = now_ny().strftime("%Y-%m-%d")

    for item in movers[:MAX_SCAN_SYMBOLS]:

        symbol = item["symbol"]
        price = item["price"]
        change = item["change"]

        if abs(change) < PRICE_CHANGE_LIMIT:
            continue

        alert_id = make_id(
            f"PRICE|{symbol}|{today}"
        )

        if already_sent(alert_id):
            continue

        if change > 0:
            emoji = "🚀"
        else:
            emoji = "🔻"

        message = (
            f"{emoji} {symbol} ${price:.4f}\n\n"
            f"التغير اليومي: {change:+.2f}%\n\n"
            f"🕒 {ny_time_text()}\n"
            "المصدر: Yahoo Finance"
        )

        if send_telegram(message):

            remember(alert_id)
            count += 1

    return count


# ==================================================
# INTRADAY PRICE MOMENTUM
# ==================================================

def get_intraday_candles(symbol):

    try:

        response = session.get(
            f"{YAHOO_CHART_URL}{symbol}",
            params={
                "range": "1d",
                "interval": "5m",
                "includePrePost": "false"
            },
            timeout=20
        )

        response.raise_for_status()

        data = response.json()

        result = (
            data.get("chart", {})
            .get("result", [])
        )

        if not result:
            return []

        quote_data = (
            result[0]
            .get("indicators", {})
            .get("quote", [])
        )

        if not quote_data:
            return []

        closes = quote_data[0].get("close", [])

        return [
            float(value)
            for value in closes
            if value is not None
        ]

    except Exception as error:

        logging.warning(
            f"Intraday data error {symbol}: {error}"
        )

        return []


def process_momentum(movers):

    if not is_market_open():
        return 0

    count = 0

    today = now_ny().strftime("%Y-%m-%d")

    # نفحص عدداً محدوداً لتقليل طلبات البيانات
    for item in movers[:MAX_SCAN_SYMBOLS]:

        symbol = item["symbol"]

        closes = get_intraday_candles(symbol)

        if len(closes) < 4:
            continue

        recent = closes[-4:]

        if any(value <= 0 for value in recent):
            continue

        move = (
            (recent[-1] - recent[0])
            / recent[0]
        ) * 100

        rising = (
            recent[-1] > recent[-2] > recent[-3]
        )

        falling = (
            recent[-1] < recent[-2] < recent[-3]
        )

        if abs(move) < MOMENTUM_LIMIT:
            continue

        if not rising and not falling:
            continue

        alert_id = make_id(
            f"MOMENTUM|{symbol}|{today}"
        )

        if already_sent(alert_id):
            continue

        if rising:
            emoji = "⚡"
            direction = "زخم صاعد"
        else:
            emoji = "🔻"
            direction = "زخم هابط"

        message = (
            f"{emoji} {symbol} ${item['price']:.4f}\n\n"
            f"{direction}\n"
            f"حركة آخر 15 دقيقة تقريباً: {move:+.2f}%\n\n"
            f"🕒 {ny_time_text()}\n"
            "المصدر: Yahoo Finance - شموع 5 دقائق"
        )

        if send_telegram(message):

            remember(alert_id)
            count += 1

        time.sleep(0.5)

    return count


# ==================================================
# COMPANY NEWS
# ==================================================

def process_company_news(symbols):

    count = 0

    today = now_ny().date()

    from_date = today - timedelta(days=1)

    for symbol in symbols[:MAX_NEWS_SYMBOLS]:

        articles = finnhub_request(
            "company-news",
            {
                "symbol": symbol,
                "from": from_date.strftime("%Y-%m-%d"),
                "to": today.strftime("%Y-%m-%d")
            }
        )

        if not isinstance(articles, list):
            continue

        for article in articles[:3]:

            headline = article.get(
                "headline", ""
            ).strip()

            summary = article.get(
                "summary", ""
            ).strip()

            link = article.get("url", "")

            source = article.get(
                "source", "Finnhub"
            )

            timestamp = article.get(
                "datetime", 0
            )

            if not headline or not link:
                continue

            alert_id = make_id(
                f"NEWS|{symbol}|{link}"
            )

            if already_sent(alert_id):
                continue

            # نعرض النص المتاح من المصدر كاملاً دون اختصار
            full_text = summary if summary else headline

            arabic_text = translate_arabic(full_text)

            price = get_stock_price(symbol)

            article_time = "غير متاح"

            if timestamp:

                try:

                    article_time = datetime.fromtimestamp(
                        int(timestamp),
                        tz=ZoneInfo("UTC")
                    ).astimezone(
                        NY_TIMEZONE
                    ).strftime(
                        "%Y-%m-%d %I:%M:%S %p ET"
                    )

                except Exception:
                    pass

            message = (
                f"{symbol} {format_price(price)}\n\n"
                f"{arabic_text}\n\n"
                f"🔗 {link}\n"
                f"🕒 {article_time}\n"
                f"المصدر: {source}"
            )

            if send_telegram(message):

                remember(alert_id)
                count += 1

        time.sleep(1)

    return count


# ==================================================
# HALT ONLY - NO RESUME
# ==================================================

def extract_halt_symbol(text):

    patterns = [
        r"SECURITY\s+([A-Z][A-Z0-9.$-]{0,9})\s+HALTED",

        r"(?:SYMBOL|TICKER|ISSUE SYMBOL)\s*[:\-]\s*"
        r"([A-Z][A-Z0-9.$-]{0,9})",

        r"\b([A-Z]{1,5})\s+HALTED\b",

        r"\(([A-Z]{1,5})\)"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:
            return match.group(1).upper()

    return "غير محدد"


def process_halts():

    if not is_market_open():

        logging.info(
            "Outside regular market hours. HALT skipped."
        )

        return 0

    try:

        response = session.get(
            NASDAQ_HALT_URL,
            timeout=25
        )

        response.raise_for_status()

        feed = feedparser.parse(response.content)

    except Exception as error:

        logging.error(
            f"Nasdaq halt feed error: {error}"
        )

        return 0

    count = 0

    for item in feed.entries:

        title = item.get("title", "")
        summary = item.get("summary", "")
        description = item.get("description", "")
        link = item.get("link", "")

        full_text = (
            f"{title} {summary} {description}"
        )

        upper_text = full_text.upper()

        # تجاهل الاستئناف نهائياً
        if re.search(
            r"\b(RESUMED|RESUME|RESUMPTION|TRADING RESUMES)\b",
            upper_text
        ):
            continue

        if not re.search(
            r"\bHALTED\b|\bHALT\b",
            upper_text
        ):
            continue

        symbol = extract_halt_symbol(full_text)

        alert_id = make_id(
            f"HALT|{symbol}|{full_text}|{link}"
        )

        if already_sent(alert_id):
            continue

        price = None

        if symbol != "غير محدد":
            price = get_stock_price(symbol)

        message = (
            f"🔴 {symbol} {format_price(price)}\n\n"
            "HALT - إيقاف تداول\n\n"
            f"{title}\n\n"
            f"🔗 {link}\n"
            f"🕒 {ny_time_text()}\n"
            "المصدر: Nasdaq Trader"
        )

        if send_telegram(message):

            remember(alert_id)
            count += 1

    return count


# ==================================================
# WHALE ALERT
# ==================================================

def process_whale_alerts():

    # لا نعتبر ارتفاع الحجم وحده صفقة حوت.
    # لا نعتبر عرض NBBO صفقة منفذة.
    # يلزم مصدر Time & Sales موثوق للصفقات المنفذة.
    # ويلزم Level 2 لرصد الأوامر المعروضة.

    if not WHALE_DATA_AVAILABLE:

        logging.info(
            "WHALE disabled: verified trade/order-book "
            "data source is not configured."
        )

        return 0

    return 0


# ==================================================
# MAIN
# ==================================================

def main():

    logging.info(
        "Foras Market US scan started"
    )

    if not TOKEN or not CHAT_ID:

        logging.error(
            "Telegram configuration incomplete."
        )

        return

    if not FINNHUB_API_KEY:

        logging.error(
            "Finnhub API key missing."
        )

        return

    # 1 - HALT
    halt_count = process_halts()

    # 2 - Market movers
    movers = get_market_movers()

    if not movers:

        logging.warning(
            "No market movers returned. "
            "Check Yahoo Finance data availability."
        )

        save_state()
        return

    # 3 - Top gainers
    gainers_count = process_top_gainers(movers)

    # 4 - Price movement
    price_count = process_price_moves(movers)

    # 5 - Strong momentum
    momentum_count = process_momentum(movers)

    # 6 - Company-specific news
    news_symbols = [
        item["symbol"]
        for item in movers[:MAX_NEWS_SYMBOLS]
    ]

    news_count = process_company_news(
        news_symbols
    )

    # 7 - Whale
    whale_count = process_whale_alerts()

    save_state()

    logging.info(
        "Scan completed | "
        f"HALT: {halt_count} | "
        f"GAINERS: {gainers_count} | "
        f"PRICE: {price_count} | "
        f"MOMENTUM: {momentum_count} | "
        f"NEWS: {news_count} | "
        f"WHALE: {whale_count}"
    )


if __name__ == "__main__":
    main()
