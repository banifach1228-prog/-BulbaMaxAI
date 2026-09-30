from pathlib import Path
from urllib.request import urlopen
import subprocess
import sys

REPO = "https://raw.githubusercontent.com/banifach1228-prog/-BulbaMaxAI/main"
FILES = ["bot.py", "launcher.py", "licenses.py", "media_service.py", "requirements.txt"]
ROOT = Path(__file__).resolve().parent


def fetch(name: str) -> None:
    target = ROOT / name
    url = f"{REPO}/{name}"
    print(f"[BulbaMaxAI] downloading {name}...")
    with urlopen(url, timeout=30) as r:
        target.write_bytes(r.read())


def main() -> None:
    for name in FILES:
        fetch(name)

    # Install the main bot dependencies. BulbaX itself uses only stdlib.
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])

    # Apply the local, idempotent BulbaX bridge to the freshly downloaded bot.
    subprocess.check_call([sys.executable, "integrate_bulbamaxai.py", "bot.py"])

    print("[BulbaMaxAI] BulbaX integration ready.")
    print("[BulbaMaxAI] Starting launcher.py...")
    subprocess.check_call([sys.executable, "launcher.py"])


if __name__ == "__main__":
    main()
