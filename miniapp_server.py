import os
import json
import time
import hmac
import hashlib
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bot


HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "8080"))

MAX_BODY = 12 * 1024 * 1024
AUTH_MAX_AGE = 24 * 60 * 60
MAX_MESSAGE = 12000
MAX_IMAGE_BASE64 = 10 * 1024 * 1024


# =========================================================
# JSON
# =========================================================

def send_json(handler, status, data):
    body = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":")
    ).encode("utf-8")

    handler.send_response(status)
    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8"
    )
    handler.send_header(
        "Content-Length",
        str(len(body))
    )
    handler.send_header(
        "Access-Control-Allow-Origin",
        "*"
    )
    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, X-Telegram-Init-Data"
    )
    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, POST, OPTIONS"
    )
    handler.end_headers()

    handler.wfile.write(body)


def read_json(handler):
    try:
        length = int(
            handler.headers.get("Content-Length", "0")
        )
    except ValueError:
        raise ValueError("Некорректный Content-Length.")

    if length <= 0:
        return {}

    if length > MAX_BODY:
        raise ValueError("Запрос слишком большой.")

    raw = handler.rfile.read(length)

    try:
        return json.loads(
            raw.decode("utf-8")
        )
    except Exception:
        raise ValueError("Некорректный JSON.")


# =========================================================
# TELEGRAM AUTH
# =========================================================

