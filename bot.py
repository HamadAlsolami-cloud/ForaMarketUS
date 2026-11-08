import os
import re
import json
import time
import fcntl
import hashlib
import logging
import traceback
import requests
import feedparser
from datetime import datetime, timedelta, time as dtime
from functools import lru_cache
from zoneinfo import ZoneInfo
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =============== SETTINGS ===============
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("FINNHUB_API_KEY")
MASSIVE_KEY = os.getenv("MASSIVE_API_KEY")

STATE_FILE = os.getenv("STATE_FILE", "alert_state.json")
LOCK_FILE = os.getenv("LOCK_FILE", "bot.lock")
LOG_FILE = os.getenv("LOG_FILE", "bot.log")

# فترة الانتظار بين التشغيلات (ثوانٍ) — افتراضي 15 دقيقة
INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "900"))

BASE = "https://finnhub.io/api/v1"
NASDAQ_API = "https://api.nasdaq.com/api/screener/stocks"
HALT_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"

MAX_PRICE = 5.00
MAX_SCAN = 20
MAX_NEWS = 10
MAX_SEC = 10

MAX_PRICE_CHECKS = 45
MAX_HALT_PRICE_CHECKS = 20

PRICE_LIMIT = 5.0
MOMENTUM_LIMIT = 2.5

MIN_VOLUME = 100_000
PRE_MARKET_VOLUME_DIVISOR = 3
NEWS_MAX_AGE_HOURS = 12
HALT_MAX_AGE_HOURS = 4

MIN_SEC_PRICE = 0.30

NASDAQ_PAGE_SIZE = 5000
NASDAQ_MAX_PAGES = 4

NASDAQ_EXCHANGES = ("NASDAQ", "NYSE", "AMEX")

SEC_FORMS = {
    "8-K", "6-K", "10-K", "10-Q", "20-F",
    "S-1", "F-1", "S-3", "F-3", "424B5",
    "SC 13D", "SC 13G", "144"
}

TZ = ZoneInfo("America/New_York")
T_OPEN = dtime(9, 30)
T_CLOSE = dtime(16, 0)
T_PRE = dtime(4, 0)
T_POST_END = dtime(20, 0)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# جلسة مع إعادة محاولة تلقائية
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

PRICE_CACHE = {}
PRICE_CHECKS = 0
HALT_PRICE_CHECKS = 0


# =============== TIME ===============
def now():
    return datetime.now(TZ)


def time_text():
    return now().strftime("%Y-%m-%d %I:%M:%S %p ET")


@lru_cache(maxsize=32)
def is_trading_day(day):
    """تقويم NYSE (مخزّن مؤقتاً) مع fallback آمن."""
    try:
        import pandas_market_calendars as mcal
        cal = mcal.get_calendar("NYSE")
        schedule = cal.schedule(
            start_date=day.isoformat(), end_date=day.isoformat()
        )
        return not schedule.empty
    except Exception as e:
        logging.warning(f"NYSE calendar unavailable; weekday fallback: {e}")
        return day.weekday() < 5


def market_open():
    n = now()
    if not is_trading_day(n.date()):
        return False
    return T_OPEN <= n.time() < T_CLOSE


def effective_min_volume():
    """حد السيولة يتكيّف مع جلسة ما قبل الافتتاح."""
    n = now()
    if is_trading_day(n.date()) and T_PRE <= n.time() < T_OPEN:
        return MIN_VOLUME // PRE_MARKET_VOLUME_DIVISOR
    return MIN_VOLUME


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
    """يسجّل في الذاكرة فقط؛ الحفظ على القرص مرة واحدة في النهاية."""
    global _dirty
    SENT[key] = None
    _dirty = True


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
def translate(text):
    if not text:
        return ""

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
                timeout=20
            )
            r.raise_for_status()
            output.append("".join(
                p[0] for p in r.json()[0] if p and p[0]
            ))
        except Exception:
            output.append(part)

    return "\n".join(output)


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


