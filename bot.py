import os
import json
import time
import base64
import hashlib
from pathlib import Path

import requests


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()

BASE_URL = "https://api.baza-ai.org/v1"
TG_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

DATA_FILE = "v13_memory.json"

MAX_HISTORY = 20
MAX_CHATS = 20

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILE_TEXT = 30000

MAX_REPLY = 3900

POLL_TIMEOUT = 25
API_TIMEOUT = 90

RATE_LIMIT_COUNT = 5
RATE_LIMIT_WINDOW = 10


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not API_KEY:
    raise RuntimeError("API_KEY is not set")


# ============================================================
# HTTP SESSIONS
# ============================================================

tg_session = requests.Session()

api_session = requests.Session()

api_session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
})


# ============================================================
# DATABASE
# ============================================================

db = {
    "users": {},
    "total_requests": 0,
    "total_errors": 0
}


def load_db():
    global db

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)

        if isinstance(loaded, dict):
            db.update(loaded)

        db.setdefault("users", {})
        db.setdefault("total_requests", 0)
        db.setdefault("total_errors", 0)

    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass


def save_db():
    try:
        temp_file = DATA_FILE + ".tmp"

        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(
                db,
                f,
                ensure_ascii=False,
                indent=2
            )

        os.replace(temp_file, DATA_FILE)

    except OSError as e:
        print("DB save error:", e)


def default_chat():
    return {
        "history": [],
        "requests": 0,
        "created": int(time.time()),
        "last_prompt": None
    }


def default_user():
    return {
        "model": "auto",
        "style": "normal",

        "memory": {},

        "chats": {
            "main": default_chat()
        },

        "active_chat": "main",

        "requests": 0,
        "errors": 0,

        "rate": []
    }


def get_user(user_id):
    uid = str(user_id)

    if uid not in db["users"]:
        db["users"][uid] = default_user()

    user = db["users"][uid]

    user.setdefault("model", "auto")
    user.setdefault("style", "normal")
    user.setdefault("memory", {})

    user.setdefault(
        "chats",
        {
            "main": default_chat()
        }
    )

    user.setdefault("active_chat", "main")
    user.setdefault("requests", 0)
    user.setdefault("errors", 0)
    user.setdefault("rate", [])

    if not user["chats"]:
        user["chats"]["main"] = default_chat()
        user["active_chat"] = "main"

    return user


def get_chat(user):
    name = user.get(
        "active_chat",
        "main"
    )

    if name not in user["chats"]:
        user["chats"][name] = default_chat()

    return user["chats"][name]


# ============================================================
# RATE LIMIT
# ============================================================

def clean_rate(user):
    now = time.time()

    user["rate"] = [
        timestamp
        for timestamp in user.get("rate", [])
        if now - timestamp < RATE_LIMIT_WINDOW
    ]


def allowed_request(user):
    clean_rate(user)

    if len(user["rate"]) >= RATE_LIMIT_COUNT:
        return False

    user["rate"].append(time.time())

    return True


# ============================================================
# TELEGRAM API
# ============================================================

def tg(method, data=None, timeout=40):
    try:
        response = tg_session.post(
            f"{TG_URL}/{method}",
            data=data or {},
            timeout=timeout
        )

        if not response.ok:
            print(
                "Telegram HTTP:",
                response.status_code,
                response.text[:500]
            )
            return None

        payload = response.json()

        if not payload.get("ok"):
            print(
                "Telegram API:",
                payload
            )
            return None

        return payload.get("result")

    except Exception as e:
        print(
            "Telegram error:",
            repr(e)
        )
        return None


def send_message(
    chat_id,
    text,
    keyboard=None
):
    if not text:
        text = "..."

    chunks = [
        text[i:i + MAX_REPLY]
        for i in range(
            0,
            len(text),
            MAX_REPLY
        )
    ]

    first_message = None

    for index, chunk in enumerate(chunks):

        data = {
            "chat_id": chat_id,
            "text": chunk
        }

        if (
            keyboard
            and index == len(chunks) - 1
        ):
            data["reply_markup"] = json.dumps(
                keyboard,
                ensure_ascii=False
            )

        result = tg(
            "sendMessage",
            data
        )

        if first_message is None:
            first_message = result

    return first_message


def edit_message(
    chat_id,
    message_id,
    text,
    keyboard=None
):
    if len(text) <= MAX_REPLY:

        data = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text
        }

        if keyboard:
            data["reply_markup"] = json.dumps(
                keyboard,
                ensure_ascii=False
            )

        return tg(
            "editMessageText",
            data
        )

    first = text[:MAX_REPLY]
    rest = text[MAX_REPLY:]

    tg(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": first
        }
    )

    return send_message(
        chat_id,
        rest,
        keyboard
    )


