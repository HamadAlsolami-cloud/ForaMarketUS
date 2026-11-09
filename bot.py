rt os
import re
import json
import time
import fcntl
import hashlib
import logging
import traceback
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
from collections import deque
from functools import lru_cache

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# =========================================================
# CONFIGURATION
# =========================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()

# SEC requires a descriptive User-Agent with a contact email.
SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "USStockNewsBot contact admin@example.com"
).strip()

STATE_FILE = os.getenv("STATE_FILE", "news_state.json")
LOCK_FILE = os.getenv("LOCK_FILE", "news_bot.lock")
LOG_FILE = os.getenv("LOG_FILE", "news_bot.log")

INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "300"))
MAX_SYMBOLS_PER_CYCLE = int(os.getenv("MAX_SYMBOLS_PER_CYCLE", "25"))
NEWS_LOOKBACK_HOURS = int(os.getenv("NEWS_LOOKBACK_HOURS", "72"))
SYMBOL_REFRESH_DAYS = int(os.getenv("SYMBOL_REFRESH_DAYS", "7"))

# Finnhub free-plan protection. Keep this below the account's actual limit.
FINNHUB_CALLS_PER_MINUTE = int(os.getenv("FINNHUB_CALLS_PER_MINUTE", "45"))
FINNHUB_MIN_DELAY = 60.0 / max(1, FINNHUB_CALLS_PER_MINUTE)

# Penny-stock / small-company focus:
# There is intentionally NO stock-price filter.
# This optional market-cap filter helps focus the universe on small companies.
# Set MAX_MARKET_CAP_M=0 to disable the market-cap filter entirely.
MAX_MARKET_CAP_M = float(os.getenv("MAX_MARKET_CAP_M", "300"))

BASE_FINNHUB = "https://finnhub.io/api/v1"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_ATOM_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
FDA_RSS_FEEDS = [
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/drugs/rss.xml",
]

NY_TZ = ZoneInfo("America/New_York")
UTC = timezone.utc

# Finnhub exchange MICs. OTC is excluded.
ALLOWED_MICS = {"XNAS", "XNYS", "XASE", "ARCX", "BATS", "IEXG"}

# Relevant SEC forms. Additional forms can be added through the environment.
SEC_FORMS = os.getenv(
    "SEC_FORMS",
    "8-K,10-Q,10-K,S-1,F-1,424B1,424B2,424B3,424B4,424B5,"
    "13D,13G,3,4,5,6-K,20-F"
).split(",")

# Only clearly positive catalysts are sent.
POSITIVE_PHRASES = [
    "fda approves", "approved by the fda", "fda approval",
    "receives fda approval", "fda grants", "breakthrough therapy designation",
    "fast track designation", "orphan drug designation",
    "positive topline results", "positive top-line results",
    "met primary endpoint", "met the primary endpoint",
    "primary endpoint was met", "successful phase 2", "successful phase 3",
    "phase 2 results", "phase 3 results", "positive clinical results",
    "clinical trial meets", "clinical study meets",
    "wins contract", "awarded contract", "receives contract",
    "new contract", "contract award", "receives purchase order",
    "purchase order", "selected for", "strategic partnership",
    "strategic collaboration", "definitive agreement to acquire",
    "acquisition agreement", "acquired", "to be acquired",
    "merger agreement", "record revenue", "record sales",
    "revenue increases", "revenue growth", "raises guidance",
    "raised guidance", "beats estimates", "beat estimates",
    "earnings beat", "returns to profitability", "turns profitable",
    "debt reduced", "debt reduction", "extends cash runway",
    "receives grant", "grant award", "government award",
    "license agreement", "licensing agreement",
    "patent granted", "patent allowance", "commercial launch",
    "launches commercial", "receives regulatory clearance",
    "510(k) clearance", "de novo clearance", "ce mark approval",
    "موافقة إدارة الغذاء والدواء", "حصلت على موافقة",
    "نتائج إيجابية", "حقق الهدف الأساسي", "نجاح المرحلة",
    "فاز بعقد", "ترسية عقد", "طلب شراء", "شراكة استراتيجية",
    "اتفاقية استحواذ", "زيادة الإيرادات", "رفع التوقعات",
    "تجاوز التوقعات", "منحة", "اعتماد تنظيمي",
]