def price(symbol, purpose="general"):
    global PRICE_CHECKS, HALT_PRICE_CHECKS

    symbol = symbol.upper()
    purpose = "halt" if purpose == "halt" else "general"
    cache_key = (purpose, symbol)

    if cache_key in PRICE_CACHE:
        return PRICE_CACHE[cache_key]

    if purpose == "halt":
        if HALT_PRICE_CHECKS >= MAX_HALT_PRICE_CHECKS:
            return None
        HALT_PRICE_CHECKS += 1
    else:
        if PRICE_CHECKS >= MAX_PRICE_CHECKS:
            return None
        PRICE_CHECKS += 1

    q = finnhub("quote", {"symbol": symbol})

    if q is None:
        return None

    try:
        value = float(q["c"])
        result = value if value > 0 else None
    except (TypeError, ValueError, KeyError):
        result = None

    PRICE_CACHE[cache_key] = result
    return result


def eligible(value):
    return value is not None and 0 < value < MAX_PRICE


def money(value):
    if not value:
        return "غير متاح"
    return f"${value:.2f}" if value >= 1 else f"${value:.4f}"


# =============== NASDAQ SCREENER HELPERS ===============
def parse_number(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    s = str(value).strip()
    if not s or s in {"N/A", "--", "NA"}:
        return None

    s = s.replace(",", "").replace("$", "").replace("%", "")
    s = s.replace("+", "").strip()

    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]

    try:
        return float(s)
    except (TypeError, ValueError):
        return None


_EXCLUDE_RE = re.compile(
    r"\b(etf|etn|warrants?|units?|rights?|debentures?|"
    r"depositary shares?|exchange[- ]traded fund)\b"
    r"|\bpreferred\b|\bpfd\b|\bnotes? due\b|\b\d+(\.\d+)?% notes?\b"
    r"|\bsenior notes?\b|\bsubordinated\b",
    re.I
)

_BAD_SYMBOLS = {"ETF", "TEST", "NULL", "NA", "N/A"}


def is_common_equity(row):
    name = str(row.get("name") or row.get("securityName") or "")
    return not _EXCLUDE_RE.search(name)


def normalize_nasdaq_row(row, requested_exchange):
    symbol = str(row.get("symbol") or "").strip().upper()
    if not symbol or len(symbol) > 15:
        return None
    if not re.fullmatch(r"[A-Z][A-Z0-9.$-]{0,14}", symbol):
        return None
    if symbol in _BAD_SYMBOLS:
        return None

    last = parse_number(
        row.get("lastsale") or row.get("lastSale") or row.get("last")
    )
    pct = parse_number(
        row.get("pctchange")
        or row.get("percentageChange")
        or row.get("percentChange")
    )

    if last is None or last <= 0 or pct is None:
        return None
    if not is_common_equity(row):
        return None

    return {
        "symbol": symbol,
        "price": last,
        "change": pct,
        "exchange": requested_exchange,
        "volume": int(parse_number(row.get("volume")) or 0),
        "name": str(row.get("name") or row.get("securityName") or "")
    }


def fetch_nasdaq_exchange(exchange):
    """يجلب كل الصفحات (offset) حتى الانتهاء أو الوصول للحد الأقصى."""
    rows = []
    offset = 0
    req_headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/market-activity/stocks/screener"
    }

    for _ in range(NASDAQ_MAX_PAGES):
        try:
            r = session.get(
                NASDAQ_API,
                params={
                    "tableonly": "false",
                    "limit": NASDAQ_PAGE_SIZE,
                    "offset": offset,
                    "exchange": exchange.lower(),
                    "download": "true"
                },
                headers=req_headers,
                timeout=35
            )
            r.raise_for_status()
            payload = r.json()

            data = payload.get("data") or {}
            table = data.get("table") or {}
            page_rows = (
                table.get("rows") or data.get("rows") or payload.get("rows") or []
            )

            if not isinstance(page_rows, list) or not page_rows:
                if offset == 0:
                    logging.warning(
                        f"Nasdaq {exchange}: zero rows; "
                        f"top_keys={list(payload.keys())}; "
                        f"data_keys={list(data.keys())}"
                    )
                break

            rows.extend(page_rows)

            total = parse_number(
                data.get("totalrecords") or table.get("totalrecords")
            )
            offset += len(page_rows)

            if len(page_rows) < NASDAQ_PAGE_SIZE:
                break
            if total and offset >= total:
                break

            time.sleep(0.5)

        except Exception as e:
            logging.warning(f"Nasdaq screener {exchange}: {type(e).__name__}: {e}")
            break

    logging.info(f"NASDAQ SCREENER {exchange}: received {len(rows)} rows")
    return rows


