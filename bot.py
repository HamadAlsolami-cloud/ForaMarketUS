import os
import re
import json
import time
import hashlib
import logging
import requests
import feedparser
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# =============== SETTINGS ===============
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("FINNHUB_API_KEY")

STATE_FILE = "alert_state.json"
BASE = "https://finnhub.io/api/v1"
NASDAQ_API = "https://api.nasdaq.com/api/screener/stocks"
HALT_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"

# شرط السعر أقل من 5 دولارات للأقسام العادية فقط
MAX_PRICE = 5.00
TOP_COUNT = 10
MAX_SCAN = 20
MAX_NEWS = 10
MAX_SEC = 10

# فلاتر قائمة أعلى الرابحين (تنظيف الحركات الوهمية)
MIN_GAINER_PRICE = 0.50
MIN_GAINER_VOLUME = 100_000

# عدادات مستقلة
MAX_PRICE_CHECKS = 45
MAX_HALT_PRICE_CHECKS = 20

# حد Finnhub المجاني 60 طلب/دقيقة؛ نترك هامش أمان
FINNHUB_PER_MIN = 50

PRICE_LIMIT = 5.0
MOMENTUM_LIMIT = 2.5

# أقصى عدد صفحات يتم جلبها من فاحص Nasdaq لكل بورصة
MAX_SCREENER_PAGES = 10

# NASDAQ / NYSE / NYSE American فقط. لا يتم طلب OTC.
NASDAQ_EXCHANGES = ("NASDAQ", "NYSE", "AMEX")

SEC_FORMS = {
    "8-K", "6-K", "S-1", "F-1", "S-3", "F-3",
    "424B5", "SC 13D", "SC 13G", "3", "4", "5", "144"
}

TZ = ZoneInfo("America/New_York")

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

session = requests.Session()
session.headers.update(HEADERS)

PRICE_CACHE = {}
PRICE_CHECKS = 0
HALT_PRICE_CHECKS = 0
FINNHUB_CALLS = deque()

# قائمة أعلى الرابحين مستقلة عن قائمة الأسهم تحت 5 دولارات
TOP_GAINERS_DATA = []


# =============== TIME ===============
def now():
    return datetime.now(TZ)


def time_text():
    return now().strftime("%Y-%m-%d %I:%M:%S %p ET")


def _t(s):
    return datetime.strptime(s, "%H:%M").time()


def session_name():
    """pre / regular / after / None (مغلق أو عطلة نهاية أسبوع)."""
    n = now()
    if n.weekday() >= 5:
        return None

    t = n.time()
    if _t("04:00") <= t < _t("09:30"):
        return "pre"
    if _t("09:30") <= t < _t("16:00"):
        return "regular"
    if _t("16:00") <= t < _t("20:00"):
        return "after"
    return None


SESSION_TITLES = {
    "pre": "🌅 رابحو ما قبل الافتتاح",
    "regular": "🏆 أكثر 10 أسهم ارتفاعاً",
    "after": "🌙 رابحو ما بعد الإغلاق",
}


def reason_window_start():
    """بداية نافذة البحث عن الأخبار: إغلاق آخر يوم تداول قبل يوم التداول الحالي."""
    d = now().date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return datetime.combine(d, _t("16:00"), tzinfo=TZ)


# =============== STATE ===============
try:
    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    if not isinstance(state, dict):
        state = {"sent": []}
except Exception:
    state = {"sent": []}

state.setdefault("sent", [])


def save_state():
    state["sent"] = state["sent"][-5000:]
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        logging.error(f"State: {e}")


