import os
import json
import time
import fcntl
import hashlib
import logging
import traceback
import requests
from datetime import datetime, time as dtime
from functools import lru_cache
from zoneinfo import ZoneInfo
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =============== SETTINGS ===============
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("FINNHUB_API_KEY")

STATE_FILE = os.getenv("STATE_FILE", "news_state.json")
LOCK_FILE = os.getenv("LOCK_FILE", "news_bot.lock")
LOG_FILE = os.getenv("LOG_FILE", "news_bot.log")

INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "300"))
MAX_NEWS_PER_CYCLE = int(os.getenv("MAX_NEWS_PER_CYCLE", "10"))
NEWS_CATEGORY = os.getenv("NEWS_CATEGORY", "general")

BASE = "https://finnhub.io/api/v1"
TZ = ZoneInfo("America/New_York")

# =============== SMART SCHEDULE (ET) ===============
T_PRE_OPEN = dtime(7, 0)      # بداية نافذة ما قبل الافتتاح
T_OPEN = dtime(9, 30)          # افتتاح السوق
T_CLOSE = dtime(16, 0)         # إغلاق السوق
T_POST_CLOSE = dtime(18, 0)    # نهاية نافذة ما بعد الإغلاق

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
session.headers.update(HEADERS)
_retry = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=(500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST"]),
)
session.mount("https://", HTTPAdapter(max_retries=_retry))
session.mount("http://", HTTPAdapter(max_retries=_retry))


# =============== TIME & SCHEDULE ===============
def now():
    return datetime.now(TZ)


def time_text():
    return now().strftime("%Y-%m-%d %I:%M:%S %p ET")


def is_trading_day(day):
    """عطلة نهاية الأسبوع فقط (بدون مكتبة تقويم إضافية)."""
    return day.weekday() < 5


def in_news_window():
    """نافذة ذكية: 7:00 صباحاً حتى 6:00 مساءً بتوقيت نيويورك، أيام التداول."""
    n = now()
    if not is_trading_day(n.date()):
        return False
    return T_PRE_OPEN <= n.time() < T_POST_CLOSE


def window_label():
    """وصف النافذة الحالية لأغراض اللوج."""
    t = now().time()
    if T_PRE_OPEN <= t < T_OPEN:
        return "pre-market"
    if T_OPEN <= t < T_CLOSE:
        return "market"
    if T_CLOSE <= t < T_POST_CLOSE:
        return "post-market"
    return "closed"


# =============== STATE ===============
try:
    with open(STATE_FILE, encoding="utf-8") as f:
        _loaded = json.load(f)
    _sent_list = _loaded.get("sent", []) if isinstance(_loaded, dict) else []
except Exception:
    _sent_list = []

SENT = dict.fromkeys(_sent_list)
_dirty = False


def save_state():
    global _dirty
    keys = list(SENT.keys())[-5000:]
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"sent": keys}, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
        _dirty = False
    except Exception as e:
        logging.error(f"State: {e}")


