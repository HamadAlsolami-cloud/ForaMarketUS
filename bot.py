import os
import re
import json
import time
import hashlib
import logging
import requests
import feedparser

from datetime import datetime, timezone

# ==========================================
# FORAS MARKET US
# ==========================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

STATE_FILE = "alert_state.json"

FINNHUB_URL = "https://finnhub.io/api/v1"
NASDAQ_HALT_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"

PRICE_CHANGE_LIMIT = 5.0
MAX_SYMBOLS_PER_SCAN = 15
MAX_NEWS_PER_SCAN = 8

HEADERS = {
    "User-Agent": "ForasMarketUS/1.0"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
session.headers.update(HEADERS)


# ==========================================
# ALERT MEMORY
# ==========================================

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return {
            "sent": []
        }


state = load_state()


def save_state():
    try:
        state["sent"] = state.get("sent", [])[-3000:]

        with open(STATE_FILE, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)

    except Exception as error:
        logging.error(f"State save error: {error}")


def make_id(value):
    return hashlib.sha256(
        str(value).encode("utf-8")
    ).hexdigest()


def already_sent(alert_id):
    return alert_id in state.get("sent", [])


def remember(alert_id):
    if "sent" not in state:
        state["sent"] = []

    state["sent"].append(alert_id)
    save_state()


# ==========================================
# TELEGRAM
# ==========================================

def send_telegram(message):

    if not TOKEN or not CHAT_ID:
        logging.error("Telegram secrets are missing.")
        return False

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    for attempt in range(5):
        try:
            response = session.post(
                url,
                data={
                    "chat_id": CHAT_ID,
                    "text": message,
                    "disable_web_page_preview": True
                },
                timeout=20
            )

            if response.status_code == 429:
                try:
                    wait_time = int(
                        response.json().get(
                            "parameters", {}
                        ).get("retry_after", 5)
                    )
                except Exception:
                    wait_time = 5

                logging.warning(
                    f"Telegram rate limit. Waiting {wait_time} seconds."
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
            time.sleep(2)

    logging.error("Telegram sending failed after 5 attempts.")
    return False

# ==========================================
# ARABIC TRANSLATION
# ==========================================

def translate_arabic(text):

    if not text:
        return ""

    try:
        url = "https://translate.googleapis.com/translate_a/single"

        response = session.get(
            url,
            params={
                "client": "gtx",
                "sl": "auto",
                "tl": "ar",
                "dt": "t",
                "q": text[:3500]
            },
            timeout=15
        )

        response.raise_for_status()

        data = response.json()

        translated = ""

        for part in data[0]:
            if part and part[0]:
                translated += part[0]

        return translated if translated else text

    except Exception as error:
        logging.warning(f"Translation unavailable: {error}")
        return text


# ==========================================
# FINNHUB REQUEST
# ==========================================

def finnhub_request(endpoint, params=None):

    if not FINNHUB_API_KEY:
        logging.error("FINNHUB_API_KEY is missing.")
        return None

    request_params = params.copy() if params else {}
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
            f"Finnhub error ({endpoint}): {error}"
        )
        return None


# ==========================================
# HALT / RESUME ALERTS
# ==========================================

def extract_halt_symbol(text):

    patterns = [
        r"(?:NASDAQ|NYSE|AMEX|OTC)\s*:\s*([A-Z][A-Z0-9.-]{0,9})",
        r"(?:Symbol|Ticker)\s*[:\-]\s*([A-Z][A-Z0-9.-]{0,9})",
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

    from zoneinfo import ZoneInfo
    from datetime import time as dt_time

    logging.info("Checking HALT UP / HALT DOWN...")

    halt_count = 0

    # جلسة التداول الأمريكية بتوقيت نيويورك
    now_ny = datetime.now(ZoneInfo("America/New_York"))

    if now_ny.weekday() >= 5:
        logging.info("Market closed: weekend.")
        return 0

    market_open = dt_time(9, 30)
    market_close = dt_time(16, 0)

    if not (market_open <= now_ny.time() < market_close):
        logging.info("Outside regular market hours.")
        return 0

    try:
        response = session.get(
            NASDAQ_HALT_URL,
            timeout=20
        )
        response.raise_for_status()
        feed = feedparser.parse(response.content)

    except Exception as error:
        logging.error(f"Halt feed error: {error}")
        return 0

    for item in feed.entries:

        title = item.get("title", "")
        summary = item.get("summary", "")
        link = item.get("link", "")

        full_text = f"{title} {summary}".upper()

        # استبعاد الاستئناف
        if any(word in full_text for word in [
            "RESUME",
            "RESUMPTION",
            "TRADING RESUMES"
        ]):
            continue

        # تحديد اتجاه الإيقاف من رمز السبب
        if re.search(r"\bT1\b", full_text):
            halt_type = "HALT UP"
            emoji = "🟢"

        elif re.search(r"\bT2\b", full_text):
            halt_type = "HALT DOWN"
            emoji = "🔴"

        else:
            continue

        alert_id = make_id(
            "HALT_" + link if link else full_text
        )

        if already_sent(alert_id):
            continue

        symbol = extract_halt_symbol(full_text)

        arabic_title = translate_arabic(title)

        message = (
            f"{emoji} {halt_type}\n\n"
            f"السهم: {symbol}\n"
            f"نوع الإيقاف: {halt_type}\n"
            f"التفاصيل: {arabic_title}\n\n"
            f"المصدر: Nasdaq Trader\n"
            f"{link}"
        )

        if send_telegram(message):
            remember(alert_id)
            halt_count += 1

    return halt_count

# ==========================================
# GENERAL MARKET NEWS
# ==========================================

def get_general_news():

    news = finnhub_request(
        "news",
        {
            "category": "general"
        }
    )

    return news if isinstance(news, list) else []


def extract_related_symbols(article):

    symbols = set()

    related = article.get("related", "")

    if isinstance(related, str):

        for symbol in related.split(","):
            symbol = symbol.strip().upper()

            if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol):
                symbols.add(symbol)

    headline = article.get("headline", "")

    mentions = re.findall(
        r"\$([A-Z][A-Z0-9.-]{0,9})\b",
        headline
    )

    for symbol in mentions:
        symbols.add(symbol)

    return sorted(symbols)


def process_news():

    logging.info("Checking market news...")

    news_count = 0
    discovered_symbols = set()

    articles = get_general_news()

    if not articles:
        logging.warning("No general news returned.")
        return 0, discovered_symbols

    for article in articles[:MAX_NEWS_PER_SCAN]:

        headline = article.get("headline", "")
        summary = article.get("summary", "")
        link = article.get("url", "")
        source = article.get("source", "Unknown")
        timestamp = article.get("datetime", 0)

        if not headline:
            continue

        symbols = extract_related_symbols(article)

        for symbol in symbols:
            discovered_symbols.add(symbol)

        alert_id = make_id(
            "NEWS_" + link if link else headline
        )

        if already_sent(alert_id):
            continue

        arabic_headline = translate_arabic(headline)

        arabic_summary = translate_arabic(
            summary[:1800]
        ) if summary else "لا يوجد ملخص متاح."

        symbol_text = (
            ", ".join(symbols)
            if symbols else "لم يتم تحديد رمز"
        )

        date_text = ""

        if timestamp:
            try:
                date_text = datetime.fromtimestamp(
                    int(timestamp),
                    tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                pass

        message = (
            "📰 خبر جديد في السوق الأمريكي\n\n"
            f"📌 العنوان:\n{arabic_headline}\n\n"
            f"📝 الملخص:\n{arabic_summary}\n\n"
            f"🔎 الأسهم المرتبطة: {symbol_text}\n"
            f"🏢 المصدر: {source}\n"
            f"🕒 الوقت: {date_text}\n\n"
            f"🔗 رابط الخبر:\n{link}"
        )

        if send_telegram(message):
            remember(alert_id)
            news_count += 1

        time.sleep(0.3)

    return news_count, discovered_symbols


# ==========================================
# PRICE MOVEMENT ALERTS
# ==========================================

def get_quote(symbol):

    return finnhub_request(
        "quote",
        {
            "symbol": symbol
        }
    )


def process_price_moves(symbols):

    logging.info("Checking price movements...")

    alert_count = 0

    scan_symbols = sorted(symbols)[:MAX_SYMBOLS_PER_SCAN]

    for symbol in scan_symbols:

        quote = get_quote(symbol)

        if not isinstance(quote, dict):
            continue

        price = quote.get("c", 0)
        change = quote.get("dp", 0)
        previous_close = quote.get("pc", 0)

        try:
            price = float(price)
            change = float(change)
            previous_close = float(previous_close)
        except (ValueError, TypeError):
            continue

        if price <= 0 or abs(change) < PRICE_CHANGE_LIMIT:
            continue

        today = datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%d")

        alert_id = make_id(
            f"PRICE_{symbol}_{today}"
        )

        if already_sent(alert_id):
            continue

        if change > 0:
            direction = "ارتفاع"
            emoji = "🚀"
        else:
            direction = "انخفاض"
            emoji = "🔻"

        message = (
            f"{emoji} تنبيه حركة سعرية\n\n"
            f"السهم: {symbol}\n"
            f"السعر الحالي: ${price:.4f}\n"
            f"التغير اليومي: {change:+.2f}%\n"
            f"إغلاق سابق: ${previous_close:.4f}\n\n"
            f"نوع الحركة: {direction}\n"
            f"شرط التنبيه: تغير يومي 5% أو أكثر\n\n"
            "المصدر: Finnhub\n"
            "تنبيه آلي للمراقبة، وليس توصية استثمارية."
        )

        if send_telegram(message):
            remember(alert_id)
            alert_count += 1

        time.sleep(1)

    return alert_count


# ==========================================
# MAIN SCAN
# ==========================================

def main():

    logging.info("Foras Market US scan started")

    if not TOKEN or not CHAT_ID:
        logging.error("Telegram configuration is incomplete.")
        return

    if not FINNHUB_API_KEY:
        logging.error("Finnhub API key is missing.")
        return

    halt_count = process_halts()

    news_count, discovered_symbols = process_news()

    price_count = process_price_moves(
        discovered_symbols
    )

    save_state()

    logging.info(
        "Scan completed | "
        f"Halts: {halt_count} | "
        f"News: {news_count} | "
        f"Price alerts: {price_count} | "
        f"Symbols discovered: {len(discovered_symbols)}"
    )


if __name__ == "__main__":
    main()
