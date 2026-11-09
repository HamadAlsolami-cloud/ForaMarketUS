
import os
import re
import json
import time
import fcntl
import hashlib
import logging
import traceback
import requests

from datetime import datetime, date, timedelta, time as dtime
from zoneinfo import ZoneInfo
from functools import lru_cache
from collections import deque
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# =====================================================
# SETTINGS
# =====================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("FINNHUB_API_KEY")

STATE_FILE = os.getenv("STATE_FILE", "news_state.json")
LOCK_FILE = os.getenv("LOCK_FILE", "news_bot.lock")
LOG_FILE = os.getenv("LOG_FILE", "news_bot.log")

INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "300"))

# Number of symbols checked per cycle
MAX_SYMBOLS_PER_CYCLE = int(
    os.getenv("MAX_SYMBOLS_PER_CYCLE", "25")
)

# Maximum news alerts per cycle
MAX_NEWS_PER_CYCLE = int(
    os.getenv("MAX_NEWS_PER_CYCLE", "10")
)

# Refresh symbols every 7 days
SYMBOL_REFRESH_DAYS = 7

# Safety limit below Finnhub free tier limit
FINNHUB_CALLS_PER_MINUTE = 50

# Minimum delay between Finnhub requests
FINNHUB_REQUEST_DELAY = 1.1

# Maximum stock price
MAX_STOCK_PRICE = 10.0

BASE = "https://finnhub.io/api/v1"

TZ = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

T_PRE_OPEN = dtime(7, 0)
T_OPEN = dtime(9, 30)
T_CLOSE = dtime(16, 0)
T_POST_CLOSE = dtime(18, 0)

NEWS_LOOKBACK_DAYS = 1

HEADERS = {
    "User-Agent": "StockNewsScanner/2.0",
    "Accept": "application/json",
}

# =====================================================
# LOGGING
# =====================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# =====================================================
# HTTP SESSION
# =====================================================

session = requests.Session()
session.headers.update(HEADERS)

_retry = Retry(
    total=3,
    connect=3,
    read=2,
    backoff_factor=1,
    status_forcelist=(500, 502, 503, 504),
    allowed_methods=frozenset(["GET"]),
)

adapter = HTTPAdapter(max_retries=_retry)

session.mount("https://", adapter)
session.mount("http://", adapter)

# =====================================================
# FINNHUB RATE LIMITER
# =====================================================

_last_api_call = 0.0
_api_calls = deque()


def api_wait():

    global _last_api_call

    current = time.monotonic()

    elapsed = current - _last_api_call

    if elapsed < FINNHUB_REQUEST_DELAY:
        time.sleep(FINNHUB_REQUEST_DELAY - elapsed)

    current = time.monotonic()

    while _api_calls and current - _api_calls[0] >= 60:
        _api_calls.popleft()

    if len(_api_calls) >= FINNHUB_CALLS_PER_MINUTE:
        wait = 60 - (current - _api_calls[0]) + 1
        logging.warning(f"Finnhub rate protection: waiting {wait:.1f}s")
        time.sleep(max(1, wait))

    current = time.monotonic()

    _api_calls.append(current)
    _last_api_call = current


# =====================================================
# TIME & SCHEDULE
# =====================================================

def now():
    return datetime.now(TZ)


def is_trading_day(day):
    return day.weekday() < 5


def in_news_window():

    n = now()

    if not is_trading_day(n.date()):
        return False

    return T_PRE_OPEN <= n.time() < T_POST_CLOSE


def window_label():

    t = now().time()

    if T_PRE_OPEN <= t < T_OPEN:
        return "PRE-MARKET"

    if T_OPEN <= t < T_CLOSE:
        return "MARKET"

    if T_CLOSE <= t < T_POST_CLOSE:
        return "POST-MARKET"

    return "CLOSED"


def news_dates():

    today = now().date()

    start = today - timedelta(days=NEWS_LOOKBACK_DAYS)

    return start.isoformat(), today.isoformat()


# =====================================================
# STATE MANAGEMENT
# =====================================================

def load_state():

    try:

        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            data = {}

    except Exception:
        data = {}

    sent_list = data.get("sent", [])
    symbols = data.get("symbols", [])

    if not isinstance(sent_list, list):
        sent_list = []

    if not isinstance(symbols, list):
        symbols = []

    return {
        "sent": dict.fromkeys(sent_list),
        "symbols": symbols,
        "symbol_index": int(data.get("symbol_index", 0)),
        "symbols_updated": data.get("symbols_updated", ""),
    }


