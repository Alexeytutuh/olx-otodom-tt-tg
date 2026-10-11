# olx-tiktok

Оголошення про оренду з **OLX** і **Otodom** (Вроцлав, від власників) → Telegram-бот → за кнопками
пост у **Telegram-канал** і карусель у **чернетки TikTok**.

| Сервіс | Що робить |
|---|---|
| [`bot/`](bot/) — `olx-tiktok-bot` 1.1.0 | парсить OLX/Otodom кожні 5 хв, шле оферти з готовим постом, генерує слайди, публікує в канал і TikTok |
| [`tiktok-connect/`](tiktok-connect/) — 1.0.0 | OAuth TikTok, публічні фото для TikTok на `media.example.com`, внутрішній API для чернеток |

- Повна документація (запит, стек, хід робіт, експлуатація): [docs/DOCUMENTATION.md](docs/DOCUMENTATION.md)
- Код і модулі: [docs/CODE.md](docs/CODE.md)
- Встановлення з нуля: [bot/INSTALL_UA.md](bot/INSTALL_UA.md), [tiktok-connect/INSTALL_UA.md](tiktok-connect/INSTALL_UA.md)

Секрети (`.env`) і дані (`data/`) у репозиторій не входять; шаблони — `.env.example`.

Ліцензія: [MIT](LICENSE). Довідник вулиць — дані © OpenStreetMap contributors (ODbL). Шрифт Montserrat — SIL OFL.
