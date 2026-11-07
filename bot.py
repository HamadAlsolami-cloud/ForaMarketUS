
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


# ==========================================
# FORAS MARKET US
# ==========================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

STATE_FILE = "alert_state.json"

FINNHUB_URL = "https://finnhub.io/api/v1"
NASDAQ_HALT_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"

YAHOO_SCREENER_URL = (
    "https://query1.finance.yahoo.com/v1/finance/"
    "screener/predefined/saved"
)

YAHOO_CHART_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/"
)

NY_TIMEZONE = ZoneInfo("America/New_York")

# إعدادات التنبيهات
TOP_GAINERS_COUNT = 10
MAX_NEWS_SYMBOLS = 10
PRICE_ALERT_LIMIT = 5.0
MOMENTUM_LIMIT = 2.5

# لا يتم تضمين OTC
ALLOWED_EXCHANGES = {
    "NMS", "NGM", "NCM",   # Nasdaq
    "NYQ",                 # NYSE
    "ASE"                  # NYSE American
}

# لا توجد حالياً بيانات موثوقة للصفقات الكبيرة أو Level 2
WHALE_DATA_AVAILABLE = False

HEADERS = {
    "User-Agent": "Mozilla/5.0 ForasMarketUS/3.0"
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
        with open(STATE_FILE, "r", encoding="utf-8") as file:
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

        with open(STATE_FILE, "w", encoding="utf-8") as file:
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


# ==========================================
# TIME
# ==========================================

def now_ny():
    return datetime.now(NY_TIMEZONE)


def ny_time_text():
    return now_ny().strftime("%Y-%m-%d %H:%M:%S ET")


def is_market_open():
    now = now_ny()

    if now.weekday() >= 5:
        return False

    current = now.time()

    start = datetime.strptime("09:30", "%H:%M").time()
    end = datetime.strptime("16:00", "%H:%M").time()

    return start <= current < end


# ==========================================
# TELEGRAM
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
                wait_time = response.json().get(
                    "parameters", {}
                ).get("retry_after", 10)

                time.sleep(int(wait_time) + 1)
                continue

            response.raise_for_status()

            result = response.json()

            if result.get("ok"):
                time.sleep(1.2)
                return True

            logging.error(f"Telegram error: {result}")
            return False

        except Exception as error:
            logging.error(f"Telegram send error: {error}")
            time.sleep(3)

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
                "q": text[:3000]
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
        logging.warning(f"Finnhub {endpoint} error: {error}")
        return None


# ==========================================
# YAHOO MARKET MOVERS
# ==========================================

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

            symbol = str(item.get("symbol", "")).upper()
            exchange = str(item.get("exchange", "")).upper()
            quote_type = str(item.get("quoteType", "")).upper()

            if not symbol:
                continue

            if exchange not in ALLOWED_EXCHANGES:
                continue

            if quote_type != "EQUITY":
                continue

            try:
                price = float(item.get("regularMarketPrice", 0))
                change = float(
                    item.get("regularMarketChangePercent", 0)
                )
            except (TypeError, ValueError):
                continue

            if price <= 0:
                continue

            movers.append({
                "symbol": symbol,
                "price": price,
                "change": change,
                "exchange": exchange,
                "name": item.get("shortName", symbol)
            })

        movers.sort(
            key=lambda item: item["change"],
            reverse=True
        )

        return movers[:TOP_GAINERS_COUNT]

    except Exception as error:
        logging.error(f"Yahoo movers error: {error}")
        return []


# ==========================================
# TOP 10 GAINERS REPORT
# ==========================================

def process_top_gainers(movers):

    if not movers:
        logging.warning("No market movers available.")
        return 0

    lines = []

    for index, item in enumerate(movers, start=1):

        direction = "🟢" if item["change"] >= 0 else "🔴"

        lines.append(
            f"{index}. {direction} {item['symbol']} "
            f"| ${item['price']:.4f} "
            f"| {item['change']:+.2f}%"
        )

    message = (
        "🏆 أعلى الأسهم الرابحة\n"
        "السوق الأمريكي\n\n"
        + "\n".join(lines)
        + f"\n\n🕒 {ny_time_text()}"
        + "\nالمصدر: Yahoo Finance"
        + "\n\nتقرير آلي للمراقبة وليس توصية استثمارية."
    )

    report_id = make_id(
        "GAINERS|" + now_ny().strftime("%Y-%m-%d-%H")
    )

    if already_sent(report_id):
        return 0

    if send_telegram(message):
        remember(report_id)
        return 1

    return 0


# ==========================================
# PRICE MOVEMENT ALERTS
# ==========================================

def process_price_moves(movers):

    count = 0
    today = now_ny().strftime("%Y-%m-%d")

    for item in movers:

        symbol = item["symbol"]
        price = item["price"]
        change = item["change"]

        if abs(change) < PRICE_ALERT_LIMIT:
            continue

        alert_id = make_id(
            f"PRICE|{symbol}|{today}"
        )

        if already_sent(alert_id):
            continue

        if change > 0:
            emoji = "🚀"
            direction = "ارتفاع"
        else:
            emoji = "🔻"
            direction = "انخفاض"

        message = (
            f"{emoji} تنبيه حركة سعرية\n\n"
            f"السهم: {symbol}\n"
            f"السعر: ${price:.4f}\n"
            f"التغير اليومي: {change:+.2f}%\n"
            f"الحركة: {direction}\n\n"
            f"🕒 {ny_time_text()}\n"
            "المصدر: Yahoo Finance\n\n"
            "تنبيه آلي للمراقبة وليس توصية استثمارية."
        )

        if send_telegram(message):
            remember(alert_id)
            count += 1

    return count


# ==========================================
# STRONG MOMENTUM
# ==========================================

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

        indicators = result[0].get(
            "indicators", {}
        ).get("quote", [])

        if not indicators:
            return []

        closes = indicators[0].get("close", [])

        return [
            float(value)
            for value in closes
            if value is not None
        ]

    except Exception as error:
        logging.warning(
            f"Intraday data error for {symbol}: {error}"
        )
        return []


def process_momentum(movers):

    if not is_market_open():
        return 0

    count = 0
    today = now_ny().strftime("%Y-%m-%d")

    for item in movers:

        symbol = item["symbol"]
        closes = get_intraday_candles(symbol)

        if len(closes) < 4:
            continue

        recent = closes[-4:]

        if any(value <= 0 for value in recent):
            continue

        # نقيس حركة السعر خلال آخر 3 شموع مكتملة/متاحة.
        move = (
            (recent[-1] - recent[0]) / recent[0]
        ) * 100

        # زخم صاعد أو هابط واضح
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
            f"{emoji} تنبيه زخم سعري قوي\n\n"
            f"السهم: {symbol}\n"
            f"السعر الحالي: ${item['price']:.4f}\n"
            f"حركة آخر 15 دقيقة تقريباً: {move:+.2f}%\n"
            f"الاتجاه: {direction}\n\n"
            f"🕒 {ny_time_text()}\n"
            "المصدر: Yahoo Finance - شموع 5 دقائق\n\n"
            "هذا قياس لحركة السعر وليس تأكيداً لتدفق الأوامر."
        )

        if send_telegram(message):
            remember(alert_id)
            count += 1

        time.sleep(0.5)

    return count


