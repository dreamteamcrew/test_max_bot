"""Простой эхо-бот для Telegram на вебхуке. Только стандартная библиотека Python."""
import hashlib
import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"
# Секрет, по которому бот узнаёт запросы от Telegram (выводится из токена).
SECRET = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
WEBHOOK_PATH = "/webhook"


def call(method, payload):
    req = urllib.request.Request(
        f"{API}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def handle_update(update):
    message = update.get("message")
    if not message:
        return
    chat_id = message["chat"]["id"]
    text = message.get("text")
    if text == "/start":
        reply = "Привет! Я тестовый бот. Напиши что-нибудь, и я повторю."
    elif text:
        reply = text
    else:
        reply = "Я пока понимаю только текст."
    call("sendMessage", {"chat_id": chat_id, "text": reply})


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._respond(200, b"ok")

    def do_HEAD(self):
        self._respond(200, b"")

    def do_POST(self):
        if self.path != WEBHOOK_PATH:
            return self._respond(404, b"not found")
        if self.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
            return self._respond(403, b"forbidden")
        length = int(self.headers.get("Content-Length", 0))
        try:
            handle_update(json.loads(self.rfile.read(length)))
        except Exception as exc:  # не роняем сервер из-за одного сообщения
            print("error:", exc, flush=True)
        self._respond(200, b"ok")

    def _respond(self, code, body):
        self.send_response(code)
        self.end_headers()
        if body:
            self.wfile.write(body)


def set_webhook():
    base = os.environ.get("RENDER_EXTERNAL_URL")
    if not base:
        print("RENDER_EXTERNAL_URL не задан, вебхук не установлен", flush=True)
        return
    result = call("setWebhook", {"url": base + WEBHOOK_PATH, "secret_token": SECRET})
    print("setWebhook:", result, flush=True)


if __name__ == "__main__":
    set_webhook()
    port = int(os.environ.get("PORT", 10000))
    print(f"Слушаю порт {port}", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
