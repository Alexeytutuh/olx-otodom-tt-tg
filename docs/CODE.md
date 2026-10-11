# Код: структура, модулі, ключові фрагменти

Додаток до [DOCUMENTATION.md](DOCUMENTATION.md). ~6400 рядків робочого Python у двох сервісах
(olxbot 2632 + parsers 1879 + tiktok_connect 1882; з тестами й tools — ~8900).

## Структура репозиторію

```
olx-tiktok/
├── README.md
├── docs/
│   ├── DOCUMENTATION.md        запит, стек, хід робіт, експлуатація
│   └── CODE.md                 цей файл
├── bot/                        olx-tiktok-bot 1.1.0 — контейнер olx-tiktok-bot
│   ├── Dockerfile, docker-compose.yml, requirements.txt, .env.example
│   ├── README_UA.md, INSTALL_UA.md      інструкції першої версії (встановлення, оновлення, відкат)
│   ├── samples/                приклади постів, слайдів і макетів Telegram з першої версії
│   ├── olxbot/                 сам бот
│   │   ├── app.py              потоки, цикл перевірок, кнопки, публікація
│   │   ├── formatting.py       пост, картка, тексти TikTok, вулиці, витрати
│   │   ├── streets.py          довідник вулиць Вроцлава
│   │   ├── data/wroclaw_streets.txt     2443 назви з OpenStreetMap
│   │   ├── tiktok_hook.py      клієнт tiktok-connect
│   │   ├── slides.py, images.py         слайди 1080×1920, фото, dHash
│   │   ├── telegram.py         клієнт Bot API (паузи, повтори, flood control)
│   │   ├── storage.py          SQLite
│   │   ├── dedupe.py           дублікати між OLX і Otodom
│   │   ├── alerts.py, runtime.py, schedule.py, settings.py
│   ├── parsers/                парсери OLX і Otodom, фільтри, витяг сум із тексту
│   │                           (+ README_parsers.md, REVIEW.md, smoke_live.py, cli.py)
│   ├── tests/                  49 тестів (unittest) + фікстури живих оголошень
│   ├── tools/                  fake_telegram.py, build_streets.py, render_samples.py, e2e_live.py, build_zip.sh
│   └── deploy/olx-tiktok-bot.service   альтернатива Docker (systemd), не використовується
└── tiktok-connect/             tiktok-connect 1.0.0 — контейнер tiktok-connect, порт 8090
    ├── Dockerfile, docker-compose.yml, Caddyfile.snippet, .env.example, pyproject.toml
    ├── README.md, INSTALL_UA.md
    ├── tiktok_connect/
    │   ├── server.py           HTTP: /, /health, /tiktok/connect, /tiktok/callback, /media/*,
    │   │                       /privacy, /terms, POST /internal/draft
    │   ├── drafts.py           створення чернетки (для /internal/draft)
    │   ├── test_draft.py       CLI тестової чернетки
    │   ├── tiktok_api.py, tokens.py, storage.py, oauth_state.py   TikTok API, OAuth, токени
    │   ├── images.py, media.py конвертація в JPEG ≤1080p, безпечна роздача файлів
    │   ├── pages.py, admin.py, config.py
    └── tests/                  36 тестів + фейковий TikTok API
```

## Потік даних

1. **Перевірка (`app.poller_loop` → `run_source`)** кожні `POLL_INTERVAL_S` (300 с, вирівняно на :00, :05…).
   Парсер повертає нові оголошення → фільтр чорного списку → `find_duplicate` → `send_post`.
   Перший прогін «тихий»: усе поточне позначається баченим.
2. **`send_post`**: альбом до 10 фото + пост (`build_post`, HTML) у підписі або окремим повідомленням,
   далі картка (`build_info`) з кнопками `🎬 Згенерувати` / `🏢 Посередник`.
