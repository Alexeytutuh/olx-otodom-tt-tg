# Встановлення tiktok-connect на VPS (інструкція для серверного агента)

Мета: запустити контейнер `tiktok-connect` у мережі `n8n_default` і додати в Caddy сайт `media.example.com`.
Після цього мають відкриватися `https://media.example.com/` (сторінка «Підключити TikTok»), `/privacy`, `/terms`, `/health`.

Правила:
- **Ніколи не виводь у консоль/лог вміст `.env`, токени чи `data/tokens.json`.**
- Не чіпай інші сайти в Caddyfile. Перед будь-якою зміною Caddyfile зроби бекап.
- Caddyfile змонтовано в контейнер як **один файл**. Тому змінюй його лише **на місці** (`cat >>`, `cp поверх`).
  Не використовуй `sed -i` чи редактори, що замінюють файл новим: контейнер продовжить бачити стару версію.
- Усі команди виконуються від root.

## 0. Перевірки перед стартом

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'caddy-caddy-1|n8n'
docker network ls | grep n8n_default
docker inspect caddy-caddy-1 --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
#   очікується рядок: /opt/caddy/Caddyfile -> /etc/caddyfile
docker port caddy-caddy-1          # мають бути 80 і 443
free -m                            # потрібно ~150 МБ вільної RAM
grep -n 'media.example.com' /opt/caddy/Caddyfile || echo "блоку ще немає — ок"
```

Діагностика Cloudflare. Caddy ще не знає цього хоста, тому редирект тут може дати лише Cloudflare:
```bash
curl -sI http://media.example.com/.well-known/acme-challenge/test | grep -iE '^(HTTP|location|server)'
```
Якщо бачиш `301`/`308` з `location: https://...`, у Cloudflare увімкнено **Always Use HTTPS**. Тоді Let's Encrypt (HTTP-01) не зможе видати сертифікат.
Попроси користувача **вимкнути Always Use HTTPS** (SSL/TLS → Edge Certificates) або додати правило-виняток для `/.well-known/acme-challenge/*`.
Альтернатива: тимчасово перевести запис `media` в «DNS only» (сіра хмара). Див. розділ «Якщо сертифікат не видається».

## 1. Розпакувати файли

Архів `tiktok-connect.zip` має бути на сервері, наприклад `/root/tiktok-connect.zip`. Він містить теку `tiktok-connect/`.

```bash
apt-get install -y unzip >/dev/null 2>&1 || true
[ -d /opt/tiktok-connect ] && cp -a /opt/tiktok-connect /opt/tiktok-connect.bak-$(date +%Y%m%d-%H%M%S)
unzip -o /root/tiktok-connect.zip -d /opt/
ls -la /opt/tiktok-connect
```

## 2. Тека даних (токени + картинки)

У контейнері сервіс працює як uid 10001.
```bash
install -d -o 10001 -g 10001 -m 700 /opt/tiktok-connect/data
```

## 3. Файл `.env` (права 600)

```bash
cd /opt/tiktok-connect
[ -f .env ] || install -m 600 -o root -g root .env.example .env
chmod 600 .env && chown root:root .env
```
Далі **користувач сам** вписує `TIKTOK_CLIENT_KEY` і `TIKTOK_CLIENT_SECRET` (`nano /opt/tiktok-connect/.env`).
Під час тестування в Sandbox це ключі **Sandbox «test»**, вони відрізняються від Production.
Не проси надсилати секрети в чат. Перевір, що поля заповнені, але не виводь значень:
```bash
grep -cE '^TIKTOK_CLIENT_KEY=.+' .env; grep -cE '^TIKTOK_CLIENT_SECRET=.+' .env   # обидва мають дати 1
grep -E '^(PUBLIC_BASE_URL|REDIRECT_URI)=' .env
#   PUBLIC_BASE_URL=https://media.example.com
#   REDIRECT_URI=https://media.example.com/tiktok/callback   (точно як у Login Kit, без / в кінці)
stat -c '%a %U' .env    # 600 root
```

## 4. Збірка і запуск контейнера

```bash
cd /opt/tiktok-connect
docker compose up -d --build
docker compose ps                      # статус має стати "healthy" (до ~1 хв)
docker logs --tail 30 tiktok-connect   # має бути рядок "listening on 0.0.0.0:8090", без ERROR
docker exec caddy-caddy-1 wget -qO- http://tiktok-connect:8090/health; echo   # {"status": "ok"}
docker stats --no-stream tiktok-connect   # пам'ять ~25–40 МБ з ліміту 128 МБ
```

## 5. Додати сайт у Caddy (з бекапом)