# =============== MARKET MOVERS ===============
def market_movers():
    all_rows = {}
    stats = {
        "received": 0, "valid_equity": 0,
        "under_5": 0, "liquid_under_5": 0, "verified_under_5": 0
    }

    for exchange in NASDAQ_EXCHANGES:
        rows = fetch_nasdaq_exchange(exchange)
        stats["received"] += len(rows)

        for row in rows:
            item = normalize_nasdaq_row(row, exchange)
            if not item:
                continue
            stats["valid_equity"] += 1
            all_rows[item["symbol"]] = item

    under_5 = [x for x in all_rows.values() if eligible(x["price"])]
    stats["under_5"] = len(under_5)

    min_vol = effective_min_volume()
    liquid = [x for x in under_5 if x["volume"] >= min_vol]
    stats["liquid_under_5"] = len(liquid)

    liquid.sort(key=lambda x: abs(x["change"]), reverse=True)

    verified = []
    for item in liquid:
        if PRICE_CHECKS >= MAX_PRICE_CHECKS:
            break
        live_price = price(item["symbol"], "general")
        if eligible(live_price):
            item["price"] = live_price
            verified.append(item)

    stats["verified_under_5"] = len(verified)
    logging.info(f"NASDAQ FILTER STATS: {stats} (min_vol={min_vol})")
    logging.info(
        f"Finnhub verified under $5: {len(verified)} "
        f"| Checks: {PRICE_CHECKS}/{MAX_PRICE_CHECKS}"
    )

    return sorted(verified, key=lambda x: abs(x["change"]), reverse=True)


# =============== NEWS TIME / HOLIDAY FILTER ===============
def parse_finnhub_time(value):
    try:
        return datetime.fromtimestamp(
            int(value), ZoneInfo("UTC")
        ).astimezone(TZ)
    except (TypeError, ValueError, OSError):
        return None


def news_session_label(published):
    if published is None:
        return "وقت النشر غير متاح"
    t = published.time()
    if T_PRE <= t < T_OPEN:
        return "قبل افتتاح السوق"
    if T_OPEN <= t < T_CLOSE:
        return "أثناء جلسة السوق"
    if T_CLOSE <= t < T_POST_END:
        return "بعد إغلاق السوق"
    return "خارج أوقات التداول الممتدة"


POSITIVE_KEYWORDS = (
    "merger", "acquisition", "acquires", "acquired", "takeover",
    "definitive agreement", "strategic agreement", "contract award",
    "million contract", "billion contract", "fda approval", "fda clears",
    "clinical trial", "phase 2", "phase 3", "positive topline",
    "receives approval", "ceo appointed", "material agreement",
    "guidance raised", "earnings results", "settlement"
)

NEGATIVE_KEYWORDS = (
    "bankruptcy", "chapter 11", "going concern", "default", "delisting",
    "offering", "registered direct", "private placement", "public offering",
    "sec investigation", "ceo resigns", "material weakness",
    "guidance cut", "restatement"
)


def news_tone(text):
    """يرجع 'neg' أو 'pos' أو None. السلبي يفوز عند التعارض (تصنيف محافظ)."""
    t = (text or "").lower()
    if any(k in t for k in NEGATIVE_KEYWORDS):
        return "neg"
    if any(k in t for k in POSITIVE_KEYWORDS):
        return "pos"
    return None


# =============== PRICE MOVES ===============
def process_moves(movers):
    count = 0
    today = now().strftime("%Y-%m-%d")

    for x in movers[:MAX_SCAN]:
        change = x["change"]
        if abs(change) < PRICE_LIMIT:
            continue

        symbol = x["symbol"]
        key = alert_id(f"PRICE|{symbol}|{today}")
        if sent(key):
            continue

        current_price = price(symbol)
        if not eligible(current_price):
            continue

        emoji = "🚀" if change > 0 else "🔻"
        msg = (
            f"{emoji} {symbol} {money(current_price)}\n\n"
            f"التغير اليومي: {change:+.2f}%\n"
            f"🕒 {time_text()}\n"
            "المصدر: Nasdaq Screener / Finnhub"
        )

        if send(msg):
            remember(key)
            count += 1

    return count


