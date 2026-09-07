import os
import json
import time
import requests

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()

BASE_URL = "https://api.baza-ai.org/v1"
DATA_FILE = "v12_memory.json"

MAX_HISTORY = 20
MAX_TEXT = 3900
REQUEST_TIMEOUT = 90
POLL_TIMEOUT = 25

MODELS = {
    "mini": "gpt-5.4-mini",
    "luna": "gpt-5.6-luna",
    "terra": "gpt-5.6-terra",
    "sol": "gpt-5.6-sol",
    "gpt55": "gpt-5.5",
}

MODEL_NAMES = {
    "mini": "🟢 GPT-5.4 Mini",
    "luna": "🌙 GPT-5.6 Luna",
    "terra": "🌍 GPT-5.6 Terra",
    "sol": "☀️ GPT-5.6 Sol",
    "gpt55": "🔥 GPT-5.5",
}

STYLES = {
    "normal": "Отвечай понятно, естественно и полезно.",
    "short": "Отвечай кратко и по делу.",
    "detailed": "Отвечай подробно, структурированно и с объяснениями.",
}

db = {
    "chats": {},
    "total_requests": 0,
    "total_errors": 0,
}


def load_db():
    global db

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)

        if isinstance(saved, dict):
            db.update(saved)

    except Exception:
        pass


def save_db():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(
                db,
                f,
                ensure_ascii=False,
                indent=2
            )
    except Exception:
        pass


def get_chat(user_id):
    uid = str(user_id)

    if uid not in db["chats"]:
        db["chats"][uid] = {
            "model": MODELS["mini"],
            "style": "normal",
            "history": [],
            "profile": {},
            "requests": 0,
            "errors": 0,
            "rate": [],
        }

    chat = db["chats"][uid]

    chat.setdefault("model", MODELS["mini"])
    chat.setdefault("style", "normal")
    chat.setdefault("history", [])
    chat.setdefault("profile", {})
    chat.setdefault("requests", 0)
    chat.setdefault("errors", 0)
    chat.setdefault("rate", [])

    return chat


def telegram(method, data=None):
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        json=data or {},
        timeout=40,
    )

    response.raise_for_status()
    return response.json()


def send(chat_id, text, keyboard=None):
    text = str(text)

    if not text:
        return

    for i in range(0, len(text), MAX_TEXT):
        part = text[i:i + MAX_TEXT]

        data = {
            "chat_id": chat_id,
            "text": part,
        }

        if keyboard:
            data["reply_markup"] = keyboard

        try:
            telegram("sendMessage", data)
        except Exception as e:
            print("Telegram send error:", e)


def edit(chat_id, message_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": str(text)[:MAX_TEXT],
    }

    if keyboard:
        data["reply_markup"] = keyboard

    try:
        telegram("editMessageText", data)
    except Exception:
        pass


def callback_answer(callback_id):
    try:
        telegram(
            "answerCallbackQuery",
            {"callback_query_id": callback_id}
        )
    except Exception:
        pass


def main_keyboard():
    return {
        "keyboard": [
            [
                {"text": "🧠 Модель"},
                {"text": "📊 Статистика"},
            ],
            [
                {"text": "🆕 Новый чат"},
                {"text": "🧹 Очистить"},
            ],
            [
                {"text": "⚙️ Настройки"},
                {"text": "💾 Память"},
            ],
        ],
        "resize_keyboard": True,
    }


def models_keyboard():
    buttons = []

    for key, name in MODEL_NAMES.items():
        buttons.append([
            {
                "text": name,
                "callback_data": "model:" + key,
            }
        ])

    buttons.append([
        {
            "text": "⬅️ Назад",
            "callback_data": "back",
        }
    ])

    return {
        "inline_keyboard": buttons
    }


def settings_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🙂 Обычный",
                    "callback_data": "style:normal",
                }
            ],
            [
                {
                    "text": "⚡ Короткий",
                    "callback_data": "style:short",
                }
            ],
            [
                {
                    "text": "📚 Подробный",
                    "callback_data": "style:detailed",
                }
            ],
            [
                {
                    "text": "⬅️ Назад",
                    "callback_data": "back",
                }
            ],
        ]
    }


def rate_limit(chat):
    now = time.time()

    chat["rate"] = [
        t for t in chat["rate"]
        if now - t < 10
    ]

    if len(chat["rate"]) >= 5:
        return False

    chat["rate"].append(now)
    return True


def build_system_prompt(chat):
    style = STYLES.get(
        chat.get("style", "normal"),
        STYLES["normal"]
    )

    prompt = (
        "Ты BulbaMaxAI — умный, дружелюбный AI-ассистент "
        "в Telegram.\n"
        "Отвечай на русском языке, если пользователь не попросил "
        "другой язык.\n"
        f"{style}\n"
        "Не повторяй вопрос пользователя без необходимости."
    )

    profile = chat.get("profile", {})

    if profile:
        prompt += "\n\nПамять о пользователе:\n"

        for key, value in profile.items():
            prompt += f"- {key}: {value}\n"

    return prompt


