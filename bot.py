
import os
import requests

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_message(message):
    if not TOKEN or not CHAT_ID:
        raise ValueError("Missing Telegram settings")

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True
        },
        timeout=20
    )

    response.raise_for_status()
    print("Message sent successfully")

def main():
    send_message(
        "🦅 فرص السوق الأمريكي\n"
        "تم تشغيل البوت بنجاح.\n"
        "نستعد لإعداد نظام تنبيهات الأسهم الأقل من 5 دولارات."
    )

if __name__ == "__main__":
    main()
