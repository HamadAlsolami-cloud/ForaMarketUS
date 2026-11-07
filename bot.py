
import os
import re
import json
import time
import hashlib
import logging
import requests
import feedparser

from datetime import datetime
from zoneinfo import ZoneInfo

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

NY_TIMEZONE = ZoneInfo("America/New_York")

HEADERS = {
    "User-Agent": "ForasMarketUS/2.0"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
session.headers.update(HEADERS)


# ==========================================
# STATE / DUPLICATE PROTECTION
# ==========================================

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass

    return {"sent": []}


state = load_state()


def save_state():
    try:
        state["sent"] = state.get("sent", [])[-3000:]

        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

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


# ==========================================
# US REGULAR MARKET HOURS
# ==========================================

def is_market_open():

    now = datetime.now(NY_TIMEZONE)

    # Monday to Friday
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


# ==========================================
# TELEGRAM SENDING + RATE LIMIT
# ==========================================

def send_telegram(message):

    if not TOKEN or not CHAT_ID:
        logging.error("Telegram configuration missing.")
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
                timeout=25
            )

            if response.status_code == 429:

                try:
                    wait_time = int(
                        response.json().get(
                            "parameters", {}
                        ).get("retry_after", 10)
                    )
                except Exception:
                    wait_time = 10

                logging.warning(
                    f"Telegram rate limit. Waiting {wait_time}s."
                )

                time.sleep(wait_time + 1)
                continue

            response.raise_for_status()

            result = response.json()

            if result.get("ok"):
                time.sleep(1.3)
                return True

            logging.error(f"Telegram error: {result}")
            return False

        except requests.exceptions.HTTPError as error:
            logging.error(f"Telegram HTTP error: {error}")
            return False

        except Exception as error:
            logging.error(f"Telegram sending error: {error}")
            time.sleep(3)

    logging.error("Telegram sending failed after retries.")
    return False


# ==========================================
# ARABIC TRANSLATION
# ==========================================