STATE = load_state()

SENT = STATE["sent"]
SYMBOLS = STATE["symbols"]
SYMBOL_INDEX = STATE["symbol_index"]
SYMBOLS_UPDATED = STATE["symbols_updated"]

_dirty = False


def save_state():

    global _dirty

    data = {
        "sent": list(SENT.keys())[-5000:],
        "symbols": SYMBOLS,
        "symbol_index": SYMBOL_INDEX,
        "symbols_updated": SYMBOLS_UPDATED,
    }

    try:

        tmp = STATE_FILE + ".tmp"

        with open(tmp, "w", encoding="utf-8") as f:

            json.dump(
                data,
                f,
                ensure_ascii=False
            )

            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp, STATE_FILE)

        _dirty = False

    except Exception as e:

        logging.error(f"State save error: {e}")


def alert_id(value):

    return hashlib.sha256(
        str(value).encode("utf-8")
    ).hexdigest()


def sent(key):

    return key in SENT


def remember(key):

    global _dirty

    SENT[key] = None
    _dirty = True

    if len(SENT) > 5000:

        old_keys = list(SENT.keys())[:-5000]

        for old in old_keys:
            SENT.pop(old, None)


# =====================================================
# TELEGRAM
# =====================================================

def send(message):

    if not TOKEN or not CHAT_ID:
        logging.error("Telegram settings missing")
        return False

    if len(message) > 3900:
        message = message[:3890] + "..."

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    for attempt in range(5):

        try:

            response = session.post(
                url,
                data={
                    "chat_id": CHAT_ID,
                    "text": message,
                    "disable_web_page_preview": True,
                },
                timeout=25
            )

            if response.status_code == 429:

                wait = response.json().get(
                    "parameters", {}
                ).get("retry_after", 10)

                time.sleep(int(wait) + 1)
                continue

            response.raise_for_status()

            result = response.json()

            if result.get("ok"):
                return True

            logging.error("Telegram rejected message")
            return False

        except requests.exceptions.RequestException as e:

            logging.error(
                f"Telegram request error: {type(e).__name__}"
            )

            # Do not blindly resend after an uncertain POST result.
            return False

        except Exception as e:

            logging.error(
                f"Telegram error: {type(e).__name__}"
            )

            return False

    return False


# =====================================================
# TRANSLATION
# =====================================================

@lru_cache(maxsize=512)
def _translate_cached(text):

    output = []

    for i in range(0, len(text), 2500):

        part = text[i:i + 2500]

        try:

            response = session.get(
                "https://translate.googleapis.com/translate_a/single",
                params={
                    "client": "gtx",
                    "sl": "auto",
                    "tl": "ar",
                    "dt": "t",
                    "q": part,
                },
                timeout=15
            )

            response.raise_for_status()

            translated = "".join(
                p[0]
                for p in response.json()[0]
                if p and p[0]
            )

            output.append(translated)

        except Exception:

            logging.warning("Translation unavailable")
            output.append(part)

    return " ".join(output)


def translate(text):

    if not text:
        return ""

    return _translate_cached(text)


# =====================================================
# FINNHUB API
# =====================================================

def finnhub(endpoint, params=None):

    if not API_KEY:
        return None

    for attempt in range(3):

        try:

            api_wait()

            response = session.get(
                f"{BASE}/{endpoint}",
                params=params or {},
                headers={
                    "X-Finnhub-Token": API_KEY
                },
                timeout=20
            )

            if response.status_code == 429:

                wait = int(
                    response.headers.get("Retry-After", 10)
                )

                logging.warning(
                    f"Finnhub 429: waiting {wait}s"
                )

                time.sleep(wait + 1)
                continue

            if response.status_code == 403:

                logging.error(
                    f"Finnhub access denied: {endpoint}"
                )

                return None

            response.raise_for_status()

            return response.json()

        except requests.exceptions.RequestException as e:

            logging.warning(
                f"Finnhub {endpoint}: {type(e).__name__}"
            )

            if attempt < 2:
                time.sleep(3 * (attempt + 1))

        except Exception as e:

            logging.error(
                f"Finnhub unexpected error: {e}"
            )

            return None

    return None