NEGATIVE_PHRASES = [
    "public offering", "registered direct offering", "at-the-market offering",
    "atm offering", "private placement", "pipe financing", "convertible notes",
    "convertible debt", "warrants", "dilutive", "dilution",
    "reverse stock split", "stock split", "going concern",
    "bankruptcy", "chapter 11", "delisting", "non-compliance",
    "receives delisting notice", "nasdaq notice", "nyse notice",
    "fda rejects", "fda rejection", "clinical trial failed",
    "failed to meet primary endpoint", "missed primary endpoint",
    "negative topline", "negative top-line", "discontinues trial",
    "clinical hold", "suspends operations", "ceases operations",
    "default", "debt default", "misses estimates", "cuts guidance",
    "lowers guidance", "revenue declines", "revenue decreased",
    "class action", "securities lawsuit", "fraud investigation",
    "sec investigation", "resigns", "auditor resignation",
    "restatement", "material weakness", "layoffs",
    "طرح أسهم", "طرح عام", "طرح مباشر", "تمويل خاص", "تخفيف",
    "أسهم قابلة للتحويل", "تقسيم عكسي", "إفلاس", "شطب",
    "عدم الامتثال", "رفضت FDA", "فشل التجربة", "نتائج سلبية",
    "خفض التوقعات", "دعوى جماعية", "تحقيق احتيال",
]

# If a negative phrase is found, the item is rejected even if it contains
# positive wording. This conservative rule avoids sending mixed announcements.
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"


# =========================================================
# LOGGING AND HTTP
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
retry = Retry(
    total=3,
    connect=3,
    read=2,
    backoff_factor=1,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST"]),
    respect_retry_after_header=True,
)
adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
session.mount("https://", adapter)
session.mount("http://", adapter)
session.headers.update({"Accept": "application/json, application/atom+xml, application/rss+xml, */*"})

_last_finnhub_call = 0.0
_finnhub_calls = deque()


def finnhub_wait():
    global _last_finnhub_call

    now_mono = time.monotonic()
    elapsed = now_mono - _last_finnhub_call
    if elapsed < FINNHUB_MIN_DELAY:
        time.sleep(FINNHUB_MIN_DELAY - elapsed)

    now_mono = time.monotonic()
    while _finnhub_calls and now_mono - _finnhub_calls[0] >= 60:
        _finnhub_calls.popleft()

    if len(_finnhub_calls) >= FINNHUB_CALLS_PER_MINUTE:
        pause = 60 - (now_mono - _finnhub_calls[0]) + 1
        logging.warning("Finnhub rate-limit protection: waiting %.1fs", pause)
        time.sleep(max(1, pause))

    now_mono = time.monotonic()
    _finnhub_calls.append(now_mono)
    _last_finnhub_call = now_mono


def http_get(url, params=None, headers=None, timeout=25):
    try:
        response = session.get(
            url, params=params, headers=headers, timeout=timeout
        )
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        logging.warning("HTTP GET failed (%s): %s", url, type(exc).__name__)
        return None


