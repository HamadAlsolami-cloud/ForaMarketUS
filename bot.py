"""
Simplified Positive News Bot for Small US Stocks
يرسل فقط الأخبار الإيجابية لشركات صغيرة إلى Telegram
"""

import os, re, json, time, hashlib, logging
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
from functools import lru_cache

import requests

# ==================== الإعدادات ====================
TG_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT     = os.getenv("TELEGRAM_CHAT_ID", "").strip()
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
SEC_UA      = os.getenv("SEC_USER_AGENT", "NewsBot admin@example.com").strip()

INTERVAL        = int(os.getenv("INTERVAL_SECONDS", "300"))
BATCH_SIZE      = int(os.getenv("MAX_SYMBOLS_PER_CYCLE", "25"))
LOOKBACK_HOURS  = int(os.getenv("NEWS_LOOKBACK_HOURS", "72"))
MAX_MC_M        = float(os.getenv("MAX_MARKET_CAP_M", "300"))  # 0 = معطّل
STATE_FILE      = "news_state.json"

FINNHUB_BASE = "https://finnhub.io/api/v1"
SEC_TICKERS  = "https://www.sec.gov/files/company_tickers.json"
SEC_ATOM     = "https://www.sec.gov/cgi-bin/browse-edgar"
FDA_FEEDS    = [
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/drugs/rss.xml",
]
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"

NY_TZ = ZoneInfo("America/New_York")
UTC   = timezone.utc

ALLOWED_MICS = {"XNAS", "XNYS", "XASE", "ARCX", "BATS", "IEXG"}
SEC_FORMS    = os.getenv("SEC_FORMS", "8-K,10-Q,10-K,S-1,424B5,13D,13G,4").split(",")

POSITIVE = [
    "fda approval", "fda approves", "breakthrough therapy", "fast track",
    "orphan drug", "positive topline", "positive top-line",
    "met primary endpoint", "met the primary endpoint",
    "phase 2 results", "phase 3 results", "successful phase",
    "wins contract", "awarded contract", "new contract", "purchase order",
    "strategic partnership", "strategic collaboration",
    "acquisition agreement", "merger agreement", "to be acquired",
    "record revenue", "revenue growth", "raises guidance", "raised guidance",
    "beats estimates", "beat estimates", "turns profitable",
    "extends cash runway", "grant award", "license agreement",
    "patent granted", "510(k) clearance", "ce mark approval",
    "موافقة إدارة الغذاء", "نتائج إيجابية", "حقق الهدف الأساسي",
    "فاز بعقد", "شراكة استراتيجية", "اتفاقية استحواذ", "رفع التوقعات",
]

NEGATIVE = [
    "public offering", "registered direct", "at-the-market",
    "private placement", "pipe financing", "convertible note",
    "convertible debt", "warrant", "dilution", "dilutive",
    "reverse split", "stock split", "going concern", "bankruptcy",
    "chapter 11", "delisting", "non-compliance",
    "fda rejects", "fda rejection", "clinical trial failed",
    "failed to meet primary", "missed primary", "clinical hold",
    "ceases operations", "debt default", "misses estimates",
    "cuts guidance", "lowers guidance", "revenue declines",
    "class action", "securities lawsuit", "fraud investigation",
    "resigns", "restatement", "material weakness", "layoffs",
    "طرح أسهم", "تخفيف", "إفلاس", "شطب", "فشل التجربة", "خفض التوقعات",
]

# ==================== التسجيل ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("bot")

# ==================== الجلسة ====================
session = requests.Session()
session.headers["Accept"] = "application/json, application/xml, */*"

# ==================== ضبط معدل Finnhub ====================
_last_call = 0.0
_calls: deque = deque()
MIN_DELAY = 60.0 / max(1, int(os.getenv("FINNHUB_CALLS_PER_MINUTE", "45")))


def _throttle():
    global _last_call
    now = time.monotonic()
    if now - _last_call < MIN_DELAY:
        time.sleep(MIN_DELAY - (now - _last_call))
    now = time.monotonic()
    while _calls and now - _calls[0] >= 60:
        _calls.popleft()
    if len(_calls) >= 45:
        time.sleep(max(1, 60 - (now - _calls[0]) + 1))
    _calls.append(time.monotonic())
    _last_call = _calls[-1]


