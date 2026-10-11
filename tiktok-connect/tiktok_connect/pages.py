"""HTML pages (no external assets; strict CSP-friendly)."""

from __future__ import annotations

import html
import os
from datetime import datetime, UTC

# Operator details for the Privacy / Terms pages come from the env file (TikTok requires real ones).
APP_NAME = os.environ.get("PAGES_APP_NAME") or "Listing Publisher"
OPERATOR = os.environ.get("PAGES_OPERATOR") or "Your Name"
OPERATOR_UK = os.environ.get("PAGES_OPERATOR_UK") or OPERATOR
CONTACT = os.environ.get("PAGES_CONTACT") or "contact@example.com"
POLICY_DATE_EN = "October 9, 2026"
POLICY_DATE_UK = "9 жовтня 2026 р."

_CSS = """
:root{color-scheme:light dark;--fg:#111;--bg:#fafafa;--card:#fff;--muted:#666;--acc:#fe2c55;--ok:#0a7d38;--err:#b00020}
@media (prefers-color-scheme:dark){:root{--fg:#eee;--bg:#121212;--card:#1d1d1d;--muted:#aaa}}
*{box-sizing:border-box}body{margin:0;font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:var(--fg);background:var(--bg)}
main{max-width:760px;margin:0 auto;padding:32px 20px}
.card{background:var(--card);border-radius:14px;padding:24px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:20px}
h1{font-size:1.6rem;margin:.2em 0}h2{font-size:1.2rem;margin-top:1.6em}.muted{color:var(--muted)}
.btn{display:inline-block;background:var(--acc);color:#fff;
text-decoration:none;font-weight:600;padding:14px 26px;border-radius:10px;font-size:1.05rem}
.btn:hover{filter:brightness(1.08)}.ok{color:var(--ok)}.err{color:var(--err)}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid rgba(127,127,127,.25);vertical-align:top}
footer{font-size:.9rem;color:var(--muted);text-align:center;padding:10px 0 30px}footer a{color:inherit}
code{font-size:.92em}
"""


def esc(v: object) -> str:
    return html.escape(str(v), quote=True)


def layout(title: str, body: str, lang: str = "uk") -> str:
    return f"""<!doctype html>
<html lang="{esc(lang)}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{esc(title)}</title><style>{_CSS}</style></head>
<body><main>{body}</main>
<footer>{esc(APP_NAME)} · <a href="/">Home</a> · <a href="/privacy">Privacy Policy</a> · <a href="/terms">Terms of Service</a></footer>
</body></html>"""


def fmt_ts(ts: int | None, tz) -> str:
    if not ts:
        return "-"
    dt = datetime.fromtimestamp(int(ts), tz=UTC).astimezone(tz)
    return dt.strftime("%Y-%m-%d %H:%M ") + (dt.tzname() or "")


def home_page(accounts: list[dict], tz, configured: bool) -> str:
    if accounts:
        rows = "".join(
            f"<tr><td><b>{esc(a.get('display_name') or '(name unavailable)')}</b><br>"
            f"<span class='muted'>open_id {esc(a['open_id'][:6])}…</span></td>"
            f"<td>{esc(a.get('scope', ''))}</td>"
            f"<td>{esc(fmt_ts(a.get('access_expires_at'), tz))}</td>"
            f"<td>{esc(fmt_ts(a.get('refresh_expires_at'), tz))}</td></tr>"
            for a in accounts
        )
        status = (
            "<p class='ok'>✅ Підключено / Connected</p>"
            "<table><tr><th>Акаунт / Account</th><th>Scopes</th><th>Access token до / until</th>"
            f"<th>Refresh token до / until</th></tr>{rows}</table>"
            "<p class='muted'>Access token оновлюється автоматично / refreshed automatically.</p>"
        )
    else:
        status = "<p class='muted'>Ще не підключено / Not connected yet.</p>"
    button = ('<p><a class="btn" href="/tiktok/connect">Підключити TikTok · Connect TikTok</a></p>'
              if configured else "<p class='err'>Service is not configured (missing client credentials).</p>")
    body = f"""
<div class="card">
<h1>Підключити TikTok</h1>
<p>Сервіс <b>{esc(APP_NAME)}</b> завантажує фото-каруселі як <b>чернетки</b> у ваш акаунт TikTok.
Публікуєте ви самі — у застосунку TikTok через сповіщення у «Вхідних».</p>
<p class="muted">{esc(APP_NAME)} uploads photo carousels to your TikTok account as <b>drafts</b>.
You review and publish them yourself in the TikTok app (via the inbox notification).
We request only: basic profile info (<code>user.info.basic</code>) and permission to upload drafts
(<code>video.upload</code>). We never post publicly on your behalf.</p>
{button}
</div>
<div class="card"><h2>Статус / Status</h2>{status}</div>
"""
    return layout("Підключити TikTok · Listing Publisher", body)


