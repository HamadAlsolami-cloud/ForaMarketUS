if alerts:
    message = "🦅 فرص السوق الأمريكي\n\n"
    message += "\n━━━━━━━━━━━━━━\n".join(alerts)

    send_message(message)
else:
    print("No new qualifying alerts")