def alert_id(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def sent(key):
    return key in state["sent"]


def remember(key):
    state["sent"].append(key)
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
        for attempt in range(5):
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
                logging.warning(f"Telegram: {e}")
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
        try:
            r = session.get(
                "https://translate.googleapis.com/translate_a/single",
                params={
                    "client": "gtx",
                    "sl": "auto",
                    "tl": "ar",
                    "dt": "t",
                    "q": text[i:i + 2500]
                },
                timeout=20
            )
            r.raise_for_status()
            output.append("".join(
                p[0] for p in r.json()[0] if p and p[0]
            ))
        except Exception:
            output.append(text[i:i + 2500])

    return "\n".join(output)


# =============== FINNHUB ===============
def finnhub_throttle():
    """منع تجاوز حد الطلبات: لا أكثر من FINNHUB_PER_MIN طلب كل 60 ثانية."""
    while True:
        t = time.time()
        while FINNHUB_CALLS and t - FINNHUB_CALLS[0] > 60:
            FINNHUB_CALLS.popleft()

        if len(FINNHUB_CALLS) < FINNHUB_PER_MIN:
            FINNHUB_CALLS.append(t)
            return

        time.sleep(max(0.5, 60 - (t - FINNHUB_CALLS[0]) + 0.2))


def finnhub(endpoint, params=None):
    if not API_KEY:
        return None

    p = dict(params or {})
    p["token"] = API_KEY

    for attempt in range(2):
        try:
            finnhub_throttle()
            r = session.get(
                f"{BASE}/{endpoint}",
                params=p,
                timeout=20
            )

            if r.status_code == 429 and attempt == 0:
                logging.warning(f"Finnhub {endpoint}: rate limited, waiting")
                time.sleep(12)
                continue

            r.raise_for_status()
            return r.json()

        except Exception as e:
            logging.warning(f"Finnhub {endpoint}: {e}")
            return None

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
    return f"${value:.4f}" if value else "غير متاح"


def short_money(value):
    if not value:
        return "غير متاح"
    return f"${value:.2f}" if value >= 1 else f"${value:.4f}"


def fmt_volume(v):
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}K"
    return str(v)


# =============== NASDAQ SCREENER HELPERS ===============
def parse_number(value):
    """تحويل قيم Nasdaq النصية مثل $1.25 و +12.5% و 1,200 إلى أرقام."""
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    s = str(value).strip()
    if not s or s in {"N/A", "--", "NA"}:
        return None

    s = s.replace(",", "").replace("$", "").replace("%", "")
    s = s.replace("+", "").strip()

    # الأقواس تعني قيمة سالبة في بعض صيغ البيانات
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]

    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# استبعاد الأدوات غير العادية بحسب الاسم (كلمات كاملة فقط)
NAME_EXCLUDE = re.compile(
    r"\b(etf|etn|warrants?|depositary|debentures?|senior notes?|"
    r"exchange[- ]traded)\b"
    r"|\bpreferred (stock|shares|securities)\b"
    r"|\bunits?,? each\b|\bunits?$|\brights?$",
    re.I
)

# لواحق الرموز: warrant / unit / right
SYMBOL_EXCLUDE = re.compile(r"[.\-](WS|WT|W|U|UN|RT|R)$")


def is_common_equity(symbol, name):
    """فلتر أدق: يعتمد على لاحقة الرمز وكلمات الاسم الكاملة."""
    if "$" in symbol or "^" in symbol:
        return False

    # رموز Nasdaq الخماسية: W = warrant, U = unit, R = right
    if len(symbol) == 5 and symbol[-1] in {"W", "U", "R"}:
        return False

    if SYMBOL_EXCLUDE.search(symbol):
        return False

    if NAME_EXCLUDE.search(str(name or "")):
        return False

    return True


def normalize_nasdaq_row(row, requested_exchange):
    if not isinstance(row, dict):
        return None

    symbol = str(row.get("symbol") or "").strip().upper()
    if not symbol or len(symbol) > 15:
        return None

    # الرمز يجب أن يكون رمز سهم وليس نصاً وصفياً
    if not re.fullmatch(r"[A-Z][A-Z0-9.$-]{0,14}", symbol):
        return None

    last = parse_number(
        row.get("lastsale")
        or row.get("lastSale")
        or row.get("last")
    )
    pct = parse_number(
        row.get("pctchange")
        or row.get("percentageChange")
        or row.get("percentChange")
    )

    if last is None or last <= 0 or pct is None:
        return None

    name = str(row.get("name") or row.get("securityName") or "")

    if not is_common_equity(symbol, name):
        return None

    return {
        "symbol": symbol,
        "price": last,
        "change": pct,
        "exchange": requested_exchange,
        "volume": int(parse_number(row.get("volume")) or 0),
        "name": name
    }


