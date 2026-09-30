from pathlib import Path
import sys


p = Path(sys.argv[1] if len(sys.argv) > 1 else "bot.py")

if not p.exists():
    raise SystemExit(f"bot.py not found: {p}")


s = p.read_text(encoding="utf-8")


# ---------------------------------------------------------
# 1. Import BulbaX bridge
# ---------------------------------------------------------

imp = "from bulbax.telegram_bridge import handle_text as bulbax_handle_text\n"

if imp not in s:
    marker = "import requests\n"

    if marker not in s:
        raise SystemExit(
            "Could not find safe import anchor: import requests"
        )

    s = s.replace(
        marker,
        marker + imp,
        1,
    )


# ---------------------------------------------------------
# 2. Insert BulbaX routing
# ---------------------------------------------------------

anchor = '    if text and not msg.get("photo") and not msg.get("document"):\n'

block = (
    anchor
    + "        bulbax_response = bulbax_handle_text(user_id, text)\n"
    + "        if bulbax_response is not None:\n"
    + "            send_message(chat_id, bulbax_response, main_keyboard(user_id=user_id))\n"
    + "            return\n\n"
)

if block not in s:

    if anchor not in s:
        raise SystemExit(
            'Could not find message-routing anchor in bot.py'
        )

    s = s.replace(
        anchor,
        block,
        1,
    )


p.write_text(
    s,
    encoding="utf-8",
)

print(f"Integrated BulbaX into {p}")