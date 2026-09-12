import threading

import bot
import miniapp_server


def main():
    # Загружаем базу один раз до запуска обоих компонентов.
    bot.load_db()

    print("BulbaMaxAI: database loaded")

    web_thread = threading.Thread(
        target=miniapp_server.run_server,
        name="bulba-miniapp",
        daemon=True,
    )

    web_thread.start()

    print("BulbaMaxAI: Mini App server started")
    print("BulbaMaxAI: Telegram bot starting")

    bot.main()


if __name__ == "__main__":
    main()