# ==========================================
# STOCK-SPECIFIC NEWS
# ==========================================

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

            headline = article.get("headline", "").strip()
            summary = article.get("summary", "").strip()
            link = article.get("url", "")
            source = article.get("source", "Finnhub")
            timestamp = article.get("datetime", 0)

            if not headline or not link:
                continue

            alert_id = make_id(
                f"COMPANY_NEWS|{symbol}|{link}"
            )

            if already_sent(alert_id):
                continue

            arabic_headline = translate_arabic(headline)

            arabic_summary = (
                translate_arabic(summary[:1500])
                if summary else "لا يوجد ملخص متاح."
            )

            article_time = ""

            if timestamp:
                try:
                    article_time = datetime.fromtimestamp(
                        int(timestamp),
                        tz=ZoneInfo("UTC")
                    ).astimezone(NY_TIMEZONE).strftime(
                        "%Y-%m-%d %H:%M ET"
                    )
                except Exception:
                    pass

            message = (
                "📰 خبر خاص بسهم أمريكي\n\n"
                f"🔎 السهم: {symbol}\n\n"
                f"📌 العنوان:\n{arabic_headline}\n\n"
                f"📝 الملخص:\n{arabic_summary}\n\n"
                f"🏢 المصدر: {source}\n"
                f"🕒 وقت الخبر: {article_time}\n"
                f"🕒 وقت الرصد: {ny_time_text()}\n\n"
                f"🔗 رابط الخبر:\n{link}"
            )

            if send_telegram(message):
                remember(alert_id)
                count += 1

        time.sleep(1)

    return count


