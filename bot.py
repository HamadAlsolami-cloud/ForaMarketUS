
import os
import requests
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

# ==================================
# إعدادات الاتصال
# ==================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")

BASE_URL = "https://finnhub.io/api/v1"
STATE_FILE = "alert_state.json"

MAX_PRICE = 5.00
MOVE_ALERT_PERCENT = 5.0
MAX_MESSAGE_LENGTH = 3500

# ==================================
# الأسهم المبدئية للمراقبة
# ==================================

WATCHLIST = [
    "ATCH", "DVLT", "INLF", "XLAB", "LAES",
    "AREB", "ASST", "PFSA", "FLYE", "DFLI",
    "APLT", "GNS", "CYBH", "CYPH", "PAVS",
    "CXAI", "TMDE", "PSIG", "PBM", "ELAB",
    "AMIX", "YYGH", "SMTK", "KXIN", "KNRX",
    "XRPN"
]

# ==================================
# الكلمات المهمة للأخبار
# ==================================

IMPORTANT_KEYWORDS = [
    "contract", "partnership", "merger", "acquisition",
    "fda", "approval", "clinical trial", "earnings",
    "sec filing", "8-k", "13d", "13g", "form 4",
    "nasdaq", "compliance", "revenue", "guidance",
    "strategic", "government", "award", "orders",
    "offering", "bankruptcy", "delisting", "halt"
]

# ==================================
# حالة التنبيهات السابقة
# ==================================

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)

    except (FileNotFoundError, json.JSONDecodeError):
        return {"news": [], "moves": []}


def save_state(state):
    state["news"] = state["news"][-1000:]
    state["moves"] = state["moves"][-1000:]

    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False)


# ==================================
# إرسال رسائل تيليجرام
# ==================================

def send_message(message):

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    for attempt in range(4):

        response = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": message,
                "disable_web_page_preview": True
            },
            timeout=30
        )

        if response.status_code == 429:

            try:
                data = response.json()
                wait_time = data.get(
                    "parameters", {}
                ).get("retry_after", 5)

            except (ValueError, AttributeError):
                wait_time = 5

            print(
                f"Telegram rate limit. "
                f"Waiting {wait_time} seconds."
            )

            if attempt < 3:
                time.sleep(wait_time + 1)
                continue

        response.raise_for_status()

        print("Telegram message sent successfully")
        return


# ==================================
# تقسيم التنبيهات حسب حد تيليجرام
# ==================================

def send_alerts(alerts):

    if not alerts:
        print("No new qualifying alerts")
        return

    header = "🦅 فرص السوق الأمريكي\n\n"
    separator = "\n━━━━━━━━━━━━━━\n"

    messages = []
    current_message = header

    for alert in alerts:

        if len(alert) > MAX_MESSAGE_LENGTH - len(header):
            alert = alert[:MAX_MESSAGE_LENGTH - len(header) - 30]

        addition = alert + separator

        if len(current_message) + len(addition) > MAX_MESSAGE_LENGTH:

            messages.append(current_message)
            current_message = header + addition

        else:
            current_message += addition

    if current_message.strip() != header.strip():
        messages.append(current_message)

    for index, message in enumerate(messages):

        send_message(message)

        print(
            f"Sent message {index + 1} "
            f"of {len(messages)}"
        )

        if index < len(messages) - 1:
            time.sleep(1.2)


# ==================================
# الاتصال بـ Finnhub
# ==================================

def finnhub_request(endpoint, params):

    request_params = params.copy()
    request_params["token"] = FINNHUB_KEY

    response = requests.get(
        f"{BASE_URL}/{endpoint}",
        params=request_params,
        timeout=20
    )

    response.raise_for_status()

    return response.json()


def get_quote(symbol):

    return finnhub_request(
        "quote",
        {"symbol": symbol}
    )


def get_company_news(symbol):

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=2)

    return finnhub_request(
        "company-news",
        {
            "symbol": symbol,
            "from": start.isoformat(),
            "to": today.isoformat()
        }
    )


# ==================================
# فلترة الأخبار المهمة
# ==================================

def is_important_news(item):

    text = (
        str(item.get("headline", "")) + " " +
        str(item.get("summary", ""))
    ).lower()

    return any(
        word in text
        for word in IMPORTANT_KEYWORDS
    )


def news_id(item):

    value = (
        str(item.get("id", "")) +
        str(item.get("url", "")) +
        str(item.get("headline", ""))
    )

    return hashlib.sha256(
        value.encode()
    ).hexdigest()


# ==================================
# مراقبة الأسهم
# ==================================

def scan_stocks(state):

    news_sent = set(state["news"])
    moves_sent = set(state["moves"])

    alerts = []

    for symbol in WATCHLIST:

        try:

            quote = get_quote(symbol)

            price = float(quote.get("c", 0) or 0)
            change = float(quote.get("dp", 0) or 0)

            if price <= 0:
                continue

            if price >= MAX_PRICE:
                continue

            print(
                f"{symbol}: ${price:.4f} | "
                f"{change:+.2f}%"
            )

            # تنبيه الحركة السعرية

            if abs(change) >= MOVE_ALERT_PERCENT:

                direction = (
                    "صعود" if change > 0 else "هبوط"
                )

                move_key = (
                    f"{symbol}-"
                    f"{datetime.now(timezone.utc).date()}-"
                    f"{direction}"
                )

                if move_key not in moves_sent:

                    alerts.append(
                        f"📊 تنبيه حركة سعرية\n"
                        f"السهم: {symbol}\n"
                        f"السعر: ${price:.4f}\n"
                        f"التغير اليومي: {change:+.2f}%\n"
                        f"الاتجاه: {direction}\n"
                    )

                    moves_sent.add(move_key)

            # أخبار الشركة

            try:

                news_items = get_company_news(symbol)

                for item in news_items[:20]:

                    if not is_important_news(item):
                        continue

                    identifier = news_id(item)

                    if identifier in news_sent:
                        continue

                    headline = item.get(
                        "headline",
                        "خبر جديد"
                    )

                    link = item.get("url", "")

                    alerts.append(
                        f"🚨 خبر مهم: {symbol}\n"
                        f"📰 {headline}\n"
                        f"💵 السعر: ${price:.4f}\n"
                        f"📈 التغير اليومي: {change:+.2f}%\n"
                        f"🔗 {link}\n"
                    )

                    news_sent.add(identifier)

            except Exception as error:

                print(
                    f"News error {symbol}: {error}"
                )

        except Exception as error:

            print(
                f"Quote error {symbol}: {error}"
            )

    state["news"] = list(news_sent)
    state["moves"] = list(moves_sent)

    return alerts


# ==================================
# تشغيل البوت
# ==================================

def main():

    if not TOKEN or not CHAT_ID or not FINNHUB_KEY:

        raise ValueError(
            "Missing API settings"
        )

    print("Foras Market US started")

    state = load_state()

    alerts = scan_stocks(state)

    if alerts:

        print(
            f"Total alerts found: {len(alerts)}"
        )

        send_alerts(alerts)

    else:

        print("No new qualifying alerts")

    save_state(state)

    print("Scan completed successfully")


# ==================================
# بدء التشغيل
# ==================================

if __name__ == "__main__":
    main()