# =============== 1V + RVOL / MOMENTUM ===============
def massive_json(path, params=None):
    if not MASSIVE_KEY:
        return None
    try:
        q = dict(params or {})
        q["apiKey"] = MASSIVE_KEY
        r = session.get(f"https://api.massive.com{path}", params=q, timeout=25)
        r.raise_for_status()
        data = r.json()
        return data if data.get("status") in ("OK", "DELAYED", None) else None
    except Exception as e:
        ticker = path.split("/")[4] if path.count("/") > 4 else path
        logging.warning(f"Massive {ticker}: {type(e).__name__}")
        return None


def volume_metrics(symbol):
    """1V = آخر شمعة دقيقة مكتملة. RVOL = حجم اليوم / متوسط 20 جلسة."""
    if not MASSIVE_KEY:
        return None, None
    today = now().date()
    day_data = massive_json(
        f"/v2/aggs/ticker/{symbol}/range/1/day/"
        f"{(today - timedelta(days=40)).isoformat()}/{today.isoformat()}",
        {"adjusted": "true", "sort": "asc", "limit": 50}
    )
    minute_data = massive_json(
        f"/v2/aggs/ticker/{symbol}/range/1/minute/"
        f"{today.isoformat()}/{today.isoformat()}",
        {"adjusted": "true", "sort": "desc", "limit": 5}
    )
    try:
        days = (day_data or {}).get("results", [])
        if not days:
            return None, None

        def bar_date(b):
            return datetime.fromtimestamp(b["t"] / 1000, TZ).date()

        today_bars = [b for b in days if bar_date(b) == today]
        prior = [b for b in days if bar_date(b) < today][-20:]

        current_day_volume = (
            float(today_bars[-1].get("v", 0)) if today_bars else None
        )
        avg_daily = (
            sum(float(b.get("v", 0)) for b in prior) / len(prior)
            if prior else None
        )

        rvol = None
        if (
            current_day_volume is not None
            and avg_daily is not None
            and avg_daily > 0
        ):
            rvol = current_day_volume / avg_daily

        minutes = (minute_data or {}).get("results", [])
        if len(minutes) >= 2:
            one_minute = float(minutes[1].get("v", 0))
        elif minutes:
            one_minute = float(minutes[0].get("v", 0))
        else:
            one_minute = None

        return one_minute, rvol
    except Exception as e:
        logging.warning(f"Volume metrics {symbol}: {e}")
        return None, None


def candles(symbol):
    """
    شموع 5 دقائق من Yahoo (مصدر غير رسمي).
    نحذف أي شمعة لم تكتمل مدتها (5 دقائق) بعد.
    """
    try:
        r = session.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"range": "1d", "interval": "5m"}, timeout=20
        )
        r.raise_for_status()
        result = (r.json().get("chart") or {}).get("result") or []
        if not result:
            return []

        data = result[0]
        timestamps = data.get("timestamp") or []
        values = data["indicators"]["quote"][0]["close"]

        now_ts = time.time()
        clean = []
        for ts, v in zip(timestamps, values):
            if v is None:
                continue
            if now_ts - ts >= 300:
                clean.append(float(v))

        return clean
    except Exception as e:
        logging.warning(f"Candles {symbol}: {type(e).__name__}")
        return []


def process_momentum(movers):
    if not market_open():
        return 0
    count = 0
    today = now().strftime("%Y-%m-%d")

    for x in movers[:MAX_SCAN]:
        if PRICE_CHECKS >= MAX_PRICE_CHECKS:
            logging.warning("Momentum: price budget exhausted, stopping")
            break

        symbol = x["symbol"]

        key = alert_id(f"MOMENTUM|{symbol}|{today}")
        if sent(key):
            continue

        current_price = price(symbol)
        if not eligible(current_price):
            continue

        values = candles(symbol)
        if len(values) < 4:
            continue

        recent = values[-4:]
        if any(v <= 0 for v in recent):
            continue

        change = (recent[-1] / recent[0] - 1) * 100
        rising = recent[-1] > recent[-2] > recent[-3]
        falling = recent[-1] < recent[-2] < recent[-3]
        if abs(change) < MOMENTUM_LIMIT or not (rising or falling):
            continue

        one_v, rvol = volume_metrics(symbol)

        direction = "⚡ زخم صاعد" if rising else "🔻 زخم هابط"
        one_v_text = f"{one_v:,.0f}" if one_v is not None else "غير متاح"
        rvol_text = f"{rvol:.2f}x" if rvol is not None else "غير متاح"
        msg = (
            f"{direction} | {symbol}\n"
            f"السعر: {money(current_price)}\n"
            f"حركة آخر 15-20 دقيقة تقريباً: {change:+.2f}%\n"
            f"1V (حجم آخر دقيقة مكتملة): {one_v_text}\n"
            f"RVOL اليومي: {rvol_text}\n"
            f"🕒 {time_text()}"
        )
        if send(msg):
            remember(key)
            count += 1
        time.sleep(0.3)

    return count