def finnhub(endpoint, params=None):
    if not FINNHUB_KEY:
        return None
    for attempt in range(3):
        try:
            _throttle()
            r = session.get(
                f"{FINNHUB_BASE}/{endpoint}",
                params=params or {},
                headers={"X-Finnhub-Token": FINNHUB_KEY},
                timeout=25,
            )
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "10")) + 1)
                continue
            if r.status_code in (403, 404):
                return None
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
    return None


def http_get(url, params=None, headers=None, timeout=25):
    try:
        r = session.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r
    except requests.RequestException:
        return None


# ==================== الحالة ====================
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        d = {}
    return {
        "symbols":        d.get("symbols", []) or [],
        "sent":           d.get("sent", []) or [],
        "index":          int(d.get("index", 0)),
        "updated":        d.get("updated", ""),
    }


STATE = load_state()


def save_state():
    payload = {
        "symbols": STATE["symbols"],
        "sent":    STATE["sent"][-10000:],
        "index":   STATE["index"],
        "updated": STATE["updated"],
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
    except OSError as exc:
        log.error("Save state failed: %s", exc)


def sid(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def mark_sent(key):
    STATE["sent"].append(key)
    if len(STATE["sent"]) > 10000:
        STATE["sent"] = STATE["sent"][-10000:]


# ==================== الوقت ====================
def utc_now():
    return datetime.now(UTC)


def parse_dt(value):
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, UTC)
        except (ValueError, OSError):
            return None
    if not value:
        return None
    text = str(value).strip()
    try:
        p = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return (p if p.tzinfo else p.replace(tzinfo=UTC)).astimezone(UTC)
    except ValueError:
        pass
    try:
        p = parsedate_to_datetime(text)
        return (p if p.tzinfo else p.replace(tzinfo=UTC)).astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def within_lookback(dt):
    return bool(dt) and 0 <= (utc_now() - dt).total_seconds() <= LOOKBACK_HOURS * 3600


def fmt_time(dt):
    return dt.astimezone(NY_TZ).strftime("%Y-%m-%d %I:%M %p ET") if dt else "—"


# ==================== التصنيف ====================
def classify(text):
    c = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not c:
        return False, "empty"
    if any(p in c for p in NEGATIVE):
        return False, "negative"
    if any(p in c for p in POSITIVE):
        return True, "positive"
    return False, "neutral"


# ==================== الترجمة ====================
@lru_cache(maxsize=1024)
def translate(text):
    if not text:
        return ""
    parts = []
    for i in range(0, len(text), 1800):
        chunk = text[i:i + 1800]
        try:
            r = session.get(
                TRANSLATE_URL,
                params={"client": "gtx", "sl": "auto", "tl": "ar",
                        "dt": "t", "q": chunk},
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            parts.append("".join(seg[0] for seg in data[0] if seg and seg[0]) or chunk)
        except Exception:
            parts.append(chunk)
    return " ".join(parts)


# ==================== Telegram ====================
def send_tg(message):
    if not TG_TOKEN or not TG_CHAT:
        log.error("Telegram credentials missing")
        return False
    try:
        r = session.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": message[:3900],
                  "disable_web_page_preview": True},
            timeout=30,
        )
        if r.status_code == 429:
            wait = int(r.json().get("parameters", {}).get("retry_after", 10))
            time.sleep(wait + 1)
            return False
        r.raise_for_status()
        return bool(r.json().get("ok"))
    except requests.RequestException:
        return False


def make_message(symbol, headline, summary, url, published, source):
    raw = headline.strip()
    if summary and summary.strip() not in raw:
        raw += " — " + summary.strip()
    body = re.sub(r"\s+", " ", translate(raw)).strip()
    link = f"\nالمصدر: {url}" if url else ""
    return (
        f"🟢 ${symbol}\n"
        f"الخبر: {body}\n"
        f"المصدر: {source}\n"
        f"الوقت: {fmt_time(published)}{link}"
    )


# ==================== كون الأسهم ====================
_SYM_RE = re.compile(r"[A-Z][A-Z0-9.-]{0,14}")