3. **Кнопки (`handle_callback`)**: `g:` згенерувати, `c:` у канал, `t:` у TikTok, `b:`/`u:`/`r:` чорний
   список. `g`, `c`, `t` ставляться в чергу `jobs` і виконуються потоком `generator` по одній.
4. **`generate`**: слайди документами + текст TikTok, під текстом `kb_publish` (📢 / 🎵).
5. **`publish_channel`**: альбом + той самий пост у `TELEGRAM_CHANNEL_ID`; позначка `published_tg:<id>` у `meta`.
6. **`push_tiktok`**: слайди генеруються знову (результат той самий) → `TikTokConnectClient.push_draft`
   → `POST /internal/draft` → `drafts.create_draft` → TikTok `content/init` + опитування статусу;
   позначка `published_tt:<id>`.

## База (SQLite, `/data/bot.sqlite3`)

Таблиці: `meta` (ключ-значення: власник, offset, прапорці `published_tg:N` / `published_tt:N`, пауза),
`seen`, `listings`, `posts` (з `listing_json` — пости перебудовуються з нього), `blacklist`, `agent_fps`,
`health`. Нових таблиць у 1.1.0 немає.

## Ключові фрагменти

### Кнопки публікації (`bot/olxbot/app.py`)

```python
def kb_publish(post_id: int, channel: bool, tiktok: bool, done_tg: bool = False, done_tt: bool = False) -> Optional[dict]:
    """Buttons under the generated TikTok text. Pressed ones turn into ✅ (pressing again only reminds)."""
    row = []
    if channel:
        row.append({"text": "✅ В каналі" if done_tg else "📢 В канал", "callback_data": f"c:{post_id}"})
    if tiktok:
        row.append({"text": "✅ Чернетка TikTok" if done_tt else "🎵 В чернетки TikTok", "callback_data": f"t:{post_id}"})
    return {"inline_keyboard": [row]} if row else None
```

Кнопка показується, лише якщо налаштована: канал — коли `TELEGRAM_CHANNEL_ID` не порожній; TikTok —
коли `get_sink()` повернув клієнта (є `TIKTOK_CONNECT_ENABLED=true`, URL і токен).

### Тексти TikTok (`bot/olxbot/formatting.py`)

```python
def tiktok_caption(lst: Listing, today: Optional[dt.date] = None, footer: str = "") -> str:
    """Description under the TikTok carousel: the Telegram post without its first line, which is the title
    (TikTok drops a description that starts with the title). Plain text, no hashtags."""
    lines = post_lines(lst, today, footer)[1:]
    while lines and not lines[0].text:
        lines = lines[1:]
    text = render_plain(lines)
    ...

def tiktok_title(lst: Listing) -> str:
    """Title of the TikTok post: the bold first line of the post ('Kawalerka | 35 m² | taras')."""
```

### Парковка без суми (`bot/olxbot/formatting.py`)

```python
def parking_without_price(lst: Listing) -> Optional[str]:
    """Parking is offered but no amount was found: 'w cenie' (all-inclusive price) or 'dostępne'. None = no parking."""
    param = (lst.parking or "").lower()
    text = f"{lst.title}\n{lst.description_text or ''}"
    offered = bool(param) and not re.search(r"brak|ulic", param)
    for m in _PARKING_TEXT.finditer(text):
        before = text[max(0, m.start() - 40):m.start()]
        if not _NEG.search(before) and not _PARKING_PUBLIC_BEFORE.search(before) \
                and not _PARKING_PUBLIC.search(text[m.end():]):
            offered = True
            break
    if not offered:
        return None
    if _ALL_INCLUSIVE.search(text):
        return "w cenie"
    return "dostępny" if _parking_label(lst) == "Garaż" else "dostępne"
```

У `cost_lines` цей рядок додається лише коли `parking_pln is None` і в `Razem` не входить.

### Довідник вулиць (`bot/olxbot/streets.py`)