def alert_id(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def sent(key):
    return key in SENT


def remember(key):
    global _dirty
    SENT[key] = None
    _dirty = True
    if len(SENT) % 20 == 0:
        save_state()


# =============== TELEGRAM ===============
def send(message):
    if not TOKEN or not CHAT_ID:
        logging.error("Telegram settings missing")
        return False

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    chunks = []
    while len(message) > 3900:
        pos = message.rfind("\n", 0, 3900)
        if pos < 1:
            pos = 3900
        chunks.append(message[:pos])
        message = message[pos:].lstrip()
    chunks.append(message)

    for chunk in chunks:
        for _ in range(5):
            try:
                r = session.post(
                    url,
                    data={
                        "chat_id": CHAT_ID,
                        "text": chunk,
                        "disable_web_page_preview": True
                    },
                    timeout=25
                )

                if r.status_code == 429:
                    wait = r.json().get("parameters", {}).get("retry_after", 10)
                    time.sleep(int(wait) + 1)
                    continue

                r.raise_for_status()
                if not r.json().get("ok"):
                    return False

                time.sleep(1.2)
                break

            except Exception as e:
                logging.warning(f"Telegram error: {type(e).__name__}")
                time.sleep(3)
        else:
            return False

    return True


# =============== TRANSLATION ===============
@lru_cache(maxsize=256)
def _translate_cached(text):
    output = []
    for i in range(0, len(text), 2500):
        part = text[i:i + 2500]
        try:
            r = session.get(
                "https://translate.googleapis.com/translate_a/single",
                params={
                    "client": "gtx", "sl": "auto", "tl": "ar",
                    "dt": "t", "q": part
                },
                timeout=15
            )
            r.raise_for_status()
            output.append("".join(
                p[0] for p in r.json()[0] if p and p[0]
            ))
        except Exception:
            output.append(part)
    return "\n".join(output)


def translate(text):
    if not text:
        return ""
    return _translate_cached(text)


# =============== FINNHUB ===============
def finnhub(endpoint, params=None):
    if not API_KEY:
        return None
    try:
        p = dict(params or {})
        p["token"] = API_KEY
        r = session.get(f"{BASE}/{endpoint}", params=p, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.warning(f"Finnhub {endpoint}: {type(e).__name__}")
        return None


def fetch_news():
    data = finnhub("news", {"category": NEWS_CATEGORY})
    if not isinstance(data, list):
        logging.warning("Finnhub news: invalid response")
        return []
    return data


# =============== NEWS PROCESSING ===============
def format_source(article):
    src = article.get("source") or "Finnhub"
    return str(src).strip()


def process_general_news():
    count = 0
    articles = fetch_news()

    if not articles:
        logging.info("No news returned from Finnhub")
        return 0

    articles.sort(key=lambda a: a.get("datetime", 0), reverse=True)
    label = window_label()

    for article in articles[:MAX_NEWS_PER_CYCLE]:
        headline = (article.get("headline") or "").strip()
        summary = (article.get("summary") or "").strip()
        link = (article.get("url") or "").strip()
        source = format_source(article)

        if not headline or not link:
            continue

        key = alert_id(f"NEWS|{link}")
        if sent(key):
            continue

        raw_text = headline + ("\n\n" + summary if summary else "")
        translated = translate(raw_text)

        published_ts = article.get("datetime")
        if published_ts:
            try:
                pub_dt = datetime.fromtimestamp(
                    int(published_ts), ZoneInfo("UTC")
                ).astimezone(TZ)
                pub_text = pub_dt.strftime("%Y-%m-%d %I:%M %p ET")
            except (TypeError, ValueError, OSError):
                pub_text = "غير متاح"
        else:
            pub_text = "غير متاح"

        msg = (
            f"📰 خبر جديد\n"
            f"المصدر: {source}\n"
            f"🕒 {pub_text}\n"
            f"📊 النافذة: {label}\n\n"
            f"{translated}\n\n"
            f"🔗 {link}"
        )

        if send(msg):
            remember(key)
            count += 1
            time.sleep(1.5)

    return count


# =============== LOCK ===============
def acquire_lock():
    try:
        f = open(LOCK_FILE, "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.error(
                f"Another instance is running (lock: {LOCK_FILE}). Exiting."
            )
            f.close()
            return None
        f.write(str(os.getpid()))
        f.flush()
        return f
    except Exception as e:
        logging.error(f"Lock error: {e}")
        return None


# =============== MAIN ===============
def run_once():
    logging.info("=" * 50)
    logging.info("News Bot: new cycle")

    if not TOKEN or not CHAT_ID or not API_KEY:
        logging.error(
            "Missing TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID or FINNHUB_API_KEY"
        )
        return

    if not in_news_window():
        logging.info(
            f"Outside news window ({now().strftime('%A %I:%M %p ET')}) — skipping"
        )
        return

    try:
        logging.info(f"Inside window: {window_label()}")
        count = process_general_news()
        logging.info(f"Cycle done | NEWS:{count}")
    finally:
        if _dirty:
            save_state()


def run_forever():
    logging.info(
        f"News Bot started | interval={INTERVAL_SECONDS}s "
        f"({INTERVAL_SECONDS // 60} min) | category={NEWS_CATEGORY} | "
        f"window=07:00–18:00 ET (Mon–Fri)"
    )

    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            logging.warning("Stopped by user (Ctrl+C)")
            break
        except Exception as e:
            logging.error(f"Cycle error: {e}\n{traceback.format_exc()}")
            try:
                send(f"❌ خطأ في البوت: {type(e).__name__}: {str(e)[:300]}")
            except Exception:
                pass
        finally:
            if _dirty:
                save_state()

        logging.info(f"Sleeping {INTERVAL_SECONDS}s until next cycle...")
        try:
            time.sleep(INTERVAL_SECONDS)
        except KeyboardInterrupt:
            logging.warning("Sleep interrupted by user")
            break


def main():
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s"
        ))
        logging.getLogger().addHandler(fh)
    except Exception as e:
        logging.warning(f"File logging disabled: {e}")

    lock_handle = acquire_lock()
    if lock_handle is None:
        return

    try:
        run_forever()
    finally:
        if _dirty:
            save_state()
        logging.info("Shutdown complete")
        try:
            lock_handle.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