def fetch_nasdaq_exchange(exchange):
    """
    جلب كل صفحات الفاحص لبورصة واحدة عبر offset.
    الواجهة غير موثقة؛ لذلك يوجد كشف لتجاهل offset وحد أقصى للصفحات.
    """
    rows = []
    offset = 0
    limit = 5000
    last_first = None

    for page in range(MAX_SCREENER_PAGES):
        try:
            r = session.get(
                NASDAQ_API,
                params={
                    "tableonly": "false",
                    "limit": limit,
                    "offset": offset,
                    "exchange": exchange.lower(),
                    "download": "true"
                },
                headers={
                    "User-Agent": HEADERS["User-Agent"],
                    "Accept": "application/json, text/plain, */*",
                    "Origin": "https://www.nasdaq.com",
                    "Referer": "https://www.nasdaq.com/market-activity/stocks/screener"
                },
                timeout=35
            )
            r.raise_for_status()
            payload = r.json()
        except Exception as e:
            logging.warning(f"Nasdaq screener {exchange} page {page}: {e}")
            break

        data = payload.get("data") or {}
        table = data.get("table") or {}
        page_rows = (
            table.get("rows")
            or data.get("rows")
            or payload.get("rows")
            or []
        )

        if not isinstance(page_rows, list) or not page_rows:
            if page == 0:
                logging.warning(
                    f"Nasdaq {exchange}: zero rows. "
                    f"top_keys={list(payload.keys())}; "
                    f"data_keys={list(data.keys())}"
                )
            break

        # لو الواجهة تتجاهل offset تعود نفس الصفحة: نتوقف
        first = page_rows[0].get("symbol") if isinstance(page_rows[0], dict) else None
        if first is not None and first == last_first:
            break
        last_first = first

        rows.extend(page_rows)
        offset += len(page_rows)

        total = parse_number(
            data.get("totalrecords")
            or table.get("totalrecords")
        )

        if len(page_rows) < limit or (total and offset >= total):
            break

        time.sleep(0.5)

    logging.info(f"NASDAQ SCREENER {exchange}: received {len(rows)} rows")
    return rows