def result_page(ok: bool, title_uk: str, title_en: str, detail: str = "") -> str:
    cls = "ok" if ok else "err"
    icon = "✅" if ok else "⚠️"
    body = f"""<div class="card"><h1 class="{cls}">{icon} {esc(title_uk)}</h1>
<p>{esc(title_en)}</p>{f'<p class="muted">{esc(detail)}</p>' if detail else ''}
<p><a href="/">← На головну / Back</a></p></div>"""
    return layout(title_en, body)


def privacy_page() -> str:
    body = f"""<div class="card" lang="en">
<h1>Privacy Policy</h1>
<p class="muted">Effective date: {POLICY_DATE_EN}</p>
<p>This Privacy Policy explains how <b>{APP_NAME}</b> (“the App”, “we”), operated by
<b>{OPERATOR}</b> (contact: <a href="mailto:{CONTACT}">{CONTACT}</a>), handles information when you
connect your TikTok account.</p>
<h2>1. Information we access</h2>
<ul>
<li><b>Basic TikTok profile</b> (scope <code>user.info.basic</code>): your TikTok open_id (an app-specific
identifier), display name and avatar URL.</li>
<li><b>Permission to upload content to your drafts</b> (scope <code>video.upload</code>): lets the App send photo
carousels to your TikTok inbox/drafts. The App cannot publish publicly on your behalf; you decide whether
and how to publish inside the TikTok app.</li>
<li><b>OAuth tokens</b> issued by TikTok (access token and refresh token) and their expiry times.</li>
<li>Standard technical server logs (IP address, time, requested URL path, user agent) kept for security
and troubleshooting. Logs never contain your tokens.</li>
</ul>
<p>We do not access your videos, messages, followers, contacts or any other TikTok data.</p>
<h2>2. Purpose</h2>
<p>The information is used solely to (a) identify which TikTok account is connected, and (b) upload photo
posts (images, title and description) prepared for you as drafts so that you can review and publish them.</p>
<h2>3. Storage and security</h2>
<p>Tokens and profile data are stored on a server controlled by the operator (a virtual server rented from Hetzner) in a file readable only by
the service, transmitted only over HTTPS, and never written to logs. Images prepared for upload are stored
temporarily on the same server so TikTok can download them.</p>
<h2>4. Sharing</h2>
<p>We do not sell, rent or share your data with third parties. Data is sent only to TikTok, as required to
perform the upload you requested. We use no advertising or analytics trackers and no cookies except a
short-lived security cookie used during login.</p>
<h2>5. Retention</h2>
<p>Tokens and profile data are kept while your account remains connected and are deleted when you disconnect
or request deletion; refresh tokens expire automatically after at most 365 days. Uploaded images are
deleted from our server automatically within 14 days. Server logs are rotated and kept for no more than
30 days.</p>
<h2>6. How to revoke access</h2>
<p>You can revoke the App’s access at any time in the TikTok app: <i>Profile → Menu (☰) → Settings and
privacy → Security and permissions (Security) → Apps and services permissions / Manage app permissions</i>,
then remove “{APP_NAME}”. You can also email us and we will revoke the tokens via TikTok’s revoke endpoint.</p>
<h2>7. Your rights and deletion requests</h2>
<p>You may request access to, correction or deletion of your data by emailing
<a href="mailto:{CONTACT}">{CONTACT}</a>. We will delete your stored data within 30 days (normally much
sooner) and confirm by email. If you are in the EU/EEA you also have the right to lodge a complaint with your
data protection authority.</p>
<h2>8. Children</h2>
<p>The App is not intended for persons under 18.</p>
<h2>9. Changes</h2>
<p>We may update this policy; the effective date above will change accordingly.</p>
</div>
<div class="card" lang="uk">
<h1>Політика конфіденційності</h1>
<p class="muted">Дата набрання чинності: {POLICY_DATE_UK}</p>
<p>Ця політика пояснює, як застосунок <b>{APP_NAME}</b>, оператор — <b>{OPERATOR_UK}</b>
(контакт: <a href="mailto:{CONTACT}">{CONTACT}</a>), обробляє дані, коли ви підключаєте свій акаунт TikTok.</p>
<ul>
<li><b>Які дані:</b> базовий профіль TikTok (open_id, відображуване ім’я, аватар; scope
<code>user.info.basic</code>), дозвіл завантажувати контент у ваші чернетки (scope <code>video.upload</code>),
OAuth-токени та строк їх дії, стандартні технічні журнали сервера (без токенів).</li>
<li><b>Мета:</b> лише визначити підключений акаунт і завантажити підготовлені фото-каруселі (зображення,
заголовок, опис) як чернетки. Публікуєте ви самі в застосунку TikTok.</li>
<li><b>Зберігання:</b> на сервері під контролем оператора (орендований у Hetzner), у файлі з обмеженим доступом, лише через HTTPS.</li>
<li><b>Передача третім особам:</b> не продаємо і не передаємо; дані надсилаються лише до TikTok для виконання
завантаження.</li>
<li><b>Строк зберігання:</b> поки акаунт підключено; refresh-токен спливає максимум через 365 днів;
зображення видаляються протягом 14 днів; журнали — до 30 днів.</li>
<li><b>Відкликання доступу:</b> у TikTok: Профіль → Меню (☰) → Налаштування та конфіденційність → Безпека
(та дозволи) → Дозволи застосунків і сервісів / Manage app permissions → видалити «{APP_NAME}»; або напишіть
нам на email.</li>
<li><b>Видалення даних:</b> напишіть на <a href="mailto:{CONTACT}">{CONTACT}</a> — видалимо протягом 30 днів
і підтвердимо.</li>
</ul>
</div>"""
    return layout("Privacy Policy · Listing Publisher", body, lang="en")