def answer_callback(
    callback_id,
    text=None
):
    data = {
        "callback_query_id": callback_id
    }

    if text:
        data["text"] = text

    tg(
        "answerCallbackQuery",
        data
    )


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():
    return {
        "keyboard": [
            [
                {
                    "text": "🤖 Авто"
                },
                {
                    "text": "🧠 Модель"
                }
            ],
            [
                {
                    "text": "📊 Статистика"
                },
                {
                    "text": "💾 Память"
                }
            ],
            [
                {
                    "text": "💬 Чаты"
                },
                {
                    "text": "🆕 Новый чат"
                }
            ],
            [
                {
                    "text": "🧹 Очистить"
                },
                {
                    "text": "⚙️ Настройки"
                }
            ]
        ],
        "resize_keyboard": True
    }


def settings_keyboard(style):
    def mark(value, label):
        if style == value:
            return "✅ " + label

        return label

    return {
        "inline_keyboard": [
            [
                {
                    "text": mark(
                        "normal",
                        "🧠 Обычно"
                    ),
                    "callback_data": "style:normal"
                }
            ],
            [
                {
                    "text": mark(
                        "short",
                        "⚡ Кратко"
                    ),
                    "callback_data": "style:short"
                }
            ],
            [
                {
                    "text": mark(
                        "detailed",
                        "📚 Подробно"
                    ),
                    "callback_data": "style:detailed"
                }
            ],
            [
                {
                    "text": "⬅️ Назад",
                    "callback_data": "back"
                }
            ]
        ]
    }


def memory_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🗑 Очистить память",
                    "callback_data": "memory_clear"
                }
            ],
            [
                {
                    "text": "⬅️ Назад",
                    "callback_data": "back"
                }
            ]
        ]
    }


def retry_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🔄 Повторить",
                    "callback_data": "retry"
                }
            ]
        ]
    }


# ============================================================
# MODELS
# ============================================================

_model_cache = {
    "time": 0,
    "models": []
}


def get_models(force=False):
    now = time.time()

    if (
        not force
        and _model_cache["models"]
        and now - _model_cache["time"] < 60
    ):
        return _model_cache["models"]

    try:
        response = api_session.get(
            f"{BASE_URL}/models",
            timeout=20
        )

        if not response.ok:
            print(
                "Models HTTP:",
                response.status_code,
                response.text[:500]
            )

            return _model_cache["models"]

        data = response.json()

        models = []

        for item in data.get(
            "data",
            []
        ):

            if (
                isinstance(item, dict)
                and item.get("id")
            ):
                models.append(item)

        models.sort(
            key=lambda x: x["id"].lower()
        )

        _model_cache["models"] = models
        _model_cache["time"] = now

        print(
            "Models loaded:",
            len(models)
        )

        return models

    except Exception as e:
        print(
            "Models error:",
            repr(e)
        )

        return _model_cache["models"]


def model_keyboard(user):
    models = get_models()

    rows = [
        [
            {
                "text": "🤖 Авто",
                "callback_data": "model:auto"
            }
        ]
    ]

    for index, model in enumerate(models):

        model_id = model["id"]

        title = model_id

        if user["model"] == model_id:
            title = "✅ " + title

        rows.append(
            [
                {
                    "text": title[:55],
                    "callback_data": f"model:{index}"
                }
            ]
        )

    rows.append(
        [
            {
                "text": "⬅️ Назад",
                "callback_data": "back"
            }
        ]
    )

    return {
        "inline_keyboard": rows
    }


def is_bad_chat_model(model_id):
    value = model_id.lower()

    bad_words = (
        "embedding",
        "moderation",
        "tts",
        "transcrib",
        "realtime",
        "image-generation"
    )

    return any(
        word in value
        for word in bad_words
    )


def rank_models(
    models,
    vision=False
):
    def score(item):

        model_id = item["id"].lower()

        if is_bad_chat_model(model_id):
            return -1000

        score_value = 0

        if vision:

            if any(
                word in model_id
                for word in (
                    "vision",
                    "vl",
                    "omni",
                    "4o",
                    "multimodal",
                    "gemini",
                    "mistral"
                )
            ):
                score_value += 100

        if any(
            word in model_id
            for word in (
                "gpt",
                "claude",
                "gemini",
                "mistral",
                "deepseek",
                "qwen",
                "kimi",
                "minimax",
                "glm",
                "grok"
            )
        ):
            score_value += 20

        return score_value

    return sorted(
        models,
        key=score,
        reverse=True
    )


