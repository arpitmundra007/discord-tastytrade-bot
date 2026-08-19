"""
Runs the Discord self-bot listener in a background thread, and the FastAPI
app (dashboard + API endpoints) in the main thread.
"""
import threading

import uvicorn

from app.discord_selfbot import run as run_listener


def main():
    bot_thread = threading.Thread(target=run_listener, daemon=True)
    bot_thread.start()
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
