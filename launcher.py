import threading

import bot
import miniapp_server
import image_generator


def main():
    bot.load_db()

    print(
        f"BulbaMaxAI {bot.BOT_VERSION}: database loaded"
    )

    # Тестовая генерация изображений.
    # Пока работает только через Telegram-бота.
    image_generator.install(bot)

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