def choose_candidates(vision=False):
    models = get_models()

    ranked = rank_models(
        models,
        vision
    )

    return [
        model["id"]
        for model in ranked
        if not is_bad_chat_model(
            model["id"]
        )
    ]


# ============================================================
# BAZAAI API
# ============================================================

def extract_error(response):
    try:
        data = response.json()

        error = data.get(
            "error",
            data
        )

        if isinstance(error, dict):
            return str(
                error.get(
                    "message",
                    error
                )
            )

        return str(error)

    except Exception:
        return response.text[:800]


def call_ai(
    messages,
    model
):
    payload = {
        "model": model,
        "messages": messages
    }

    for attempt in range(3):

        try:

            response = api_session.post(
                f"{BASE_URL}/chat/completions",
                json=payload,
                timeout=API_TIMEOUT
            )

            if response.status_code in (
                429,
                500,
                502,
                503,
                504
            ):
                time.sleep(
                    2 + attempt
                )

                continue

            if not response.ok:

                return (
                    None,
                    extract_error(response),
                    response.status_code
                )

            data = response.json()

            choices = data.get(
                "choices"
            ) or []

            if not choices:

                return (
                    None,
                    "API не вернуло choices.",
                    200
                )

            message = choices[0].get(
                "message",
                {}
            )

            content = message.get(
                "content",
                ""
            )

            if isinstance(
                content,
                list
            ):

                parts = []

                for part in content:

                    if (
                        isinstance(
                            part,
                            dict
                        )
                        and part.get(
                            "type"
                        ) == "text"
                    ):
                        parts.append(
                            part.get(
                                "text",
                                ""
                            )
                        )

                content = "\n".join(parts)

            if (
                not isinstance(
                    content,
                    str
                )
                or not content.strip()
            ):

                return (
                    None,
                    "Модель вернула пустой ответ.",
                    200
                )

            return (
                content.strip(),
                None,
                200
            )

        except requests.Timeout:

            if attempt == 2:

                return (
                    None,
                    "API слишком долго отвечает.",
                    0
                )

        except requests.RequestException as e:

            if attempt == 2:

                return (
                    None,
                    f"Сетевая ошибка: {e}",
                    0
                )

        time.sleep(1.5)

    return (
        None,
        "Временная ошибка API.",
        0
    )


def ask_auto(
    messages,
    vision=False
):
    candidates = choose_candidates(
        vision
    )

    if not candidates:

        return (
            None,
            "В API не найдено подходящих моделей.",
            None
        )

    last_error = None

    for model in candidates[:8]:

        answer, error, status = call_ai(
            messages,
            model
        )

        if answer:

            return (
                answer,
                None,
                model
            )

        last_error = error

        if status == 401:
            break

        if status in (
            400,
            404
        ):
            continue

        if status == 429:
            continue

    return (
        None,
        last_error or "Не удалось получить ответ.",
        None
    )


# ============================================================
# AI PROMPTS
# ============================================================

def style_prompt(style):

    if style == "short":
        return "Отвечай кратко и по делу."

    if style == "detailed":
        return (
            "Отвечай подробно, структурированно "
            "и понятно. Если задача сложная — "
            "объясняй по шагам."
        )

    return (
        "Отвечай естественно, понятно "
        "и без лишней воды."
    )


def system_prompt(user):

    prompt = (
        "Ты — BulbaMaxAI, AI-помощник в Telegram.\n"
        "Отвечай на языке пользователя.\n"
        "Учитывай историю текущего разговора.\n"
    )

    prompt += style_prompt(
        user.get(
            "style",
            "normal"
        )
    )

    memory = user.get(
        "memory",
        {}
    )

    if memory:

        prompt += (
            "\n\nПостоянная память пользователя:\n"
        )

        for key, value in memory.items():

            prompt += (
                f"- {key}: {value}\n"
            )

    return prompt


# ============================================================
# HISTORY
# ============================================================

def add_history(
    chat,
    role,
    content
):
    chat["history"].append(
        {
            "role": role,
            "content": content
        }
    )

    chat["history"] = chat[
        "history"
    ][-MAX_HISTORY:]


def build_messages(
    user,
    extra=None
):
    chat = get_chat(user)

    messages = [
        {
            "role": "system",
            "content": system_prompt(user)
        }
    ]

    messages.extend(
        chat["history"]
    )

    if extra is not None:

        messages.append(
            {
                "role": "user",
                "content": extra
            }
        )

    return messages


