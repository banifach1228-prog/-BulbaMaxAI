import os
import time
import threading
import requests


PLUSVIBE_URL = "https://plusvibeapi.ru"
IMAGE_MODEL = "gpt-image-1-5"

POLL_INTERVAL = 3
POLL_TIMEOUT = 90

IMAGE_MAX_BYTES = 10 * 1024 * 1024

_PENDING = set()
_PENDING_LOCK = threading.Lock()


def install(bot):
    """Подключает тестовую генерацию изображений только к Telegram-боту."""

    original_keyboard = bot.main_keyboard
    original_process_message = bot.process_message

    def image_keyboard():
        keyboard = original_keyboard()
        rows = list(keyboard.get("keyboard", []))

        if not any(
            any(
                button.get("text") == "🖼️ Изображение"
                for button in row
            )
            for row in rows
        ):
            rows.append([
                {"text": "🖼️ Изображение"}
            ])

        keyboard["keyboard"] = rows
        return keyboard

    bot.main_keyboard = image_keyboard

    def wrapped_process_message(msg):
        if "chat" not in msg:
            return original_process_message(msg)

        user_id = msg.get(
            "from",
            {}
        ).get(
            "id",
            msg["chat"]["id"]
        )

        text = (
            msg.get("text") or ""
        ).strip()

        chat_id = msg["chat"]["id"]

        with bot.get_user_lock(user_id):

            if text == "🖼️ Изображение":

                with _PENDING_LOCK:
                    _PENDING.add(
                        str(user_id)
                    )

                bot.send_message(
                    chat_id,
                    "🖼️ Генерация изображения\n\n"
                    "Отправь одним сообщением описание картинки.\n\n"
                    "Или используй: /image описание",
                    image_keyboard(),
                )

                return

            if text.startswith("/image"):

                prompt = text[
                    len("/image"):
                ].strip()

                if not prompt:

                    with _PENDING_LOCK:
                        _PENDING.add(
                            str(user_id)
                        )

                    bot.send_message(
                        chat_id,
                        "🖼️ Напиши описание после /image.",
                        image_keyboard(),
                    )

                    return

                with _PENDING_LOCK:
                    _PENDING.discard(
                        str(user_id)
                    )

                generate_image(
                    bot,
                    chat_id,
                    user_id,
                    prompt,
                )

                return

            with _PENDING_LOCK:
                pending = (
                    str(user_id)
                    in _PENDING
                )

            if (
                pending
                and text
                and not text.startswith("/")
            ):

                with _PENDING_LOCK:
                    _PENDING.discard(
                        str(user_id)
                    )

                generate_image(
                    bot,
                    chat_id,
                    user_id,
                    text,
                )

                return

        return original_process_message(msg)

    bot.process_message = wrapped_process_message

    print(
        "Image Generator: Telegram test mode installed"
    )