def ask_ai(chat, text):
    messages = [
        {
            "role": "system",
            "content": build_system_prompt(chat),
        }
    ]

    for item in chat["history"][-MAX_HISTORY:]:
        messages.append(item)

    messages.append({
        "role": "user",
        "content": text,
    })

    payload = {
        "model": chat["model"],
        "messages": messages,
        "temperature": 0.7,
    }

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    last_error = None

    for attempt in range(3):
        try:
            response = requests.post(
                f"{BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code in (
                429,
                500,
                502,
                503,
                504,
            ):
                last_error = (
                    f"API temporary error: "
                    f"{response.status_code}"
                )
                time.sleep(2 + attempt)
                continue

            response.raise_for_status()

            data = response.json()

            answer = data["choices"][0]["message"]["content"]

            if isinstance(answer, list):
                answer = " ".join(
                    str(x.get("text", x))
                    if isinstance(x, dict)
                    else str(x)
                    for x in answer
                )

            return str(answer).strip()

        except Exception as e:
            last_error = str(e)
            time.sleep(2 + attempt)

    raise RuntimeError(
        last_error or "Unknown API error"
    )


def show_models(chat_id):
    send(
        chat_id,
        "🧠 Выбери модель:",
        models_keyboard()
    )


def show_settings(chat_id, chat):
    style = chat.get("style", "normal")

    send(
        chat_id,
        f"⚙️ Настройки\n\n"
        f"Текущий стиль: {style}\n\n"
        "Выбери стиль ответа:",
        settings_keyboard()
    )


def show_stats(chat_id, chat):
    model = chat.get("model", MODELS["mini"])

    text = (
        "📊 СТАТИСТИКА BULBAMAXAI\n\n"
        f"🤖 Модель: {model}\n"
        f"💬 Твоих запросов: {chat['requests']}\n"
        f"🌐 Всего запросов: {db['total_requests']}\n"
        f"❌ Ошибок: {db['total_errors']}\n"
        f"🧠 История: {len(chat['history'])} сообщений\n"
        f"💾 Фактов в памяти: {len(chat['profile'])}"
    )

    send(
        chat_id,
        text,
        main_keyboard()
    )


def show_memory(chat_id, chat):
    profile = chat.get("profile", {})

    if not profile:
        send(
            chat_id,
            "💾 Память пока пустая.\n\n"
            "Пример:\n"
            "/remember имя=Баня\n"
            "/remember город=Каспийск",
            main_keyboard()
        )
        return

    lines = ["💾 ПАМЯТЬ\n"]

    for key, value in profile.items():
        lines.append(f"• {key}: {value}")

    send(
        chat_id,
        "\n".join(lines),
        main_keyboard()
    )


def help_message(chat_id):
    text = (
        "🤖 BULBAMAXAI\n\n"
        "Команды:\n\n"
        "/start — запуск\n"
        "/help — помощь\n"
        "/menu — меню\n"
        "/new — новый чат\n"
        "/clear — очистить историю\n"
        "/stats — статистика\n"
        "/models — модели\n"
        "/settings — настройки\n"
        "/memory — память\n"
        "/remember имя=значение — запомнить\n"
        "/forget имя — удалить факт\n\n"
        "Просто напиши сообщение, чтобы поговорить с AI."
    )

    send(
        chat_id,
        text,
        main_keyboard()
    )


