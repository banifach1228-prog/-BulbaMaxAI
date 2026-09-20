import threading

import bot
import miniapp_server


def run_web_server():
    try:
        miniapp_server.run_server()
    except Exception as exc:
        print("BulbaMaxAI Mini App server error:", repr(exc))


def main():
    bot.load_db()
    print(f"BulbaMaxAI {bot.BOT_VERSION}: database loaded")

    web_thread = threading.Thread(
        target=run_web_server,
        name="bulba-miniapp",
        daemon=True,
    )
    web_thread.start()
    print(f"BulbaMaxAI: Mini App server started on port {miniapp_server.PORT}")
    print("BulbaMaxAI: Telegram bot starting")
    bot.main()


if __name__ == "__main__":
    main()
