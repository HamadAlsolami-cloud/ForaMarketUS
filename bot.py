
import os
import re
import json
import hashlib
import logging
from datetime import datetime, timezone

import requests
import feedparser

# =========================================================
# FORAS MARKET US - Dynamic Scanner
# النسخة المجانية الأولية
# =========================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

STATE_FILE = "alert_state.json"

FINNHUB_URL = "https://finnhub.io/api/v1"
NASDAQ_HALT_RSS = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"

MAX_NEWS_PER_RUN = 10
MAX_SYMBOLS_PER_RUN = 8
MOVE_ALERT_PERCENT = 5.0
MAX_MESSAGE_LENGTH = 3500

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

session = requests.Session()
session.headers.update({
    "User-Agent": "ForasMarketUS/1.0"
})


# =========================================================
# STATE
# =========================================================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "news": [],
            "halts": [],
            "moves": []
        }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        return {
            "news": data.get("news", []),
            "halts": data.get("halts", []),
            "moves": data.get("moves", [])
        }

    except Exception:
        logging.exception("Could not read state file")
        return {
            "news": [],
            "halts": [],
            "moves": []
        }


state = load_state()


def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    except Exception:
        logging.exception("Could not save state")


def remember(category, item_id, limit=2000):
    items = state.setdefault(category, [])

    if item_id in items:
        return False

    items.append(item_id)

    if len(items) > limit:
        state[category] = items[-limit:]

    return True


def make_id(value):
    return hashlib.sha256(
        str(value).encode("utf-8")
    ).hexdigest()


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        logging.error("Telegram credentials are missing")
        return False

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

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

        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            logging.error("Telegram returned an unsuccessful response")
            return False

        return True

    except Exception:
        logging.exception("Telegram sending failed")
        return False


def send_long_message(message):
    if len(message) <= MAX_MESSAGE_LENGTH:
        send_telegram(message)
        return

    parts = []

    while message:
        split_at = message.rfind(
            "\n",
            0,
            MAX_MESSAGE_LENGTH
        )

        if split_at < 500:
            split_at = MAX_MESSAGE_LENGTH

        parts.append(message[:split_at])
        message = message[split_at:].lstrip()

    for part in parts:
        send_telegram(part)


# =========================================================
# ARABIC TRANSLATION
# خدمة مجانية غير رسمية - قد تتوقف أو تفرض حدوداً
# =========================================================

def translate_arabic(text):
    if not text:
        return "لا يوجد ملخص"

    try:
        url = "https://translate.googleapis.com/translate_a/single"

        response = session.get(
            url,
            params={
                "client": "gtx",
                "sl": "auto",
                "tl": "ar",
                "dt": "t",
                "q": text[:2500]
            },
            timeout=12
        )

        response.raise_for_status()
        data = response.json()

        translated = "".join(
            part[0]
            for part in data[0]
            if part and part[0]
        )

        return translated or text

    except Exception:
        logging.warning("Translation unavailable")
        return "تعذرت الترجمة، النص الأصلي:\n" + text


# =========================================================
# FINNHUB NEWS
# اكتشاف الرموز من الأخبار بدلاً من قائمة ثابتة
# =========================================================

def get_general_news():
    if not FINNHUB_API_KEY:
        logging.error("FINNHUB_API_KEY is missing")
        return []

    try:
        response = session.get(
            f"{FINNHUB_URL}/news",
            params={
                "category": "general",
                "token": FINNHUB_API_KEY
            },
            timeout=20
        )

        response.raise_for_status()
        data = response.json()

        return data if isinstance(data, list) else []

    except Exception:
        logging.exception("Finnhub news request failed")
        return []


def get_related_symbols(news):
    symbols = set()

    for item in news:
        related = item.get("related", "")

        if isinstance(related, str):
            for symbol in related.split(","):
                symbol = symbol.strip().upper()

                if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol):
                    symbols.add(symbol)

        # استخراج الرموز المكتوبة بصيغة $TICKER فقط
        text = (
            str(item.get("headline", "")) + " " +
            str(item.get("summary", ""))
        )

        for symbol in re.findall(r"\$([A-Z]{1,5})\b", text):
            symbols.add(symbol)

    # استبعاد بعض الكلمات الشائعة التي قد تُفهم كرموز
    excluded = {
        "CEO", "USA", "SEC", "FDA", "NYSE",
        "IPO", "ETF", "AI", "THE", "USD"
    }

    return sorted(symbols - excluded)


