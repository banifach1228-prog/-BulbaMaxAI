import threading

import bot
import web_api


def main():
    web_thread = threading.Thread(
        target=web_api.run_server,
        name="bulba-web-api",
        daemon=True,
    )

    web_thread.start()

    bot.main()


if __name__ == "__main__":
    main()