```bash
BK=/opt/caddy/Caddyfile.bak-$(date +%Y%m%d-%H%M%S)
cp -a /opt/caddy/Caddyfile "$BK" && echo "backup: $BK"
grep -q 'media.example.com' /opt/caddy/Caddyfile || cat /opt/tiktok-connect/Caddyfile.snippet >> /opt/caddy/Caddyfile
docker exec caddy-caddy-1 tail -n 15 /etc/caddyfile        # контейнер бачить новий блок?
docker exec caddy-caddy-1 caddy validate --config /etc/caddyfile
```
Якщо validate видав помилку, **не перезавантажуй**, а відкоти зміни (розділ «Відкат», крок 1).
Якщо `Valid configuration`, перезавантаж Caddy:
```bash
docker exec caddy-caddy-1 caddy reload --config /etc/caddyfile
sleep 20
docker logs --since 3m caddy-caddy-1 2>&1 | grep -iE 'media.example.com|obtain|certificate|acme|error' | tail -20
#   очікується "certificate obtained successfully" для media.example.com
```

## 6. Перевірка

```bash
# сертифікат прямо на origin (оминаючи Cloudflare) — має бути Let's Encrypt (або ZeroSSL), не самопідписаний
echo | openssl s_client -connect 127.0.0.1:443 -servername media.example.com 2>/dev/null | openssl x509 -noout -issuer -subject -dates
curl -sS --resolve media.example.com:443:127.0.0.1 https://media.example.com/health; echo
# через Cloudflare
curl -sS https://media.example.com/health; echo                  # {"status": "ok"}
curl -sS -o /dev/null -w '%{http_code}\n' https://media.example.com/          # 200
curl -sS -o /dev/null -w '%{http_code}\n' https://media.example.com/privacy   # 200
curl -sS -o /dev/null -w '%{http_code}\n' https://media.example.com/terms     # 200
curl -sS --path-as-is -o /dev/null -w '%{http_code}\n' https://media.example.com/media/../tokens.json   # 404
curl -sSI https://media.example.com/tiktok/connect | grep -i '^location' | cut -c1-80   # https://www.tiktok.com/v2/auth/authorize/...
```
Коли сертифікат на origin валідний, попроси користувача виставити в Cloudflare **SSL/TLS → Full (strict)**.
Потім повтори `curl https://media.example.com/health`.

Перевірка публічних URL картинок без звернення до TikTok:
```bash
docker exec tiktok-connect python -m tiktok_connect.test_draft --demo --dry-run
#   скопіюй один URL з виводу і:
curl -sSI "<URL>" | grep -iE '^(HTTP|content-type|location)'   # 200, image/jpeg, без location
```

## Якщо сертифікат не видається (Cloudflare 525/526, в логах Caddy помилки ACME)

1. Найкраще: вимкнути в Cloudflare **Always Use HTTPS** (або зробити виняток для `/.well-known/acme-challenge/*`).
   У режимі Full/Full (strict) Cloudflare ходить на origin тією ж схемою, що й відвідувач.
   Тому HTTP-01 доходить до Caddy на порт 80, і подальші автоматичні продовження теж працюватимуть.
   HTTP→HTTPS редирект робить сам Caddy.
2. Тимчасово «DNS only» (сіра хмара) для `media`: дочекатися сертифіката (`docker restart caddy-caddy-1` або reload), повернути помаранчеву хмару.
   Через ~60 днів продовження впреться в ту саму проблему, якщо Always Use HTTPS лишиться увімкненим.
3. Cloudflare Origin Certificate (SSL/TLS → Origin Server). Потрібно змонтувати cert/key у контейнер Caddy (зміна `/opt/caddy/docker-compose.yml`)
   і в блоці сайту додати `tls /certs/media.pem /certs/media.key`. Працює лише з помаранчевою хмарою.
   Роби це тільки з дозволу користувача: це змінює контейнер Caddy, через який працює і n8n.

## Відкат

```bash
# 1) Caddy: повернути бекап НА МІСЦЕ (cp поверх, щоб зберегти inode змонтованого файлу)
cp /opt/caddy/Caddyfile.bak-<TIMESTAMP> /opt/caddy/Caddyfile
docker exec caddy-caddy-1 caddy validate --config /etc/caddyfile && docker exec caddy-caddy-1 caddy reload --config /etc/caddyfile
# 2) зупинити сервіс
cd /opt/tiktok-connect && docker compose down
# 3) (опційно) прибрати образ; дані з токенами — лише за згодою користувача
docker image rm tiktok-connect:local
# rm -rf /opt/tiktok-connect/data     # видаляє токени і картинки!
```

## Оновлення коду

```bash
cd /opt && unzip -o /root/tiktok-connect.zip -d /opt/ && cd /opt/tiktok-connect && docker compose up -d --build
```
`.env` і `data/` архів не перезаписує, бо їх у ньому немає.