# =====================================================
# US STOCK UNIVERSE
# =====================================================

def valid_symbol(symbol):

    if not symbol:
        return False

    symbol = str(symbol).strip().upper()

    if not re.fullmatch(
        r"[A-Z][A-Z0-9.-]{0,14}",
        symbol
    ):
        return False

    return True


def fetch_us_symbols():

    logging.info("Refreshing US stock universe")

    data = finnhub(
        "stock/symbol",
        {"exchange": "US"}
    )

    if not isinstance(data, list):

        logging.error("Unable to download US symbols")
        return []

    result = set()

    allowed_mics = {
        "XNAS",
        "XNYS",
        "XASE",
        "ARCX",
        "BATS",
        "IEXG",
    }

    for item in data:

        if not isinstance(item, dict):
            continue

        symbol = (
            item.get("symbol") or ""
        ).strip().upper()

        stock_type = (
            item.get("type") or ""
        ).strip().lower()

        if not valid_symbol(symbol):
            continue

        # Common stocks only.
        if stock_type != "common stock":
            continue

        # Exclude OTC when exchange MIC is supplied.
        mic = item.get("mic")

        if mic and mic not in allowed_mics:
            continue

        result.add(symbol)

    symbols = sorted(result)

    logging.info(
        f"US common stocks loaded: {len(symbols)}"
    )

    return symbols


def refresh_symbols():

    global SYMBOLS
    global SYMBOL_INDEX
    global SYMBOLS_UPDATED
    global _dirty

    should_refresh = True

    if SYMBOLS and SYMBOLS_UPDATED:

        try:

            last_update = date.fromisoformat(
                SYMBOLS_UPDATED
            )

            age = (
                now().date() - last_update
            ).days

            should_refresh = age >= SYMBOL_REFRESH_DAYS

        except ValueError:
            should_refresh = True

    if not should_refresh:
        return

    new_symbols = fetch_us_symbols()

    if not new_symbols:

        logging.warning(
            "Keeping previous symbol list"
        )

        return

    SYMBOLS = new_symbols

    SYMBOL_INDEX = SYMBOL_INDEX % len(SYMBOLS)

    SYMBOLS_UPDATED = now().date().isoformat()

    _dirty = True

    save_state()


# =====================================================
# STOCK NEWS
# =====================================================

def fetch_company_news(symbol):

    start, end = news_dates()

    data = finnhub(
        "company-news",
        {
            "symbol": symbol,
            "from": start,
            "to": end,
        }
    )

    if not isinstance(data, list):
        return []

    return data


# =====================================================
# STOCK PRICE
# =====================================================

def get_stock_price(symbol):

    data = finnhub(
        "quote",
        {"symbol": symbol}
    )

    if not isinstance(data, dict):
        return None

    try:

        price = float(data.get("c", 0))

        if price <= 0:
            return None

        return price

    except (TypeError, ValueError):

        return None


def qualifies_stock(symbol):

    price = get_stock_price(symbol)

    if price is None:
        return False

    if price >= MAX_STOCK_PRICE:
        return False

    logging.info(
        f"Qualified: {symbol} | ${price:.4f}"
    )

    return True


# =====================================================
# NEWS SENTIMENT
# =====================================================

POSITIVE_WORDS = [

    "beats estimates",
    "beat estimates",
    "earnings beat",
    "revenue growth",
    "raises guidance",
    "raised guidance",
    "fda approval",
    "approved by fda",
    "receives approval",
    "awarded contract",
    "wins contract",
    "new contract",
    "strategic partnership",
    "record revenue",
    "record sales",
    "profit increases",
    "positive results",
    "clinical success",
    "buyback",
    "share repurchase",
    "upgraded",
    "price target raised",
    "expands agreement",
    "receives order",
    "receives funding",
    "debt reduction",
    "تجاوز التوقعات",
    "نتائج إيجابية",
    "موافقة",
    "عقد جديد",
    "فوز بعقد",
    "نمو الإيرادات",
    "ارتفاع الأرباح",
    "شراكة استراتيجية",
    "زيادة التوجيهات",
]