# =============== MARKET MOVERS ===============
def market_movers():
    global TOP_GAINERS_DATA

    all_rows = {}
    stats = {
        "received": 0,
        "valid_equity": 0,
        "gainers_after_filters": 0,
        "under_5": 0,
        "verified_under_5": 0
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

    # أعلى الرابحين: لا يخضعون لشرط 5 دولارات، لكن بفلتر سعر وحجم أدنى.
    gainers = [
        dict(x) for x in all_rows.values()
        if x["change"] > 0
        and x["price"] >= MIN_GAINER_PRICE
        and x["volume"] >= MIN_GAINER_VOLUME
    ]
    gainers.sort(key=lambda x: x["change"], reverse=True)
    stats["gainers_after_filters"] = len(gainers)
    TOP_GAINERS_DATA = gainers[:TOP_COUNT]

    # بقية التنبيهات: أقل من 5 دولارات فقط، ثم تحقق إضافي من Finnhub.
    under_5 = [
        x for x in all_rows.values()
        if eligible(x["price"])
    ]
    stats["under_5"] = len(under_5)

    # نرتب حسب أكبر حركة مطلقة حتى نستهلك حصة Finnhub على المرشحين الأهم.
    under_5.sort(key=lambda x: abs(x["change"]), reverse=True)

    verified = []
    for item in under_5:
        if PRICE_CHECKS >= MAX_PRICE_CHECKS:
            break

        live_price = price(item["symbol"], "general")
        if eligible(live_price):
            item["price"] = live_price
            verified.append(item)

    stats["verified_under_5"] = len(verified)

    logging.info(f"NASDAQ FILTER STATS: {stats}")
    logging.info(
        "NASDAQ TOP GAINERS SAMPLE: "
        + str([
            {
                "symbol": x["symbol"],
                "price": x["price"],
                "change": x["change"],
                "volume": x["volume"],
                "exchange": x["exchange"]
            }
            for x in TOP_GAINERS_DATA[:10]
        ])
    )
    logging.info(
        f"Finnhub verified under $5: {len(verified)} "
        f"| Checks: {PRICE_CHECKS}/{MAX_PRICE_CHECKS}"
    )

    return sorted(
        verified,
        key=lambda x: x["change"],
        reverse=True
    )


# =============== TOP GAINERS REASONS ===============
def parse_finnhub_time(value):
    try:
        return datetime.fromtimestamp(int(value), ZoneInfo("UTC")).astimezone(TZ)
    except (TypeError, ValueError, OSError):
        return None


def gainer_reason(symbol):
    """
    أحدث خبر (أو إيداع SEC إن لم يوجد خبر) منذ إغلاق آخر يوم تداول.
    طلب واحد للأخبار، وطلب الإيداعات فقط عند عدم وجود خبر.
    لا نخمن سبب الحركة: هذا مجرد مصدر متزامن.
    """
    start = reason_window_start()
    today = now().date()
    params = {
        "symbol": symbol,
        "from": start.date().isoformat(),
        "to": today.isoformat()
    }

    news_items = finnhub("company-news", params)

    best = None
    if isinstance(news_items, list):
        for article in news_items:
            headline = (article.get("headline") or "").strip()
            link = (article.get("url") or "").strip()
            published = parse_finnhub_time(article.get("datetime"))
            if not headline or not link or not published:
                continue
            if published < start:
                continue
            if best is None or published > best[0]:
                best = (published, headline, link)

    if best:
        return {"kind": "news", "title": best[1], "link": best[2]}

    filings = finnhub("stock/filings", params)

    best = None
    if isinstance(filings, list):
        for filing in filings:
            form = str(filing.get("form", "")).upper().strip()
            if form not in SEC_FORMS:
                continue

            filed_text = str(filing.get("filedDate") or "").strip()
            try:
                filed_date = datetime.strptime(filed_text[:10], "%Y-%m-%d").date()
            except ValueError:
                continue

            if filed_date < start.date():
                continue

            link = (filing.get("filingUrl") or filing.get("reportUrl") or "").strip()
            if not link:
                continue

            if best is None or filed_date > best[0]:
                best = (filed_date, form, link)

    if best:
        return {
            "kind": "sec",
            "title": f"إيداع SEC: نموذج {best[1]}",
            "link": best[2]
        }

    return None


def format_gainer(index, item):
    symbol = item["symbol"]
    line = (
        f"{index}. 🔹 {symbol} {short_money(item['price'])} "
        f"(+{item['change']:.2f}%) | حجم {fmt_volume(item['volume'])}"
    )

    reason = gainer_reason(symbol)

    if not reason:
        return line + "\n   ⚪ لا يوجد خبر أو إيداع موثق في هذه الفترة"

    if reason["kind"] == "news":
        title = translate(reason["title"]).strip()
        if len(title) > 160:
            title = title[:157] + "..."
        return f"{line}\n   📰 {title}\n   🔗 {reason['link']}"

    return f"{line}\n   📄 {reason['title']}\n   🔗 {reason['link']}"


# =============== TOP GAINERS ===============
def process_gainers():
    sess = session_name()
    if not sess:
        logging.info("GAINERS skipped: outside extended hours")
        return 0

    # هذا القسم وحده لا يطبق شرط MAX_PRICE.
    top = TOP_GAINERS_DATA[:TOP_COUNT]

    if not top:
        logging.warning("TOP GAINERS: no Nasdaq gainers returned")
        return 0

    # رسالة واحدة في كل فترة ربع ساعة (00، 15، 30، 45).
    current_time = now()
    quarter_hour = (current_time.minute // 15) * 15
    bucket = current_time.replace(minute=quarter_hour, second=0, microsecond=0)
    key = alert_id("GAINERS|" + bucket.strftime("%Y-%m-%d-%H-%M"))
    if sent(key):
        return 0

    lines = []
    for i, item in enumerate(top, 1):
        lines.append(format_gainer(i, item))

    msg = (
        SESSION_TITLES[sess] + "\n\n"
        + "\n\n".join(lines)
        + f"\n\n🕒 {time_text()}\n"
        f"المصدر: Nasdaq Screener / Finnhub / SEC\n"
        f"الفلاتر: سعر ≥ ${MIN_GAINER_PRICE:.2f} وحجم ≥ {MIN_GAINER_VOLUME:,}\n"
        "ملاحظة: الخبر أو الإيداع متزامن وموثق، ولا يعني بالضرورة أنه السبب المؤكد "
        "للارتفاع. هذا القسم لا يطبق حد السعر الأقل من 5 دولارات."
    )

    if send(msg):
        remember(key)
        return 1

    return 0


# =============== PRICE MOVES ===============
def process_moves(movers):
    count = 0
    today = now().strftime("%Y-%m-%d")

    for x in movers[:MAX_SCAN]:
        change = x["change"]

        if abs(change) < PRICE_LIMIT:
            continue

        symbol = x["symbol"]
        # تحقق السعر عند التنبيه لضمان بقاء السهم تحت 5 دولارات.
        current_price = price(symbol)

        if not eligible(current_price):
            continue

        key = alert_id(f"PRICE|{symbol}|{today}")
        if sent(key):
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


# =============== MOMENTUM ===============
def candles(symbol):
    # includePrePost=true لتشمل الشموع ما قبل الافتتاح وما بعد الإغلاق.
    try:
        r = session.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={
                "range": "1d",
                "interval": "5m",
                "includePrePost": "true"
            },
            timeout=20
        )
        r.raise_for_status()

        data = r.json()["chart"]["result"]
        if not data:
            return []

        values = data[0]["indicators"]["quote"][0]["close"]
        return [float(v) for v in values if v is not None]

    except Exception as e:
        logging.warning(f"Candles {symbol}: {e}")
        return []


def process_momentum(movers):
    if not session_name():
        return 0

    count = 0
    today = now().strftime("%Y-%m-%d")

    for x in movers[:MAX_SCAN]:
        symbol = x["symbol"]

        # إعادة تحقق السعر قبل إرسال التنبيه.
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

        key = alert_id(f"MOMENTUM|{symbol}|{today}")
        if sent(key):
            continue

        direction = "⚡ زخم صاعد" if rising else "🔻 زخم هابط"
        msg = (
            f"{direction}\n"
            f"{symbol} {money(current_price)}\n"
            f"حركة آخر 15 دقيقة تقريباً: {change:+.2f}%\n"
            f"🕒 {time_text()}\n"
            "المصدر: Yahoo Finance / Finnhub"
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
    start = today - timedelta(days=1)

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

        for a in articles[:3]:
            headline = (a.get("headline") or "").strip()
            summary = (a.get("summary") or "").strip()
            link = a.get("url", "")

            if not headline or not link:
                continue

            key = alert_id(f"NEWS|{symbol}|{link}")
            if sent(key):
                continue

            translated = translate(
                headline + ("\n\n" + summary if summary else "")
            )

            try:
                article_time = datetime.fromtimestamp(
                    int(a["datetime"]), ZoneInfo("UTC")
                ).astimezone(TZ).strftime("%Y-%m-%d %I:%M %p ET")
            except Exception:
                article_time = "غير متاح"

            msg = (
                f"📰 {symbol} {money(p)}\n\n"
                f"{translated}\n\n"
                f"🔗 {link}\n"
                f"🕒 {article_time}\n"
                f"المصدر: {a.get('source', 'Finnhub')}"
            )

            if send(msg):
                remember(key)
                count += 1

        time.sleep(0.5)

    return count


# =============== SEC FILINGS ===============
def filing_label(form):
    if form in {"S-1", "F-1", "S-3", "F-3", "424B5"}:
        return "⚠️ تسجيل طرح أو إصدار أسهم - افحص احتمال التخفيف"

    if form in {"SC 13D", "SC 13G"}:
        return "👥 إفصاح ملكية مساهم كبير"

    if form in {"3", "4", "5"}:
        return "👔 إفصاح ملكية أو تداول مسؤول/مطلع"

    if form == "144":
        return "⚠️ إشعار بيع أسهم محتمل"

    return "📌 إفصاح حدث جوهري"


def process_sec(symbols):
    count = 0
    today = now().date()
    start = today - timedelta(days=2)

    for symbol in symbols[:MAX_SEC]:
        p = price(symbol)
        if not eligible(p):
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

            key = alert_id(
                f"SEC|{symbol}|{form}|{filed}|{link}"
            )
            if sent(key):
                continue

            msg = (
                f"📄 SEC | {symbol} {money(p)}\n\n"
                f"النموذج: {form}\n"
                f"{filing_label(form)}\n"
                f"تاريخ الإيداع: {filed or 'غير متاح'}\n\n"
                f"🔗 {link}\n"
                "المصدر: SEC / Finnhub\n"
                "ملاحظة: التصنيف حسب نوع النموذج وليس توصية تداول."
            )

            if send(msg):
                remember(key)
                count += 1

        time.sleep(0.3)

    return count


# =============== HALT ONLY ===============
def halt_symbol(text):
    patterns = [
        r"SECURITY\s+([A-Z][A-Z0-9.$-]{0,9})\s+HALTED",
        r"(?:SYMBOL|TICKER|ISSUE SYMBOL)\s*[:\-]\s*([A-Z][A-Z0-9.$-]{0,9})",
        r"\b([A-Z]{1,5})\s+HALTED\b",
        r"\(([A-Z]{1,5})\)"
    ]

    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            return m.group(1).upper()

    return None


def process_halts():
    if not session_name():
        logging.info("HALT scan skipped: outside extended hours")
        return 0

    try:
        r = session.get(HALT_URL, timeout=25)
        r.raise_for_status()
        feed = feedparser.parse(r.content)

    except Exception as e:
        logging.error(f"HALT feed: {e}")
        return 0

    count = 0

    for item in feed.entries:
        title = item.get("title", "")
        text = (
            f"{title} "
            f"{item.get('summary', '')} "
            f"{item.get('description', '')}"
        )
        upper = text.upper()

        # حذف RESUME نهائياً والإبقاء على HALT فقط.
        if re.search(
            r"\b(RESUMED|RESUME|RESUMPTION|TRADING RESUMES)\b",
            upper
        ):
            continue

        if not re.search(r"\bHALTED\b|\bHALT\b", upper):
            continue

        symbol = halt_symbol(text)
        if not symbol:
            continue

        p = price(symbol, "halt")
        if not eligible(p):
            continue

        link = item.get("link", "")
        key = alert_id(f"HALT|{symbol}|{link}|{title}")
        if sent(key):
            continue

        msg = (
            f"🔴 HALT | {symbol} {money(p)}\n\n"
            f"إيقاف تداول\n{title}\n\n"
            f"🔗 {link}\n"
            f"🕒 {time_text()}\n"
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
    # لا يتم اعتبار الحجم المرتفع صفقة حوت.
    # لا تفعيل قبل توفير بيانات Time & Sales وLevel 2 موثوقة.
    logging.info(
        "WHALE disabled: verified trade/order-book feed unavailable"
    )
    return 0


# =============== MAIN ===============
def main():
    global PRICE_CACHE, PRICE_CHECKS, HALT_PRICE_CHECKS
    global TOP_GAINERS_DATA

    PRICE_CACHE = {}
    PRICE_CHECKS = 0
    HALT_PRICE_CHECKS = 0
    TOP_GAINERS_DATA = []

    logging.info(f"Foras Market US started | session={session_name()}")

    if not TOKEN or not CHAT_ID or not API_KEY:
        logging.error("Missing Telegram or Finnhub settings")
        return

    halt_count = process_halts()

    # movers هنا مقتصرة على الأسهم التي ثبت أن سعرها أقل من 5 دولارات.
    movers = market_movers()

    # أعلى الرابحين منفصل ولا يطبق شرط 5 دولارات.
    gainers = process_gainers()

    # هذه الأقسام تستقبل فقط قائمة الأسهم تحت 5 دولارات.
    moves = process_moves(movers)
    momentum = process_momentum(movers)

    symbols = [x["symbol"] for x in movers if eligible(x["price"])]

    news = process_news(symbols)
    sec = process_sec(symbols)
    whales = process_whales()

    save_state()

    logging.info(
        f"Done | "
        f"HALT:{halt_count} | "
        f"GAINERS:{gainers} | "
        f"PRICE:{moves} | "
        f"MOMENTUM:{momentum} | "
        f"NEWS:{news} | "
        f"SEC:{sec} | "
        f"WHALES:{whales} | "
        f"STOCKS:{len(movers)} | "
        f"TOP GAINERS:{len(TOP_GAINERS_DATA)} | "
        f"PRICE CHECKS:{PRICE_CHECKS}/{MAX_PRICE_CHECKS} | "
        f"HALT CHECKS:{HALT_PRICE_CHECKS}/{MAX_HALT_PRICE_CHECKS}"
    )


if __name__ == "__main__":
    main()