# =============== COMPANY NEWS ===============
def process_news(symbols):
    count = 0
    today = now().date()
    trading_day = is_trading_day(today)
    start = today - timedelta(days=1 if trading_day else 3)
    cutoff = now() - timedelta(hours=NEWS_MAX_AGE_HOURS)

    for symbol in symbols[:MAX_NEWS]:
        p = price(symbol)
        if not eligible(p):
            continue

        articles = finnhub("company-news", {
            "symbol": symbol,
            "from": start.isoformat(),
            "to": today.isoformat()
        })
        if not isinstance(articles, list):
            continue

        for a in articles[:5]:
            headline = (a.get("headline") or "").strip()
            summary = (a.get("summary") or "").strip()
            link = (a.get("url") or "").strip()
            if not headline or not link:
                continue

            published = parse_finnhub_time(a.get("datetime"))
            if published is None or published.date() < start:
                continue

            if trading_day and published < cutoff:
                continue

            raw = headline + ("\n\n" + summary if summary else "")
            tone = news_tone(raw)

            if not trading_day and tone is None:
                continue

            key = alert_id(f"NEWS|{symbol}|{link}")
            if sent(key):
                continue

            translated = translate(raw)
            session_label = news_session_label(published)

            if tone == "neg":
                importance = "⚠️ خبر سلبي محتمل"
            elif tone == "pos":
                importance = "⭐ خبر مهم"
            else:
                importance = "📰 خبر سهم"
            if not trading_day:
                importance += " (خلال العطلة)"

            msg = (
                f"{importance} | {symbol}\n"
                f"الفترة: {session_label}\n"
                f"السعر: {money(p)}\n\n"
                f"{translated}\n\n"
                f"🕒 {published.strftime('%Y-%m-%d %I:%M %p')} بتوقيت نيويورك"
            )
            if send(msg):
                remember(key)
                count += 1

        time.sleep(0.4)

    return count


# =============== SEC FILINGS ===============
SEC_8K_ITEMS = {
    "1.01": "دخول اتفاقية جوهرية",
    "1.02": "إنهاء اتفاقية جوهرية",
    "1.03": "إفلاس أو حراسة قضائية ⚠️",
    "2.01": "إتمام استحواذ أو بيع أصول",
    "2.02": "نتائج مالية (أرباح/خسائر) 📊",
    "2.03": "التزام مالي جديد",
    "2.05": "تكاليف خروج/إعادة هيكلة",
    "2.06": "انخفاض قيمة أصول ⚠️",
    "3.01": "إشعار بعدم الامتثال لشروط الإدراج ⚠️",
    "3.02": "بيع أسهم غير مسجل (تخفيف ملكية) ⚠️",
    "3.03": "تغيير في حقوق المساهمين",
    "4.01": "تغيير المدقق المالي",
    "5.01": "تغيير في السيطرة",
    "5.02": "تغيير في الإدارة (CEO/CFO)",
    "7.01": "إفصاح منظم (Reg FD)",
    "8.01": "أحداث أخرى مهمة",
    "9.01": "بيانات مالية ومرفقات",
}


def format_8k_items(items_str):
    """يحوّل '2.02,9.01' إلى قائمة أوصاف مقروءة."""
    if not items_str:
        return ""
    lines = []
    seen = set()
    for code in re.findall(r"\d+\.\d+", str(items_str)):
        if code in seen:
            continue
        seen.add(code)
        desc = SEC_8K_ITEMS.get(code, "بند غير مصنّف")
        lines.append(f"• {code}: {desc}")
    return "\n".join(lines)


