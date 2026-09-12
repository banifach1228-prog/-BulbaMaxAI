import os
import json
import time
import hmac
import hashlib
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bot


HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "8080"))

MAX_BODY = 12 * 1024 * 1024
MAX_MESSAGE = 12000
MAX_IMAGE_BASE64 = 10 * 1024 * 1024
AUTH_MAX_AGE = 24 * 60 * 60


def send_json(handler, status, data):
    body = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    handler.send_response(status)
    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8",
    )
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()

    if status != 204:
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
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ValueError("Некорректный JSON.")

    if not isinstance(data, dict):
        raise ValueError("JSON должен быть объектом.")

    return data


def validate_init_data(init_data):
    if not init_data:
        raise ValueError(
            "Открой Bulba AI через Telegram."
        )

    token = os.getenv("BOT_TOKEN", "").strip()

    if not token:
        raise RuntimeError(
            "BOT_TOKEN не установлен."
        )

    parsed = urllib.parse.parse_qs(
        init_data,
        keep_blank_values=True,
    )

    received_hash = parsed.get(
        "hash",
        [None],
    )[0]

    if not received_hash:
        raise ValueError(
            "В Telegram initData отсутствует hash."
        )

    auth_date_raw = parsed.get(
        "auth_date",
        [None],
    )[0]

    if not auth_date_raw:
        raise ValueError(
            "В Telegram initData отсутствует auth_date."
        )

    try:
        auth_date = int(auth_date_raw)
    except ValueError:
        raise ValueError(
            "Некорректный auth_date."
        )

    if abs(int(time.time()) - auth_date) > AUTH_MAX_AGE:
        raise ValueError(
            "Сессия Telegram устарела. "
            "Перезапусти Mini App."
        )

    pairs = []

    for key in sorted(parsed):
        if key == "hash":
            continue

        pairs.append(
            f"{key}={parsed[key][0]}"
        )

    data_check_string = "\n".join(pairs)

    secret_key = hmac.new(
        b"WebAppData",
        token.encode("utf-8"),
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
        raise ValueError(
            "Неверная подпись Telegram."
        )

    user_raw = parsed.get(
        "user",
        [None],
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
        "",
    ).strip()

    tg_user = validate_init_data(init_data)

    uid = str(tg_user["id"])

    user = bot.get_user(uid)

    user["_user_id"] = uid

    user["_telegram_user"] = {
        "id": tg_user.get("id"),
        "first_name": tg_user.get(
            "first_name",
            "",
        ),
        "last_name": tg_user.get(
            "last_name",
            "",
        ),
        "username": tg_user.get(
            "username",
            "",
        ),
        "language_code": tg_user.get(
            "language_code",
            "",
        ),
        "photo_url": tg_user.get(
            "photo_url",
            "",
        ),
    }

    return user


def require_access(user):
    if (
        os.getenv(
            "LICENSE_REQUIRED",
            "0",
        ).strip()
        != "1"
    ):
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


def available_models():
    result = []

    for item in bot.get_models():
        if not isinstance(item, dict):
            continue

        mid = item.get("id")

        if not mid:
            continue

        if bot.is_bad_model(mid):
            continue

        result.append(
            {
                "id": str(mid),
                "name": str(mid),
            }
        )

    return result


def validate_model(model):
    model = (
        "auto"
        if model is None
        else str(model).strip()
    )

    if model == "auto":
        return "auto"

    available = {
        x["id"]
        for x in available_models()
    }

    if model not in available:
        raise ValueError(
            "Выбранная модель недоступна."
        )

    return model


def chat_title(chat_id, chat):
    title = str(
        chat.get(
            "title",
            "",
        )
    ).strip()

    if title:
        return title

    last_prompt = str(
        chat.get(
            "last_prompt",
            "",
        )
    ).strip()

    if last_prompt:
        clean = " ".join(
            last_prompt.split()
        )

        return clean[:42] + (
            "…" if len(clean) > 42 else ""
        )

    if str(chat_id) != "main":
        return "Новый чат"

    return "Главный чат"


def frontend_state(user):
    chats = []

    for chat_id, chat in user.get(
        "chats",
        {},
    ).items():

        if not isinstance(chat, dict):
            continue

        messages = []

        for item in chat.get(
            "history",
            [],
        ):

            if not isinstance(item, dict):
                continue

            role = item.get(
                "role",
                "user",
            )

            if role not in (
                "user",
                "assistant",
            ):
                continue

            messages.append(
                {
                    "role": role,
                    "content": str(
                        item.get(
                            "content",
                            "",
                        )
                    ),
                }
            )

        last_request = chat.get(
            "last_request"
        )

        if isinstance(
            last_request,
            (int, float),
        ):
            last_request_value = int(
                last_request
            )
        else:
            last_request_value = 0

        chats.append(
            {
                "id": str(chat_id),
                "title": chat_title(
                    chat_id,
                    chat,
                ),
                "messages": messages,
                "requests": int(
                    chat.get(
                        "requests",
                        0,
                    )
                    or 0
                ),
                "created": int(
                    chat.get(
                        "created",
                        0,
                    )
                    or 0
                ),
                "last_request":
                    last_request_value,
            }
        )

    chats.sort(
        key=lambda x: (
            x["last_request"],
            x["created"],
        ),
        reverse=True,
    )

    return {
        "model": user.get(
            "model",
            "auto",
        ),
        "style": user.get(
            "style",
            "normal",
        ),
        "active_chat": str(
            user.get(
                "active_chat",
                "main",
            )
        ),
        "chats": chats,
        "memory": user.get(
            "memory",
            {},
        ),
        "requests": int(
            user.get(
                "requests",
                0,
            )
            or 0
        ),
        "errors": int(
            user.get(
                "errors",
                0,
            )
            or 0
        ),
        "telegram_user": user.get(
            "_telegram_user",
            {},
        ),
        "version": getattr(
            bot,
            "BOT_VERSION",
            "V15",
        ),
    }


def make_messages(
    user,
    chat,
    current_content,
):
    messages = [
        {
            "role": "system",
            "content": bot.style_system(
                user
            ),
        }
    ]

    # Текущий запрос не добавляем
    # в history до вызова API.
    for item in chat.get(
        "history",
        [],
    )[-bot.MAX_HISTORY:]:

        if not isinstance(
            item,
            dict,
        ):
            continue

        role = item.get("role")
        content = item.get("content")

        if (
            role in (
                "user",
                "assistant",
            )
            and content
        ):
            messages.append(
                {
                    "role": role,
                    "content": content,
                }
            )

    messages.append(
        {
            "role": "user",
            "content": current_content,
        }
    )

    return messages


def make_image_content(
    text,
    image,
):
    if not image:
        return text

    if not isinstance(
        image,
        str,
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
            "Поддерживаются только изображения."
        )

    return [
        {
            "type": "text",
            "text": (
                text
                or "Проанализируй изображение."
            ),
        },
        {
            "type": "image_url",
            "image_url": {
                "url": image
            },
        },
    ]


def create_chat(user):
    chat_id = bot.new_chat(user)
    bot.save_db()
    return str(chat_id)


def rename_chat(
    user,
    chat_id,
    title,
):
    chat_id = str(chat_id)

    if chat_id not in user["chats"]:
        raise ValueError(
            "Чат не найден."
        )

    title = " ".join(
        str(title or "").split()
    ).strip()

    if not title:
        raise ValueError(
            "Название не может быть пустым."
        )

    if len(title) > 60:
        title = (
            title[:60].rstrip()
            + "…"
        )

    user["chats"][chat_id][
        "title"
    ] = title

    bot.save_db()


def delete_chat(
    user,
    chat_id,
):
    chat_id = str(chat_id)

    if chat_id not in user["chats"]:
        raise ValueError(
            "Чат не найден."
        )

    if len(user["chats"]) <= 1:
        user["chats"] = {
            "main": bot.default_chat()
        }

        user["active_chat"] = "main"

    else:
        del user["chats"][chat_id]

        if (
            user.get("active_chat")
            == chat_id
        ):
            user["active_chat"] = next(
                iter(user["chats"])
            )

    bot.save_db()


def clear_chat(
    user,
    chat_id,
):
    chat_id = str(chat_id)

    if chat_id not in user["chats"]:
        raise ValueError(
            "Чат не найден."
        )

    chat = user["chats"][chat_id]

    chat["history"] = []
    chat["last_prompt"] = None
    chat["last_request"] = None

    if chat_id == "main":
        chat.pop(
            "title",
            None,
        )

    bot.save_db()


def handle_chat(
    user,
    data,
):
    text = str(
        data.get(
            "message",
            "",
        )
    ).strip()

    image = data.get("image")

    if not text and not image:
        raise ValueError(
            "Сообщение пустое."
        )

    if len(text) > MAX_MESSAGE:
        raise ValueError(
            "Сообщение слишком длинное."
        )

    requested_model = validate_model(
        data.get(
            "model",
            user.get(
                "model",
                "auto",
            ),
        )
    )

    chat_id = str(
        data.get(
            "chat_id",
            user.get(
                "active_chat",
                "main",
            ),
        )
    )

    if chat_id not in user["chats"]:
        chat_id = str(
            user.get(
                "active_chat",
                "main",
            )
        )

    if chat_id not in user["chats"]:
        chat_id = "main"

    user["active_chat"] = chat_id

    if not bot.allowed_request(user):
        raise ValueError(
            "Слишком много запросов. "
            "Подожди немного."
        )

    if not require_access(user):
        raise PermissionError(
            "Для использования Bulba AI "
            "нужна активная лицензия."
        )

    vision = bool(image)

    if requested_model == "auto":
        model = bot.choose_model(
            user,
            vision=vision,
        )
    else:
        model = bot.choose_model(
            user,
            vision=vision,
            preferred=requested_model,
        )

    content = make_image_content(
        text,
        image,
    )

    chat = user["chats"][chat_id]

    messages = make_messages(
        user,
        chat,
        content,
    )

    answer, error, status = bot.call_ai(
        messages,
        model,
    )

    if error:
        user["errors"] = int(
            user.get(
                "errors",
                0,
            )
            or 0
        ) + 1

        bot.save_db()

        raise RuntimeError(
            error
        )

    answer = str(
        answer or ""
    ).strip()

    if not answer:
        answer = "Пустой ответ от AI."

    chat["history"].append(
        {
            "role": "user",
            "content": (
                text
                if text
                else "[Изображение]"
            ),
        }
    )

    chat["history"].append(
        {
            "role": "assistant",
            "content": answer,
        }
    )

    chat["history"] = chat[
        "history"
    ][-bot.MAX_HISTORY:]

    chat["last_prompt"] = (
        text
        if text
        else "Изображение"
    )

    chat["last_request"] = int(
        time.time()
    )

    chat["requests"] = int(
        chat.get(
            "requests",
            0,
        )
        or 0
    ) + 1

    user["requests"] = int(
        user.get(
            "requests",
            0,
        )
        or 0
    ) + 1

    try:
        bot.consume_license_after_success(
            user
        )
    except TypeError:
        try:
            bot.consume_license_after_success(
                user.get("_user_id")
            )
        except Exception:
            pass
    except Exception:
        pass

    bot.save_db()

    return {
        "answer": answer,
        "model": model,
        "state": frontend_state(user),
        "status": status,
    }


def serve_index(handler):
    path = (
        Path(__file__).resolve().parent
        / "miniapp"
        / "index.html"
    )

    if not path.exists():
        send_json(
            handler,
            404,
            {
                "error":
                    "Mini App index.html не найден."
            },
        )
        return

    body = path.read_bytes()

    handler.send_response(200)
    handler.send_header(
        "Content-Type",
        "text/html; charset=utf-8",
    )
    handler.send_header(
        "Cache-Control",
        "no-store",
    )
    handler.send_header(
        "Content-Length",
        str(len(body)),
    )
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):

    def log_message(
        self,
        format,
        *args,
    ):
        print(
            f"[MiniApp] {self.address_string()} "
            f"- {format % args}"
        )

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Telegram-Init-Data",
        )
        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS",
        )
        self.end_headers()

    def do_GET(self):
        try:
            if self.path in (
                "/",
                "/index.html",
            ):
                serve_index(self)
                return

            if self.path == "/health":
                send_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "version": getattr(
                            bot,
                            "BOT_VERSION",
                            "V15",
                        ),
                    },
                )
                return

            if self.path == "/api/state":
                user = get_user_from_request(
                    self
                )

                if not require_access(user):
                    raise PermissionError(
                        "Нет доступа."
                    )

                send_json(
                    self,
                    200,
                    {
                        "state":
                            frontend_state(user)
                    },
                )
                return

            if self.path == "/api/models":
                user = get_user_from_request(
                    self
                )

                if not require_access(user):
                    raise PermissionError(
                        "Нет доступа."
                    )

                send_json(
                    self,
                    200,
                    {
                        "models":
                            available_models()
                    },
                )
                return

            if self.path == "/api/stats":
                user = get_user_from_request(
                    self
                )

                if not require_access(user):
                    raise PermissionError(
                        "Нет доступа."
                    )

                send_json(
                    self,
                    200,
                    {
                        "stats": {
                            "requests":
                                int(
                                    user.get(
                                        "requests",
                                        0,
                                    )
                                    or 0
                                ),
                            "errors":
                                int(
                                    user.get(
                                        "errors",
                                        0,
                                    )
                                    or 0
                                ),
                            "chats":
                                len(
                                    user.get(
                                        "chats",
                                        {},
                                    )
                                ),
                            "version":
                                getattr(
                                    bot,
                                    "BOT_VERSION",
                                    "V15",
                                ),
                        }
                    },
                )
                return

            send_json(
                self,
                404,
                {
                    "error":
                        "Страница не найдена."
                },
            )

        except PermissionError as e:
            send_json(
                self,
                403,
                {"error": str(e)},
            )

        except Exception as e:
            print(
                "[MiniApp GET ERROR]",
                repr(e),
            )

            send_json(
                self,
                500,
                {
                    "error":
                        str(e)
                        or "Ошибка сервера."
                },
            )

    def do_POST(self):
        try:
            user = get_user_from_request(
                self
            )

            if not require_access(user):
                raise PermissionError(
                    "Нет доступа."
                )

            data = read_json(self)

            if self.path == "/api/chat":
                result = handle_chat(
                    user,
                    data,
                )

                send_json(
                    self,
                    200,
                    result,
                )
                return

            if self.path == "/api/chat/new":
                chat_id = create_chat(
                    user
                )

                send_json(
                    self,
                    200,
                    {
                        "state":
                            frontend_state(user),
                        "chat_id":
                            chat_id,
                    },
                )
                return

            if self.path == "/api/chat/select":
                chat_id = str(
                    data.get(
                        "chat_id",
                        "",
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
                        "state":
                            frontend_state(user)
                    },
                )
                return

            if self.path == "/api/chat/rename":
                rename_chat(
                    user,
                    data.get(
                        "chat_id"
                    ),
                    data.get(
                        "title"
                    ),
                )

                send_json(
                    self,
                    200,
                    {
                        "state":
                            frontend_state(user)
                    },
                )
                return

            if self.path == "/api/chat/delete":
                delete_chat(
                    user,
                    data.get(
                        "chat_id"
                    ),
                )

                send_json(
                    self,
                    200,
                    {
                        "state":
                            frontend_state(user)
                    },
                )
                return

            if self.path == "/api/chat/clear":
                clear_chat(
                    user,
                    data.get(
                        "chat_id",
                        user.get(
                            "active_chat",
                            "main",
                        ),
                    ),
                )

                send_json(
                    self,
                    200,
                    {
                        "state":
                            frontend_state(user)
                    },
                )
                return

            if self.path == "/api/settings":
                changed = False

                if "model" in data:
                    user["model"] = validate_model(
                        data["model"]
                    )
                    changed = True

                if "style" in data:
                    style = str(
                        data["style"]
                    ).strip()

                    if style not in (
                        "normal",
                        "short",
                        "detailed",
                    ):
                        raise ValueError(
                            "Некорректный стиль."
                        )

                    user["style"] = style
                    changed = True

                if changed:
                    bot.save_db()

                send_json(
                    self,
                    200,
                    {
                        "settings": {
                            "model":
                                user.get(
                                    "model",
                                    "auto",
                                ),
                            "style":
                                user.get(
                                    "style",
                                    "normal",
                                ),
                        }
                    },
                )
                return

            if self.path == "/api/memory":
                action = str(
                    data.get(
                        "action",
                        "",
                    )
                ).strip()

                if action == "clear":
                    user["memory"] = {}
                    bot.save_db()

                send_json(
                    self,
                    200,
                    {
                        "memory":
                            user.get(
                                "memory",
                                {},
                            )
                    },
                )
                return

            if self.path == "/api/stats":
                send_json(
                    self,
                    200,
                    {
                        "stats": {
                            "requests":
                                int(
                                    user.get(
                                        "requests",
                                        0,
                                    )
                                    or 0
                                ),
                            "errors":
                                int(
                                    user.get(
                                        "errors",
                                        0,
                                    )
                                    or 0
                                ),
                            "chats":
                                len(
                                    user.get(
                                        "chats",
                                        {},
                                    )
                                ),
                            "version":
                                getattr(
                                    bot,
                                    "BOT_VERSION",
                                    "V15",
                                ),
                        }
                    },
                )
                return

            send_json(
                self,
                404,
                {
                    "error":
                        "API endpoint не найден."
                },
            )

        except PermissionError as e:
            send_json(
                self,
                403,
                {"error": str(e)},
            )

        except ValueError as e:
            send_json(
                self,
                400,
                {"error": str(e)},
            )

        except Exception as e:
            print(
                "[MiniApp POST ERROR]",
                repr(e),
            )

            send_json(
                self,
                500,
                {
                    "error":
                        str(e)
                        or "Ошибка сервера."
                },
            )


def run_server():
    bot.load_db()

    server = ThreadingHTTPServer(
        (HOST, PORT),
        Handler,
    )

    print(
        f"Bulba Mini App server started "
        f"on {HOST}:{PORT}"
    )

    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()