def validate_init_data(init_data):
    if not init_data:
        raise ValueError(
            "Telegram initData отсутствует."
        )

    token = os.getenv(
        "BOT_TOKEN",
        ""
    ).strip()

    if not token:
        raise RuntimeError(
            "BOT_TOKEN не установлен."
        )

    parsed = urllib.parse.parse_qs(
        init_data,
        keep_blank_values=True
    )

    received_hash = parsed.get(
        "hash",
        [None]
    )[0]

    if not received_hash:
        raise ValueError(
            "В initData отсутствует hash."
        )

    auth_date_raw = parsed.get(
        "auth_date",
        [None]
    )[0]

    if not auth_date_raw:
        raise ValueError(
            "В initData отсутствует auth_date."
        )

    try:
        auth_date = int(auth_date_raw)
    except ValueError:
        raise ValueError(
            "Некорректный auth_date."
        )

    if abs(
        int(time.time()) - auth_date
    ) > AUTH_MAX_AGE:
        raise ValueError(
            "Telegram initData устарел."
        )

    pairs = []

    for key in sorted(parsed.keys()):
        if key == "hash":
            continue

        value = parsed[key][0]
        pairs.append(
            f"{key}={value}"
        )

    data_check_string = "\n".join(pairs)

    secret_key = hmac.new(
        b"WebAppData",
        token.encode("utf-8"),
        hashlib.sha256
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(
        calculated_hash,
        received_hash
    ):
        raise ValueError(
            "Неверная подпись Telegram."
        )

    user_raw = parsed.get(
        "user",
        [None]
    )[0]

    if not user_raw:
        raise ValueError(
            "Telegram user отсутствует."
        )

    try:
        tg_user = json.loads(user_raw)
    except Exception:
        raise ValueError(
            "Некорректные данные Telegram user."
        )

    if not tg_user.get("id"):
        raise ValueError(
            "Telegram user ID отсутствует."
        )

    return tg_user


def get_user_from_request(handler):
    init_data = handler.headers.get(
        "X-Telegram-Init-Data",
        ""
    ).strip()

    tg_user = validate_init_data(
        init_data
    )

    uid = str(
        tg_user["id"]
    )

    user = bot.get_user(uid)

    user["_user_id"] = uid

    user["_telegram_user"] = {
        "id": tg_user.get("id"),
        "first_name": tg_user.get(
            "first_name",
            ""
        ),
        "last_name": tg_user.get(
            "last_name",
            ""
        ),
        "username": tg_user.get(
            "username",
            ""
        ),
        "language_code": tg_user.get(
            "language_code",
            ""
        ),
        "photo_url": tg_user.get(
            "photo_url",
            ""
        ),
    }

    return user


# =========================================================
# ACCESS
# =========================================================

def require_access(user):
    required = os.getenv(
        "LICENSE_REQUIRED",
        "0"
    ).strip() == "1"

    if not required:
        return True

    uid = user.get("_user_id")

    if not uid:
        return False

    try:
        if bot.is_admin(uid):
            return True
    except Exception:
        pass

    try:
        return bool(
            bot.user_has_access(uid)
        )
    except Exception:
        return False


# =========================================================
# MODELS
# =========================================================

def get_models():
    result = []

    for item in bot.get_models():
        if not isinstance(item, dict):
            continue

        model_id = item.get("id")

        if not model_id:
            continue

        if bot.is_bad_model(model_id):
            continue

        result.append({
            "id": model_id,
            "name": model_id
        })

    return result


def validate_model(model):
    if not model:
        return "auto"

    model = str(model).strip()

    if model == "auto":
        return "auto"

    available = {
        x["id"]
        for x in get_models()
    }

    if model not in available:
        raise ValueError(
            "Выбранная модель недоступна."
        )

    return model


# =========================================================
# HISTORY
# =========================================================

def make_history(user, chat, current_content):
    messages = []

    system_prompt = bot.style_system(
        user
    )

    if system_prompt:
        messages.append({
            "role": "system",
            "content": system_prompt
        })

    history = chat.get(
        "history",
        []
    )

    for item in history[
        -bot.MAX_HISTORY:
    ]:
        role = item.get(
            "role",
            "user"
        )

        content = item.get(
            "content",
            ""
        )

        if not content:
            continue

        if role not in (
            "user",
            "assistant",
            "system"
        ):
            continue

        messages.append({
            "role": role,
            "content": content
        })

    messages.append({
        "role": "user",
        "content": current_content
    })

    return messages


# =========================================================
# STATE
# =========================================================

def frontend_state(user):
    chats = []

    for chat_id, chat in user.get(
        "chats",
        {}
    ).items():

        messages = []

        for item in chat.get(
            "history",
            []
        ):
            if not isinstance(
                item,
                dict
            ):
                continue

            messages.append({
                "role": item.get(
                    "role",
                    "user"
                ),
                "content": str(
                    item.get(
                        "content",
                        ""
                    )
                )
            })

        chats.append({
            "id": str(chat_id),
            "title": str(
                chat.get(
                    "title",
                    chat_id
                )
            ),
            "messages": messages,
            "requests": chat.get(
                "requests",
                0
            ),
            "created": chat.get(
                "created",
                0
            )
        })

    return {
        "model": user.get(
            "model",
            "auto"
        ),
        "style": user.get(
            "style",
            "normal"
        ),
        "active_chat": str(
            user.get(
                "active_chat",
                "main"
            )
        ),
        "chats": chats,
        "memory": user.get(
            "memory",
            {}
        ),
        "requests": user.get(
            "requests",
            0
        ),
        "errors": user.get(
            "errors",
            0
        ),
        "telegram_user": user.get(
            "_telegram_user",
            {}
        ),
        "version": getattr(
            bot,
            "BOT_VERSION",
            "V15"
        )
    }


# =========================================================
# CHAT
# =========================================================

def create_ai_content(text, image):
    if not image:
        return text

    if not isinstance(
        image,
        str
    ):
        raise ValueError(
            "Некорректное изображение."
        )

    if len(image) > MAX_IMAGE_BASE64:
        raise ValueError(
            "Изображение слишком большое."
        )

    if not image.startswith(
        "data:image/"
    ):
        raise ValueError(
            "Неподдерживаемый формат изображения."
        )

    return [
        {
            "type": "text",
            "text": text or "Проанализируй изображение."
        },
        {
            "type": "image_url",
            "image_url": {
                "url": image
            }
        }
    ]


def handle_chat(user, data):
    text = str(
        data.get(
            "message",
            ""
        )
    ).strip()

    image = data.get(
        "image"
    )

    if not text and not image:
        raise ValueError(
            "Сообщение пустое."
        )

    if len(text) > MAX_MESSAGE:
        raise ValueError(
            "Сообщение слишком длинное."
        )

    if not bot.allowed_request(
        user
    ):
        raise RuntimeError(
            "Слишком много запросов. Попробуй немного позже."
        )

    requested_model = validate_model(
        data.get("model")
    )

    if requested_model != "auto":
        user["model"] = requested_model

    else:
        user["model"] = "auto"

    style = data.get(
        "style"
    )

    if style in (
        "normal",
        "short",
        "detailed"
    ):
        user["style"] = style

    chat_id = data.get(
        "chat_id"
    )

    if chat_id:
        chat_id = str(chat_id)

        if chat_id in user["chats"]:
            user["active_chat"] = chat_id

    chat = bot.get_chat(
        user
    )

    content = create_ai_content(
        text,
        image
    )

    # Для истории сохраняем обычный текст.
    history_text = text

    if image and not text:
        history_text = "[Фото]"

    history = chat.setdefault(
        "history",
        []
    )

    history.append({
        "role": "user",
        "content": history_text
    })

    history[:] = history[
        -bot.MAX_HISTORY:
    ]

    messages = make_history(
        user,
        chat,
        content
    )

    # ВАЖНО:
    # выбираем только одну модель.
    # Это намного быстрее старого режима,
    # где AI мог перебирать до 8 моделей.

    candidates = bot.choose_model(
        user,
        vision=bool(image),
        preferred=(
            None
            if requested_model == "auto"
            else requested_model
        )
    )

    if not candidates:
        raise RuntimeError(
            "Нет доступных AI-моделей."
        )

    ai_model = candidates[0]

    answer, error, status = bot.call_ai(
        messages,
        ai_model
    )

    if not answer:
        raise RuntimeError(
            error
            or "AI не вернул ответ."
        )

    answer = str(
        answer
    ).strip()

    history.append({
        "role": "assistant",
        "content": answer
    })

    history[:] = history[
        -bot.MAX_HISTORY:
    ]

    chat["last_prompt"] = (
        text or "[Фото]"
    )

    chat["last_request"] = int(
        time.time()
    )

    chat["requests"] = (
        chat.get(
            "requests",
            0
        ) + 1
    )

    user["requests"] = (
        user.get(
            "requests",
            0
        ) + 1
    )

    bot.db["total_requests"] = (
        bot.db.get(
            "total_requests",
            0
        ) + 1
    )

    try:
        bot.consume_license_after_success(
            user
        )
    except Exception as e:
        print(
            "License consume error:",
            repr(e)
        )

    bot.save_db()

    return {
        "answer": answer,
        "model": ai_model,
        "state": frontend_state(
            user
        )
    }


# =========================================================
# HTTP HANDLER
# =========================================================

class MiniAppHandler(
    BaseHTTPRequestHandler
):

    server_version = "BulbaMiniApp/1.0"

    def log_message(
        self,
        fmt,
        *args
    ):
        print(
            "[MINIAPP]",
            fmt % args
        )

    def error(
        self,
        status,
        message
    ):
        send_json(
            self,
            status,
            {
                "ok": False,
                "error": str(message)
            }
        )

    def do_OPTIONS(self):
        send_json(
            self,
            204,
            {}
        )

    def do_GET(self):

        path = urllib.parse.urlparse(
            self.path
        ).path

        # Health check
        if path == "/health":
            send_json(
                self,
                200,
                {
                    "ok": True,
                    "service": "BulbaMaxAI",
                    "version": getattr(
                        bot,
                        "BOT_VERSION",
                        "V15"
                    )
                }
            )
            return

        # Frontend
        if path in (
            "/",
            "/index.html"
        ):
            self.serve_index()
            return

        try:
            user = get_user_from_request(
                self
            )
        except Exception as e:
            self.error(
                401,
                e
            )
            return

        if not require_access(
            user
        ):
            self.error(
                403,
                "Доступ запрещён."
            )
            return

        if path == "/api/state":
            send_json(
                self,
                200,
                {
                    "ok": True,
                    "state": frontend_state(
                        user
                    )
                }
            )
            return

        if path == "/api/models":
            send_json(
                self,
                200,
                {
                    "ok": True,
                    "models": get_models(),
                    "selected": user.get(
                        "model",
                        "auto"
                    )
                }
            )
            return

        self.error(
            404,
            "Endpoint не найден."
        )

    def do_POST(self):

        try:
            user = get_user_from_request(
                self
            )
        except Exception as e:
            self.error(
                401,
                e
            )
            return

        if not require_access(
            user
        ):
            self.error(
                403,
                "Доступ запрещён."
            )
            return

        try:
            data = read_json(
                self
            )

            path = urllib.parse.urlparse(
                self.path
            ).path

            if path == "/api/chat":
                result = handle_chat(
                    user,
                    data
                )

                send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        **result
                    }
                )
                return

            if path == "/api/chat/new":
                result = bot.new_chat(
                    user
                )

                bot.save_db()

                send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "chat_id": str(
                            result
                        ),
                        "state": frontend_state(
                            user
                        )
                    }
                )
                return

            if path == "/api/chat/select":
                chat_id = str(
                    data.get(
                        "chat_id",
                        ""
                    )
                )

                if chat_id not in user[
                    "chats"
                ]:
                    raise ValueError(
                        "Чат не найден."
                    )

                user[
                    "active_chat"
                ] = chat_id

                bot.save_db()

                send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "state": frontend_state(
                            user
                        )
                    }
                )
                return

            if path == "/api/settings":
                model = data.get(
                    "model"
                )

                if model is not None:
                    model = validate_model(
                        model
                    )
                    user[
                        "model"
                    ] = model

                style = data.get(
                    "style"
                )

                if style is not None:
                    if style not in (
                        "normal",
                        "short",
                        "detailed"
                    ):
                        raise ValueError(
                            "Неизвестный стиль."
                        )

                    user[
                        "style"
                    ] = style

                bot.save_db()

                send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "settings": {
                            "model": user.get(
                                "model",
                                "auto"
                            ),
                            "style": user.get(
                                "style",
                                "normal"
                            )
                        }
                    }
                )
                return

            if path == "/api/memory":
                action = str(
                    data.get(
                        "action",
                        "get"
                    )
                ).lower()

                if action == "clear":
                    user[
                        "memory"
                    ] = {}

                elif action == "set":
                    memory = data.get(
                        "memory",
                        {}
                    )

                    if not isinstance(
                        memory,
                        dict
                    ):
                        raise ValueError(
                            "memory должен быть объектом."
                        )

                    user[
                        "memory"
                    ] = memory

                bot.save_db()

                send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "memory": user.get(
                            "memory",
                            {}
                        )
                    }
                )
                return

            self.error(
                404,
                "Endpoint не найден."
            )

        except Exception as e:

            print(
                "Mini App error:",
                repr(e)
            )

            try:
                user["errors"] = (
                    user.get(
                        "errors",
                        0
                    ) + 1
                )

                bot.db[
                    "total_errors"
                ] = (
                    bot.db.get(
                        "total_errors",
                        0
                    ) + 1
                )

                bot.save_db()

            except Exception:
                pass

            self.error(
                500,
                e
            )

    def serve_index(self):

        possible_paths = [
            "miniapp/index.html",
            os.path.join(
                os.path.dirname(
                    __file__
                ),
                "miniapp",
                "index.html"
            )
        ]

        index_path = None

        for path in possible_paths:
            if os.path.isfile(path):
                index_path = path
                break

        if not index_path:
            self.error(
                500,
                "miniapp/index.html не найден."
            )
            return

        try:
            with open(
                index_path,
                "rb"
            ) as f:
                body = f.read()

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(body))
            )

            self.send_header(
                "Cache-Control",
                "no-cache"
            )

            self.end_headers()

            self.wfile.write(
                body
            )

        except Exception as e:
            self.error(
                500,
                e
            )


# =========================================================
# SERVER
# =========================================================

def run_server():

    bot.load_db()

    server = ThreadingHTTPServer(
        (
            HOST,
            PORT
        ),
        MiniAppHandler
    )

    print(
        f"Bulba Mini App started "
        f"on {HOST}:{PORT}"
    )

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        pass

    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()
