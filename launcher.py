import threading

import bot
import miniapp_server


def main():
    bot.load_db()
    print(f"BulbaMaxAI {bot.BOT_VERSION}: database loaded")

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