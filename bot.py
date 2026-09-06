import base64
import json
import logging
import os
import time
from typing import Any, Optional

import requests


# =========================================================
# 🔐 CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()

BASE_URL = "https://api.baza-ai.org/v1"

DEFAULT_MODEL = "gpt-5.4-mini"

DATA_FILE = "v11_memory.json"

MAX_HISTORY = 16
MAX_TEXT = 3900
MAX_IMAGE_BYTES = 10 * 1024 * 1024

POLL_TIMEOUT = 25
REQUEST_TIMEOUT = 90
TELEGRAM_TIMEOUT = 35

MAX_RETRIES = 3


MODELS = {
    "mini": "gpt-5.4-mini",
    "luna": "gpt-5.6-luna",
    "terra": "gpt-5.6-terra",
    "sol": "gpt-5.6-sol",
    "gpt55": "gpt-5.5",
}


TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_FILE = f"https://api.telegram.org/file/bot{BOT_TOKEN}"


SYSTEM_PROMPT = (
    "Ты умный AI-ассистент в Telegram. "
    "Отвечай на языке пользователя. "
    "Учитывай контекст диалога. "
    "Отвечай полезно, точно и без лишней воды. "
    "Если не уверен в факте, честно скажи об этом."
)


VISION_PROMPT = (
    "Ты AI-ассистент с поддержкой анализа изображений. "
    "Отвечай на языке пользователя. "
    "Внимательно анализируй изображение и не выдумывай "
    "детали, которых на нём нельзя уверенно определить."
)


# =========================================================
# 📝 LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("V11MAX")


# =========================================================
# 💾 DATABASE
# =========================================================

db = {
    "chats": {},
    "total_requests": 0,
    "total_errors": 0
}