## Корисні команди

```bash
docker logs -f tiktok-connect
docker exec tiktok-connect python -m tiktok_connect.admin list            # підключені акаунти (без токенів)
docker exec tiktok-connect python -m tiktok_connect.admin refresh         # примусово оновити access token
docker exec -it tiktok-connect python -m tiktok_connect.test_draft --demo
# свої фото: скопіювати в /opt/tiktok-connect/data/in/ (у контейнері це /data/in/)
docker exec -it tiktok-connect python -m tiktok_connect.test_draft --images /data/in/1.jpg /data/in/2.jpg \
    --title "Назва" --description "Опис #хештег"
```

---

# Тест із акаунтом друга (Sandbox)

### А. Що робить власник застосунку (developers.tiktok.com → Manage apps → Listing Publisher → Sandbox «test»)
1. Перевір конфігурацію Sandbox:
   - Login Kit: redirect URI `https://media.example.com/tiktok/callback`.
   - Content Posting API доданий, scopes `user.info.basic` і `video.upload`.
   - URL property `media.example.com` верифікований **саме в цьому Sandbox**.
   - Terms of Service URL = `https://media.example.com/terms`, Privacy Policy URL = `https://media.example.com/privacy`.
   - Натисни **Apply changes**.
2. **Sandbox settings → Target users → Add account.** TikTok попросить **увійти в акаунт, який додається**, і прийняти Developer ToS.
   Тому це робиться разом із другом: він сам вводить свій логін/пароль або підтверджує вхід. Пароль тобі не потрібен.
   Ліміт 10 акаунтів. Акаунт може з'явитися в списку із затримкою до 1 години.
3. Ключі в `/opt/tiktok-connect/.env` мають бути ключами **Sandbox**. Після зміни `.env` виконай `docker compose up -d`.

### Б. Що робить друг
1. Оновити застосунок TikTok до версії **31.8 або новішої** (вимога для MEDIA_UPLOAD).
2. Відкрити **https://media.example.com** → **«Підключити TikTok»**. Увійти в той самий акаунт, що доданий як target user, і дозволити обидва доступи.
   Має з'явитися «✅ TikTok підключено: <ім'я>». На головній сторінці буде видно ім'я і строк дії токена.

### В. Тестова чернетка (на сервері)
```bash
docker exec -it tiktok-connect python -m tiktok_connect.test_draft --demo
```
Очікуваний вивід: `content/init OK, publish_id=...` → `status=PROCESSING_DOWNLOAD` → `status=SEND_TO_USER_INBOX` → `✓ ... draft delivered`.

### Г. Що перевірити в TikTok (друг)
- **Вхідні (Inbox) → системні сповіщення**: прийде сповіщення про контент від Listing Publisher. Тап відкриває редактор TikTok з 3 тестовими слайдами.
  **Заголовок і опис** мають бути вже заповнені: документація каже, що для MEDIA_UPLOAD фото вони «reflected in the editing flow».
- Далі друг може відредагувати пост, додати музику і зберегти його в чернетки або опублікувати.
- Перевір, чи з'являється пост у Профіль → Чернетки без тапу по сповіщенню. Документація цього не гарантує.
- **Публічна публікація з Sandbox, найімовірніше, недоступна.**
  У доках Sandbox сказано: «Sandbox mode does not offer access to Content Posting API for public videos».
  Для неаудитованих клієнтів контент обмежено приватним переглядом.
  Якщо `content/init` поверне `unaudited_client_can_only_post_to_private_accounts`, друг має тимчасово зробити акаунт **приватним** і повторити.
  Якщо в редакторі доступна лише видимість «Тільки я», це очікувана поведінка. Публічні пости стануть можливими лише після проходження аудиту в Production.
- Не запускай більше ~5 тестів за добу: TikTok дозволяє не більше 5 «pending shares» за 24 години (`spam_risk_too_many_pending_share`).
  Сповіщення, які друг не відкрив, теж рахуються.

### Типові помилки
| Код | Що робити |
|---|---|
| `url_ownership_unverified` | верифікувати `media.example.com` у Sandbox «test» (URL properties) |
| `scope_not_authorized` | перепідключити акаунт на сторінці і дозволити всі доступи |
| `app_version_check_failed` | оновити TikTok до 31.8+ |
| `photo_pull_failed` | URL недоступний для TikTok: перевір curl з розділу 6 і Bot Fight Mode / WAF у Cloudflare для `/media/*` |
| `picture_size_check_failed` / `file_format_check_failed` | повідом розробнику (скрипт сам конвертує в JPEG ≤1080×1920) |
| `rate_limit_exceeded` | зачекати хвилину (init: 6 запитів/хв) |
