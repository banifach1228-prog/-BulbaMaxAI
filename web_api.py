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

MAX_BODY = 256 * 1024
AUTH_MAX_AGE = 24 * 60 * 60

ALLOWED_METHODS = {
    "GET",
    "POST",
    "OPTIONS",
}


def json_response(handler, status, payload):
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, X-Telegram-Init-Data",
    )
    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, POST, OPTIONS",
    )
    handler.end_headers()
    handler.wfile.write(body)


def parse_json_body(handler):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        raise ValueError("Некорректный Content-Length.")

    if length <= 0:
        return {}

    if length > MAX_BODY:
        raise ValueError("Запрос слишком большой.")

    raw = handler.rfile.read(length)

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Некорректный JSON.")


def validate_telegram_init_data(init_data):
    """
    Проверяет Telegram Mini App initData на сервере.

    ВАЖНО:
    initDataUnsafe на фронтенде доверять нельзя.
    Именно эта функция проверяет подпись Telegram.
    """

    if not init_data:
        raise ValueError("Telegram initData отсутствует.")

    bot_token = os.getenv("BOT_TOKEN", "").strip()

    if not bot_token:
        raise RuntimeError("BOT_TOKEN не установлен.")

    try:
        parsed = urllib.parse.parse_qs(
            init_data,
            keep_blank_values=True,
        )
    except Exception:
        raise ValueError("Некорректный initData.")

    received_hash = parsed.get("hash", [None])[0]

    if not received_hash:
        raise ValueError("В initData отсутствует hash.")

    auth_date_raw = parsed.get("auth_date", [None])[0]

    if not auth_date_raw:
        raise ValueError("В initData отсутствует auth_date.")

    try:
        auth_date = int(auth_date_raw)
    except ValueError:
        raise ValueError("Некорректный auth_date.")

    if abs(int(time.time()) - auth_date) > AUTH_MAX_AGE:
        raise ValueError("Telegram initData устарел.")

    data_pairs = []

    for key in sorted(parsed.keys()):
        if key == "hash":
            continue

        values = parsed.get(key, [])

        if not values:
            value = ""
        else:
            value = values[0]

        data_pairs.append(f"{key}={value}")

    data_check_string = "\n".join(data_pairs)

    secret_key = hmac.new(
        b"WebAppData",
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(
        calculated_hash,
        received_hash,
    ):
        raise ValueError("Неверная подпись Telegram.")

    user_raw = parsed.get("user", [None])[0]

    if not user_raw:
        raise ValueError("Telegram user отсутствует.")

    try:
        telegram_user = json.loads(user_raw)
    except json.JSONDecodeError:
        raise ValueError("Некорректные данные Telegram user.")

    user_id = telegram_user.get("id")

    if not user_id:
        raise ValueError("Telegram user ID отсутствует.")

    return telegram_user


def get_authenticated_user(handler):
    init_data = handler.headers.get(
        "X-Telegram-Init-Data",
        "",
    ).strip()

    telegram_user = validate_telegram_init_data(init_data)

    uid = str(telegram_user["id"])

    user = bot.get_user(uid)

    # Храним Telegram ID внутри пользователя.
    user["_user_id"] = uid

    # Эти данные используются только для отображения.
    user["_telegram_user"] = {
        "id": telegram_user.get("id"),
        "first_name": telegram_user.get("first_name", ""),
        "last_name": telegram_user.get("last_name", ""),
        "username": telegram_user.get("username", ""),
        "language_code": telegram_user.get("language_code", ""),
        "photo_url": telegram_user.get("photo_url", ""),
    }

    return user


def require_access(user):
    """
    Проверка лицензии.

    Если LICENSE_REQUIRED=1, доступ должен быть разрешён.
    По умолчанию проверка лицензии выключена для Mini App.
    """

    required = os.getenv(
        "LICENSE_REQUIRED",
        "0",
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
        return bool(bot.user_has_access(uid))
    except Exception:
        return False


def clean_user_for_frontend(user):
    chats = user.get("chats", {})

    result_chats = []

    for chat_id, chat in chats.items():
        history = chat.get("history", [])

        result_chats.append(
            {
                "id": str(chat_id),
                "title": str(
                    chat.get(
                        "title",
                        chat_id,
                    )
                ),
                "created": chat.get(
                    "created",
                    0,
                ),
                "requests": chat.get(
                    "requests",
                    0,
                ),
                "messages": [
                    {
                        "role": (
                            "assistant"
                            if item.get("role") == "assistant"
                            else item.get("role", "user")
                        ),
                        "content": item.get(
                            "content",
                            "",
                        ),
                    }
                    for item in history
                    if isinstance(item, dict)
                ],
            }
        )

    memory = user.get(
        "memory",
        {},
    )

    # Не отдаём внутренние поля.
    return {
        "model": user.get(
            "model",
            "auto",
        ),
        "style": user.get(
            "style",
            "normal",
        ),
        "memory": memory,
        "active_chat": user.get(
            "active_chat",
            "main",
        ),
        "chats": result_chats,
        "requests": user.get(
            "requests",
            0,
        ),
        "errors": user.get(
            "errors",
            0,
        ),
        "telegram_user": user.get(
            "_telegram_user",
            {},
        ),
        "total_requests": bot.db.get(
            "total_requests",
            0,
        ),
        "total_errors": bot.db.get(
            "total_errors",
            0,
        ),
        "version": getattr(
            bot,
            "BOT_VERSION",
            "V15",
        ),
    }


def get_chat_title(chat_id, chat):
    title = chat.get("title")

    if title:
        return str(title)

    history = chat.get(
        "history",
        [],
    )

    for item in history:
        if item.get("role") == "user":
            text = str(
                item.get(
                    "content",
                    "",
                )
            ).strip()

            if text:
                return text[:40]

    return str(chat_id)


def make_message_history(user, chat):
    messages = []

    system_prompt = bot.style_system(user)

    if system_prompt:
        messages.append(
            {
                "role": "system",
                "content": system_prompt,
            }
        )

    for item in chat.get(
        "history",
        [],
    )[-bot.MAX_HISTORY:]:
        role = item.get(
            "role",
            "user",
        )

        content = item.get(
            "content",
            "",
        )

        if not content:
            continue

        if role not in (
            "user",
            "assistant",
            "system",
        ):
            continue

        messages.append(
            {
                "role": role,
                "content": content,
            }
        )

    return messages


def get_available_models():
    models = bot.get_models()

    result = []

    for item in models:
        if not isinstance(item, dict):
            continue

        model_id = item.get("id")

        if not model_id:
            continue

        if bot.is_bad_model(model_id):
            continue

        result.append(
            {
                "id": model_id,
                "name": model_id,
            }
        )

    return result


def select_model(user, requested_model):
    if not requested_model:
        return user.get(
            "model",
            "auto",
        )

    requested_model = str(
        requested_model
    ).strip()

    if requested_model == "auto":
        user["model"] = "auto"
        return "auto"

    available = {
        item["id"]
        for item in get_available_models()
    }

    if requested_model not in available:
        raise ValueError(
            "Выбранная модель недоступна."
        )

    user["model"] = requested_model

    return requested_model


def choose_ai_model(user, preferred=None):
    if preferred and preferred != "auto":
        return preferred

    configured = user.get(
        "model",
        "auto",
    )

    if configured and configured != "auto":
        return configured

    candidates = bot.choose_model(
        user,
        vision=False,
    )

    if not candidates:
        raise RuntimeError(
            "Нет доступных AI-моделей."
        )

    return candidates[0]


class BulbaHandler(BaseHTTPRequestHandler):

    server_version = "BulbaAPI/1.0"

    def log_message(self, fmt, *args):
        print(
            "[WEB]",
            fmt % args,
        )

    def send_error_json(
        self,
        status,
        message,
    ):
        json_response(
            self,
            status,
            {
                "ok": False,
                "error": str(message),
            },
        )

    def do_OPTIONS(self):
        json_response(
            self,
            204,
            {},
        )

    def do_GET(self):
        if self.path == "/health":
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "service": "BulbaMaxAI",
                    "version": getattr(
                        bot,
                        "BOT_VERSION",
                        "V15",
                    ),
                },
            )
            return

        try:
            user = get_authenticated_user(
                self
            )
        except Exception as e:
            self.send_error_json(
                401,
                str(e),
            )
            return

        path = urllib.parse.urlparse(
            self.path
        ).path

        if path == "/api/models":
            if not require_access(user):
                self.send_error_json(
                    403,
                    "Доступ запрещён.",
                )
                return

            json_response(
                self,
                200,
                {
                    "ok": True,
                    "models": get_available_models(),
                    "selected": user.get(
                        "model",
                        "auto",
                    ),
                },
            )
            return

        if path == "/api/state":
            if not require_access(user):
                self.send_error_json(
                    403,
                    "Доступ запрещён.",
                )
                return

            json_response(
                self,
                200,
                {
                    "ok": True,
                    "state": clean_user_for_frontend(
                        user
                    ),
                },
            )
            return

        self.send_error_json(
            404,
            "Endpoint не найден.",
        )

    def do_POST(self):
        try:
            user = get_authenticated_user(
                self
            )
        except Exception as e:
            self.send_error_json(
                401,
                str(e),
            )
            return

        if not require_access(user):
            self.send_error_json(
                403,
                "Доступ запрещён.",
            )
            return

        path = urllib.parse.urlparse(
            self.path
        ).path

        try:
            data = parse_json_body(
                self
            )
        except Exception as e:
            self.send_error_json(
                400,
                str(e),
            )
            return

        try:
            if path == "/api/chat":
                self.handle_chat(
                    user,
                    data,
                )
                return

            if path == "/api/chat/new":
                self.handle_new_chat(
                    user,
                    data,
                )
                return

            if path == "/api/chat/select":
                self.handle_select_chat(
                    user,
                    data,
                )
                return

            if path == "/api/memory":
                self.handle_memory(
                    user,
                    data,
                )
                return

            if path == "/api/settings":
                self.handle_settings(
                    user,
                    data,
                )
                return

            self.send_error_json(
                404,
                "Endpoint не найден.",
            )

        except Exception as e:
            print(
                "API error:",
                repr(e),
            )

            try:
                user["errors"] = (
                    user.get(
                        "errors",
                        0,
                    )
                    + 1
                )

                bot.db["total_errors"] = (
                    bot.db.get(
                        "total_errors",
                        0,
                    )
                    + 1
                )

                bot.save_db()
            except Exception:
                pass

            self.send_error_json(
                500,
                str(e),
            )

    def handle_chat(
        self,
        user,
        data,
    ):
        text = str(
            data.get(
                "message",
                "",
            )
        ).strip()

        if not text:
            self.send_error_json(
                400,
                "Сообщение пустое.",
            )
            return

        if len(text) > 12000:
            self.send_error_json(
                400,
                "Сообщение слишком длинное.",
            )
            return

        if not bot.allowed_request(
            user
        ):
            self.send_error_json(
                429,
                "Слишком много запросов. Попробуй немного позже.",
            )
            return

        requested_model = data.get(
            "model"
        )

        selected_model = select_model(
            user,
            requested_model,
        )

        requested_style = data.get(
            "style"
        )

        if requested_style:
            requested_style = str(
                requested_style
            ).strip()

            if requested_style in (
                "normal",
                "short",
                "detailed",
            ):
                user["style"] = requested_style

        chat_id = data.get(
            "chat_id"
        )

        if chat_id:
            chat_id = str(
                chat_id
            )

            if chat_id in user["chats"]:
                user["active_chat"] = chat_id

        chat = bot.get_chat(
            user
        )

        # Поддерживаем название чата,
        # если фронтенд его передал.
        chat_name = data.get(
            "chat_name"
        )

        if chat_name:
            chat["title"] = str(
                chat_name
            )[:80]

        history = chat.setdefault(
            "history",
            [],
        )

        history.append(
            {
                "role": "user",
                "content": text,
            }
        )

        history[:] = history[
            -bot.MAX_HISTORY:
        ]

        chat["last_prompt"] = text
        chat["last_request"] = int(
            time.time()
        )

        messages = make_message_history(
            user,
            chat,
        )

        ai_model = choose_ai_model(
            user,
            preferred=selected_model,
        )

        answer = None
        error = None

        try:
            answer = bot.ai_chat(
                user,
                messages,
                vision=False,
            )
        except Exception as e:
            error = str(e)

        if not answer:
            if error:
                raise RuntimeError(
                    error
                )

            raise RuntimeError(
                "AI не вернул ответ."
            )

        answer = str(
            answer
        ).strip()

        history.append(
            {
                "role": "assistant",
                "content": answer,
            }
        )

        history[:] = history[
            -bot.MAX_HISTORY:
        ]

        chat["requests"] = (
            chat.get(
                "requests",
                0,
            )
            + 1
        )

        user["requests"] = (
            user.get(
                "requests",
                0,
            )
            + 1
        )

        bot.db["total_requests"] = (
            bot.db.get(
                "total_requests",
                0,
            )
            + 1
        )

        try:
            bot.consume_license_after_success(
                user
            )
        except Exception as e:
            print(
                "License consume error:",
                repr(e),
            )

        bot.save_db()

        json_response(
            self,
            200,
            {
                "ok": True,
                "answer": answer,
                "model": ai_model,
                "chat": {
                    "id": str(
                        user.get(
                            "active_chat",
                            "main",
                        )
                    ),
                    "title": get_chat_title(
                        user.get(
                            "active_chat",
                            "main",
                        ),
                        chat,
                    ),
                },
                "state": clean_user_for_frontend(
                    user
                ),
            },
        )

    def handle_new_chat(
        self,
        user,
        data,
    ):
        requested_name = data.get(
            "title"
        )

        chat_id = None

        try:
            result = bot.new_chat(
                user
            )

            if isinstance(
                result,
                str,
            ):
                chat_id = result
        except Exception as e:
            print(
                "new_chat compatibility:",
                repr(e),
            )

        if not chat_id:
            base = "chat"

            index = 1

            while (
                f"{base}_{index}"
                in user["chats"]
            ):
                index += 1

            chat_id = (
                f"{base}_{index}"
            )

            user["chats"][
                chat_id
            ] = bot.default_chat()

            user["active_chat"] = chat_id

        chat = user["chats"][
            chat_id
        ]

        if requested_name:
            chat["title"] = str(
                requested_name
            )[:80]

        bot.save_db()

        json_response(
            self,
            200,
            {
                "ok": True,
                "chat_id": str(
                    chat_id
                ),
                "state": clean_user_for_frontend(
                    user
                ),
            },
        )

    def handle_select_chat(
        self,
        user,
        data,
    ):
        chat_id = str(
            data.get(
                "chat_id",
                "",
            )
        )

        if not chat_id:
            self.send_error_json(
                400,
                "chat_id отсутствует.",
            )
            return

        if chat_id not in user["chats"]:
            self.send_error_json(
                404,
                "Чат не найден.",
            )
            return

        user["active_chat"] = chat_id

        bot.save_db()

        json_response(
            self,
            200,
            {
                "ok": True,
                "active_chat": chat_id,
                "state": clean_user_for_frontend(
                    user
                ),
            },
        )

    def handle_memory(
        self,
        user,
        data,
    ):
        action = str(
            data.get(
                "action",
                "get",
            )
        ).lower()

        if action == "clear":
            user["memory"] = {}

            bot.save_db()

            json_response(
                self,
                200,
                {
                    "ok": True,
                    "memory": {},
                },
            )
            return

        if action == "set":
            memory = data.get(
                "memory",
                {},
            )

            if not isinstance(
                memory,
                dict,
            ):
                self.send_error_json(
                    400,
                    "memory должен быть объектом.",
                )
                return

            user["memory"] = memory

            bot.save_db()

            json_response(
                self,
                200,
                {
                    "ok": True,
                    "memory": memory,
                },
            )
            return

        json_response(
            self,
            200,
            {
                "ok": True,
                "memory": user.get(
                    "memory",
                    {},
                ),
            },
        )

    def handle_settings(
        self,
        user,
        data,
    ):
        model = data.get(
            "model"
        )

        style = data.get(
            "style"
        )

        if model is not None:
            select_model(
                user,
                model,
            )

        if style is not None:
            style = str(
                style
            ).strip()

            if style not in (
                "normal",
                "short",
                "detailed",
            ):
                self.send_error_json(
                    400,
                    "Неизвестный стиль.",
                )
                return

            user["style"] = style

        bot.save_db()

        json_response(
            self,
            200,
            {
                "ok": True,
                "settings": {
                    "model": user.get(
                        "model",
                        "auto",
                    ),
                    "style": user.get(
                        "style",
                        "normal",
                    ),
                },
            },
        )


def run_server():
    # Загружаем общую БД до запуска API.
    bot.load_db()

    server = ThreadingHTTPServer(
        (
            HOST,
            PORT,
        ),
        BulbaHandler,
    )

    print(
        f"Bulba Web API started on "
        f"{HOST}:{PORT}"
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(
            "Bulba Web API stopped."
        )
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()
