
import os, re, json, time, hashlib, logging, requests, feedparser
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# =============== SETTINGS ===============
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("FINNHUB_API_KEY")

STATE_FILE = "alert_state.json"
BASE = "https://finnhub.io/api/v1"
YAHOO = "https://query1.finance.yahoo.com"
HALT_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"

MAX_PRICE = 5.00
TOP_COUNT = 10
MAX_SCAN = 20
MAX_NEWS = 10
MAX_SEC = 10
MAX_PRICE_CHECKS = 45

PRICE_LIMIT = 5.0
MOMENTUM_LIMIT = 2.5

EXCHANGES = {"NMS", "NGM", "NCM", "NYQ", "ASE"}

# النماذج ذات الأولوية
SEC_FORMS = {
    "8-K", "6-K", "S-1", "F-1", "S-3", "F-3",
    "424B5", "SC 13D", "SC 13G", "3", "4", "5", "144"
}

TZ = ZoneInfo("America/New_York")
HEADERS = {"User-Agent": "Mozilla/5.0 ForasMarketUS/6.0"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
session.headers.update(HEADERS)

PRICE_CACHE = {}
PRICE_CHECKS = 0

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
                r = session.post(url, data={
                    "chat_id": CHAT_ID,
                    "text": chunk,
                    "disable_web_page_preview": True
                }, timeout=25)

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
                    "client": "gtx", "sl": "auto",
                    "tl": "ar", "dt": "t",
                    "q": text[i:i + 2500]
                }, timeout=20
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
        r = session.get(f"{BASE}/{endpoint}", params=p, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.warning(f"Finnhub {endpoint}: {e}")
        return None

def price(symbol):
    global PRICE_CHECKS

    symbol = symbol.upper()
    if symbol in PRICE_CACHE:
        return PRICE_CACHE[symbol]

    if PRICE_CHECKS >= MAX_PRICE_CHECKS:
        return None

    PRICE_CHECKS += 1
    q = finnhub("quote", {"symbol": symbol})

    try:
        value = float(q["c"])
        result = value if value > 0 else None
    except (TypeError, ValueError, KeyError):
        result = None

    PRICE_CACHE[symbol] = result
    return result

def eligible(value):
    return value is not None and 0 < value < MAX_PRICE

def money(value):
    return f"${value:.4f}" if value else "غير متاح"

# =============== MARKET MOVERS ===============
def market_movers():
    candidates = {}

    for screener in ("day_gainers", "day_losers"):
        try:
            r = session.get(
                f"{YAHOO}/v1/finance/screener/predefined/saved",
                params={
                    "formatted": "false",
                    "scrIds": screener,
                    "count": 100,
                    "start": 0
                }, timeout=25
            )
            r.raise_for_status()

            quotes = r.json()["finance"]["result"][0]["quotes"]

            for x in quotes:
                symbol = str(x.get("symbol", "")).upper()
                exchange = str(x.get("exchange", "")).upper()

                if not symbol or exchange not in EXCHANGES:
                    continue
                if x.get("quoteType") != "EQUITY":
                    continue

                try:
                    p = float(x.get("regularMarketPrice", 0))
                    change = float(x.get("regularMarketChangePercent", 0))
                except (ValueError, TypeError):
                    continue

                if not eligible(p):
                    continue

                candidates[symbol] = {
                    "symbol": symbol,
                    "change": change,
                    "exchange": exchange
                }

        except Exception as e:
            logging.warning(f"Yahoo {screener}: {e}")

    # فحص الأقوى صعوداً وهبوطاً أولاً
    ordered = sorted(
        candidates.values(),
        key=lambda x: abs(x["change"]),
        reverse=True
    )

    verified = []
    for item in ordered:
        if PRICE_CHECKS >= MAX_PRICE_CHECKS:
            break

        p = price(item["symbol"])
        if eligible(p):
            item["price"] = p
            verified.append(item)

    return sorted(verified, key=lambda x: x["change"], reverse=True)

# =============== TOP GAINERS ===============
def process_gainers(movers):
    top = [x for x in movers if x["change"] > 0][:TOP_COUNT]
    if not top:
        return 0

    lines = [
        f"{i}. 🟢 {x['symbol']} {money(x['price'])} (+{x['change']:.2f}%)"
        for i, x in enumerate(top, 1)
    ]

    key = alert_id("GAINERS|" + now().strftime("%Y-%m-%d-%H"))
    if sent(key):
        return 0

    msg = (
        "🏆 أعلى الأسهم الرابحة تحت 5 دولارات\n\n"
        + "\n".join(lines)
        + f"\n\n🕒 {time_text()}\nالمصدر: Yahoo Finance / Finnhub"
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
        key = alert_id(f"PRICE|{symbol}|{today}")
        if sent(key):
            continue

        emoji = "🚀" if change > 0 else "🔻"
        msg = (
            f"{emoji} {symbol} {money(x['price'])}\n\n"
            f"التغير اليومي: {change:+.2f}%\n"
            f"🕒 {time_text()}\nالمصدر: Yahoo Finance / Finnhub"
        )

        if send(msg):
            remember(key)
            count += 1

    return count

# =============== MOMENTUM ===============
def candles(symbol):
    try:
        r = session.get(
            f"{YAHOO}/v8/finance/chart/{symbol}",
            params={"range": "1d", "interval": "5m"},
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
            f"{direction}\n{symbol} {money(x['price'])}\n"
            f"حركة آخر 15 دقيقة تقريباً: {change:+.2f}%\n"
            f"🕒 {time_text()}\nالمصدر: Yahoo Finance"
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

            translated = translate(headline + ("\n\n" + summary if summary else ""))

            try:
                article_time = datetime.fromtimestamp(
                    int(a["datetime"]), ZoneInfo("UTC")
                ).astimezone(TZ).strftime("%Y-%m-%d %I:%M %p ET")
            except Exception:
                article_time = "غير متاح"

            msg = (
                f"📰 {symbol} {money(p)}\n\n{translated}\n\n"
                f"🔗 {link}\n🕒 {article_time}\n"
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

            key = alert_id(f"SEC|{symbol}|{form}|{filed}|{link}")
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
        text = f"{title} {item.get('summary', '')} {item.get('description', '')}"
        upper = text.upper()

        if re.search(r"\b(RESUMED|RESUME|RESUMPTION|TRADING RESUMES)\b", upper):
            continue
        if not re.search(r"\bHALTED\b|\bHALT\b", upper):
            continue

        symbol = halt_symbol(text)
        if not symbol:
            continue

        p = price(symbol)
        if not eligible(p):
            continue

        link = item.get("link", "")
        key = alert_id(f"HALT|{symbol}|{link}|{title}")
        if sent(key):
            continue

        msg = (
            f"🔴 HALT | {symbol} {money(p)}\n\n"
            f"إيقاف تداول\n{title}\n\n"
            f"🔗 {link}\n🕒 {time_text()}\nالمصدر: Nasdaq Trader"
        )

        if send(msg):
            remember(key)
            count += 1

    return count

# =============== WHALES ===============
def process_whales():
    # لا يتم اعتبار الحجم المرتفع صفقة حوت.
    # لا تفعيل قبل توفير بيانات Time & Sales وLevel 2 موثوقة.
    logging.info("WHale disabled: verified trade/order-book feed unavailable")
    return 0

# =============== MAIN ===============
def main():
    global PRICE_CACHE, PRICE_CHECKS
    PRICE_CACHE, PRICE_CHECKS = {}, 0

    logging.info("Foras Market US started")

    if not TOKEN or not CHAT_ID or not API_KEY:
        logging.error("Missing Telegram or Finnhub settings")
        return

    halt_count = process_halts()
    movers = market_movers()

    if not movers:
        logging.warning("No verified eligible stocks")
        save_state()
        return

    gainers = process_gainers(movers)
    moves = process_moves(movers)
    momentum = process_momentum(movers)

    symbols = [x["symbol"] for x in movers if eligible(x["price"])]
    news = process_news(symbols)
    sec = process_sec(symbols)
    whales = process_whales()

    save_state()
    logging.info(
        f"Done | HALT:{halt_count} | GAINERS:{gainers} | "
        f"PRICE:{moves} | MOMENTUM:{momentum} | NEWS:{news} | "
        f"SEC:{sec} | WHALES:{whales} | PRICE CHECKS:{PRICE_CHECKS}"
    )

if __name__ == "__main__":
    main()
