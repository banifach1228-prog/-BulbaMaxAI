from pathlib import Path
import sys


p = Path(sys.argv[1] if len(sys.argv) > 1 else "bot.py")

if not p.exists():
    raise SystemExit(f"bot.py not found: {p}")

s = p.read_text(encoding="utf-8")

# Add the BulbaX bridge import once.
imp = "from bulbax.telegram_bridge import handle_text as bulbax_handle_text\n"
if imp not in s:
    marker = "import requests\n"
    if marker not in s:
        raise SystemExit("Could not find safe import anchor: import requests")
    s = s.replace(marker, marker + "\n" + imp, 1)

# Route BulbaX commands before the existing global-rule and normal-AI handling.
# This is the real V18.10 anchor in bot.py.
marker = '    caption = (msg.get("caption") or "").strip()\n'
block = '''    # BulbaX handles its own commands before normal AI chat.
    # Ordinary messages return None and continue through the existing bot flow.
    if text:
        bulbax_response = bulbax_handle_text(user_id, text)
        if bulbax_response is not None:
            send_message(
                chat_id,
                bulbax_response,
                main_keyboard(user_id=user_id),
            )
            return

'''

if block not in s:
    if marker not in s:
        raise SystemExit("Could not find safe BulbaMaxAI message-routing anchor")
    s = s.replace(marker, block + marker, 1)

p.write_text(s, encoding="utf-8")
print(f"Integrated BulbaX into {p}")