def translate_arabic(text):

    if not text:
        return ""

    try:
        response = session.get(
            "https://translate.googleapis.com/translate_a/single",
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

        translated = "".join(
            part[0]
            for part in data[0]
            if part and part[0]
        )

        return translated or text

    except Exception as error:
        logging.warning(f"Translation error: {error}")
        return text


# ==========================================
# FINNHUB
# ==========================================

def finnhub_request(endpoint, params=None):

    if not FINNHUB_API_KEY:
        logging.error("Finnhub API key missing.")
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
        logging.warning(f"Finnhub error: {error}")
        return None


# ==========================================
# HALT / RESUME SYMBOL EXTRACTION
# ==========================================

def extract_halt_symbol(text):

    patterns = [
        r"SECURITY\s+([A-Z][A-Z0-9.$-]{0,9})\s+(?:HALTED|RESUMED)",
        r"(?:SYMBOL|TICKER|ISSUE SYMBOL)\s*[:\-]\s*([A-Z][A-Z0-9.$-]{0,9})",
        r"\b([A-Z]{1,5})\s+(?:HALTED|RESUMED)\b",
        r"\(([A-Z]{1,5})\)"
    ]

    for pattern in patterns:

        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            return match.group(1).upper()

    return "غير محدد"


# ==========================================
# HALT AND RESUME ALERTS
# ==========================================

def process_halts():

    logging.info("Checking HALT / RESUME...")

    if not is_market_open():
        logging.info("Outside regular US market hours. Halts skipped.")
        return 0

    count = 0

    try:
        response = session.get(
            NASDAQ_HALT_URL,
            timeout=25
        )

        response.raise_for_status()

        feed = feedparser.parse(response.content)

    except Exception as error:
        logging.error(f"Nasdaq feed error: {error}")
        return 0

    for item in feed.entries:

        title = item.get("title", "")
        summary = item.get("summary", "")
        description = item.get("description", "")
        link = item.get("link", "")

        full_text = (
            f"{title} {summary} {description}"
        )

        upper_text = full_text.upper()

        # Determine whether this is a resume or a halt.
        is_resume = bool(
            re.search(
                r"\b(RESUMED|RESUME|RESUMPTION|TRADING RESUMES)\b",
                upper_text
            )
        )

        is_halt = bool(
            re.search(r"\bHALTED\b|\bHALT\b", upper_text)
        )

        if is_resume:
            status = "RESUME"
            emoji = "🟢"

        elif is_halt:
            status = "HALT"
            emoji = "🔴"

        else:
            continue

        symbol = extract_halt_symbol(full_text)

        # Include the feed content in the ID so a changed
        # status can be treated as a new event.
        alert_id = make_id(
            f"{status}|{symbol}|{full_text}|{link}"
        )

        if already_sent(alert_id):
            continue

        message = (
            f"{emoji} {status} - US MARKET\n\n"
            f"السهم: {symbol}\n"
            f"الحالة: {status}\n"
            f"الوقت: {datetime.now(NY_TIMEZONE).strftime('%H:%M:%S')} "
            f"بتوقيت نيويورك\n\n"
            f"تفاصيل المصدر:\n{title}\n\n"
            f"المصدر: Nasdaq Trader\n"
            f"{link}"
        )

        if send_telegram(message):
            remember(alert_id)
            count += 1

        else:
            logging.warning(
                f"Could not send {status} alert for {symbol}"
            )

    logging.info(f"Halt / Resume alerts sent: {count}")

    return count


# ==========================================
# GENERAL MARKET NEWS
# ==========================================

def get_general_news():

    news = finnhub_request(
        "news",
        {"category": "general"}
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

    symbols.update(mentions)

    return sorted(symbols)


def process_news():

    logging.info("Checking market news...")

    count = 0
    discovered_symbols = set()

    articles = get_general_news()

    if not articles:
        logging.info("No general news returned.")
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
        discovered_symbols.update(symbols)

        alert_id = make_id(
            "NEWS|" + (link or headline)
        )

        if already_sent(alert_id):
            continue

        arabic_headline = translate_arabic(headline)

        arabic_summary = (
            translate_arabic(summary[:1800])
            if summary else "لا يوجد ملخص متاح."
        )

        symbol_text = (
            ", ".join(symbols)
            if symbols else "لم يتم تحديد رمز"
        )

        date_text = ""

        if timestamp:
            try:
                date_text = datetime.fromtimestamp(
                    int(timestamp)
                ).strftime("%Y-%m-%d %H:%M")
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
            count += 1

    return count, discovered_symbols


# ==========================================
# PRICE MOVEMENTS
# ==========================================

def get_quote(symbol):

    return finnhub_request(
        "quote",
        {"symbol": symbol}
    )


def process_price_moves(symbols):

    logging.info("Checking price movements...")

    count = 0

    for symbol in sorted(symbols)[:MAX_SYMBOLS_PER_SCAN]:

        quote = get_quote(symbol)

        if not isinstance(quote, dict):
            continue

        try:
            price = float(quote.get("c", 0))
            change = float(quote.get("dp", 0))
            previous_close = float(quote.get("pc", 0))

        except (ValueError, TypeError):
            continue

        if price <= 0 or abs(change) < PRICE_CHANGE_LIMIT:
            continue

        today = datetime.now(NY_TIMEZONE).strftime("%Y-%m-%d")

        alert_id = make_id(
            f"PRICE|{symbol}|{today}"
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
            f"الإغلاق السابق: ${previous_close:.4f}\n\n"
            f"الحركة: {direction}\n"
            "المصدر: Finnhub\n\n"
            "تنبيه آلي للمراقبة وليس توصية استثمارية."
        )

        if send_telegram(message):
            remember(alert_id)
            count += 1

        time.sleep(1)

    return count


# ==========================================
# MAIN
# ==========================================

def main():

    logging.info("Foras Market US scan started")

    if not TOKEN or not CHAT_ID:
        logging.error("Telegram configuration incomplete.")
        return

    if not FINNHUB_API_KEY:
        logging.error("Finnhub API key missing.")
        return

    # HALT and RESUME only during regular US session
    halt_count = process_halts()

    # News
    news_count, discovered_symbols = process_news()

    # Price movements for discovered symbols
    price_count = process_price_moves(
        discovered_symbols
    )

    save_state()

    logging.info(
        "Scan completed | "
        f"HALT/RESUME: {halt_count} | "
        f"News: {news_count} | "
        f"Price alerts: {price_count} | "
        f"Symbols discovered: {len(discovered_symbols)}"
    )


if __name__ == "__main__":
    main()