# ============================================================
# TEXT REQUEST
# ============================================================

def process_text(
    chat_id,
    user_id,
    text
):
    user = get_user(
        user_id
    )

    if not allowed_request(user):

        send_message(
            chat_id,
            "🛡 Слишком много запросов.\n"
            "Подожди несколько секунд.",
            main_keyboard()
        )

        return

    requested_model = user[
        "model"
    ]

    messages = build_messages(
        user,
        text
    )

    status = send_message(
        chat_id,
        (
            "🤖 Авто: выбираю модель…"
            if requested_model == "auto"
            else
            f"🧠 Думаю…\nМодель: {requested_model}"
        )
    )

    if requested_model == "auto":

        answer, error, used_model = ask_auto(
            messages,
            vision=False
        )

    else:

        answer, error, code = call_ai(
            messages,
            requested_model
        )

        used_model = (
            requested_model
            if answer
            else None
        )

    if not answer:

        user["errors"] += 1
        db["total_errors"] += 1

        save_db()

        send_message(
            chat_id,
            "❌ Ошибка API:\n\n"
            + (
                error
                or
                "Неизвестная ошибка."
            )
        )

        return

    chat = get_chat(user)

    add_history(
        chat,
        "user",
        text
    )

    add_history(
        chat,
        "assistant",
        answer
    )

    chat["requests"] += 1

    chat["last_prompt"] = text

    user["requests"] += 1

    db["total_requests"] += 1

    save_db()

    if (
        status
        and len(answer) <= MAX_REPLY
    ):

        edit_message(
            chat_id,
            status["message_id"],
            answer,
            retry_keyboard()
        )

    else:

        if status:

            tg(
                "deleteMessage",
                {
                    "chat_id": chat_id,
                    "message_id": status[
                        "message_id"
                    ]
                }
            )

        send_message(
            chat_id,
            answer,
            retry_keyboard()
        )


# ============================================================
# TELEGRAM FILE DOWNLOAD
# ============================================================

def telegram_file_bytes(
    file_id
):
    info = tg(
        "getFile",
        {
            "file_id": file_id
        }
    )

    if (
        not info
        or not info.get("file_path")
    ):
        return None

    try:

        response = tg_session.get(
            (
                "https://api.telegram.org/"
                f"file/bot{BOT_TOKEN}/"
                f"{info['file_path']}"
            ),
            timeout=40
        )

        if not response.ok:
            return None

        return response.content

    except Exception:
        return None


# ============================================================
# IMAGE
# ============================================================

def process_photo(
    chat_id,
    user_id,
    photos,
    question
):
    user = get_user(
        user_id
    )

    if not allowed_request(user):

        send_message(
            chat_id,
            "🛡 Слишком много запросов.\n"
            "Подожди несколько секунд.",
            main_keyboard()
        )

        return

    photo = photos[-1]

    data = telegram_file_bytes(
        photo["file_id"]
    )

    if not data:

        send_message(
            chat_id,
            "❌ Не удалось получить изображение."
        )

        return

    if len(data) > MAX_IMAGE_BYTES:

        send_message(
            chat_id,
            "❌ Изображение слишком большое."
        )

        return

    question = (
        question.strip()
        if question
        else
        "Проанализируй это изображение."
    )

    encoded = base64.b64encode(
        data
    ).decode()

    image_url = (
        "data:image/jpeg;base64,"
        + encoded
    )

    content = [
        {
            "type": "text",
            "text": question
        },
        {
            "type": "image_url",
            "image_url": {
                "url": image_url
            }
        }
    ]

    messages = [
        {
            "role": "system",
            "content": system_prompt(user)
        }
    ]

    messages.extend(
        get_chat(user)[
            "history"
        ][-10:]
    )

    messages.append(
        {
            "role": "user",
            "content": content
        }
    )

    status = send_message(
        chat_id,
        "📸 Анализирую изображение…"
    )

    requested_model = user[
        "model"
    ]

    if requested_model == "auto":

        answer, error, used_model = ask_auto(
            messages,
            vision=True
        )

    else:

        answer, error, code = call_ai(
            messages,
            requested_model
        )

        used_model = (
            requested_model
            if answer
            else None
        )

    if not answer:

        user["errors"] += 1
        db["total_errors"] += 1

        save_db()

        send_message(
            chat_id,
            "❌ Не удалось обработать фото.\n\n"
            + (
                error
                or
                "Неизвестная ошибка."
            )
        )

        return

    chat = get_chat(user)

    add_history(
        chat,
        "user",
        "[Фото] " + question
    )

    add_history(
        chat,
        "assistant",
        answer
    )

    chat["last_prompt"] = question

    user["requests"] += 1

    db["total_requests"] += 1

    save_db()

    if (
        status
        and len(answer) <= MAX_REPLY
    ):

        edit_message(
            chat_id,
            status["message_id"],
            answer,
            retry_keyboard()
        )

    else:

        if status:

            tg(
                "deleteMessage",
                {
                    "chat_id": chat_id,
                    "message_id": status[
                        "message_id"
                    ]
                }
            )

        send_message(
            chat_id,
            answer,
            retry_keyboard()
        )