def finnhub(endpoint, params=None):
    if not FINNHUB_API_KEY:
        logging.error("FINNHUB_API_KEY is missing")
        return None

    for attempt in range(3):
        try:
            finnhub_wait()
            response = session.get(
                f"{BASE_FINNHUB}/{endpoint}",
                params=params or {},
                headers={"X-Finnhub-Token": FINNHUB_API_KEY},
                timeout=25,
            )
            if response.status_code == 429:
                wait = int(response.headers.get("Retry-After", "10"))
                logging.warning("Finnhub 429; waiting %ss", wait)
                time.sleep(wait + 1)
                continue
            if response.status_code in (403, 404):
                logging.warning("Finnhub returned %s for %s", response.status_code, endpoint)
                return None
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logging.warning("Finnhub %s attempt %s: %s", endpoint, attempt + 1, type(exc).__name__)
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
        except (ValueError, TypeError) as exc:
            logging.warning("Finnhub JSON error %s: %s", endpoint, exc)
            return None
    return None


# =========================================================
# PERSISTENT STATE / DEDUPLICATION
# =========================================================

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raw = {}
    except (OSError, ValueError):
        raw = {}

    symbols = raw.get("symbols", [])
    sent_ids = raw.get("sent", [])
    return {
        "symbols": symbols if isinstance(symbols, list) else [],
        "sent": dict.fromkeys(sent_ids if isinstance(sent_ids, list) else []),
        "symbol_index": max(0, int(raw.get("symbol_index", 0))),
        "symbols_updated": raw.get("symbols_updated", ""),
        "last_sec_check": int(raw.get("last_sec_check", 0) or 0),
        "last_fda_check": int(raw.get("last_fda_check", 0) or 0),
    }


STATE = load_state()
SYMBOLS = STATE["symbols"]
SENT = STATE["sent"]
SYMBOL_INDEX = STATE["symbol_index"]
SYMBOLS_UPDATED = STATE["symbols_updated"]
LAST_SEC_CHECK = STATE["last_sec_check"]
LAST_FDA_CHECK = STATE["last_fda_check"]
DIRTY = False


def save_state():
    global DIRTY
    payload = {
        "symbols": SYMBOLS,
        "sent": list(SENT.keys())[-10000:],
        "symbol_index": SYMBOL_INDEX,
        "symbols_updated": SYMBOLS_UPDATED,
        "last_sec_check": LAST_SEC_CHECK,
        "last_fda_check": LAST_FDA_CHECK,
    }
    temp_path = STATE_FILE + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, STATE_FILE)
        DIRTY = False
    except OSError as exc:
        logging.error("Could not save state: %s", exc)