def terms_page() -> str:
    body = f"""<div class="card" lang="en">
<h1>Terms of Service</h1>
<p class="muted">Effective date: {POLICY_DATE_EN}</p>
<p>These Terms govern your use of <b>{APP_NAME}</b> (“the App”), operated by <b>{OPERATOR}</b>
(contact: <a href="mailto:{CONTACT}">{CONTACT}</a>). By connecting your TikTok account you agree to them.</p>
<h2>1. The service</h2>
<p>The App lets you connect your TikTok account via TikTok Login Kit and upload photo carousels (for example,
listing photos with a title and description) to your TikTok inbox as drafts using the TikTok Content
Posting API. You finish and publish each post yourself in the TikTok app. The App does not publish
content publicly on your behalf.</p>
<h2>2. Your responsibilities</h2>
<ul>
<li>Only connect a TikTok account that you own or are authorized to manage.</li>
<li>Only upload content you own or have the rights to use, and that complies with the
<a href="https://www.tiktok.com/legal/terms-of-service">TikTok Terms of Service</a> and
<a href="https://www.tiktok.com/community-guidelines">Community Guidelines</a>.</li>
<li>You are responsible for reviewing every draft before publishing.</li>
</ul>
<h2>3. TikTok</h2>
<p>The App is an independent tool and is not affiliated with, endorsed or sponsored by TikTok. Your use of
TikTok remains subject to TikTok’s own terms. TikTok may limit API usage (e.g. daily upload caps).</p>
<h2>4. Privacy</h2>
<p>Our handling of your data is described in the <a href="/privacy">Privacy Policy</a>.</p>
<h2>5. Availability and changes</h2>
<p>The App is provided free of charge, “as is” and “as available”, without warranties of any kind. We may
modify, suspend or discontinue it at any time.</p>
<h2>6. Limitation of liability</h2>
<p>To the maximum extent permitted by law, the operator is not liable for indirect or consequential damages,
lost content or lost profits arising from use of the App. Nothing in these Terms limits liability that
cannot be limited under applicable law.</p>
<h2>7. Termination</h2>
<p>You may stop using the App at any time and revoke its access in TikTok settings (Security / Manage app
permissions) or by emailing us. We may suspend access in case of misuse.</p>
<h2>8. Governing law and contact</h2>
<p>These Terms are governed by the laws of the Republic of Poland, without prejudice to mandatory consumer
protection rules of your country of residence. Questions: <a href="mailto:{CONTACT}">{CONTACT}</a>.</p>
</div>
<div class="card" lang="uk">
<h1>Умови використання</h1>
<p class="muted">Дата набрання чинності: {POLICY_DATE_UK}</p>
<ul>
<li><b>Сервіс:</b> {APP_NAME} (оператор — {OPERATOR_UK}) дозволяє підключити акаунт TikTok і завантажувати
фото-каруселі як чернетки через TikTok Content Posting API. Публікуєте ви самі в застосунку TikTok.</li>
<li><b>Ваші обов’язки:</b> підключайте лише власний акаунт; завантажуйте лише контент, на який маєте права і
який відповідає правилам TikTok; перевіряйте кожну чернетку перед публікацією.</li>
<li><b>TikTok:</b> застосунок не пов’язаний з TikTok і не схвалений ним; діють також умови TikTok та ліміти API.</li>
<li><b>Конфіденційність:</b> див. <a href="/privacy">Політику конфіденційності</a>.</li>
<li><b>Відповідальність:</b> сервіс надається безкоштовно «як є», без гарантій; оператор не відповідає за
непрямі збитки в межах, дозволених законом.</li>
<li><b>Припинення:</b> ви можете будь-коли відкликати доступ у налаштуваннях TikTok або написати на
<a href="mailto:{CONTACT}">{CONTACT}</a>.</li>
<li><b>Право:</b> законодавство Республіки Польща (без обмеження імперативних норм захисту споживачів).</li>
</ul>
</div>"""
    return layout("Terms of Service · Listing Publisher", body, lang="en")