NEGATIVE_WORDS = [

    "misses estimates",
    "missed estimates",
    "earnings miss",
    "revenue decline",
    "cuts guidance",
    "lowered guidance",
    "stock offering",
    "public offering",
    "registered direct offering",
    "dilution",
    "share dilution",
    "reverse stock split",
    "bankruptcy",
    "chapter 11",
    "delisting",
    "sec investigation",
    "fraud investigation",
    "class action",
    "securities lawsuit",
    "going concern",
    "default",
    "debt restructuring",
    "negative results",
    "clinical trial failure",
    "fda rejects",
    "fda rejection",
    "downgraded",
    "price target lowered",
    "suspends operations",
    "layoffs",
    "تراجع الإيرادات",
    "خسائر",
    "طرح أسهم",
    "تخفيف الأسهم",
    "تقسيم عكسي",
    "إفلاس",
    "شطب",
    "تحقيق",
    "دعوى قضائية",
    "خفض التوقعات",
    "نتائج سلبية",
    "رفض الموافقة",
]


def evaluate_news(text):

    content = (text or "").lower()

    positive_score = sum(
        content.count(word.lower())
        for word in POSITIVE_WORDS
    )

    negative_score = sum(
        content.count(word.lower())
        for word in NEGATIVE_WORDS
    )

    if positive_score > negative_score:

        return "🟢 إيجابي"

    if negative_score > positive_score:

        return "🔴 سلبي"

    return "🟡 محايد"


# =====================================================
# NEWS IDENTIFICATION
# =====================================================

def article_timestamp(article):

    try:

        return int(article.get("datetime", 0))

    except (TypeError, ValueError):

        return 0


def article_key(symbol, article):

    article_id = article.get("id")

    url = article.get("url")

    headline = article.get("headline", "")

    timestamp = article_timestamp(article)

    identity = (
        article_id
        or url
        or f"{headline}|{timestamp}"
    )

    return alert_id(
        f"{symbol}|{identity}"
    )


def article_is_recent(article):

    timestamp = article_timestamp(article)

    if timestamp <= 0:
        return False

    try:

        published = datetime.fromtimestamp(
            timestamp,
            UTC
        )

        age = (
            datetime.now(UTC) - published
        ).total_seconds()

        return 0 <= age <= 48 * 3600

    except Exception:

        return False


# =====================================================
# NEWS MESSAGE
# =====================================================

def build_message(symbol, article):

    headline = (
        article.get("headline") or ""
    ).strip()

    summary = (
        article.get("summary") or ""
    ).strip()

    raw_news = headline

    if summary:
        raw_news += " — " + summary

    rating = evaluate_news(raw_news)

    translated = translate(raw_news)

    translated = re.sub(
        r"\s+",
        " ",
        translated
    ).strip()

    return (
        f"${symbol}\n"
        f"تقييم الخبر: {rating}\n"
        f"الخبر: {translated}"
    )


# =====================================================
# ROTATING SYMBOL SCANNER
# =====================================================

def get_next_symbols():

    global SYMBOL_INDEX
    global _dirty

    if not SYMBOLS:
        return []

    total = len(SYMBOLS)

    amount = min(
        MAX_SYMBOLS_PER_CYCLE,
        total
    )

    selected = []

    for _ in range(amount):

        selected.append(
            SYMBOLS[SYMBOL_INDEX]
        )

        SYMBOL_INDEX = (
            SYMBOL_INDEX + 1
        ) % total

    _dirty = True

    return selected


def process_stock_news():

    count = 0

    candidates = []

    symbols = get_next_symbols()

    if not symbols:

        logging.warning("No stock symbols available")
        return 0

    logging.info(
        f"Scanning {len(symbols)} symbols"
    )

    # First stage: company news
    for symbol in symbols:

        try:

            articles = fetch_company_news(symbol)

            if not articles:
                continue

            articles.sort(
                key=article_timestamp,
                reverse=True
            )

            for article in articles:

                headline = (
                    article.get("headline") or ""
                ).strip()

                if not headline:
                    continue

                if not article_is_recent(article):
                    continue

                key = article_key(symbol, article)

                if sent(key):
                    continue

                candidates.append(
                    (symbol, article, key)
                )

        except Exception as e:

            logging.warning(
                f"News scan {symbol}: {type(e).__name__}"
            )

    # Newest articles first
    candidates.sort(
        key=lambda item: article_timestamp(item[1]),
        reverse=True
    )

    logging.info(
        f"New article candidates: {len(candidates)}"
    )

    # Second stage: price verification
    checked_prices = {}

    for symbol, article, key in candidates:

        if count >= MAX_NEWS_PER_CYCLE:
            break

        if symbol not in checked_prices:

            checked_prices[symbol] = qualifies_stock(
                symbol
            )

        if not checked_prices[symbol]:
            continue

        try:

            message = build_message(
                symbol,
                article
            )

            if not message:
                continue

            if send(message):

                remember(key)

                count += 1

                logging.info(
                    f"News sent: {symbol}"
                )

                time.sleep(1.2)

        except Exception as e:

            logging.error(
                f"Processing {symbol}: {type(e).__name__}"
            )

    return count


