# test_max_bot

Тестовый эхо-бот для Telegram: отвечает на сообщения тем же текстом. Работает на вебхуке, рассчитан на бесплатный Web Service в Render.

## Настройка в Render

1. **New → Web Service**, выбрать этот репозиторий.
2. Language: **Python 3**
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `python bot.py`
5. Instance Type: **Free**
6. Environment Variables: `BOT_TOKEN` = токен от @BotFather

При запуске бот сам регистрирует вебхук на адрес сервиса (`RENDER_EXTERNAL_URL` + `/webhook`).

На бесплатном тарифе сервис засыпает после 15 минут без запросов, поэтому первое сообщение после паузы бот может обработать с задержкой около минуты.