def fetch_symbols():
    data = finnhub("stock/symbol", {"exchange": "US"})
    if not isinstance(data, list):
        return []
    out = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol", "")).strip().upper()
        if not _SYM_RE.fullmatch(sym):
            continue
        if str(item.get("type", "")).lower() != "common stock":
            continue
        mic = item.get("mic")
        if mic and mic not in ALLOWED_MICS:
            continue
        out.add(sym)
    return sorted(out)


def refresh_symbols():
    need = not STATE["symbols"]
    if not need and STATE["updated"]:
        try:
            age = (datetime.now(NY_TZ).date()
                   - datetime.fromisoformat(STATE["updated"]).date()).days
            need = age >= 7
        except ValueError:
            need = True
    if not need:
        return
    syms = fetch_symbols()
    if syms:
        STATE["symbols"] = syms
        STATE["index"] %= len(syms)
        STATE["updated"] = datetime.now(NY_TZ).date().isoformat()
        save_state()


def next_batch():
    syms = STATE["symbols"]
    if not syms:
        return []
    n = min(BATCH_SIZE, len(syms))
    out = []
    for _ in range(n):
        out.append(syms[STATE["index"]])
        STATE["index"] = (STATE["index"] + 1) % len(syms)
    return out


@lru_cache(maxsize=4096)
def small_enough(symbol):
    if MAX_MC_M <= 0:
        return True
    p = finnhub("stock/profile2", {"symbol": symbol})
    if not isinstance(p, dict):
        return True
    cap = p.get("marketCapitalization")
    try:
        return float(cap) <= MAX_MC_M
    except (TypeError, ValueError):
        return True


# ==================== Finnhub News ====================
def fetch_company_news(symbol):
    end = utc_now().date()
    start = end - timedelta(days=2)
    data = finnhub("company-news",
                   {"symbol": symbol,
                    "from": start.isoformat(),
                    "to": end.isoformat()})
    return data if isinstance(data, list) else []


def collect_finnhub():
    out = []
    for symbol in next_batch():
        try:
            for art in fetch_company_news(symbol):
                headline = str(art.get("headline") or "").strip()
                if not headline:
                    continue
                published = parse_dt(art.get("datetime"))
                if not within_lookback(published):
                    continue
                summary = str(art.get("summary") or "").strip()
                ok, _ = classify(headline + " " + summary)
                if not ok or not small_enough(symbol):
                    continue
                url = str(art.get("url") or "").strip()
                ident = art.get("id") or url or f"{headline}|{published}"
                key = sid(f"FH|{symbol}|{ident}")
                if key in STATE["sent"]:
                    continue
                out.append({
                    "key": key, "symbol": symbol,
                    "headline": headline, "summary": summary,
                    "url": url, "published": published,
                    "source": str(art.get("source") or "Finnhub"),
                })
        except Exception:
            log.exception("Finnhub scan failed: %s", symbol)
    return out