# =====================================================
# LOCK
# =====================================================

def acquire_lock():

    try:

        f = open(LOCK_FILE, "w")

        try:

            fcntl.flock(
                f,
                fcntl.LOCK_EX | fcntl.LOCK_NB
            )

        except BlockingIOError:

            logging.error(
                "Another instance is running"
            )

            f.close()

            return None

        f.write(str(os.getpid()))
        f.flush()

        return f

    except Exception as e:

        logging.error(
            f"Lock error: {e}"
        )

        return None


# =====================================================
# MAIN CYCLE
# =====================================================

def run_once():

    global _dirty

    logging.info("=" * 60)
    logging.info("STOCK NEWS SCANNER - NEW CYCLE")

    if not TOKEN or not CHAT_ID or not API_KEY:

        logging.error(
            "Missing TELEGRAM_BOT_TOKEN / "
            "TELEGRAM_CHAT_ID / FINNHUB_API_KEY"
        )

        return

    if not in_news_window():

        logging.info(
            f"Outside schedule: {now().strftime('%A %I:%M %p ET')}"
        )

        return

    try:

        logging.info(
            f"Active session: {window_label()}"
        )

        refresh_symbols()

        if not SYMBOLS:

            logging.warning(
                "Stock universe unavailable"
            )

            return

        count = process_stock_news()

        logging.info(
            f"Cycle completed | NEWS SENT: {count} | "
            f"SYMBOLS: {len(SYMBOLS)} | "
            f"INDEX: {SYMBOL_INDEX}"
        )

    finally:

        if _dirty:
            save_state()


# =====================================================
# CONTINUOUS RUNNER
# =====================================================

def run_forever():

    logging.info(
        "STOCK NEWS SCANNER STARTED"
    )

    logging.info(
        f"Interval: {INTERVAL_SECONDS}s"
    )

    logging.info(
        f"Symbols per cycle: {MAX_SYMBOLS_PER_CYCLE}"
    )

    logging.info(
        f"Maximum news per cycle: {MAX_NEWS_PER_CYCLE}"
    )

    logging.info(
        "Stock price filter: UNDER $10"
    )

    logging.info(
        "Schedule: 07:00-18:00 ET | Monday-Friday"
    )

    while True:

        try:

            run_once()

        except KeyboardInterrupt:

            logging.warning(
                "Stopped by user"
            )

            break

        except Exception as e:

            logging.error(
                f"Cycle error: {e}\n"
                f"{traceback.format_exc()}"
            )

        finally:

            if _dirty:
                save_state()

        logging.info(
            f"Sleeping {INTERVAL_SECONDS}s..."
        )

        try:

            time.sleep(INTERVAL_SECONDS)

        except KeyboardInterrupt:

            logging.warning(
                "Sleep interrupted"
            )

            break


# =====================================================
# ENTRY POINT
# =====================================================

def main():

    try:

        os.makedirs(
            os.path.dirname(LOG_FILE) or ".",
            exist_ok=True
        )

        fh = logging.FileHandler(
            LOG_FILE,
            encoding="utf-8"
        )

        fh.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)s | %(message)s"
            )
        )

        logging.getLogger().addHandler(fh)

    except Exception as e:

        logging.warning(
            f"File logging disabled: {e}"
        )

    lock_handle = acquire_lock()

    if lock_handle is None:
        return

    try:

        run_forever()

    finally:

        if _dirty:
            save_state()

        logging.info(
            "Stock News Scanner shutdown"
        )

        try:
            lock_handle.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