def handle_message(message):
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]

    text = message.get("text", "").strip()

    if not text:
        return

    chat = get_chat(user_id)

    if text == "/start":
        send(
            chat_id,
            "🚀 BULBAMAXAI V12\n\n"
            "AI успешно запущен.\n\n"
            "Я умею:\n"
            "🧠 помнить контекст\n"
            "🤖 переключать модели\n"
            "💾 хранить память\n"
            "📊 показывать статистику\n"
            "⚙️ менять стиль ответов\n\n"
            "Просто напиши мне сообщение.",
            main_keyboard()
        )
        return

    if text in ("/help",):
        help_message(chat_id)
        return

    if text in ("/menu",):
        send(
            chat_id,
            "📋 Главное меню",
            main_keyboard()
        )
        return

    if text in ("/models", "🧠 Модель"):
        show_models(chat_id)
        return

    if text in ("/settings", "⚙️ Настройки"):
        show_settings(chat_id, chat)
        return

    if text in ("/stats", "📊 Статистика"):
        show_stats(chat_id, chat)
        return

    if text in ("/memory", "💾 Память"):
        show_memory(chat_id, chat)
        return

    if text in ("/new", "🆕 Новый чат"):
        chat["history"] = []
        save_db()

        send(
            chat_id,
            "🆕 Новый чат создан!\n"
            "Старая история разговора очищена.",
            main_keyboard()
        )
        return

    if text in ("/clear", "🧹 Очистить"):
        chat["history"] = []
        save_db()

        send(
            chat_id,
            "🧹 История очищена.",
            main_keyboard()
        )
        return

    if text.startswith("/remember "):
        value = text[len("/remember "):].strip()

        if "=" not in value:
            send(
                chat_id,
                "❗ Используй:\n"
                "/remember имя=значение",
                main_keyboard()
            )
            return

        key, val = value.split("=", 1)

        key = key.strip()
        val = val.strip()

        if not key or not val:
            send(
                chat_id,
                "❗ Имя и значение не должны быть пустыми.",
                main_keyboard()
            )
            return

        chat["profile"][key] = val

        save_db()

        send(
            chat_id,
            f"✅ Запомнил:\n{key} = {val}",
            main_keyboard()
        )
        return

    if text.startswith("/forget "):
        key = text[len("/forget "):].strip()

        if key in chat["profile"]:
            del chat["profile"][key]
            save_db()

            send(
                chat_id,
                f"🗑 Удалил из памяти: {key}",
                main_keyboard()
            )
        else:
            send(
                chat_id,
                "Такого факта в памяти нет.",
                main_keyboard()
            )

        return

    if not API_KEY:
        send(
            chat_id,
            "❌ API_KEY не настроен на хостинге.",
            main_keyboard()
        )
        return

    if not rate_limit(chat):
        send(
            chat_id,
            "⏳ Слишком много запросов подряд.\n"
            "Подожди несколько секунд.",
            main_keyboard()
        )
        return

    chat["history"].append({
        "role": "user",
        "content": text,
    })

    try:
        answer = ask_ai(chat, text)

        chat["history"].append({
            "role": "assistant",
            "content": answer,
        })

        chat["history"] = chat["history"][-MAX_HISTORY:]

        chat["requests"] += 1
        db["total_requests"] += 1

        save_db()

        send(
            chat_id,
            answer,
            main_keyboard()
        )

    except Exception as error:
        print("AI ERROR:", error)

        chat["errors"] += 1
        db["total_errors"] += 1

        if (
            chat["history"]
            and chat["history"][-1]["role"] == "user"
        ):
            chat["history"].pop()

        save_db()

        send(
            chat_id,
            "⚠️ AI временно не смог ответить.\n\n"
            "Попробуй отправить сообщение ещё раз.",
            main_keyboard()
        )


def handle_callback(callback):
    callback_id = callback["id"]

    data = callback.get("data", "")

    message = callback.get("message")

    if not message:
        callback_answer(callback_id)
        return

    chat_id = message["chat"]["id"]
    user_id = callback["from"]["id"]

    chat = get_chat(user_id)

    if data.startswith("model:"):
        key = data.split(":", 1)[1]

        if key in MODELS:
            chat["model"] = MODELS[key]

            save_db()

            edit(
                chat_id,
                message["message_id"],
                "✅ Модель изменена:\n\n"
                + MODEL_NAMES[key],
                models_keyboard()
            )

        callback_answer(callback_id)
        return

    if data.startswith("style:"):
        style = data.split(":", 1)[1]

        if style in STYLES:
            chat["style"] = style

            save_db()

            edit(
                chat_id,
                message["message_id"],
                f"✅ Стиль изменён: {style}",
                settings_keyboard()
            )

        callback_answer(callback_id)
        return

    if data == "back":
        edit(
            chat_id,
            message["message_id"],
            "📋 Главное меню"
        )

        send(
            chat_id,
            "📋 Главное меню",
            main_keyboard()
        )

        callback_answer(callback_id)
        return

    callback_answer(callback_id)


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не задан"
        )

    if not API_KEY:
        raise RuntimeError(
            "API_KEY не задан"
        )

    load_db()

    print("================================")
    print("      BULBAMAXAI V12 START")
    print("================================")

    offset = None

    while True:
        try:
            params = {
                "timeout": POLL_TIMEOUT,
                "allowed_updates": [
                    "message",
                    "callback_query"
                ],
            }

            if offset is not None:
                params["offset"] = offset

            response = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params=params,
                timeout=40,
            )

            response.raise_for_status()

            updates = response.json().get(
                "result",
                []
            )

            for update in updates:
                offset = update["update_id"] + 1

                try:
                    if "message" in update:
                        handle_message(
                            update["message"]
                        )

                    elif "callback_query" in update:
                        handle_callback(
                            update["callback_query"]
                        )

                except Exception as error:
                    print(
                        "UPDATE ERROR:",
                        error
                    )

        except Exception as error:
            print(
                "POLLING ERROR:",
                error
            )

            time.sleep(3)


if __name__ == "__main__":
    main()
