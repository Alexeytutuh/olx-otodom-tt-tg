# Парсери OLX і Otodom (Вроцлав, оренда квартир від власників)

Два незалежні модулі. Пізніше їх підключить Telegram-бот. Зовнішня залежність лише одна, і вона необов'язкова: `curl_cffi`. Якщо її немає, модулі звертаються до системного `curl`.

```
pip install curl_cffi          # бажано
cd /workspace/olx-tiktok
python3 -m parsers.olx --once --json            # поточні підхожі оголошення + відхилені за причинами
python3 -m parsers.otodom --once --json         # те саме для Otodom (заходить на сторінку кожного нового оголошення)
python3 -m parsers.otodom --once --no-details   # швидко, без сторінок оголошень (менше даних)
python3 -m parsers.smoke_live                   # живий тест обох сайтів + покриття полів
python3 -m unittest discover -s parsers/tests -t .   # юніт-тести на збережених відповідях
```
Корисні прапорці: `--show-rejected`, `--seen-file seen.json` (запам'ятовує вже бачені id між запусками), `--loop` (опитування раз на 5 хв), `--save-raw DIR` (зберігає сирі відповіді), `--transport curl`.

## Файли
| Файл | Що робить |
|---|---|
| `config.py` | **Усі критерії в одному місці**: кімнати 1–3, які типи будинків дозволені й заборонені, ціна і площа (без обмежень), вікно новизни 48 год, інтервал 300 с, затримки, ретраї, слова-ознаки агентів |
| `models.py` | `Listing`: однакова схема для обох джерел (`to_dict()` / `to_json()`), `FetchResult`, `Rejection` |
| `common.py` | HTTP-клієнт (curl_cffi → curl): таймаути, ретраї з наростаючою паузою, пауза між запитами, розпізнавання капчі. Також `SourceDownError` і `Health` |
| `extract.py` | Регулярні вирази для польських текстів: czynsz, kaucja, media, prąd, garaż, «w cenie», дата «dostępne od» |
| `filters.py` | Тип будинку, приватний власник чи агент (враховує заперечення: «bez prowizji» означає власника), кімната чи ціла квартира, перевірка критеріїв |
| `olx.py`, `otodom.py` | Парсери з функціями `fetch_new(seen_ids)` і `health()`, а також CLI |

## Як використовувати в боті
```python
from parsers.olx import OlxParser
from parsers.common import SourceDownError
p = OlxParser()
try:
    new = p.fetch_new(seen_ids)              # list[Listing], найновіші першими
    seen_ids |= p.last_result.seen_ids        # усе переглянуте, зокрема відхилене
    # дедуплікація між OLX і Otodom: Listing.dedupe_key ("otodom:ID4Dsnv" для імпорту з Otodom і для самого Otodom)
    # p.last_result.retry_ids НЕ позначати як бачені: це тимчасові збої, повторимо наступного разу
except SourceDownError as e:
    alert(f"{e.source} відвалився: {e.reason}")   # e.kind: blocked/captcha/rate_limited/structure/network/http
p.health()   # last_success_at, consecutive_failures, last_error, status
```

## Як працюють фільтри
* **OLX**: категорія 15 (перевірено: це «mieszkania/wynajem», а 11 означає «stancje-pokoje»), місто 19701, `owner_type=private`, кімнати фільтруються на сервері. Локально ще раз перевіряються `business=false`, бізнес-сторінка, місто, тип будинку, ознаки агента в тексті та імені продавця. Додатково відкидається автор, у якого 4 і більше оголошень в одній видачі. Імпорти з Otodom (`partner.code=otodom_pl`) лишаються, якщо вони приватні, і отримують `cross_source_ref` з id та посиланням на Otodom.
* **Otodom**: параметр `isPrivateOwner=true` **працює на сервері** (перевірено: 146 із 146 приватні). `ownerTypeSingleSelect` діє лише для продажу, тому для оренди він нічого не фільтрує. Локально ще раз перевіряються `isPrivateOwner`, `extendedAdvertiserType` і на сторінці оголошення `advertiserType/owner.type`. Тип будинку, czynsz, kaucja, «dostępne od», поверх і координати беруться зі сторінки оголошення (`ad.characteristics`). Фільтр у URL `buildingType=[BLOCK,...]` існує, але не використовується: він викинув би оголошення без типу, а їх потрібно пропускати.
* **Тип будинку**: blok, plomba, szeregowiec, apartamentowiec проходять. Kamienica, dom wolnostojący, loft відкидаються. «pozostałe/other» або порожнє поле проходять.
* **Новизна** визначається за часом створення (`created_time` / `createdAtFirst`), позиція у видачі не враховується. Підняті старі оголошення й промо не вважаються новими і не змушують гортати далі.

## Результати живої перевірки (09.10.2026)
| | OLX | Otodom |
|---|---|---|
| Час одного прогону | ~12–13 с (5 сторінок) | ~50–55 с (1 сторінка пошуку і 20 сторінок оголошень); з `--no-details` ~2 с |
| Блокування | немає, усі відповіді HTTP 200 | немає, усі відповіді HTTP 200 |
| czynsz / kaucja / тип будинку / dostępne od | 83% / 41% / 100% / 60% | 100% / 86% / 93% / 100% |

Детальніше в `test_output.txt`.

## Відомі обмеження
* У OLX немає окремого поля для kaucja і дати заїзду, тому вони беруться з опису регулярками: ~41% і ~60%. Якщо в тексті «czynsz + prąd 1200 zł», ці суми не розділяються.
* Пошук агентів за текстом і за іменем продавця (наприклад, «luxholiday.pl», «Vantage Rent») евристичний. Можливі помилки в обидва боки. Причина кожного відхилення записується в `rejected`.
* Otodom з `--no-details` не знає типу будинку, тому кам'яниці проходять. Для бота сторінки оголошень потрібні, і це ~1,5–2,5 с на кожне нове оголошення.
* Оголошення OLX, імпортовані з Otodom, мають `cross_source_ref.public_id`, і він збігається з `raw_extra.public_id` у Otodom. Якщо власник сам виклав оголошення на обох сайтах, спільного id немає. Для дедуплікації знадобляться ціна, площа, кімнати, заголовок і фото.
* Ліміти запитів публічного API OLX і сайту Otodom не опубліковані. Інтервал 5 хв і пауза 1,2–2,5 с між запитами дуже обережні.

## Після рев'ю (09.10.2026)
Див. `REVIEW.md`. Коротко: виправлено хибні суми (рядок за рядком, «w cenie 200 zł», спільні суми «czynsz i prąd 500»), дату заїзду (дати огляду, дефолтний `free_from` Otodom), ізоляцію збою одного оголошення (`retry_ids`, потім `parse_error`), пейджинг OLX (`OLX_MIN_LOOKBACK_HOURS`), 503-капчу, нові ознаки агентів (бренди агентств, `agent_duplicate_text` для того самого тексту з другого акаунта). Нове поле `dedupe_key`. Python ≥ 3.9 (`python3`).