`complete(captured, text, degenitive)` — `captured` це те, що парсер OLX узяв після «ul./ulicy»:

1. багатослівна назва: текст одразу після першого слова має продовжувати назву зі списку
   (з пробілами або злито: «Róży wiatrów» / «AntoniaVivaldiego»);
2. одне слово: шукається як є, потім у називному відмінку (`_degenitive`: «Braterskiej» → «Braterska»);
3. немає в списку → `None`, і `normalize_street` лишає старе правило.

Список перебудовується `tools/build_streets.py` (команда Overpass — у docstring файлу).

### Сума з іншого речення (`bot/parsers/extract.py::_find_amount`)

```python
    for m in rev.finditer(text):
        if m.group("key")[:1].isupper() and re.search(r"\.\s+$", text[m.start():m.start("key")]):
            continue                         # "200 zł/mies. Kaucja zwrotna: 2500" - new sentence, not "200 zł kaucji"
```

### Внутрішній API (`tiktok-connect/tiktok_connect/server.py`)

```
POST /internal/draft
Authorization: Bearer <INTERNAL_TOKEN>
{"title": "...", "description": "...", "images": ["<base64 JPEG>", ...]}

200 {"ok": true,  "publish_id": "...", "status": "SEND_TO_USER_INBOX"}
    (або status "PROCESSING_*", якщо TikTok не завершив за 60 с — зазвичай доходить сам пізніше)
502 {"ok": false, "error": "spam_risk_too_many_pending_share", "message": "...", "hint": "..."}  помилка TikTok
401 неправильний токен
404 токен не задано або запит прийшов через проксі (X-Forwarded-For / CF-Connecting-IP)
400 поганий JSON, невалідний base64 або немає поля images
413 тіло порожнє або більше 30 МБ
429 busy — інша чернетка не завершилась за 120 с
503 not_configured — немає ключів TikTok
500 internal_error
```

Обмеження: тіло до 30 МБ, одна чернетка за раз (`draft_lock`), чекання статусу до 60 с.

### Створення чернетки (`tiktok-connect/tiktok_connect/drafts.py`)

`create_draft(config, tm, sources, title, description)`: перевірка лімітів (1–35 фото, назва ≤ 90,
опис ≤ 4000 UTF-16) → валідний токен (з оновленням) → `prepare_images` (JPEG ≤1080p у `data/media/<batch>/`)
→ перевірка, що публічні URL віддають `image/*` без редиректу → `content/init` з
`post_mode=MEDIA_UPLOAD`, `source=PULL_FROM_URL` → опитування `status/fetch` до `SEND_TO_USER_INBOX`
або `FAILED`. Помилки повертаються з підказкою з `test_draft.HINTS`.

### Помилки TikTok людською мовою (`bot/olxbot/tiktok_hook.py`)

`ERRORS_UA` перекладає коди: `spam_risk_too_many_pending_share`, `not_connected`, `access_token_invalid`,
`scope_not_authorized`, `app_version_check_failed`, `rate_limit_exceeded`, `photo_pull_failed`, `busy`.
Невідомі коди показуються як `код: повідомлення`.

## Тести

| Набір | Кількість | Що покриває (нове в 1.1.0) |
|---|---|---|
| `bot/tests` | 49 | контакти в пості, текст і назва TikTok, картка без поверху/ціни за м²/«Здає», вулиці зі списку й злиті, сума з іншого речення, парковка без суми, кнопки канал/TikTok з ✅ і захистом від повтору, помилка TikTok |
| `tiktok-connect/tests` | 36 | `/internal/draft`: успіх, токен, захист від проксі, вимкнення без токена, поганий запит, помилка TikTok, інші POST = 405 |

Запуск обох наборів — див. «Команди» в [DOCUMENTATION.md](DOCUMENTATION.md#команди). Telegram і TikTok у тестах
фейкові (`tools/fake_telegram.py`, `tests/fake_tiktok.py`), мережа не потрібна.
