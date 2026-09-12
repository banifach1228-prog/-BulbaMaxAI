import threading

import bot
import miniapp_server


def main():
    web_thread = threading.Thread(
        target=miniapp_server.run_server,
        name="bulba-miniapp",
        daemon=True,
    )

    web_thread.start()

    bot.main()


if __name__ == "__main__":
    main()