# ============================================================
# FILES
# ============================================================

def decode_text(data):

    encodings = (
        "utf-8",
        "utf-8-sig",
        "cp1251",
        "latin-1"
    )

    for encoding in encodings:

        try:

            return data.decode(
                encoding
            )

        except UnicodeDecodeError:
            pass

    return None


def process_document(
    chat_id,
    user_id,
    document,
    question
):
    user = get_user(
        user_id
    )

    if not allowed_request(user):

        send_message(
            chat_id,
            "🛡 Слишком много запросов.\n"
            "Подожди несколько секунд.",
            main_keyboard()
        )

        return

    filename = document.get(
        "file_name",
        "file.txt"
    )

    extension = Path(
        filename
    ).suffix.lower()

    allowed_extensions = {
        ".txt",
        ".md",
        ".json",
        ".csv"
    }

    if extension not in allowed_extensions:

        send_message(
            chat_id,
            "📎 Поддерживаются:\n"
            "TXT, MD, JSON и CSV."
        )

        return

    data = telegram_file_bytes(
        document["file_id"]
    )

    if not data:

        send_message(
            chat_id,
            "❌ Не удалось скачать файл."
        )

        return

    if len(data) > MAX_FILE_BYTES:

        send_message(
            chat_id,
            "❌ Файл слишком большой."
        )

        return

    text = decode_text(
        data
    )

    if text is None:

        send_message(
            chat_id,
            "❌ Не удалось прочитать файл."
        )

        return

    if len(text) > MAX_FILE_TEXT:

        text = (
            text[:MAX_FILE_TEXT]
            + "\n\n"
            "[Содержимое файла сокращено]"
        )

    question = (
        question.strip()
        if question
        else
        "Проанализируй этот файл."
    )

    prompt = (
        f"Файл: {filename}\n\n"
        "Содержимое файла:\n"
        "----------------\n"
        f"{text}\n"
        "----------------\n\n"
        f"Задача пользователя: {question}"
    )

    status = send_message(
        chat_id,
        "📎 Читаю и анализирую файл…"
    )

    messages = [
        {
            "role": "system",
            "content": system_prompt(user)
        },
        {
            "role": "user",
            "content": prompt
        }
    ]

    requested_model = user[
        "model"
    ]

    if requested_model == "auto":

        answer, error, used_model = ask_auto(
            messages,
            vision=False
        )

    else:

        answer, error, code = call_ai(
            messages,
            requested_model
        )

        used_model = (
            requested_model
            if answer
            else None
        )

    if not answer:

        user["errors"] += 1
        db["total_errors"] += 1

        save_db()

        send_message(
            chat_id,
            "❌ Ошибка анализа файла:\n\n"
            + (
                error
                or
                "Неизвестная ошибка."
            )
        )

        return

    chat = get_chat(user)

    add_history(
        chat,
        "user",
        f"[Файл: {filename}] {question}"
    )

    add_history(
        chat,
        "assistant",
        answer
    )

    chat["last_prompt"] = question

    user["requests"] += 1

    db["total_requests"] += 1

    save_db()

    if (
        status
        and len(answer) <= MAX_REPLY
    ):

        edit_message(
            chat_id,
            status["message_id"],
            answer,
            retry_keyboard()
        )

    else:

        if status:

            tg(
                "deleteMessage",
                {
                    "chat_id": chat_id,
                    "message_id": status[
                        "message_id"
                    ]
                }
            )

        send_message(
            chat_id,
            answer,
            retry_keyboard()
        )


# ============================================================
# MEMORY
# ============================================================

def show_memory(
    chat_id,
    user_id
):
    user = get_user(
        user_id
    )

    memory = user[
        "memory"
    ]

    if not memory:

        text = (
            "💾 Память пуста.\n\n"
            "Используй:\n"
            "/remember имя=значение"
        )

    else:

        lines = [
            "💾 Память:\n"
        ]

        for key, value in memory.items():

            lines.append(
                f"• {key}: {value}"
            )

        text = "\n".join(
            lines
        )

    send_message(
        chat_id,
        text,
        memory_keyboard()
    )