def filing_label(form):
    if form in {"S-1", "F-1", "S-3", "F-3", "424B5"}:
        return "⚠️ تسجيل طرح أو إصدار أسهم؛ يجب فحص احتمال تخفيف الملكية"
    if form in {"SC 13D", "SC 13G"}:
        return "👥 إفصاح عن ملكية مساهم كبير أو تغير في حصته"
    if form == "144":
        return "⚠️ إشعار نية بيع أسهم"
    if form == "8-K":
        return "📌 تقرير حدث جوهري أو تطور مهم في الشركة"
    if form == "6-K":
        return "🌐 تقرير إفصاح دوري/حدث جوهري لشركة أجنبية مدرجة"
    if form == "10-Q":
        return "📊 تقرير مالي ربع سنوي"
    if form == "10-K":
        return "📚 تقرير مالي سنوي"
    if form == "20-F":
        return "🌐 تقرير سنوي لشركة أجنبية مدرجة"
    return "📌 إفصاح رسمي لدى هيئة الأوراق المالية الأمريكية"


def process_sec(symbols):
    count = 0
    today = now().date()
    start = today - timedelta(days=2)

    for symbol in symbols[:MAX_SEC]:
        p = price(symbol)
        if p is None or not (MIN_SEC_PRICE <= p < MAX_PRICE):
            continue

        filings = finnhub("stock/filings", {
            "symbol": symbol,
            "from": start.isoformat(),
            "to": today.isoformat()
        })
        if not isinstance(filings, list):
            continue

        for filing in filings:
            form = str(filing.get("form", "")).upper().strip()
            if form not in SEC_FORMS:
                continue

            filed = filing.get("filedDate", "")
            link = filing.get("filingUrl") or filing.get("reportUrl") or ""
            if not link:
                continue

            key = alert_id(f"SEC|{symbol}|{form}|{filed}|{link}")
            if sent(key):
                continue

            items_detail = ""
            if form == "8-K":
                items_raw = (
                    filing.get("items")
                    or filing.get("itemsDesc")
                    or ""
                )
                detail = format_8k_items(items_raw)
                if detail:
                    items_detail = f"\nالبنود:\n{detail}\n"

            filed_short = str(filed)[:10] if filed else "غير متاح"

            msg = (
                f"📄 تنبيه إفصاح رسمي | {symbol}\n"
                f"السعر: {money(p)}\n\n"
                f"نوع النموذج: {form}\n"
                f"التصنيف: {filing_label(form)}\n"
                f"تاريخ الإيداع: {filed_short}\n"
                f"{items_detail}\n"
                "المصدر: SEC\n"
                "ملاحظة: التصنيف يشرح نوع الإفصاح ولا يمثل توصية تداول."
            )

            if send(msg):
                remember(key)
                count += 1

        time.sleep(0.3)

    return count


