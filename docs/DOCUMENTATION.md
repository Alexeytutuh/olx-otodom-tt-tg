# OLX / Otodom → Telegram → TikTok: документація проєкту

Автоматизація для друга-рієлтора у Вроцлаві: бот сам знаходить нові оголошення про оренду від власників,
готує пост у форматі рієлтора і за двома кнопками публікує його в Telegram-канал і в чернетки TikTok.

- Версії: бот `olx-tiktok-bot` **1.1.0**, сервіс `tiktok-connect` **1.0.0** (з доданим внутрішнім API).
- Стан на 2026-10-11: працює на сервері, тести зелені (бот 49, tiktok-connect 36).
- Код і деталі по модулях: [CODE.md](CODE.md).

---

## 1. Запит: що треба було зробити

**Початкова ідея (2026-10-01…05).** Рієлтор хотів не набирати пости вручну. Спершу планували варіант
«рієлтор кидає фото й опис у бот → бот публікує». Тоді ж з'ясували обмеження TikTok (див. розділ 5)
і вибрали варіант **А — чернетки**: TikTok-пост приходить у застосунок як чернетка, музику й кнопку
«Опублікувати» рієлтор тисне сам.

**Кінцевий запит (2026-10-10).**

1. Бот кожні кілька хвилин перевіряє **OLX** і **Otodom**: оренда цілих квартир у Вроцлаві, 1–3 кімнати,
   тільки від власників.
2. Кожна нова оферта приходить у Telegram-бот: фото + готовий пост польською у форматі рієлтора.
3. Кнопка **«Згенерувати»** готує пост для Telegram-каналу і пост для TikTok (слайди + текст).
4. Кнопка **«В канал»** публікує пост у Telegram-канал.
5. Кнопка **«В чернетки TikTok»** відправляє карусель у TikTok як чернетку. Рієлтор відкриває її,
   додає музику й публікує.

**Уточнення від рієлтора після першого запуску:**