def stable_id(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def was_sent(key):
    return key in SENT


def mark_sent(key):
    global DIRTY
    SENT[key] = None
    while len(SENT) > 10000:
        SENT.pop(next(iter(SENT)))
    DIRTY = True


# =========================================================
# TIME HELPERS
# =========================================================

def utc_now():
    return datetime.now(UTC)


def parse_datetime(value):
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, UTC)
        except (ValueError, OSError):
            return None

    if not value:
        return None

    text_value = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        pass

    try:
        parsed = parsedate_to_datetime(text_value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        pass

    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text_value[:10], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def within_lookback(published):
    if not published:
        return False
    age = (utc_now() - published).total_seconds()
    return 0 <= age <= NEWS_LOOKBACK_HOURS * 3600


def format_time(dt):
    if not dt:
        return "وقت النشر غير متاح"
    return dt.astimezone(NY_TZ).strftime("%Y-%m-%d %I:%M %p ET")


# =========================================================
# STOCK UNIVERSE
# =========================================================

def valid_symbol(symbol):
    return bool(re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", str(symbol or "").strip().upper()))


def fetch_us_symbols():
    logging.info("Downloading US common-stock universe from Finnhub")
    data = finnhub("stock/symbol", {"exchange": "US"})
    if not isinstance(data, list):
        return []

    symbols = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip().upper()
        stock_type = str(item.get("type", "")).strip().lower()
        mic = item.get("mic")

        if not valid_symbol(symbol) or stock_type != "common stock":
            continue
        if mic and mic not in ALLOWED_MICS:
            continue
        symbols.add(symbol)

    result = sorted(symbols)
    logging.info("Loaded %s US common stocks (OTC MICs excluded)", len(result))
    return result


def refresh_symbols():
    global SYMBOLS, SYMBOL_INDEX, SYMBOLS_UPDATED, DIRTY

    refresh = not SYMBOLS
    if SYMBOLS and SYMBOLS_UPDATED:
        try:
            age = (datetime.now(NY_TZ).date() - datetime.fromisoformat(SYMBOLS_UPDATED).date()).days
            refresh = age >= SYMBOL_REFRESH_DAYS
        except ValueError:
            refresh = True

    if not refresh:
        return

    new_symbols = fetch_us_symbols()
    if not new_symbols:
        logging.warning("Symbol refresh failed; retaining previous universe")
        return

    SYMBOLS = new_symbols
    SYMBOL_INDEX %= len(SYMBOLS)
    SYMBOLS_UPDATED = datetime.now(NY_TZ).date().isoformat()
    DIRTY = True
    save_state()


def next_symbol_batch():
    global SYMBOL_INDEX, DIRTY

    if not SYMBOLS:
        return []

    count = min(MAX_SYMBOLS_PER_CYCLE, len(SYMBOLS))
    batch = []
    for _ in range(count):
        batch.append(SYMBOLS[SYMBOL_INDEX])
        SYMBOL_INDEX = (SYMBOL_INDEX + 1) % len(SYMBOLS)
    DIRTY = True
    return batch


@lru_cache(maxsize=4096)
def passes_small_company_filter(symbol):
    """No price filter. Optional market-cap filter uses Finnhub profile2."""
    if MAX_MARKET_CAP_M <= 0:
        return True

    profile = finnhub("stock/profile2", {"symbol": symbol})
    if not isinstance(profile, dict):
        # Do not silently reject a stock if profile data is unavailable.
        logging.info("Market cap unavailable for %s; allowing news for review", symbol)
        return True

    market_cap = profile.get("marketCapitalization")
    if market_cap in (None, ""):
        return True

    try:
        # Finnhub marketCapitalization is generally expressed in millions.
        return float(market_cap) <= MAX_MARKET_CAP_M
    except (TypeError, ValueError):
        return True


# =========================================================
# POSITIVE-ONLY CLASSIFICATION
# =========================================================

def classify_positive(text):
    content = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not content:
        return False, "empty"

    negative_hits = [phrase for phrase in NEGATIVE_PHRASES if phrase in content]
    if negative_hits:
        return False, "negative_or_dilutive:" + ",".join(negative_hits[:3])

    positive_hits = [phrase for phrase in POSITIVE_PHRASES if phrase in content]
    if positive_hits:
        return True, "positive:" + ",".join(positive_hits[:3])

    return False, "neutral_or_unconfirmed"


# =========================================================
# TRANSLATION
# =========================================================

@lru_cache(maxsize=1024)
def translate_cached(text):
    chunks = []
    for offset in range(0, len(text), 1800):
        part = text[offset:offset + 1800]
        try:
            response = session.get(
                TRANSLATE_URL,
                params={"client": "gtx", "sl": "auto", "tl": "ar", "dt": "t", "q": part},
                timeout=20,
            )
            response.raise_for_status()
            data = response.json()
            translated = "".join(
                segment[0] for segment in data[0]
                if segment and segment[0]
            )
            chunks.append(translated or part)
        except Exception as exc:
            logging.warning("Translation failed: %s", type(exc).__name__)
            chunks.append(part)
    return " ".join(chunks)


def translate(text):
    return translate_cached(text) if text else ""


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram credentials are missing")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = session.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message[:3900],
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if response.status_code == 429:
            wait = int(response.json().get("parameters", {}).get("retry_after", 10))
            logging.warning("Telegram rate limit; wait %ss", wait)
            time.sleep(wait + 1)
            return False

        response.raise_for_status()
        result = response.json()
        return bool(result.get("ok"))
    except requests.RequestException as exc:
        logging.error("Telegram request failed: %s", type(exc).__name__)
        return False
    except (ValueError, TypeError) as exc:
        logging.error("Telegram response error: %s", exc)
        return False


def make_message(symbol, headline, summary, url, published, source):
    raw = headline.strip()
    if summary and summary.strip() and summary.strip() not in raw:
        raw += " — " + summary.strip()

    arabic = re.sub(r"\s+", " ", translate(raw)).strip()
    link_line = f"\nالمصدر: {url}" if url else ""
    return (
        f"🟢 ${symbol}\n"
        f"الخبر: {arabic}\n"
        f"المصدر الإخباري: {source}\n"
        f"وقت النشر: {format_time(published)}"
        f"{link_line}"
    )


# =========================================================
# FINNHUB COMPANY NEWS
# =========================================================

def fetch_company_news(symbol):
    end = utc_now().date()
    start = (end - timedelta(days=2)).date()
    data = finnhub(
        "company-news",
        {"symbol": symbol, "from": start.isoformat(), "to": end.isoformat()},
    )
    return data if isinstance(data, list) else []


def collect_finnhub_news():
    candidates = []
    batch = next_symbol_batch()
    logging.info("Finnhub company-news scan: %s symbols", len(batch))

    for symbol in batch:
        try:
            articles = fetch_company_news(symbol)
            for article in articles:
                headline = str(article.get("headline") or "").strip()
                if not headline:
                    continue
                published = parse_datetime(article.get("datetime"))
                if not within_lookback(published):
                    continue

                summary = str(article.get("summary") or "").strip()
                positive, reason = classify_positive(headline + " " + summary)
                if not positive:
                    continue

                if not passes_small_company_filter(symbol):
                    continue

                url = str(article.get("url") or "").strip()
                identity = article.get("id") or url or f"{headline}|{published}"
                key = stable_id(f"FINNHUB|{symbol}|{identity}")
                if was_sent(key):
                    continue

                candidates.append({
                    "symbol": symbol,
                    "headline": headline,
                    "summary": summary,
                    "url": url,
                    "published": published,
                    "source": str(article.get("source") or "Finnhub"),
                    "key": key,
                    "reason": reason,
                })
        except Exception:
            logging.exception("Finnhub news processing failed for %s", symbol)

    return candidates


# =========================================================
# SEC EDGAR
# =========================================================

def sec_headers():
    return {
        "User-Agent": SEC_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
        "Host": "www.sec.gov",
    }


def load_sec_company_map():
    """Returns normalized company-name -> ticker mapping."""
    response = http_get(SEC_TICKERS_URL, headers=sec_headers(), timeout=40)
    if response is None:
        return {}

    try:
        raw = response.json()
    except ValueError:
        return {}

    mapping = {}
    values = raw.values() if isinstance(raw, dict) else []
    for item in values:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker", "")).upper().strip()
        title = normalize_company_name(item.get("title", ""))
        if ticker and title:
            mapping[title] = ticker
    return mapping


def normalize_company_name(name):
    text_value = str(name or "").upper()
    text_value = re.sub(r"\b(INCORPORATED|INC|CORPORATION|CORP|COMPANY|CO|LTD|LIMITED|PLC|LLC|LP)\b", " ", text_value)
    text_value = re.sub(r"[^A-Z0-9 ]", " ", text_value)
    return re.sub(r"\s+", " ", text_value).strip()


def map_sec_company(company, company_map):
    normalized = normalize_company_name(company)
    if not normalized:
        return None

    if normalized in company_map:
        return company_map[normalized]

    # Conservative containment match; avoid very short names.
    if len(normalized) >= 8:
        matches = [
            (name, ticker) for name, ticker in company_map.items()
            if normalized in name or name in normalized
        ]
        if len(matches) == 1:
            return matches[0][1]
    return None


def fetch_sec_recent_filings(company_map):
    """Fetch recent SEC filings by form using the official EDGAR Atom feed."""
    output = []
    for form in [f.strip().upper() for f in SEC_FORMS if f.strip()]:
        params = {
            "action": "getcurrent",
            "type": form,
            "output": "atom",
            "count": "100",
        }
        response = http_get(SEC_ATOM_URL, params=params, headers=sec_headers(), timeout=35)
        if response is None:
            continue

        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            logging.warning("Could not parse SEC Atom feed for %s", form)
            continue

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", ns):
            title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            updated = entry.findtext("atom:updated", default="", namespaces=ns)
            published = parse_datetime(updated)
            if not within_lookback(published):
                continue

            link_node = entry.find("atom:link", ns)
            link = link_node.attrib.get("href", "") if link_node is not None else ""
            summary = entry.findtext("atom:summary", default="", namespaces=ns) or ""
            # EDGAR Atom titles commonly look like:
            # "8-K - Example Company, Inc. (0001234567)"
            company = title.split(" - ", 1)[-1].strip()
            company = re.sub(r"\s*\(\d{5,}\)\s*$", "", company).strip()
            symbol = map_sec_company(company, company_map)
            if not symbol:
                continue

            combined = title + " " + summary
            positive, reason = classify_positive(combined)
            if not positive:
                continue

            if not passes_small_company_filter(symbol):
                continue

            key = stable_id(f"SEC|{form}|{link or title}|{published}")
            if was_sent(key):
                continue

            output.append({
                "symbol": symbol,
                "headline": title,
                "summary": summary,
                "url": link,
                "published": published,
                "source": f"SEC EDGAR ({form})",
                "key": key,
                "reason": reason,
            })

    return output


# =========================================================
# FDA RSS
# =========================================================

def text_from_xml(element, path, ns=None):
    node = element.find(path, ns or {})
    return (node.text or "").strip() if node is not None and node.text else ""


def fetch_fda_news(company_map):
    output = []
    for feed_url in FDA_RSS_FEEDS:
        response = http_get(feed_url, headers={"User-Agent": "USStockNewsBot/1.0"}, timeout=30)
        if response is None:
            continue

        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            logging.warning("Could not parse FDA RSS feed: %s", feed_url)
            continue

        for item in root.findall(".//item"):
            headline = text_from_xml(item, "title")
            summary = text_from_xml(item, "description")
            link = text_from_xml(item, "link")
            date_text = text_from_xml(item, "pubDate")
            published = parse_datetime(date_text)

            if not headline or not within_lookback(published):
                continue

            # FDA news is only linked to a ticker when a company-name match
            # is clear. Unmatched FDA headlines are not sent as stock alerts.
            combined = headline + " " + summary
            matched = []
            for company_name, ticker in company_map.items():
                if len(company_name) >= 8 and company_name in normalize_company_name(combined):
                    matched.append(ticker)

            matched = sorted(set(matched))
            if len(matched) != 1:
                continue

            symbol = matched[0]
            positive, reason = classify_positive(combined)
            if not positive or not passes_small_company_filter(symbol):
                continue

            key = stable_id(f"FDA|{link or headline}|{symbol}")
            if was_sent(key):
                continue

            output.append({
                "symbol": symbol,
                "headline": headline,
                "summary": summary,
                "url": link,
                "published": published,
                "source": "FDA",
                "key": key,
                "reason": reason,
            })

    return output


# =========================================================
# CANDIDATE MERGE AND DELIVERY
# =========================================================

def merge_candidates(*groups):
    merged = {}
    for group in groups:
        for item in group:
            # Keep the strongest/earliest source record by unique key.
            merged.setdefault(item["key"], item)
    return sorted(
        merged.values(),
        key=lambda item: item["published"] or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )


def deliver_candidates(candidates):
    sent_count = 0

    for item in candidates:
        # Recheck classification at the final send gate.
        combined = item["headline"] + " " + item.get("summary", "")
        positive, reason = classify_positive(combined)
        if not positive:
            continue

        message = make_message(
            item["symbol"],
            item["headline"],
            item.get("summary", ""),
            item.get("url", ""),
            item.get("published"),
            item.get("source", "غير محدد"),
        )

        if send_telegram(message):
            mark_sent(item["key"])
            sent_count += 1
            logging.info(
                "Positive alert sent: %s | %s",
                item["symbol"],
                reason,
            )
            time.sleep(1.0)
        else:
            # Do not mark as sent. It can be retried in a later scan.
            logging.warning("Alert not sent; will retry later: %s", item["symbol"])

    return sent_count


# =========================================================
# PROCESS LOCK
# =========================================================

def acquire_lock():
    handle = open(LOCK_FILE, "w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.write(str(os.getpid()))
        handle.flush()
        return handle
    except BlockingIOError:
        handle.close()
        logging.error("Another bot instance is already running")
        return None


# =========================================================
# MAIN CYCLE
# =========================================================

def run_once():
    global LAST_SEC_CHECK, LAST_FDA_CHECK, DIRTY

    logging.info("=" * 65)
    logging.info("POSITIVE PENNY-STOCK NEWS SCANNER - NEW CYCLE")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return

    if not FINNHUB_API_KEY:
        logging.error("Set FINNHUB_API_KEY")
        return

    # No weekday/market-hours restriction: material positive news can arrive
    # before market, during market, after market, on weekends and holidays.
    refresh_symbols()
    if not SYMBOLS:
        logging.warning("US stock universe is unavailable")
        return

    company_map = load_sec_company_map()
    if not company_map:
        logging.warning("SEC company/ticker map unavailable this cycle")

    groups = []

    # Rotating Finnhub company-news scan.
    groups.append(collect_finnhub_news())

    # SEC and FDA global feeds are checked every cycle. Their own feeds return
    # recent items; state-based deduplication prevents repeated Telegram alerts.
    if company_map:
        groups.append(fetch_sec_recent_filings(company_map))
        groups.append(fetch_fda_news(company_map))

    candidates = merge_candidates(*groups)
    logging.info("Positive, eligible, new candidates: %s", len(candidates))

    sent_count = deliver_candidates(candidates)

    logging.info(
        "Cycle complete | sent=%s | universe=%s | next_index=%s",
        sent_count, len(SYMBOLS), SYMBOL_INDEX
    )

    if DIRTY:
        save_state()


def run_forever():
    logging.info("Bot started")
    logging.info("Cycle interval: %ss", INTERVAL_SECONDS)
    logging.info("Symbols per Finnhub cycle: %s", MAX_SYMBOLS_PER_CYCLE)
    logging.info("Price filter: DISABLED")
    logging.info("Focus: US-listed small companies / penny-stock universe")
    logging.info("OTC: excluded where exchange MIC is available")
    logging.info("News filter: POSITIVE ONLY")
    logging.info("Weekend/holiday scanning: ENABLED")

    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            logging.info("Stopped by user")
            break
        except Exception:
            logging.error("Unexpected cycle failure:\n%s", traceback.format_exc())
        finally:
            if DIRTY:
                save_state()

        try:
            time.sleep(max(30, INTERVAL_SECONDS))
        except KeyboardInterrupt:
            logging.info("Sleep interrupted; shutting down")
            break


def main():
    try:
        log_dir = os.path.dirname(LOG_FILE)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logging.getLogger().addHandler(file_handler)
    except OSError as exc:
        logging.warning("File logging unavailable: %s", exc)

    try:
        lock_handle = acquire_lock()
    except OSError as exc:
        logging.error("Could not acquire lock: %s", exc)
        return

    if lock_handle is None:
        return

    try:
        run_forever()
    finally:
        if DIRTY:
            save_state()
        try:
            lock_handle.close()
        except Exception:
            pass
        logging.info("Bot shutdown complete")


if __name__ == "__main__":
    main()