# ==================== SEC ====================
def normalize_name(name):
    t = str(name or "").upper()
    t = re.sub(r"\b(INC|CORP|CO|LTD|LLC|PLC|LP)\b", " ", t)
    t = re.sub(r"[^A-Z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def load_sec_map():
    r = http_get(SEC_TICKERS, headers={"User-Agent": SEC_UA}, timeout=40)
    if r is None:
        return {}
    try:
        raw = r.json()
    except ValueError:
        return {}
    m = {}
    for item in (raw.values() if isinstance(raw, dict) else []):
        if not isinstance(item, dict):
            continue
        tk = str(item.get("ticker", "")).upper().strip()
        nm = normalize_name(item.get("title", ""))
        if tk and nm:
            m[nm] = tk
    return m


def map_company(company, cmap):
    n = normalize_name(company)
    if not n:
        return None
    if n in cmap:
        return cmap[n]
    if len(n) >= 8:
        matches = [(k, v) for k, v in cmap.items() if n in k or k in n]
        if len(matches) == 1:
            return matches[0][1]
    return None


def fetch_sec(cmap):
    out = []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for form in [f.strip().upper() for f in SEC_FORMS if f.strip()]:
        r = http_get(
            SEC_ATOM,
            params={"action": "getcurrent", "type": form,
                    "output": "atom", "count": "100"},
            headers={"User-Agent": SEC_UA, "Host": "www.sec.gov"},
            timeout=35,
        )
        if r is None:
            continue
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            continue
        for entry in root.findall("a:entry", ns):
            title = (entry.findtext("a:title", "", ns) or "").strip()
            published = parse_dt(entry.findtext("a:updated", "", ns))
            if not within_lookback(published):
                continue
            link_node = entry.find("a:link", ns)
            link = link_node.attrib.get("href", "") if link_node is not None else ""
            summary = entry.findtext("a:summary", "", ns) or ""
            company = re.sub(r"\s*\(\d{5,}\)\s*$", "",
                             title.split(" - ", 1)[-1].strip()).strip()
            symbol = map_company(company, cmap)
            if not symbol:
                continue
            ok, _ = classify(title + " " + summary)
            if not ok or not small_enough(symbol):
                continue
            key = sid(f"SEC|{form}|{link or title}|{published}")
            if key in STATE["sent"]:
                continue
            out.append({
                "key": key, "symbol": symbol,
                "headline": title, "summary": summary,
                "url": link, "published": published,
                "source": f"SEC ({form})",
            })
    return out


# ==================== FDA ====================
def fetch_fda(cmap):
    out = []
    for feed in FDA_FEEDS:
        r = http_get(feed, headers={"User-Agent": "NewsBot/1.0"}, timeout=30)
        if r is None:
            continue
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            continue
        for item in root.findall(".//item"):
            def txt(tag):
                node = item.find(tag)
                return (node.text or "").strip() if node is not None else ""
            headline = txt("title")
            summary = txt("description")
            link = txt("link")
            published = parse_dt(txt("pubDate"))
            if not headline or not within_lookback(published):
                continue
            combined = normalize_name(headline + " " + summary)
            matched = sorted({
                tk for name, tk in cmap.items()
                if len(name) >= 8 and name in combined
            })
            if len(matched) != 1:
                continue
            symbol = matched[0]
            ok, _ = classify(headline + " " + summary)
            if not ok or not small_enough(symbol):
                continue
            key = sid(f"FDA|{link or headline}|{symbol}")
            if key in STATE["sent"]:
                continue
            out.append({
                "key": key, "symbol": symbol,
                "headline": headline, "summary": summary,
                "url": link, "published": published,
                "source": "FDA",
            })
    return out


# ==================== الإرسال ====================
def deliver(items):
    sent = 0
    for it in items:
        ok, _ = classify(it["headline"] + " " + it.get("summary", ""))
        if not ok:
            continue
        msg = make_message(
            it["symbol"], it["headline"], it.get("summary", ""),
            it.get("url", ""), it.get("published"), it.get("source", "—"),
        )
        if send_tg(msg):
            mark_sent(it["key"])
            sent += 1
            log.info("Sent: %s", it["symbol"])
            time.sleep(1)
    return sent


# ==================== الحلقة الرئيسية ====================
def run_once():
    log.info("=" * 55)
    if not (TG_TOKEN and TG_CHAT and FINNHUB_KEY):
        log.error("Missing credentials")
        return

    refresh_symbols()
    if not STATE["symbols"]:
        log.warning("No symbols available")
        return

    cmap = load_sec_map()
    groups = [collect_finnhub()]
    if cmap:
        groups.append(fetch_sec(cmap))
        groups.append(fetch_fda(cmap))

    merged = {}
    for g in groups:
        for it in g:
            merged.setdefault(it["key"], it)
    items = sorted(
        merged.values(),
        key=lambda x: x["published"] or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    log.info("New candidates: %s", len(items))
    deliver(items)
    save_state()
    log.info("Cycle done | universe=%s | index=%s",
             len(STATE["symbols"]), STATE["index"])


def main():
    log.info("Bot started | interval=%ss", INTERVAL)
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            log.info("Stopped by user")
            break
        except Exception:
            log.error("Cycle failure:\n%s", traceback.format_exc())
            save_state()
        try:
            time.sleep(max(30, INTERVAL))
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