def load_db():

    global db

    if not os.path.exists(DATA_FILE):
        log.info("Файл памяти отсутствует. Создаём новую память.")
        return

    try:

        with open(
            DATA_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            saved = json.load(f)

        if not isinstance(saved, dict):
            raise ValueError("Некорректный формат базы")

        if isinstance(saved.get("chats"), dict):
            db["chats"] = saved["chats"]

        if isinstance(
            saved.get("total_requests"),
            int
        ):
            db["total_requests"] = saved[
                "total_requests"
            ]

        if isinstance(
            saved.get("total_errors"),
            int
        ):
            db["total_errors"] = saved[
                "total_errors"
            ]

        log.info("💾 Память загружена.")

    except Exception:

        log.exception(
            "❌ Не удалось загрузить память."
        )


def save_db():

    temp_file = DATA_FILE + ".tmp"

    try:

        with open(
            temp_file,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                db,
                f,
                ensure_ascii=False,
                indent=2
            )

        os.replace(
            temp_file,
            DATA_FILE
        )

    except Exception:

        log.exception(
            "❌ Ошибка сохранения памяти."
        )

        try:

            if os.path.exists(temp_file):
                os.remove(temp_file)

        except Exception:
            pass


def get_chat(chat_id):

    key = str(chat_id)

    if (
        key not in db["chats"]
        or
        not isinstance(
            db["chats"][key],
            dict
        )
    ):

        db["chats"][key] = {

            "model":
                DEFAULT_MODEL,

            "history":
                [],

            "requests":
                0,

            "errors":
                0,

            "time":
                0.0
        }

    chat = db["chats"][key]

    chat.setdefault(
        "model",
        DEFAULT_MODEL
    )

    chat.setdefault(
        "history",
        []
    )

    chat.setdefault(
        "requests",
        0
    )

    chat.setdefault(
        "errors",
        0
    )

    chat.setdefault(
        "time",
        0.0
    )

    if not isinstance(
        chat["history"],
        list
    ):
        chat["history"] = []

    return chat


def clear_history(chat_id):

    get_chat(chat_id)["history"] = []

    save_db()


# =========================================================
# 📡 TELEGRAM API
# =========================================================

def telegram(
    method,
    payload=None,
    files=None,
    timeout=TELEGRAM_TIMEOUT
):

    try:

        response = requests.post(

            f"{TG}/{method}",

            data=payload or {},

            files=files,

            timeout=timeout
        )

        try:

            data = response.json()

        except ValueError:

            data = {
                "ok": False,
                "error":
                    f"Telegram HTTP {response.status_code}"
            }

        if not data.get("ok"):

            log.warning(
                "Telegram %s error: %s",
                method,
                str(data)[:500]
            )

        return data

    except requests.RequestException as e:

        log.warning(
            "Telegram %s network error: %s",
            method,
            e
        )

        return {
            "ok": False,
            "error": str(e)
        }


# =========================================================
# 📤 SEND MESSAGE
# =========================================================

def send_message(
    chat_id,
    text
):

    if not text:
        return

    text = str(text)

    while len(text) > MAX_TEXT:

        cut = text.rfind(
            "\n",
            0,
            MAX_TEXT
        )

        if cut < 500:
            cut = MAX_TEXT

        telegram(
            "sendMessage",
            {
                "chat_id":
                    chat_id,

                "text":
                    text[:cut]
            }
        )

        text = text[cut:].lstrip()

    if text:

        telegram(
            "sendMessage",
            {
                "chat_id":
                    chat_id,

                "text":
                    text
            }
        )


def typing(chat_id):

    telegram(
        "sendChatAction",
        {
            "chat_id":
                chat_id,

            "action":
                "typing"
        },
        timeout=10
    )


# =========================================================
# 🎛️ MAIN MENU
# =========================================================

def show_menu(chat_id):

    keyboard = {

        "inline_keyboard": [

            [
                {
                    "text":
                        "🧠 Модель",

                    "callback_data":
                        "models"
                },

                {
                    "text":
                        "📊 Статистика",

                    "callback_data":
                        "stats"
                }
            ],

            [
                {
                    "text":
                        "🧹 Очистить",

                    "callback_data":
                        "clear"
                },

                {
                    "text":
                        "🆕 Новый чат",

                    "callback_data":
                        "new"
                }
            ],

            [
                {
                    "text":
                        "⚙️ Настройки",

                    "callback_data":
                        "settings"
                }
            ]
        ]
    }

    telegram(
        "sendMessage",
        {
            "chat_id":
                chat_id,

            "text":
                "🎛️ V11 MAX\n\n"
                "Выбирай действие:",

            "reply_markup":
                json.dumps(
                    keyboard,
                    ensure_ascii=False
                )
        }
    )


# =========================================================
# 🧠 MODELS MENU
# =========================================================

def show_models(chat_id):

    keyboard = {

        "inline_keyboard": [

            [
                {
                    "text":
                        "⚡ Mini",

                    "callback_data":
                        "model:mini"
                },

                {
                    "text":
                        "🧠 Luna",

                    "callback_data":
                        "model:luna"
                }
            ],

            [
                {
                    "text":
                        "🚀 Terra",

                    "callback_data":
                        "model:terra"
                },

                {
                    "text":
                        "🔥 Sol",

                    "callback_data":
                        "model:sol"
                }
            ],

            [
                {
                    "text":
                        "🧩 GPT-5.5",

                    "callback_data":
                        "model:gpt55"
                }
            ],

            [
                {
                    "text":
                        "⬅️ Меню",

                    "callback_data":
                        "menu"
                }
            ]
        ]
    }

    telegram(
        "sendMessage",
        {
            "chat_id":
                chat_id,

            "text":
                "🧠 Выбери модель:",

            "reply_markup":
                json.dumps(
                    keyboard,
                    ensure_ascii=False
                )
        }
    )


# =========================================================
# 🧠 BAZAAI
# =========================================================

def api_headers():

    return {

        "Authorization":
            f"Bearer {API_KEY}",

        "Content-Type":
            "application/json"
    }


def extract_answer(data):

    try:

        answer = (
            data
            ["choices"]
            [0]
            ["message"]
            ["content"]
        )

    except (
        KeyError,
        IndexError,
        TypeError
    ):

        raise ValueError(
            "Неожиданный формат ответа BazaAI"
        )

    if isinstance(
        answer,
        str
    ):

        return answer.strip()

    if isinstance(
        answer,
        list
    ):

        parts = []

        for item in answer:

            if (
                isinstance(item, dict)
                and
                isinstance(
                    item.get("text"),
                    str
                )
            ):

                parts.append(
                    item["text"]
                )

            elif isinstance(
                item,
                str
            ):

                parts.append(item)

        return "\n".join(parts).strip()

    return str(answer).strip()


def is_retryable(status_code):

    return status_code in {
        429,
        500,
        502,
        503,
        504
    }


def call_bazaai(
    model,
    messages
):

    last_error = "unknown error"

    for attempt in range(
        1,
        MAX_RETRIES + 1
    ):

        started = time.time()

        try:

            response = requests.post(

                f"{BASE_URL}/chat/completions",

                headers=api_headers(),

                json={
                    "model":
                        model,

                    "messages":
                        messages
                },

                timeout=REQUEST_TIMEOUT
            )

            elapsed = (
                time.time()
                - started
            )

            if response.status_code == 200:

                data = response.json()

                answer = extract_answer(
                    data
                )

                if not answer:

                    raise ValueError(
                        "BazaAI вернул пустой ответ"
                    )

                log.info(
                    "BazaAI OK | model=%s | attempt=%s | %.2fs",
                    model,
                    attempt,
                    elapsed
                )

                return answer

            last_error = (
                f"HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

            log.warning(
                "BazaAI ERROR | model=%s | attempt=%s | %s",
                model,
                attempt,
                last_error
            )

            if not is_retryable(
                response.status_code
            ):
                break

        except (
            requests.RequestException,
            ValueError
        ) as e:

            last_error = repr(e)

            log.warning(
                "BazaAI EXCEPTION | model=%s | attempt=%s | %s",
                model,
                attempt,
                last_error
            )

        if attempt < MAX_RETRIES:

            time.sleep(
                1.2 * attempt
            )

    raise RuntimeError(
        last_error
    )


# =========================================================
# 🤖 TEXT AI
# =========================================================

def ask_ai(
    chat_id,
    content
):

    user = get_chat(chat_id)

    history = user["history"]

    user_message = {

        "role":
            "user",

        "content":
            content
    }

    history.append(
        user_message
    )

    history[:] = (
        history[-MAX_HISTORY:]
    )

    messages = [

        {
            "role":
                "system",

            "content":
                SYSTEM_PROMPT
        }

    ]

    messages.extend(history)

    model = user["model"]

    started = time.time()

    try:

        answer = call_bazaai(
            model,
            messages
        )

        history.append(
            {
                "role":
                    "assistant",

                "content":
                    answer
            }
        )

        history[:] = (
            history[-MAX_HISTORY:]
        )

        elapsed = (
            time.time()
            - started
        )

        user["requests"] += 1

        user["time"] += elapsed

        db["total_requests"] += 1

        save_db()

        return answer

    except Exception:

        log.exception(
            "Text AI request failed"
        )

        if (
            history
            and
            history[-1] is user_message
        ):

            history.pop()

        user["errors"] += 1

        db["total_errors"] += 1

        save_db()

        return (
            "❌ AI временно не смог ответить.\n\n"
            "Попробуй ещё раз."
        )


# =========================================================
# 🖼️ DOWNLOAD TELEGRAM PHOTO
# =========================================================

def download_telegram_file(
    file_id
):

    result = telegram(
        "getFile",
        {
            "file_id":
                file_id
        }
    )

    if not result.get("ok"):
        return None

    file_path = (
        result
        .get("result", {})
        .get("file_path")
    )

    if not file_path:
        return None

    try:

        response = requests.get(

            f"{TG_FILE}/{file_path}",

            timeout=30
        )

        response.raise_for_status()

        if len(
            response.content
        ) > MAX_IMAGE_BYTES:

            log.warning(
                "Image too large."
            )

            return None

        return response.content

    except requests.RequestException as e:

        log.warning(
            "Photo download error: %s",
            e
        )

        return None


# =========================================================
# 🖼️ VISION
# =========================================================

def ask_image(
    chat_id,
    image_bytes,
    caption
):

    user = get_chat(chat_id)

    encoded = base64.b64encode(
        image_bytes
    ).decode("ascii")

    image_url = (
        "data:image/jpeg;base64,"
        + encoded
    )

    if (
        isinstance(caption, str)
        and
        caption.strip()
    ):

        question = caption.strip()

    else:

        question = (
            "Проанализируй это изображение "
            "и подробно расскажи, что на нём."
        )

    content = [

        {
            "type":
                "text",

            "text":
                question
        },

        {
            "type":
                "image_url",

            "image_url":
                {
                    "url":
                        image_url
                }
        }
    ]

    user_message = {

        "role":
            "user",

        "content":
            content
    }

    history = user["history"]

    history.append(
        user_message
    )

    history[:] = (
        history[-MAX_HISTORY:]
    )

    messages = [

        {
            "role":
                "system",

            "content":
                VISION_PROMPT
        }

    ]

    messages.extend(history)

    try:

        answer = call_bazaai(

            user["model"],

            messages
        )

        history.append(
            {
                "role":
                    "assistant",

                "content":
                    answer
            }
        )

        history[:] = (
            history[-MAX_HISTORY:]
        )

        user["requests"] += 1

        db["total_requests"] += 1

        save_db()

        return answer

    except Exception:

        log.exception(
            "Vision request failed"
        )

        if (
            history
            and
            history[-1] is user_message
        ):

            history.pop()

        user["errors"] += 1

        db["total_errors"] += 1

        save_db()

        return (
            "❌ Не удалось обработать изображение.\n\n"
            "Возможно, выбранная модель "
            "не поддерживает анализ изображений."
        )


# =========================================================
# 📊 STATISTICS
# =========================================================

def get_stats(chat_id):

    user = get_chat(chat_id)

    requests_count = int(
        user.get(
            "requests",
            0
        )
    )

    if requests_count:

        average = (
            float(
                user.get(
                    "time",
                    0
                )
            )
            /
            requests_count
        )

    else:

        average = 0

    return (

        "📊 V11 MAX\n\n"

        f"🧠 Модель: "
        f"{user['model']}\n"

        f"💾 Память: "
        f"{len(user['history'])}/"
        f"{MAX_HISTORY}\n"

        f"📨 Твоих запросов: "
        f"{requests_count}\n"

        f"❌ Ошибок: "
        f"{user.get('errors', 0)}\n"

        f"⏱️ Среднее время: "
        f"{average:.2f} сек.\n\n"

        f"🌍 Всего запросов: "
        f"{db['total_requests']}\n"

        f"❌ Всего ошибок: "
        f"{db['total_errors']}"
    )


# =========================================================
# ⚙️ SETTINGS
# =========================================================

def get_settings(chat_id):

    user = get_chat(chat_id)

    return (

        "⚙️ НАСТРОЙКИ\n\n"

        f"🧠 Модель: "
        f"{user['model']}\n"

        f"💾 Память: "
        f"{len(user['history'])}/"
        f"{MAX_HISTORY}\n"

        "📡 API: BazaAI\n\n"

        "Смена модели:\n"

        "/model mini\n"
        "/model luna\n"
        "/model terra\n"
        "/model sol\n"
        "/model gpt55"
    )


# =========================================================
# 📨 MESSAGE HANDLER
# =========================================================

def handle_message(message):

    chat = message.get(
        "chat",
        {}
    )

    chat_id = chat.get("id")

    if chat_id is None:
        return

    text = message.get("text")


    # =====================================================
    # START
    # =====================================================

    if text == "/start":

        send_message(

            chat_id,

            "🤖 V11 MAX ONLINE\n\n"

            "✅ AI подключён\n"
            "✅ Память включена\n"
            "✅ Модели подключены\n"
            "✅ Фото-анализ готов\n\n"

            "Напиши сообщение "
            "или открой /menu."
        )

        return


    # =====================================================
    # MENU
    # =====================================================

    if text == "/menu":

        show_menu(chat_id)

        return


    # =====================================================
    # HELP
    # =====================================================

    if text == "/help":

        send_message(

            chat_id,

            "🤖 V11 MAX\n\n"

            "/menu — главное меню\n"
            "/model — модели\n"
            "/clear — очистить память\n"
            "/newchat — новый чат\n"
            "/stats — статистика\n"
            "/settings — настройки\n"
            "/id — Telegram ID\n\n"

            "🖼️ Отправь фотографию — "
            "бот попробует её "
            "проанализировать."
        )

        return


    # =====================================================
    # CLEAR
    # =====================================================

    if text in (
        "/clear",
        "/newchat"
    ):

        clear_history(
            chat_id
        )

        send_message(

            chat_id,

            "🧹 История очищена.\n\n"
            "🆕 Новый диалог готов!"
        )

        return


    # =====================================================
    # STATS
    # =====================================================

    if text == "/stats":

        send_message(

            chat_id,

            get_stats(chat_id)
        )

        return


    # =====================================================
    # SETTINGS
    # =====================================================

    if text == "/settings":

        send_message(

            chat_id,

            get_settings(chat_id)
        )

        return


    # =====================================================
    # ID
    # =====================================================

    if text == "/id":

        send_message(

            chat_id,

            f"🆔 Твой Telegram ID:\n{chat_id}"
        )

        return


    # =====================================================
    # MODEL
    # =====================================================

    if text == "/model":

        show_models(chat_id)

        return


    # =====================================================
    # MODEL CHANGE
    # =====================================================

    if (
        isinstance(text, str)
        and
        text.startswith("/model ")
    ):

        selected = (
            text
            .split(
                maxsplit=1
            )[1]
            .strip()
            .lower()
        )

        if selected not in MODELS:

            send_message(

                chat_id,

                "❌ Такой модели нет.\n\n"

                "Доступно:\n"

                "/model mini\n"
                "/model luna\n"
                "/model terra\n"
                "/model sol\n"
                "/model gpt55"
            )

            return

        get_chat(
            chat_id
        )["model"] = MODELS[
            selected
        ]

        save_db()

        send_message(

            chat_id,

            "✅ Модель изменена:\n\n"
            + MODELS[selected]
        )

        return


    # =====================================================
    # PHOTO
    # =====================================================

    photos = message.get(
        "photo"
    )

    if photos:

        try:

            file_id = (
                photos[-1]
                ["file_id"]
            )

        except (
            IndexError,
            KeyError,
            TypeError
        ):

            send_message(
                chat_id,
                "❌ Не удалось определить фотографию."
            )

            return


        typing(chat_id)


        image = download_telegram_file(
            file_id
        )


        if not image:

            send_message(

                chat_id,

                "❌ Не удалось получить фото "
                "или файл слишком большой."
            )

            return


        answer = ask_image(

            chat_id,

            image,

            message.get(
                "caption"
            )
        )


        send_message(
            chat_id,
            answer
        )

        return


    # =====================================================
    # VOICE
    # =====================================================

    if (
        "voice" in message
        or
        "audio" in message
    ):

        send_message(

            chat_id,

            "🎤 Голосовые сообщения "
            "пока не поддерживаются.\n\n"

            "Отправь текст "
            "или фотографию."
        )

        return


    # =====================================================
    # OTHER
    # =====================================================

    if not text:

        send_message(

            chat_id,

            "Я пока умею работать "
            "с текстом и фотографиями."
        )

        return


    # =====================================================
    # TEXT
    # =====================================================

    typing(chat_id)

    answer = ask_ai(
        chat_id,
        text
    )

    send_message(
        chat_id,
        answer
    )


# =========================================================
# 🔘 CALLBACKS
# =========================================================

def handle_callback(query):

    callback_id = query.get(
        "id"
    )

    data = query.get(
        "data",
        ""
    )

    message = query.get(
        "message",
        {}
    )

    chat_id = (
        message
        .get("chat", {})
        .get("id")
    )


    if callback_id:

        telegram(

            "answerCallbackQuery",

            {
                "callback_query_id":
                    callback_id
            }
        )


    if chat_id is None:
        return


    if data == "menu":

        show_menu(chat_id)

        return


    if data == "models":

        show_models(chat_id)

        return


    if data == "stats":

        send_message(
            chat_id,
            get_stats(chat_id)
        )

        return


    if data == "settings":

        send_message(
            chat_id,
            get_settings(chat_id)
        )

        return


    if data in (
        "clear",
        "new"
    ):

        clear_history(
            chat_id
        )

        send_message(

            chat_id,

            "🧹 История очищена.\n\n"
            "🆕 Новый диалог готов!"
        )

        return


    if data.startswith(
        "model:"
    ):

        selected = data.split(
            ":",
            1
        )[1]


        if selected not in MODELS:
            return


        get_chat(
            chat_id
        )["model"] = MODELS[
            selected
        ]


        save_db()


        send_message(

            chat_id,

            "✅ Модель:\n"
            + MODELS[selected]
        )


# =========================================================
# 📡 GET UPDATES
# =========================================================

def get_updates(offset):

    try:

        response = requests.get(

            f"{TG}/getUpdates",

            params={

                "offset":
                    offset,

                "timeout":
                    POLL_TIMEOUT,

                "allowed_updates":
                    json.dumps(
                        [
                            "message",
                            "callback_query"
                        ]
                    )
            },

            timeout=
                POLL_TIMEOUT + 10
        )


        try:

            return response.json()

        except ValueError:

            return {
                "ok":
                    False
            }


    except requests.RequestException as e:

        log.warning(
            "Polling error: %s",
            e
        )

        return {
            "ok":
                False
        }


# =========================================================
# 🩺 STARTUP
# =========================================================

def validate_config():

    missing = []

    if not BOT_TOKEN:
        missing.append(
            "BOT_TOKEN"
        )

    if not API_KEY:
        missing.append(
            "API_KEY"
        )

    if missing:

        raise RuntimeError(

            "Не заданы переменные окружения: "
            +
            ", ".join(missing)
        )


def telegram_startup_check():

    result = telegram(
        "getMe",
        timeout=15
    )

    if not result.get("ok"):

        raise RuntimeError(
            "BOT_TOKEN неправильный "
            "или Telegram недоступен."
        )

    bot = result.get(
        "result",
        {}
    )

    log.info(
        "Telegram bot: @%s",
        bot.get(
            "username",
            "unknown"
        )
    )


def bazaai_startup_check():

    try:

        response = requests.get(

            f"{BASE_URL}/models",

            headers={
                "Authorization":
                    f"Bearer {API_KEY}"
            },

            timeout=20
        )

        if response.status_code == 200:

            log.info(
                "BazaAI API: OK"
            )

        elif response.status_code == 401:

            raise RuntimeError(
                "API_KEY BazaAI недействителен."
            )

        else:

            log.warning(
                "BazaAI /models returned HTTP %s",
                response.status_code
            )

    except requests.RequestException as e:

        log.warning(
            "BazaAI startup check failed: %s",
            e
        )


# =========================================================
# 🚀 MAIN
# =========================================================

def main():

    validate_config()

    load_db()


    log.info(
        "================================"
    )

    log.info(
        "🤖 AI TELEGRAM BOT V11 MAX"
    )

    log.info(
        "================================"
    )

    log.info(
        "⚡ BazaAI: %s",
        BASE_URL
    )

    log.info(
        "🧠 Default model: %s",
        DEFAULT_MODEL
    )

    log.info(
        "💾 Memory: %s",
        DATA_FILE
    )


    telegram_startup_check()

    bazaai_startup_check()


    log.info(
        "🟢 ONLINE"
    )

    log.info(
        "================================"
    )


    offset = None


    while True:

        try:

            data = get_updates(
                offset
            )


            if not data.get("ok"):

                time.sleep(3)

                continue


            updates = data.get(
                "result",
                []
            )


            for update in updates:

                update_id = update.get(
                    "update_id"
                )


                if isinstance(
                    update_id,
                    int
                ):

                    offset = (
                        update_id + 1
                    )


                try:

                    if (
                        "callback_query"
                        in update
                    ):

                        handle_callback(
                            update[
                                "callback_query"
                            ]
                        )

                    elif (
                        "message"
                        in update
                    ):

                        handle_message(
                            update[
                                "message"
                            ]
                        )


                except Exception:

                    log.exception(
                        "Update processing error"
                    )


                    try:

                        cid = (

                            update
                            .get(
                                "message",
                                {}
                            )
                            .get(
                                "chat",
                                {}
                            )
                            .get(
                                "id"
                            )
                        )


                        if cid:

                            send_message(

                                cid,

                                "❌ Внутренняя ошибка. "
                                "Попробуй ещё раз."
                            )


                    except Exception:

                        log.exception(
                            "Failed to send error message"
                        )


        except KeyboardInterrupt:

            log.info(
                "🛑 Бот остановлен."
            )

            break


        except Exception:

            log.exception(
                "Main loop error"
            )

            time.sleep(3)


# =========================================================
# ▶️ RUN
# =========================================================

if __name__ == "__main__":
    main()