# ==========================================
# HALT ONLY - NO RESUME ALERTS
# ==========================================

def extract_halt_symbol(text):

    patterns = [
        r"SECURITY\s+([A-Z][A-Z0-9.$-]{0,9})\s+HALTED",
        r"(?:SYMBOL|TICKER|ISSUE SYMBOL)\s*[:\-]\s*([A-Z][A-Z0-9.$-]{0,9})",
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
        logging.info("Outside regular session. HALT scan skipped.")
        return 0

    try:
        response = session.get(
            NASDAQ_HALT_URL,
            timeout=25
        )

        response.raise_for_status()
        feed = feedparser.parse(response.content)

    except Exception as error:
        logging.error(f"Nasdaq halt feed error: {error}")
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

        # تجاهل أي خبر عن استئناف التداول
        if re.search(
            r"\b(RESUMED|RESUME|RESUMPTION|TRADING RESUMES)\b",
            upper_text
        ):
            continue

        # لا نرسل إلا HALT
        if not re.search(r"\bHALTED\b|\bHALT\b", upper_text):
            continue

        symbol = extract_halt_symbol(full_text)

        alert_id = make_id(
            f"HALT|{symbol}|{full_text}|{link}"
        )

        if already_sent(alert_id):
            continue

        message = (
            "🔴 HALT - إيقاف تداول\n\n"
            f"السهم: {symbol}\n"
            "الحالة: HALT فقط\n"
            f"🕒 وقت الرصد: {ny_time_text()}\n\n"
            f"تفاصيل المصدر:\n{title}\n\n"
            "المصدر: Nasdaq Trader\n"
            f"{link}"
        )

        if send_telegram(message):
            remember(alert_id)
            count += 1

    return count


# ==========================================
# WHALE ALERT
# ==========================================

def process_whale_alerts():

    # لا نعتبر ارتفاع الحجم أو ظهور عرض NBBO صفقة حوت.
    # نحتاج بيانات Time & Sales موثوقة للصفقات المنفذة،
    # وبيانات Level 2 مستقلة للأوامر المعروضة.

    if not WHALE_DATA_AVAILABLE:
        logging.info(
            "WHALE alerts disabled: verified trade/order-book "
            "data source is not configured."
        )
        return 0

    return 0


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

    # 1 - HALT only
    halt_count = process_halts()

    # 2 - Top gainers
    movers = get_market_movers()

    gainers_count = process_top_gainers(movers)

    # 3 - Price movement
    price_count = process_price_moves(movers)

    # 4 - Strong price momentum
    momentum_count = process_momentum(movers)

    # 5 - Company-specific news for scanned symbols
    symbols = [
        item["symbol"]
        for item in movers
    ]

    news_count = process_company_news(symbols)

    # 6 - Whale alerts only if a verified data source exists
    whale_count = process_whale_alerts()

    save_state()

    logging.info(
        "Scan completed | "
        f"HALT: {halt_count} | "
        f"Gainers reports: {gainers_count} | "
        f"Price alerts: {price_count} | "
        f"Momentum: {momentum_count} | "
        f"Company news: {news_count} | "
        f"WHALE: {whale_count}"
    )


if __name__ == "__main__":
    main()