# ============================================================
# CHATS
# ============================================================

def create_chat(
    user,
    name
):
    name = name.strip()

    if not name:
        name = (
            f"Чат "
            f"{len(user['chats']) + 1}"
        )

    base_name = name[:30]

    name = base_name

    counter = 2

    while name in user["chats"]:

        name = (
            f"{base_name} "
            f"{counter}"
        )

        counter += 1

    user["chats"][name] = default_chat()

    user["active_chat"] = name

    while (
        len(user["chats"])
        > MAX_CHATS
    ):

        oldest = min(
            user["chats"],
            key=lambda key:
            user["chats"][key].get(
                "created",
                0
            )
        )

        if (
            oldest
            == user["active_chat"]
        ):
            break

        del user[
            "chats"
        ][oldest]


def chat_keyboard(user):
    rows = []

    for name in user["chats"]:

        title = name

        if name == user[
            "active_chat"
        ]:
            title = "✅ " + title

        token = hashlib.sha256(
            name.encode("utf-8")
        ).hexdigest()[:12]

        rows.append(
            [
                {
                    "text": title[:50],
                    "callback_data":
                    f"chat:{token}"
                }
            ]
        )

    rows.append(
        [
            {
                "text": "⬅️ Назад",
                "callback_data": "back"
            }
        ]
    )

    return {
        "inline_keyboard": rows
    }


def show_chats(
    chat_id,
    user_id
):
    user = get_user(
        user_id
    )

    send_message(
        chat_id,
        (
            "💬 Текущий чат: "
            + user["active_chat"]
            + "\n\nВыбери чат:"
        ),
        chat_keyboard(user)
    )


# ============================================================
# CALLBACKS
# ============================================================

def handle_callback(query):

    callback_id = query["id"]

    message = (
        query.get("message")
        or {}
    )

    chat = (
        message.get("chat")
        or {}
    )

    chat_id = chat.get(
        "id"
    )

    message_id = message.get(
        "message_id"
    )

    sender = (
        query.get("from")
        or {}
    )

    user_id = sender.get(
        "id"
    )

    if (
        not user_id
        or not chat_id
    ):

        answer_callback(
            callback_id
        )

        return

    user = get_user(
        user_id
    )

    data = query.get(
        "data",
        ""
    )

    # BACK
    if data == "back":

        answer_callback(
            callback_id
        )

        edit_message(
            chat_id,
            message_id,
            "🤖 BulbaMaxAI V13",
            main_keyboard()
        )

        return

    # MODEL
    if data.startswith(
        "model:"
    ):

        token = data.split(
            ":",
            1
        )[1]

        if token == "auto":

            user["model"] = "auto"

        elif token.isdigit():

            models = get_models()

            index = int(
                token
            )

            if index >= len(
                models
            ):

                answer_callback(
                    callback_id,
                    "Модель уже недоступна."
                )

                return

            user["model"] = (
                models[index]["id"]
            )

        else:

            answer_callback(
                callback_id,
                "Некорректная модель."
            )

            return

        save_db()

        answer_callback(
            callback_id,
            "Модель сохранена"
        )

        edit_message(
            chat_id,
            message_id,
            (
                "🧠 Выбрано:\n\n"
                + user["model"]
            ),
            main_keyboard()
        )

        return

    # STYLE
    if data.startswith(
        "style:"
    ):

        style = data.split(
            ":",
            1
        )[1]

        if style not in {
            "normal",
            "short",
            "detailed"
        }:

            return

        user["style"] = style

        save_db()

        answer_callback(
            callback_id,
            "Сохранено"
        )

        edit_message(
            chat_id,
            message_id,
            "⚙️ Стиль ответа:",
            settings_keyboard(
                style
            )
        )

        return

    # MEMORY CLEAR
    if data == "memory_clear":

        user["memory"] = {}

        save_db()

        answer_callback(
            callback_id,
            "Память очищена"
        )

        edit_message(
            chat_id,
            message_id,
            "💾 Память очищена.",
            main_keyboard()
        )

        return

    # CHAT
    if data.startswith(
        "chat:"
    ):

        token = data.split(
            ":",
            1
        )[1]

        found = None

        for name in user[
            "chats"
        ]:

            current_token = hashlib.sha256(
                name.encode("utf-8")
            ).hexdigest()[:12]

            if current_token == token:

                found = name

                break

        if found:

            user[
                "active_chat"
            ] = found

            save_db()

            answer_callback(
                callback_id,
                "Чат выбран"
            )

            edit_message(
                chat_id,
                message_id,
                (
                    "💬 Активный чат:\n"
                    + found
                ),
                main_keyboard()
            )

        else:

            answer_callback(
                callback_id,
                "Чат не найден."
            )

        return

    # RETRY
    if data == "retry":

        answer_callback(
            callback_id,
            "Повторяю…"
        )

        prompt = get_chat(
            user
        ).get(
            "last_prompt"
        )

        if prompt:

            process_text(
                chat_id,
                user_id,
                prompt
            )

        return

    answer_callback(
        callback_id
    )


