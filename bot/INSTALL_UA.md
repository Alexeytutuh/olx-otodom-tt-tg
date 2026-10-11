# Встановлення бота olx-tiktok-bot на сервер (інструкція для серверного AI-агента)

Ціль: Hetzner, Ubuntu 26.04 x86_64, Docker 29 + compose v5. Бот працює в одному контейнері `olx-tiktok-bot`
і **не потребує вхідних портів**: він лише сам звертається до Telegram, OLX і Otodom. Caddy і мережу
`n8n_default` не чіпаємо. Ліміт пам'яті контейнера 256 МБ, реальне споживання ~70–110 МБ.

Виконуй кроки по черзі. Після кожного кроку перевіряй результат. Якщо щось не так, зупинись і покажи
користувачу вивід команди. **Токен бота не виводь у лог чи чат** (не роби `cat .env`).

## 0. Перевірка сервера
```bash
docker --version            # очікується Docker 29.x
docker compose version      # очікується v5.x
free -m                     # потрібно >= 300 МБ вільної пам'яті
df -h /opt                  # потрібно >= 1 ГБ вільного місця
```

## 1. Покласти архів у /opt/olx-tiktok-bot і розпакувати
Архів `olx-tiktok-bot.zip` користувач передасть на сервер (наприклад, `scp olx-tiktok-bot.zip root@SERVER:/tmp/`).
```bash
sudo mkdir -p /opt/olx-tiktok-bot
sudo cp /tmp/olx-tiktok-bot.zip /opt/olx-tiktok-bot/
cd /opt/olx-tiktok-bot
sudo unzip -o olx-tiktok-bot.zip || sudo python3 -m zipfile -e olx-tiktok-bot.zip .
ls    # мають бути: Dockerfile docker-compose.yml .env.example olxbot parsers INSTALL_UA.md README_UA.md ...
```

## 2. Створити .env (права 600)
Потрібен **окремий новий** токен бота від @BotFather. Не використовуй токен старого бота olx-telegram:
два процеси з одним токеном заважають один одному (помилка 409 Conflict).
```bash
cd /opt/olx-tiktok-bot
sudo cp .env.example .env
sudo chmod 600 .env
# встав токен, який дав користувач (замість ВСТАВ_ТОКЕН):
sudo sed -i 's|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=ВСТАВ_ТОКЕН|' .env
sudo grep -c '^TELEGRAM_BOT_TOKEN=[0-9]\+:' .env    # має вивести 1
```
Необов'язково, але бажано (захист від чужих): `ALLOWED_CHAT_IDS=<id друга>,<id користувача>`.
Якщо id невідомі, залиш порожнім. Тоді власником стане **перший, хто напише боту /start**,
тож друг має зробити це одразу після запуску. `ALERT_CHAT_ID=<id користувача>` вмикає технічні сповіщення
користувачу, а не другу.

## 3. Папка для даних
Контейнер працює від користувача з uid 10001, тому:
```bash
cd /opt/olx-tiktok-bot
sudo mkdir -p data
sudo chown -R 10001:10001 data
sudo chmod 700 data
```

## 4. Збірка і запуск
```bash
cd /opt/olx-tiktok-bot
sudo docker compose build          # 1–3 хв
sudo docker compose up -d
sudo docker compose ps             # STATUS: Up ... (health: starting), а за кілька хвилин (healthy)
```

## 5. Перевірка логів
```bash
sudo docker compose logs --tail=50 olx-tiktok-bot
```
Очікувані рядки:
* `Telegram bot @<ім'я_бота>` означає, що токен правильний;
* `Started v1.0.0: sources=olx,otodom interval=300s ...`;
* о найближчій :00/:05/:10… `olx: first run is silent, N current listings marked as seen`, а за ~1 хв
  те саме для `otodom`. Перший прогін тихий: старі оголошення не надсилаються.

Потім друг пише боту `/start`. У логах з'явиться `Owner chat set to <id>`, а в Telegram прийде
привітання і «✅ Бот запущено, стежу за OLX і Otodom». Команда `/status` показує стан.

Пам'ять: `sudo docker stats --no-stream olx-tiktok-bot` (норма до ~120 МБ).

## 6. Оновлення до нової версії
```bash
cd /opt/olx-tiktok-bot
# 6.1 резервна копія коду і бази
sudo cp -a /opt/olx-tiktok-bot /opt/olx-tiktok-bot.bak-$(date +%Y%m%d-%H%M)
sudo docker exec olx-tiktok-bot python -c "import sqlite3; s=sqlite3.connect('/data/bot.sqlite3'); d=sqlite3.connect('/data/backup.sqlite3'); s.backup(d); d.close(); print('backup ok')"
sudo docker tag olx-tiktok-bot:latest olx-tiktok-bot:prev
# 6.2 новий код (.env і data/ не чіпаються)
sudo rm -rf olxbot parsers
sudo unzip -o /tmp/olx-tiktok-bot.zip || sudo python3 -m zipfile -e /tmp/olx-tiktok-bot.zip .
sudo docker compose build && sudo docker compose up -d
sudo docker compose logs --tail=30 olx-tiktok-bot
```

## 7. Відкат
```bash
cd /opt/olx-tiktok-bot
sudo docker tag olx-tiktok-bot:prev olx-tiktok-bot:latest
sudo docker compose up -d --no-build
# якщо потрібен і старий код (для наступних збірок), відновити папку з копії:
#   sudo rsync -a --exclude data --exclude .env /opt/olx-tiktok-bot.bak-ДАТА/ /opt/olx-tiktok-bot/
# база: сумісна між версіями 1.x; за потреби відновити копію:
#   sudo docker compose stop && sudo cp data/backup.sqlite3 data/bot.sqlite3 && sudo rm -f data/bot.sqlite3-wal data/bot.sqlite3-shm && sudo docker compose up -d
```

## 8. Корисне
| Що | Команда |
|---|---|
| Зупинити / запустити | `sudo docker compose stop` / `sudo docker compose start` |
| Перезапустити | `sudo docker compose restart` |
| Логи наживо | `sudo docker compose logs -f --tail=100` |
| Стан healthcheck | `sudo docker inspect --format '{{.State.Health.Status}}' olx-tiktok-bot` |
| Змінив .env | `sudo docker compose up -d` (контейнер перестворюється) |
| Змінив критерії в `parsers/config.py` | `sudo docker compose up -d --build` |

## 9. Якщо щось не так
* Контейнер постійно перезапускається, у логах `Telegram rejected the token` → неправильний токен у `.env`.
* `DATA_DIR /data is not writable` → повтори крок 3 (`chown -R 10001:10001 data`).
* `409 Conflict` → цей токен використовує ще якийсь процес (старий бот, webhook). Потрібен окремий токен.
* `olx down [blocked]` / `captcha` кілька разів поспіль → OLX блокує IP сервера. Бот сам робить паузи до
  15 хв і надсилає сповіщення «OLX відвалився». Нічого не роби, просто повідом користувача.
* Як бот лікує сам себе: при будь-якій помилці продовжує роботу; якщо якийсь внутрішній потік завис на
  15 хв, процес завершується, і Docker (`restart: unless-stopped`) піднімає його знову. Після рестарту
  приходить повідомлення «♻️ Бот перезапустився після зависання». Сам Docker «unhealthy» контейнери не
  перезапускає, тому healthcheck тут лише для діагностики.
