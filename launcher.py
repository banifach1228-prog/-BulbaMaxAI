import threading

import bot
import miniapp_server


def main():
    # База загружается один раз ДО запуска Mini App и Telegram-бота.
    bot.load_db()

    print("BulbaMaxAI: database loaded")

    web_thread = threading.Thread(
        target=miniapp_server.run_server,
        name="bulba-miniapp",
        daemon=True,
    )

    web_thread.start()

    print(
        "BulbaMaxAI: Mini App server started"
    )

    print(
        "BulbaMaxAI: Telegram bot starting"
    )

    bot.main()


if __name__ == "__main__":
    main()