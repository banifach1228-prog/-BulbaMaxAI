import bot


def main():
    bot.load_db()
    print(f"BulbaMaxAI {bot.BOT_VERSION}: database loaded")
    print("BulbaMaxAI: Telegram bot starting")
    bot.main()


if __name__ == "__main__":
    main()