- текст у каналі й під каруселлю TikTok однаковий, з його контактами в кінці; без хештегів
  (пізніше з'ясувалось, що опис TikTok мусить бути без першого рядка, див. 3.5.4);
- у службовій картці під постом не потрібні поверх, ціна за м² і ім'я того, хто здає;
- вулиці мають бути повними («Róży Wiatrów», а не «Róży»);
- якщо в оголошенні є щось про гараж або парковку, це має бути в пості.

---

## 2. Стек

| Шар | Що використано | Навіщо |
|---|---|---|
| Сервер | VPS (Hetzner), Ubuntu 26.04, x86_64 | Спільний з іншими сервісами (n8n тощо) |
| Контейнери | Docker 29 + Compose v5, мережа `n8n_default` | Ізоляція, автоперезапуск, ліміти пам'яті |
| Мова | Python 3.13 (образ `python:3.13-slim`) | Обидва сервіси |
| Залежності бота | `curl_cffi` (TLS-відбиток Chrome для OLX/Otodom), `Pillow` (фото, слайди, dHash) | Решта — стандартна бібліотека |
| Залежності tiktok-connect | `Pillow`, `tzdata` | HTTP-сервер на `http.server` зі стандартної бібліотеки |
| База | SQLite (WAL) у `/data/bot.sqlite3` | Бачені оголошення, пости, чорний список, стан |
| Telegram | Bot API, long polling (`getUpdates`) | Вхідні порти боту не потрібні |
| TikTok | Content Posting API, режим `MEDIA_UPLOAD` (чернетка), фото через `PULL_FROM_URL`; Login Kit (OAuth) | Чернетки без аудиту застосунку |
| Веб | Caddy (контейнер `caddy-caddy-1`), Let's Encrypt, Cloudflare (proxied) | `https://media.example.com` для OAuth, фото й сторінок Privacy/Terms |
| Вулиці | OpenStreetMap (Overpass API), 2443 назви | Довідник вулиць Вроцлава в боті |
| Інструменти | Grok (перша версія коду), Claude Code (розгортання, доопрацювання, тести) | — |

### Архітектура

```
 OLX / Otodom ──(кожні 5 хв)──► olx-tiktok-bot ──► Telegram: чат власника (оферта + кнопки)
                                    │    │
                                    │    └── «В канал» ──► Telegram-канал (альбом + пост)
                                    │
                                    └── «В чернетки TikTok»
                                         POST http://tiktok-connect:8090/internal/draft
                                         (мережа n8n_default, Bearer-токен)
                                                │
                                                ▼
                                         tiktok-connect ──► TikTok API: content/init (MEDIA_UPLOAD)
                                                │                 │
                         https://media.example.com/media/...  ◄──┘ TikTok сам забирає фото
                                                ▲
                              Caddy (Let's Encrypt) ◄── Cloudflare ◄── інтернет
```

---

## 3. Як робилося: покроково

### 3.1 Планування (2026-10-01…05)

- Розібрали варіанти TikTok: **А** — чернетки через офіційний API, **Б** — платний сервіс (Upload-Post),
  **В** — браузерний бот. Вибрали А: безкоштовно, без ризику для акаунта.
- З'ясували, що для фото TikTok вимагає `PULL_FROM_URL` з **підтвердженого домену**, тому потрібен
  проміжний веб-сервіс з публічними посиланнями на фото.

### 3.2 Перша версія коду (2026-10-09, поза сервером)

Власник проєкту разом із Grok написав і протестував:

- **`olx-tiktok-bot`**: парсери OLX і Otodom, фільтри (приватні, 1–3 кімнати, типи будинків), дедуплікація,
  пост у форматі рієлтора, слайди TikTok 1080×1920, чорний список посередників, алерти, watchdog.
- **`tiktok-connect`**: сторінка підключення TikTok, OAuth callback, роздача фото `/media`,
  сторінки `/privacy` і `/terms`, скрипт тестової чернетки `test_draft`.

Налаштування TikTok Developer:

- застосунок **Listing Publisher**, sandbox **test**;
- Login Kit, redirect `https://media.example.com/tiktok/callback`;
- Content Posting API, Direct Post вимкнено; scopes `user.info.basic`, `video.upload`;
- домен `media.example.com` підтверджено TXT-записом у Cloudflare (запис не видаляти).

DNS: `media.example.com` проксований через Cloudflare на VPS.

### 3.3 Розгортання (2026-10-10)

1. **Перевірка коду на безпеку.** Обидва архіви розпаковано окремо й переглянуто: які хости викликаються
   (лише OLX, Otodom, Telegram, TikTok), чи немає `eval`/`exec`/прихованих інструкцій. `curl` у
   парсері викликається списком аргументів, без shell.
2. **Перевірка сервера.** ~1,08 ГБ вільної RAM (бот 256 МБ ліміт, tiktok-connect 128 МБ), Caddy і n8n
   працюють, блоку `media.example.com` у Caddy ще немає.
3. **Права.** Користувач сервера в групі `docker`, але без sudo без пароля. Одну команду
   власник виконав сам: створення `/opt/olx-tiktok-bot`, `/opt/tiktok-connect` і тек `data/` (uid 10001).
4. **Бот.** Розпакування в `/opt/olx-tiktok-bot`, `.env` (chmod 600) з шаблону. Токен **нового** бота з
   BotFather, `ALLOWED_CHAT_IDS` і `ALERT_CHAT_ID` вписав власник сам; агент лише перевірив, що поля
   заповнені, не виводячи значень. `docker compose build && up -d`. Логи: `Telegram bot @<ім'я_бота>`,
   `Started v1.0.0`, `olx: first run is silent, 47…`, `otodom: first run is silent, 10…`.
   Власником бота став власник проєкту (перший `/start`): поки тестуємо, оферти йдуть йому, а не рієлтору.
5. **tiktok-connect.** Розпакування в `/opt/tiktok-connect`, ключі sandbox «test» вписав власник.
   Контейнер у мережі `n8n_default`, без опублікованих портів.
6. **Caddy і сертифікат.** Діагностика показала `308` на `http://media.example.com`. Виявилось, що це
   **не Cloudflare** («Always Use HTTPS» був вимкнений), а сам Caddy: він редиректить на https навіть
   невідомі йому хости. Після бекапу (`/opt/caddy/Caddyfile.bak-20261010-081851`) блок дописано в кінець
   файлу **на місці** (файл змонтований у контейнер як один файл, тому без `sed -i`), `caddy validate`,
   `caddy reload`. Let's Encrypt видав сертифікат з першої спроби (HTTP-01). n8n після reload працює.
7. **Перевірки.** `/health` = ok, `/`, `/privacy`, `/terms` = 200, `/media/../tokens.json` = 404,
   `/tiktok/connect` веде на авторизацію TikTok, тестові фото віддаються як `image/jpeg`.
8. **Підключення TikTok і тест.** Terms/Privacy у застосунку змінено на `media.example.com`, тестовий
   акаунт власника додано в Target users, підключено через сторінку. `test_draft --demo`:
   `PROCESSING_DOWNLOAD → SEND_TO_USER_INBOX`. У застосунку: слайди, назва й опис підставились, музику
   можна додати, видимість «всі» доступна навіть із sandbox.

### 3.4 Доопрацювання: версія 1.1.0 (2026-10-10)

Код перенесено в `~/Documents/olx-tiktok` (git, стартовий коміт = те, що було розгорнуто).

| Зміна | Де |
|---|---|
| Контакти рієлтора в кінці кожного поста (`POST_FOOTER` у `.env`) | `formatting.post_lines`, `settings` |
| Опис TikTok = текст поста без хештегів; назва TikTok = перший рядок поста | `formatting.tiktok_caption`, `tiktok_title` |
| Картка: прибрано поверх і ціну за м², «Продавець» → «Здає» (пізніше прибрано зовсім) | `formatting.build_info` |
| Довідник вулиць з OpenStreetMap: повні назви, злиті назви («AntoniaVivaldiego») | `olxbot/streets.py`, `olxbot/data/wroclaw_streets.txt`, `tools/build_streets.py` |
| Кнопки **📢 В канал** і **🎵 В чернетки TikTok** під текстом після «Згенерувати»; натиснуті стають ✅, повтор не дублює | `app.kb_publish`, `publish_channel`, `push_tiktok` |
| Клієнт tiktok-connect з людськими поясненнями помилок TikTok | `olxbot/tiktok_hook.py` |
| Внутрішній API `POST /internal/draft` у tiktok-connect | `tiktok_connect/server.py`, `drafts.py`, `config.py` |
| Бот у мережі `n8n_default` | `bot/docker-compose.yml` |

**Захист внутрішнього API** (три шари): у Caddy `respond /internal/* 404`; сервіс відкидає запити з
`X-Forwarded-For` / `CF-Connecting-IP` (тобто ті, що прийшли через проксі); Bearer-токен
(`INTERNAL_TOKEN` = `TIKTOK_CONNECT_TOKEN`), згенерований на сервері `openssl rand -hex 32` і ніде не
показаний.

**Деплой:** образи позначено `:prev` (відкат на 1.0.0), бекап бази `data/backup-<дата>.sqlite3`, бекап
Caddyfile `Caddyfile.bak-20261010-181704`, новий код, `docker compose up -d --build` для обох.
Перевірки: ззовні `/internal/draft` = 404, з контейнера бота неправильний токен = 401, правильний = 400
(запит без поля `images`, до TikTok не доходить).

> Граблі під час деплою: `tar` на сервері видав помилку через `--exclude` після шляхів, але архів
> `/opt/olx-tiktok-bot/backups/code-20261010-2016.tgz` усе ж записав — з обома `.env` усередині (виключення
> не спрацювали) і з правами 664. Знайшов агент-перевіряльник; права виправлено на 600 (тека 700).
> Окремо `--exclude=data` при копіюванні нового коду виключив і `olxbot/data` — докопійовано окремо.

### 3.5 Виправлення після живих тестів (2026-10-10…11)

1. **Kaucja 200 замість 2500.** Текст: «garażu dodatkowo 200 zł/mies. Kaucja zwrotna: 2500 zł». Пошук
   «сума перед словом» склеював суму з попереднього речення. Тепер, якщо між сумою і словом кінець
   речення, а слово з великої літери, збіг ігнорується (`parsers/extract.py::_find_amount`).
   На всіх оголошеннях з фікстур результат не змінився.
2. **Канал.** Створено тестовий канал, його ID (`-100…`) записано в `TELEGRAM_CHANNEL_ID`, бот — адмін з правом публікувати.
3. **Картка.** Рядок «Здає» прибрано: у пості все одно контакти рієлтора.
4. **Опис TikTok порожній.** Діагностика трьома тестовими чернетками:
   A — увесь текст в один рядок (з телефоном) → опис є;
   B — з переносами, без телефону → опис є;
   C — назва як у справжньому пості, а опис **без першого рядка** → опис є.
   Висновок: TikTok відкидає опис, який починається з того самого рядка, що й назва. Тепер опис TikTok —
   пост без першого рядка; у Telegram пост повний.
5. **Парковка без ціни.** Раніше рядок з'являвся лише коли була сума. Тепер:
   сума є → `Garaż: 200 zł` (у Razem); парковка є, ціна «za wszystko / bez dodatkowych opłat» →
   `Miejsce parkingowe: w cenie` (або `Garaż: w cenie`, якщо в тексті гараж); парковка є, ціни немає →
   `Garaż: dostępny` / `Miejsce parkingowe: dostępne` (не в Razem); на вулиці, «ogólnodostępne», «nie ma»,
   «brak» → нічого. Нюанс: якщо параметр OLX «Parking» заповнений (і це не «brak»/«na ulicy»), рядок
   з'являється навіть тоді, коли текст парковку заперечує.

---

## 4. Експлуатація

### Розташування на сервері

| Що | Де |
|---|---|
| Бот | `/opt/olx-tiktok-bot` (код, `.env`, `data/` з базою, uid 10001) |
| tiktok-connect | `/opt/tiktok-connect` (код, `.env`, `data/` з `tokens.json` і `media/`) |
| Caddy | `/opt/caddy/Caddyfile` (змонтований як `/etc/caddyfile`), бекапи `Caddyfile.bak-*` |
| Бекапи бота | `/opt/olx-tiktok-bot/backups/code-20261010-2016.tgz` (код 1.0.0 обох сервісів **разом з `.env`**, права 600); база — `data/backup-20261010-1816.sqlite3` |
| Локальна копія коду | `~/Documents/olx-tiktok` (git) |

### Змінні `.env`

**Бот** (`/opt/olx-tiktok-bot/.env`, chmod 600):

| Змінна | Значення |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен бота з BotFather |
| `ALLOWED_CHAT_IDS` | хто може користуватися ботом (через кому) |
| `OWNER_CHAT_ID` | необов'язково; інакше власник = перший `/start` (зберігається в базі й має пріоритет) |
| `ALERT_CHAT_ID` | куди йдуть технічні алерти |
| `POST_FOOTER` | контакти в кінці поста, `\n` = новий рядок |
| `TELEGRAM_CHANNEL_ID` | канал для кнопки «В канал» (`-100…` або `@username`); порожньо = кнопки немає |
| `TIKTOK_CONNECT_ENABLED` / `_URL` / `_TOKEN` | кнопка «В чернетки TikTok»; URL `http://tiktok-connect:8090` |
| `POLL_INTERVAL_S`, `SOURCES`, `ALERT_AFTER_FAILURES`, … | розклад і алерти, див. `bot/.env.example` |

**tiktok-connect** (`/opt/tiktok-connect/.env`, chmod 600): `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`
(ключі sandbox «test»), `PUBLIC_BASE_URL`, `REDIRECT_URI`, `INTERNAL_TOKEN`, а також `PAGES_OPERATOR`,
`PAGES_OPERATOR_UK`, `PAGES_CONTACT` (необов'язково `PAGES_APP_NAME`) — оператор і контакт на сторінках
`/privacy` і `/terms`. У коді лише заглушки (`Your Name`, `contact@example.com`); TikTok вимагає справжні дані.

### Команди

```bash
# стан і логи
ssh your-vps 'docker ps --format "{{.Names}} {{.Status}}" | grep -E "olx|tiktok"'
ssh your-vps 'docker logs --tail 50 olx-tiktok-bot'
ssh your-vps 'docker logs --tail 50 tiktok-connect'

# після зміни .env або коду
ssh your-vps 'cd /opt/olx-tiktok-bot && docker compose up -d --build'
ssh your-vps 'cd /opt/tiktok-connect && docker compose up -d --build'

# підключені TikTok-акаунти (без токенів) і тестова чернетка
ssh your-vps 'docker exec tiktok-connect python -m tiktok_connect.admin list'
ssh your-vps 'docker exec tiktok-connect python -m tiktok_connect.test_draft --demo'

# тести (у контейнері з образу, код змонтований лише для читання)
ssh your-vps 'docker run --rm --network none -v /opt/olx-tiktok-bot:/app:ro -w /app olx-tiktok-bot:latest python -m unittest discover -s tests -t .'
ssh your-vps 'docker run --rm --network none -v /opt/tiktok-connect:/app:ro -w /app tiktok-connect:local python -m unittest discover -s tests -t .'
```

### Відкат

- Бот / tiktok-connect: `docker tag olx-tiktok-bot:prev olx-tiktok-bot:latest && docker compose up -d --no-build`
  (аналогічно `tiktok-connect:prev` → `tiktok-connect:local`). `:prev` = версія 1.0.0 до доопрацювань.
- Caddy: `cp /opt/caddy/Caddyfile.bak-<дата> /opt/caddy/Caddyfile` (саме `cp` поверх), потім
  `docker exec caddy-caddy-1 caddy validate --config /etc/caddyfile` і
  `docker exec caddy-caddy-1 caddy reload --config /etc/caddyfile`.
- Код: `git` у `~/Documents/olx-tiktok` (стартовий коміт = розгорнута 1.0.0).

### Команди бота в Telegram

`/status`, `/pause`, `/resume`, `/blacklist`, `/unblock N`. Під офертою: **🎬 Згенерувати**,
**🏢 Посередник**. Після генерації: **📢 В канал**, **🎵 В чернетки TikTok**.

---

## 5. Обмеження й відомі нюанси

- **TikTok sandbox:** до 10 акаунтів у Target users. Рев'ю застосунку не подавали: видимість «всі» вже
  доступна. Якщо TikTok почне обмежувати — подавати застосунок на рев'ю й міняти ключі на production.
- **Не більше 5 невідкритих чернеток за 24 год** на акаунт (`spam_risk_too_many_pending_share`); бот
  пише про це людською мовою.
- **Опис TikTok не може починатися з назви** — інакше TikTok його відкидає (див. 3.5.4).
- Музика в чернетки через API не додається — рієлтор ставить сам.
- Старі пости зберігаються в базі з тим текстом, з яким прийшли; правки форматування діють на нові.
- OLX/Otodom можуть блокувати IP або міняти верстку — бот алертить «OLX відвалився» і сам робить паузи.
- Вулиці: якщо назви немає в тексті або в довіднику, лишається старе правило (одне слово + відмінок).

## 6. Передача рієлтору

1. Власник бота: змінити `owner_chat_id` у базі на ID рієлтора (або групу з ним) — алерти лишаються власнику проєкту.
2. TikTok: додати акаунт рієлтора в Target users sandbox; рієлтор оновлює застосунок (≥ 31.8), відкриває
   `https://media.example.com` і тисне «Підключити TikTok». Вирішити, чий акаунт лишається підключеним
   (зараз тестовий акаунт власника).
3. Канал: замінити `TELEGRAM_CHANNEL_ID` на канал рієлтора, додати бота адміном з правом публікувати.