def generate_image(
    bot,
    chat_id,
    user_id,
    prompt,
):
    api_key = os.getenv(
        "PLUSVIBE_API_KEY",
        "",
    ).strip()

    if not api_key:
        bot.send_message(
            chat_id,
            "❌ PLUSVIBE_API_KEY не настроен на сервере.",
            bot.main_keyboard(),
        )
        return

    u = bot.get_user(user_id)

    if not bot.allowed_request(u):
        bot.send_message(
            chat_id,
            "⏳ Слишком много запросов. Подожди несколько секунд.",
            bot.main_keyboard(),
        )
        return

    if (
        os.getenv(
            "LICENSE_REQUIRED",
            "0",
        ).strip() == "1"
        and not bot.is_admin(user_id)
    ):
        allowed, reason = bot.user_has_access(
            user_id
        )

        if not allowed:
            bot.send_message(
                chat_id,
                reason
                + "\n\n🔑 Активируй доступ командой /activate КОД.",
                bot.main_keyboard(),
            )
            return

    prompt = prompt.strip()[:8000]

    if not prompt:
        bot.send_message(
            chat_id,
            "❌ Описание изображения пустое.",
            bot.main_keyboard(),
        )
        return

    status = bot.send_message(
        chat_id,
        "🖼️ Создаю изображение…\n\n"
        "Модель: GPT Image 1.5\n"
        "Качество: Medium\n"
        "Формат: 1:1",
    )

    try:

        response = requests.post(
            f"{PLUSVIBE_URL}/api/media/generate",
            headers={
                "Authorization": (
                    f"Bearer {api_key}"
                ),
                "Content-Type": (
                    "application/json"
                ),
            },
            json={
                "model": IMAGE_MODEL,
                "prompt": prompt,
                "opts": {
                    "mode": "text-to-image",
                    "aspect_ratio": "1:1",
                    "quality": "medium",
                },
            },
            timeout=30,
        )

        if not response.ok:
            raise RuntimeError(
                _api_error(response)
            )

        data = response.json()

        job_id = data.get(
            "jobId"
        )

        if not job_id:
            raise RuntimeError(
                "PlusVibe не вернул jobId."
            )

        result = poll_job(
            api_key,
            job_id,
        )

        urls = (
            result.get("resultUrls")
            or []
        )

        if not urls:
            raise RuntimeError(
                "PlusVibe завершил задачу без изображения."
            )

        image_response = requests.get(
            urls[0],
            timeout=60,
        )

        if not image_response.ok:
            raise RuntimeError(
                "Не удалось скачать готовое изображение."
            )

        if (
            len(image_response.content)
            > IMAGE_MAX_BYTES
        ):
            raise RuntimeError(
                "Готовое изображение слишком большое для Telegram."
            )

        price = result.get(
            "priceRub"
        )

        caption = (
            "🖼️ Готово — GPT Image 1.5"
        )

        if isinstance(
            price,
            (int, float),
        ) and price > 0:
            caption += (
                f"\nСтоимость: {price:.2f} ₽"
            )

        sent = bot.tg(
            "sendPhoto",
            {
                "chat_id": chat_id,
                "caption": caption,
            },
            files={
                "photo": (
                    "bulba_image.png",
                    image_response.content,
                )
            },
            timeout=60,
        )

        if not sent:
            raise RuntimeError(
                "Telegram не принял изображение."
            )

        u["requests"] += 1

        bot.db[
            "total_requests"
        ] += 1

        chat = bot.get_chat(u)

        chat["requests"] += 1

        chat["last_prompt"] = prompt

        chat["last_request"] = {
            "kind": "image_generation",
            "text": prompt,
        }

        bot.add_history(
            u,
            "user",
            f"🖼️ {prompt}",
        )

        bot.add_history(
            u,
            "assistant",
            "Изображение сгенерировано.",
        )

        bot.save_db()

        bot.consume_license_after_success(
            user_id
        )

        if status:

            bot.edit_message(
                chat_id,
                status["message_id"],
                "✅ Изображение готово и отправлено выше.",
                bot.main_keyboard(),
            )

    except Exception as e:

        print(
            "Image generation error:",
            repr(e),
        )

        message = (
            "❌ Не удалось создать изображение.\n\n"
            f"{e}"
        )

        if status:

            bot.edit_message(
                chat_id,
                status["message_id"],
                message,
                bot.main_keyboard(),
            )

        else:

            bot.send_message(
                chat_id,
                message,
                bot.main_keyboard(),
            )


def poll_job(
    api_key,
    job_id,
):
    deadline = (
        time.monotonic()
        + POLL_TIMEOUT
    )

    while (
        time.monotonic()
        < deadline
    ):

        response = requests.get(
            f"{PLUSVIBE_URL}/api/media/jobs/{job_id}",
            headers={
                "Authorization": (
                    f"Bearer {api_key}"
                )
            },
            timeout=20,
        )

        if not response.ok:
            raise RuntimeError(
                _api_error(response)
            )

        data = response.json()

        status = data.get(
            "status"
        )

        if status == "success":
            return data

        if status == "fail":
            raise RuntimeError(
                data.get("failMsg")
                or "Генерация завершилась ошибкой."
            )

        time.sleep(
            POLL_INTERVAL
        )

    raise RuntimeError(
        "Генерация заняла слишком много времени. Попробуй ещё раз."
    )


def _api_error(response):
    try:

        data = response.json()

        message = (
            data.get("message")
            or data.get("error")
            or data.get("failMsg")
        )

        code = data.get(
            "errorCode"
        )

        if code and message:
            return (
                f"{code}: {message}"
            )

        if message:
            return str(message)

    except Exception:
        pass

    text = (
        response.text or ""
    ).strip()

    return (
        f"PlusVibe HTTP {response.status_code}"
        + (
            f": {text[:300]}"
            if text
            else ""
        )
    )