# ============================================================
# COMMANDS / MESSAGES
# ============================================================

def handle_update(update):

    if "callback_query" in update:

        handle_callback(
            update[
                "callback_query"
            ]
        )

        return

    message = update.get(
        "message"
    )

    if not message:
        return

    chat = (
        message.get("chat")
        or {}
    )

    sender = (
        message.get("from")
        or {}
    )

    chat_id = chat.get(
        "id"
    )

    user_id = sender.get(
        "id"
    )

    if (
        not chat_id
        or not user_id
    ):
        return

    text = message.get(
        "text",
        ""
    )

    # /start
    if text.startswith(
        "/start"
    ):

        get_user(
            user_id
        )

        send_message(
            chat_id,
            "🤖 BulbaMaxAI V13\n\n"
            "Готов к работе.\n\n"
            "🤖 Авто включён.\n"
            "📸 Фото\n"
            "📎 TXT / MD / JSON / CSV\n"
            "💾 Память\n"
            "💬 Несколько чатов",
            main_keyboard()
        )

        return

    # /help
    if text.startswith(
        "/help"
    ):

        send_message(
            chat_id,
            "📚 Команды:\n\n"
            "/start — запуск\n"
            "/models — модели\n"
            "/stats — статистика\n"
            "/memory — память\n"
            "/new — новый чат\n"
            "/clear — очистить чат\n"
            "/remember имя=значение — запомнить\n"
            "/forget имя — забыть\n\n"
            "Можно отправлять:\n"
            "📸 фото с вопросом\n"
            "📎 TXT / MD / JSON / CSV",
            main_keyboard()
        )

        return

    # /models
    if text.startswith(
        "/models"
    ):

        models = get_models(
            force=True
        )

        send_message(
            chat_id,
            (
                "🧠 Доступно моделей: "
                f"{len(models)}"
            ),
            model_keyboard(
                get_user(
                    user_id
                )
            )
        )

        return

    # /stats
    if text.startswith(
        "/stats"
    ):

        user = get_user(
            user_id
        )

        send_message(
            chat_id,
            "📊 Статистика\n\n"
            f"Ваших запросов: {user['requests']}\n"
            f"Ошибок: {user['errors']}\n"
            f"Всего запросов: {db['total_requests']}\n"
            f"Всего ошибок: {db['total_errors']}\n\n"
            f"Модель: {user['model']}\n"
            f"Стиль: {user['style']}\n"
            f"Чат: {user['active_chat']}",
            main_keyboard()
        )

        return

    # /memory
    if text.startswith(
        "/memory"
    ):

        show_memory(
            chat_id,
            user_id
        )

        return

    # /remember
    if text.startswith(
        "/remember"
    ):

        user = get_user(
            user_id
        )

        raw = text[
            len("/remember"):
        ].strip()

        if "=" not in raw:

            send_message(
                chat_id,
                "Использование:\n"
                "/remember имя=значение"
            )

            return

        key, value = raw.split(
            "=",
            1
        )

        key = key.strip()
        value = value.strip()

        if (
            not key
            or not value
        ):

            send_message(
                chat_id,
                "❌ Заполни имя и значение."
            )

            return

        user[
            "memory"
        ][key] = value

        save_db()

        send_message(
            chat_id,
            (
                "💾 Запомнил:\n"
                f"{key} = {value}"
            ),
            main_keyboard()
        )

        return

    # /forget
    if text.startswith(
        "/forget"
    ):

        user = get_user(
            user_id
        )

        key = text[
            len("/forget"):
        ].strip()

        if key in user[
            "memory"
        ]:

            del user[
                "memory"
            ][key]

            save_db()

            send_message(
                chat_id,
                f"🗑 Удалил: {key}",
                main_keyboard()
            )

        else:

            send_message(
                chat_id,
                "❌ Такой записи нет."
            )

        return

    # /new
    if text.startswith(
        "/new"
    ):

        user = get_user(
            user_id
        )

        create_chat(
            user,
            (
                "Чат "
                f"{len(user['chats']) + 1}"
            )
        )

        save_db()

        send_message(
            chat_id,
            (
                "🆕 Создан чат:\n"
                f"{user['active_chat']}"
            ),
            main_keyboard()
        )

        return

    # /clear
    if text.startswith(
        "/clear"
    ):

        user = get_user(
            user_id
        )

        chat = get_chat(
            user
        )

        chat["history"] = []
        chat["last_prompt"] = None

        save_db()

        send_message(
            chat_id,
            "🧹 Текущий чат очищен.",
            main_keyboard()
        )

        return

    # AUTO
    if text == "🤖 Авто":

        user = get_user(
            user_id
        )

        user["model"] = "auto"

        save_db()

        send_message(
            chat_id,
            "🤖 Авто включён.",
            main_keyboard()
        )

        return

    # MODEL
    if text == "🧠 Модель":

        user = get_user(
            user_id
        )

        models = get_models(
            force=True
        )

        if not models:

            send_message(
                chat_id,
                "❌ Не удалось получить список моделей."
            )

        else:

            send_message(
                chat_id,
                "🧠 Выбери модель:",
                model_keyboard(user)
            )

        return

    # STATS
    if text == "📊 Статистика":

        user = get_user(
            user_id
        )

        send_message(
            chat_id,
            "📊 Статистика\n\n"
            f"Запросов: {user['requests']}\n"
            f"Ошибок: {user['errors']}\n"
            f"Модель: {user['model']}\n"
            f"Стиль: {user['style']}",
            main_keyboard()
        )

        return

    # MEMORY
    if text == "💾 Память":

        show_memory(
            chat_id,
            user_id
        )

        return

    # CHATS
    if text == "💬 Чаты":

        show_chats(
            chat_id,
            user_id
        )

        return

    # NEW CHAT
    if text == "🆕 Новый чат":

        user = get_user(
            user_id
        )

        create_chat(
            user,
            (
                "Чат "
                f"{len(user['chats']) + 1}"
            )
        )

        save_db()

        send_message(
            chat_id,
            (
                "🆕 Создан новый чат:\n"
                f"{user['active_chat']}"
            ),
            main_keyboard()
        )

        return

    # CLEAR
    if text == "🧹 Очистить":

        user = get_user(
            user_id
        )

        chat = get_chat(
            user
        )

        chat["history"] = []
        chat["last_prompt"] = None

        save_db()

        send_message(
            chat_id,
            "🧹 Текущий чат очищен.",
            main_keyboard()
        )

        return

    # SETTINGS
    if text == "⚙️ Настройки":

        user = get_user(
            user_id
        )

        send_message(
            chat_id,
            "⚙️ Стиль ответа:",
            settings_keyboard(
                user["style"]
            )
        )

        return

    # PHOTO
    if "photo" in message:

        process_photo(
            chat_id,
            user_id,
            message["photo"],
            message.get("caption")
        )

        return

    # DOCUMENT
    if "document" in message:

        process_document(
            chat_id,
            user_id,
            message["document"],
            message.get("caption")
        )

        return

    # TEXT
    if text:

        process_text(
            chat_id,
            user_id,
            text
        )