def process_news(news_items):
    count = 0

    for item in news_items[:MAX_NEWS_PER_RUN]:
        headline = str(item.get("headline", "")).strip()
        summary = str(item.get("summary", "")).strip()
        link = str(item.get("url", "")).strip()
        related = str(item.get("related", "")).strip()

        if not headline:
            continue

        news_id = make_id(
            item.get("id") or link or headline
        )

        if not remember("news", news_id):
            continue

        arabic_headline = translate_arabic(headline)

        arabic_summary = (
            translate_arabic(summary[:1200])
            if summary else "لا يوجد ملخص متاح"
        )

        message = (
            "📰 خبر جديد - فرص السوق الأمريكي\n\n"
            f"العنوان:\n{arabic_headline}\n\n"
            f"الملخص:\n{arabic_summary}\n\n"
            f"الرموز المرتبطة: {related or 'غير محددة'}\n"
            f"المصدر: {item.get('source', 'Finnhub')}\n"
        )

        if link:
            message += f"\nرابط الخبر:\n{link}"

        send_long_message(message)
        count += 1

    return count


# =========================================================
# STOCK QUOTES
# =========================================================

def get_quote(symbol):
    try:
        response = session.get(
            f"{FINNHUB_URL}/quote",
            params={
                "symbol": symbol,
                "token": FINNHUB_API_KEY
            },
            timeout=15
        )

        response.raise_for_status()
        data = response.json()

        if not data or not data.get("c"):
            return None

        return data

    except Exception:
        logging.warning("Quote unavailable for %s", symbol)
        return None


def process_price_moves(symbols):
    alerts = 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for symbol in symbols[:MAX_SYMBOLS_PER_RUN]:
        quote = get_quote(symbol)

        if not quote:
            continue

        price = quote.get("c", 0)
        change = quote.get("dp", 0)
        previous = quote.get("pc", 0)

        if not isinstance(price, (int, float)) or price <= 0:
            continue

        if not isinstance(change, (int, float)):
            continue

        if abs(change) < MOVE_ALERT_PERCENT:
            continue

        direction = "صعود" if change > 0 else "هبوط"
        move_id = f"{today}_{symbol}_{direction}"

        if not remember("moves", move_id):
            continue

        message = (
            "📊 تنبيه حركة سعرية\n\n"
            f"السهم: {symbol}\n"
            f"السعر الحالي: ${price:.4f}\n"
            f"الإغلاق السابق: ${previous:.4f}\n"
            f"التغير اليومي: {change:+.2f}%\n"
            f"الاتجاه: {direction}\n\n"
            "⚠️ هذا رصد لحركة السعر وليس توصية تداول."
        )

        send_telegram(message)
        alerts += 1

    return alerts


# =========================================================
# NASDAQ TRADER HALT RSS
# يغطي أسهم Nasdaq والأسهم المدرجة في بورصات أخرى
# =========================================================

def extract_halt_symbol(title, description):
    text = f"{title} {description}"

    match = re.search(
        r"(?:security|symbol)\s+([A-Z][A-Z0-9.-]{0,9})",
        text,
        re.IGNORECASE
    )

    if match:
        return match.group(1).upper()

    clean_title = title.strip().upper()

    if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", clean_title):
        return clean_title

    return None


def process_halts():
    try:
        response = session.get(
            NASDAQ_HALT_RSS,
            timeout=20
        )

        response.raise_for_status()

        feed = feedparser.parse(response.content)

        count = 0

        for entry in feed.entries:
            title = str(entry.get("title", "")).strip()
            description = str(
                entry.get("summary", "")
            ).strip()

            combined = f"{title} {description}"

            if not title:
                continue

            symbol = extract_halt_symbol(
                title,
                description
            )

            entry_id = make_id(
                entry.get("id") or
                entry.get("link") or
                combined
            )

            if not remember("halts", entry_id):
                continue

            if re.search(r"\b(resum|resumption)\b", combined, re.I):
                event = "استئناف تداول"
                emoji = "🟢"
            else:
                event = "إيقاف تداول"
                emoji = "🛑"

            message = (
                f"{emoji} تنبيه {event}\n\n"
                f"السهم: {symbol or 'غير محدد'}\n"
                f"التفاصيل:\n{title}\n"
                f"{description}\n\n"
                "المصدر: Nasdaq Trader RSS\n"
                "ملاحظة: تحقق من حالة التداول لدى وسيطك."
            )

            send_long_message(message)
            count += 1

        return count

    except Exception:
        logging.exception("Nasdaq halt feed failed")
        return 0


# =========================================================
# MAIN
# =========================================================

def main():
    logging.info("Foras Market US scan started")

    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram secrets are missing")

    if not FINNHUB_API_KEY:
        raise RuntimeError("Finnhub API key is missing")

    # 1 - Halt alerts
    halt_count = process_halts()

    # 2 - General news
    news_items = get_general_news()
    news_count = process_news(news_items)

    # 3 - Dynamic ticker discovery
    symbols = get_related_symbols(news_items)

    # 4 - Price movement alerts
    move_count = process_price_moves(symbols)

    # حفظ حالة التنبيهات لمنع التكرار
    save_state()

    logging.info(
        "Scan completed | Halts: %s | News: %s | "
        "Price alerts: %s | Symbols discovered: %s",
        halt_count,
        news_count,
        move_count,
        len(symbols)
    )

    print("Scan completed successfully")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Bot execution failed")
        save_state()
        raise
