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

# عدادات مستقلة
MAX_PRICE_CHECKS = 45
MAX_HALT_PRICE_CHECKS = 20

PRICE_LIMIT = 5.0
MOMENTUM_LIMIT = 2.5

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

# قائمة أعلى الرابحين مستقلة عن قائمة الأسهم تحت 5 دولارات
TOP_GAINERS_DATA = []


# =============== TIME ===============
def now():
    return datetime.now(TZ)


def time_text():
    return now().strftime("%Y-%m-%d %I:%M:%S %p ET")


def market_open():
    n = now()
    return n.weekday() < 5 and (
        datetime.strptime("09:30", "%H:%M").time()
        <= n.time()
        < datetime.strptime("16:00", "%H:%M").time()
    )


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
def finnhub(endpoint, params=None):
    if not API_KEY:
        return None

    try:
        p = dict(params or {})
        p["token"] = API_KEY

        r = session.get(
            f"{BASE}/{endpoint}",
            params=p,
            timeout=20
        )
        r.raise_for_status()
        return r.json()

    except Exception as e:
        logging.warning(f"Finnhub {endpoint}: {e}")
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


def is_common_equity(row):
    """استبعاد ETF/ETN والوحدات والضمانات والأسهم الممتازة والسندات."""
    name = str(row.get("name") or row.get("securityName") or "").lower()
    exclusions = (
        " etf", " etn", " exchange traded fund", " exchange-traded fund",
        " warrant", " warrants", " unit", " units", " preferred",
        " depositary share", " depositary shares", " bond", " notes",
        " right", " rights", " debenture"
    )
    return not any(term in name for term in exclusions)


def normalize_nasdaq_row(row, requested_exchange):
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

    name = row.get("name") or row.get("securityName") or ""

    if not is_common_equity(row):
        return None

    return {
        "symbol": symbol,
        "price": last,
        "change": pct,
        "exchange": requested_exchange,
        "volume": int(parse_number(row.get("volume")) or 0),
        "name": str(name)
    }


def fetch_nasdaq_exchange(exchange):
    """
    واجهة Nasdaq العامة المستخدمة في صفحة الفاحص.
    هذه الواجهة عامة لكنها غير موثقة كواجهة مستقرة، لذلك يوجد تحقق دفاعي.
    """
    rows = []
    offset = 0
    limit = 5000

    try:
        # واجهة Nasdaq العامة تستخدم غالباً data.table.rows،
        # وقد تختلف الاستجابة بين tableonly=true/false؛ لذلك ندعم الشكلين.
        exchange_param = exchange.lower()

        r = session.get(
            NASDAQ_API,
            params={
                "tableonly": "false",
                "limit": limit,
                "offset": offset,
                "exchange": exchange_param,
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

        data = payload.get("data") or {}
        table = data.get("table") or {}
        page_rows = (
            table.get("rows")
            or data.get("rows")
            or payload.get("rows")
            or []
        )

        if not isinstance(page_rows, list):
            logging.warning(
                f"Nasdaq {exchange}: unexpected rows format; "
                f"data keys={list(data.keys())}"
            )
            return []

        rows.extend(page_rows)

        total = parse_number(
            data.get("totalrecords")
            or table.get("totalrecords")
        )
        if total and total > limit:
            logging.warning(
                f"Nasdaq {exchange}: returned {len(page_rows)} of "
                f"{int(total)} rows; only first page is loaded"
            )

        if not page_rows:
            logging.warning(
                f"Nasdaq {exchange}: zero rows. "
                f"HTTP={r.status_code}; top_keys={list(payload.keys())}; "
                f"data_keys={list(data.keys())}; "
                f"table_keys={list(table.keys()) if isinstance(table, dict) else 'N/A'}"
            )

        logging.info(
            f"NASDAQ SCREENER {exchange}: received {len(page_rows)} rows"
        )

    except Exception as e:
        logging.warning(f"Nasdaq screener {exchange}: {e}")

    return rows


# =============== MARKET MOVERS ===============
def market_movers():
    global TOP_GAINERS_DATA

    all_rows = {}
    stats = {
        "received": 0,
        "valid_equity": 0,
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

    # أعلى 10 رابحين لا يخضعون لشرط 5 دولارات.
    # الأسعار ونسب التغير مأخوذة مباشرة من بيانات فاحص Nasdaq.
    gainers = [
        dict(x) for x in all_rows.values()
        if x["change"] > 0 and x["price"] > 0
    ]
    gainers.sort(key=lambda x: x["change"], reverse=True)
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


# =============== TOP GAINERS ===============
def process_gainers():
    # لا يوجد أي شرط MAX_PRICE في هذا القسم.
    top = TOP_GAINERS_DATA[:TOP_COUNT]

    if not top:
        logging.warning("TOP GAINERS: no Nasdaq gainers returned")
        return 0

    lines = [
        f"{i}. 🟢 {x['symbol']} {money(x['price'])} "
        f"(+{x['change']:.2f}%)"
        for i, x in enumerate(top, 1)
    ]

    key = alert_id(
        "GAINERS|" + now().strftime("%Y-%m-%d-%H")
    )

    if sent(key):
        return 0

    msg = (
        "🏆 أعلى 10 أسهم ارتفاعاً\n\n"
        + "\n".join(lines)
        + f"\n\n🕒 {time_text()}\n"
        "المصدر: Nasdaq Screener\n"
        "تنبيه: هذا القسم لا يطبق حد السعر الأقل من 5 دولارات."
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
    # Yahoo chart endpoint منفصل عن Yahoo screener الذي أعاد 401.
    try:
        r = session.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={
                "range": "1d",
                "interval": "5m"
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
    if not market_open():
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
    if not market_open():
        logging.info("HALT scan skipped: market closed")
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

    logging.info("Foras Market US started")

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
    main(