# ============================================================
# LONG POLLING
# ============================================================

def main():

    load_db()

    print(
        "================================"
    )

    print(
        "BulbaMaxAI V13 started"
    )

    print(
        "BazaAI:",
        BASE_URL
    )

    print(
        "Auto: ON"
    )

    print(
        "Images: ON"
    )

    print(
        "Files: ON"
    )

    print(
        "Memory: ON"
    )

    print(
        "Multiple chats: ON"
    )

    print(
        "================================"
    )

    offset = None

    while True:

        try:

            data = {
                "timeout": POLL_TIMEOUT,
                "allowed_updates": json.dumps(
                    [
                        "message",
                        "callback_query"
                    ]
                )
            }

            if offset is not None:

                data[
                    "offset"
                ] = offset

            updates = tg(
                "getUpdates",
                data,
                timeout=POLL_TIMEOUT + 10
            )

            if updates is None:

                time.sleep(3)

                continue

            for update in updates:

                offset = (
                    update["update_id"]
                    + 1
                )

                try:

                    handle_update(
                        update
                    )

                except Exception as e:

                    print(
                        "Update error:",
                        repr(e)
                    )

        except KeyboardInterrupt:

            break

        except Exception as e:

            print(
                "Polling error:",
                repr(e)
            )

            time.sleep(5)


if __name__ == "__main__":
    main()