# =============== HALT ONLY ===============
def halt_symbol(item, text):
    """يقرأ الرمز من حقول الـ RSS أولاً، ثم أنماط نصية صارمة كاحتياط."""
    for field in ("ndaq_issuesymbol", "issuesymbol", "symbol"):
        val = item.get(field)
        if val and re.fullmatch(r"[A-Za-z][A-Za-z0-9.$-]{0,9}", str(val).strip()):
            return str(val).strip().upper()

    patterns = [
        r"SECURITY\s+([A-Z][A-Z0-9.$-]{0,9})\s+HALTED",
        r"(?:SYMBOL|TICKER|ISSUE SYMBOL)\s*[:\-]\s*([A-Z][A-Z0-9.$-]{0,9})",
        r"\b([A-Z]{1,5})\s+HALTED\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            return m.group(1).upper()
    return None


HALT_REASONS = {
    "T1": "بانتظار أخبار",
    "T2": "تم نشر الأخبار",
    "T5": "توقف بسبب تذبذب حاد (LULD)",
    "T6": "نشاط غير عادي في السهم",
    "T8": "صندوق متداول",
    "T12": "بانتظار معلومات إضافية من ناسداك",
    "H4": "عدم الامتثال لشروط الإدراج",
    "H9": "عدم تقديم التقارير في وقتها",
    "H10": "إيقاف من SEC",
    "H11": "إيقاف لأسباب تنظيمية",
    "LUDP": "توقف مؤقت بسبب التذبذب (LULD)",
    "M": "توقف بسبب التذبذب (Market-wide)",
    "R4": "مؤهل للاستئناف",
}


def process_halts():
    try:
        r = session.get(HALT_URL, timeout=25)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:
        logging.error(f"HALT feed: {type(e).__name__}")
        return 0

    count = 0
    cutoff = now() - timedelta(hours=HALT_MAX_AGE_HOURS)

    for item in feed.entries:
        published = item.get("published_parsed")
        if published:
            try:
                pub_dt = datetime(
                    *published[:6], tzinfo=ZoneInfo("GMT")
                ).astimezone(TZ)
                if pub_dt < cutoff:
                    continue
            except (TypeError, ValueError):
                pass

        title = item.get("title", "")
        text = f"{title} {item.get('summary', '')} {item.get('description', '')}"
        upper = text.upper()

        if re.search(r"\b(RESUMED|RESUME|RESUMPTION|TRADING RESUMES)\b", upper):
            continue
        if str(item.get("ndaq_resumptiontradetime", "")).strip():
            continue
        if str(item.get("ndaq_resumptiondate", "")).strip():
            continue

        if not re.search(r"\bHALTED\b|\bHALT\b", upper):
            continue

        symbol = halt_symbol(item, text)
        if not symbol:
            continue

        link = item.get("link", "")
        key = alert_id(f"HALT|{symbol}|{link}|{title}")
        if sent(key):
            continue

        p = price(symbol, "halt")
        if not eligible(p):
            continue

        code = str(item.get("ndaq_reasoncode", "")).strip().upper()
        reason = HALT_REASONS.get(code, "")
        reason_line = ""
        if code:
            reason_line = f"السبب: {code}" + (f" - {reason}" if reason else "") + "\n"

        msg = (
            f"🔴 إيقاف تداول | HALT | {symbol}\n"
            f"السعر: {money(p)}\n"
            f"{reason_line}"
            f"🕒 وقت رصد التنبيه: {time_text()}\n"
            "المصدر: Nasdaq Trader"
        )

        if send(msg):
            remember(key)
            count += 1

    logging.info(
        f"HALT scan completed | Alerts: {count} | "
        f"Checks: {HALT_PRICE_CHECKS}/{MAX_HALT_PRICE_CHECKS}"
    )
    return count


# =============== WHALES ===============
def process_whales():
    logging.info("WHALE disabled: verified trade/order-book feed unavailable")
    return 0


# =============== LOCK ===============
def acquire_lock():
    """يمنع تشغيل نسختين متزامنتين. يُرجع handle يجب إبقاؤه مفتوحاً."""
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
    """ينفذ دورة كاملة واحدة ثم يعود."""
    global PRICE_CACHE, PRICE_CHECKS, HALT_PRICE_CHECKS

    PRICE_CACHE = {}
    PRICE_CHECKS = 0
    HALT_PRICE_CHECKS = 0

    logging.info("=" * 50)
    logging.info("Foras Market US: new cycle")

    if not TOKEN or not CHAT_ID or not API_KEY:
        logging.error("Missing TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID or FINNHUB_API_KEY")
        return
    if not MASSIVE_KEY:
        logging.warning("MASSIVE_API_KEY missing: 1V/RVOL will show unavailable")

    halt_count = process_halts()

    movers = market_movers()

    moves = process_moves(movers)
    momentum = process_momentum(movers)

    symbols = [x["symbol"] for x in movers if eligible(x["price"])]

    news = process_news(symbols)
    sec = process_sec(symbols)

    logging.info(
        f"Cycle done | HALT:{halt_count} | PRICE:{moves} | MOMENTUM:{momentum} | "
        f"NEWS:{news} | SEC:{sec} | STOCKS:{len(movers)} | "
        f"PRICE CHECKS:{PRICE_CHECKS}/{MAX_PRICE_CHECKS} | "
        f"HALT CHECKS:{HALT_PRICE_CHECKS}/{MAX_HALT_PRICE_CHECKS}"
    )


def run_forever():
    """حلقة تشغيل مستمرة مع فاصل زمني بين الدورات."""
    logging.info(
        f"Foras Market US started | "
        f"interval={INTERVAL_SECONDS}s ({INTERVAL_SECONDS // 60} min)"
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
    """نقطة الدخول: قفل + إعداد logging + حلقة تشغيل."""
    # إعداد ملف اللوج (يُضاف إلى stdout)
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
        # حفظ أخير عند الخروج
        if _dirty:
            save_state()
        logging.info("Shutdown complete")
        try:
            lock_handle.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
