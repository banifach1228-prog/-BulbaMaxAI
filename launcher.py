import threading

import bot
import miniapp_server


def main():
    # Загружаем общую базу до запуска HTTP и Telegram-бота.
    bot.load_db()

    web_thread = threading.Thread(
        target=miniapp_server.run_server,
        name="bulba-miniapp",
        daemon=True,
    )
    web_thread.start()

    bot.main()


if __name__ == "__main__":
    main()
