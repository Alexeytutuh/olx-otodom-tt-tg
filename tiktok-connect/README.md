# tiktok-connect

A small Python service for **Listing Publisher**. It connects one TikTok account with Login Kit for Web (OAuth v2). It then sends
**photo carousels to that account as drafts** through the Content Posting API (`post_mode=MEDIA_UPLOAD`,
`source=PULL_FROM_URL`). After that, the account owner finishes and publishes the post in the TikTok app.

* Web server: Python stdlib `http.server` (threaded). Runtime dependencies: Pillow (image conversion) and tzdata.
* Idle RSS is about 25 MB. Runs with `mem_limit: 128m`, a read-only root filesystem, a non-root user (uid 10001), and all capabilities dropped.
* No host ports. Caddy reaches it as `tiktok-connect:8090` on the docker network `n8n_default`.

## Endpoints

| Path | What it does |
|---|---|
| `GET /` | Ukrainian + English page with a **«Підключити TikTok»** button and the connection status (display name, scopes, token expiry; no secrets) |
| `GET /tiktok/connect` | Creates a random `state` (stored server-side, single use, expires in 10 min, bound to an HttpOnly cookie) and sends a 302 to `https://www.tiktok.com/v2/auth/authorize/?client_key…&scope=user.info.basic,video.upload&response_type=code&redirect_uri…&state…` |
| `GET /tiktok/callback` | Validates `state` and handles `error`/`error_description` (user denied). Exchanges `code` at `POST https://open.tiktokapis.com/v2/oauth/token/`, calls `/v2/user/info/` for the display name, and writes the tokens atomically to `/data/tokens.json` (mode 0600). Warns if the user did not grant every scope. |
| `GET /media/<batch>/<file>` | Static images from `/data/media`. Only jpg/jpeg/png/webp; no listing, dotfiles, traversal or symlink escapes. Supports `HEAD`, `Range`, `ETag`/304 and `Cache-Control: public, max-age=86400`. |
| `GET /privacy`, `GET /terms` | Privacy Policy and Terms of Service (English first, Ukrainian below), effective Oct 9, 2026 |
| `GET /health` | `{"status": "ok"}` |

A background thread refreshes access tokens about 1 h before they expire (it checks every 15 min). This keeps the 365-day
refresh token rotating. The same thread deletes media batches older than `MEDIA_RETENTION_DAYS` (default 14).

## CLI (run inside the container)

```bash
# 3 generated 1080x1920 demo slides
docker exec -it tiktok-connect python -m tiktok_connect.test_draft --demo

# real images (put them in /opt/tiktok-connect/data/in on the host = /data/in in the container)
docker exec -it tiktok-connect python -m tiktok_connect.test_draft \
  --images /data/in/1.jpg /data/in/2.jpg /data/in/3.webp \
  --title "iPhone 13 128GB" --description "Продам iPhone 13… #olx #iphone" --cover-index 0

# options: --dry-run  --skip-url-check  --creator-info  --wait-publish  --timeout 300  --open-id <id>  --aigc
docker exec tiktok-connect python -m tiktok_connect.admin list|refresh|revoke --open-id X|cleanup-media
```

`test_draft` does the following:
1. Converts every image to JPEG. It applies EXIF rotation, flattens alpha onto white, and fits the image inside 1080×1920 (or 1920×1080). Output stays under 20 MB. At most 35 images.
2. Writes them to an unguessable `/data/media/<timestamp>-<random>/NN.jpg`.
3. Sends a `HEAD` to every public URL (`https://media.example.com/media/...`). Each one must return 200 with an `image/*` content type and **no redirect**.
4. Calls `POST /v2/post/publish/content/init/` with
   `{"media_type":"PHOTO","post_mode":"MEDIA_UPLOAD","post_info":{"title","description"},"source_info":{"source":"PULL_FROM_URL","photo_images":[…],"photo_cover_index":0}}`.
   If it gets `access_token_invalid`, it refreshes the token and retries once.
5. Polls `POST /v2/post/publish/status/fetch/` every 5 s or more until `SEND_TO_USER_INBOX`, which counts as success for drafts.
   It also stops on `PUBLISH_COMPLETE` (with `--wait-publish`) or `FAILED`, or when the timeout runs out. It prints hints for documented error codes and fail reasons.

Exit codes: 0 ok, 1 TikTok/API failure, 2 bad input/config, 3 poll timeout.

## Configuration (`/opt/tiktok-connect/.env`, chmod 600)

| Var | Required | Default |
|---|---|---|
| `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` | yes. In Sandbox, use the **Sandbox** app's credentials | – |
| `PUBLIC_BASE_URL` | | `https://media.example.com` |
| `REDIRECT_URI` | must match the URI registered in Login Kit **exactly** | `${PUBLIC_BASE_URL}/tiktok/callback` |
| `LOG_LEVEL`, `MEDIA_RETENTION_DAYS`, `REFRESH_MARGIN_SECONDS`, `DISPLAY_TZ` | | `INFO`, `14`, `3600`, `Europe/Warsaw` |

Secrets and tokens are never logged. Request logs record only the path, never the query string, so the OAuth `code` stays out of them. The client key is logged masked.

## Verified TikTok API facts (developers.tiktok.com, pages last updated Aug 2026, checked 2026-10-09)

**Login Kit for Web**: https://developers.tiktok.com/doc/login-kit-web
* Authorize URL: `https://www.tiktok.com/v2/auth/authorize/` with `client_key`, `scope` (comma-separated), `redirect_uri`, `state`, `response_type=code` (and an optional `disable_auto_auth`).
* Redirect URIs must be absolute `https`, static (no query parameters), and have no fragment. Up to 10 URIs, each under 512 characters.
* The callback receives `code`, `scopes`, `state`, or `error` + `error_description`. Scopes can be toggled, so a user may grant only some of them. The service checks the granted scopes.

**Tokens**: https://developers.tiktok.com/doc/oauth-user-access-token-management
* `POST https://open.tiktokapis.com/v2/oauth/token/`, `application/x-www-form-urlencoded`. The code exchange sends `client_key, client_secret, code, grant_type=authorization_code, redirect_uri`. A refresh sends `grant_type=refresh_token, refresh_token`.
* The access token is valid **24 h** (`expires_in: 86400`). The refresh token is valid **365 days** (`refresh_expires_in: 31536000`). A refresh *may return a new refresh_token*, which must replace the old one. The service does this under a cross-process file lock.
* Errors come back as flat JSON: `{"error","error_description","log_id"}`. Revoke is `POST /v2/oauth/revoke/` (`token=<access_token>`).

**Photo post** (`/v2/post/publish/content/init/`): https://developers.tiktok.com/doc/content-posting-api-reference-photo-post
* Scope is `video.upload` for MEDIA_UPLOAD (or `video.publish` for DIRECT_POST). Rate limit: **6 requests/min per user token**.
* `media_type` = `PHOTO`. `post_mode` = `MEDIA_UPLOAD` means "Upload content to TikTok for users to complete the post using TikTok's editing flow. Users will receive an inbox notification."
* `source_info.source` = `PULL_FROM_URL` only. `photo_images` holds **up to 35** URLs and is required. `photo_cover_index` is 0-based and **required**.
* `post_info.title` is at most **90** UTF-16 units; `post_info.description` at most **4000** UTF-16 units. `privacy_level`, `disable_comment`, `auto_add_music` and the brand toggles apply **only to DIRECT_POST**.
* **Title/description are honored for MEDIA_UPLOAD photo drafts.** The Upload section says: "We now support sending title and description in this method and it will be reflected in the editing flow once user clicks on the inbox notification."
* Optional `is_aigc`.
* Errors: `invalid_param`; `app_version_check_failed` (MEDIA_UPLOAD needs **TikTok app ≥ 31.8**); `spam_risk_too_many_pending_share` (**at most 5 pending shares per 24 h**); `spam_risk_too_many_posts`; `reached_active_user_cap`; `unaudited_client_can_only_post_to_private_accounts`; `url_ownership_unverified`; `privacy_level_option_mismatch`; `access_token_invalid`/`scope_not_authorized` (401); `rate_limit_exceeded` (429); `internal_error` (5xx).
* Get-started guide: https://developers.tiktok.com/doc/content-posting-api-get-started-upload-content. The app must be approved for `video.upload`, the user must authorize it, and your UI should tell users to open the inbox notification.

**Status**: https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status
* `POST /v2/post/publish/status/fetch/` with `{"publish_id"}`. Limit: **30 requests/min per token**.
* Statuses: `PROCESSING_DOWNLOAD` (pull), `SEND_TO_USER_INBOX` (draft notification sent), `PUBLISH_COMPLETE` (for uploads, this means the user posted it from TikTok's editor), `FAILED` + `fail_reason` (e.g. `file_format_check_failed`, `picture_size_check_failed`, `photo_pull_failed`, `spam_risk_text`, `auth_removed`). Note the field name is spelled `publicaly_available_post_id`.

**Media / photo restrictions + PULL_FROM_URL**: https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide
* Images: **WebP or JPEG only (PNG is not listed)**, "**Maximum 1080p**", **≤ 20 MB** each.
* The URL must be `https`, **must not redirect** (3xx is treated as invalid), and must stay reachable while TikTok downloads (1-hour timeout). The domain or URL prefix must be verified for the app.

**Creator info**: https://developers.tiktok.com/doc/content-posting-api-reference-query-creator-info
* Scope is `video.publish`, and the call is required only to render the Direct Post export UI. **It is not needed for MEDIA_UPLOAD**, and an app with only `video.upload` gets `scope_not_authorized`. `--creator-info` lets you confirm this.

**User info**: https://developers.tiktok.com/doc/tiktok-api-v2-get-user-info. `GET /v2/user/info/?fields=open_id,union_id,avatar_url,display_name`; `display_name` is available with `user.info.basic`.

**Sandbox / unaudited restrictions**
* https://developers.tiktok.com/doc/add-a-sandbox
  * Up to 10 target users per sandbox. To add an account you must **log into that TikTok account** (its credentials) and accept the developer ToS. It may take up to 1 h to appear.
  * The page also states: "**Sandbox mode does not offer access to Content Posting API for public videos**".
* https://developers.tiktok.com/doc/content-sharing-guidelines
  * Unaudited Direct Post: content is `SELF_ONLY`, at most 5 users per 24 h, and accounts must be private.
  * The guidelines list "Not acceptable: A utility tool to help upload contents to the account(s) you or your team manages". Keep this in mind for a future production audit.
  * No watermarks or logos may be added to shared content; the demo slides are plain.

**General error envelope**: https://developers.tiktok.com/doc/tiktok-api-v2-error-handling

### Uncertainties (check them in the live test)
1. **Can a sandbox MEDIA_UPLOAD draft be published publicly?** The docs explicitly say unaudited *Direct Post* content is private-only, and that the sandbox has no Content Posting API "for public videos". `unaudited_client_can_only_post_to_private_accounts` is listed for `content/init` without being limited to Direct Post. Expect the draft to be finishable only as private/"Only me", or `content/init` to require a **private** account. If init fails with that code, temporarily switch the friend's account to private.
2. **"Maximum 1080p"** has no exact rule. We fit images inside 1080×1920 (or 1920×1080). If TikTok returns `picture_size_check_failed`, try a 1080 px maximum on both sides.
3. **Where the draft shows up**: the docs promise an **inbox notification** that opens TikTok's editor with the photos and the title/description. They don't say whether it also appears automatically in Profile → Drafts.
4. **URL ownership in Sandbox**: URL properties seem to be configured per sandbox/app config. If you get `url_ownership_unverified`, verify `media.example.com` inside the Sandbox "test" config as well.
5. The menu path for revoking access in the TikTok app varies between versions. The policy describes it approximately (Settings and privacy → Security & permissions → Manage app permissions).

## Caddy + Cloudflare notes

Append `Caddyfile.snippet` (`media.example.com { … reverse_proxy tiktok-connect:8090 }`) and reload Caddy. Step-by-step instructions are in `INSTALL_UA.md`.

* Cloudflare **Full / Full (strict)** connects to the origin "using the scheme requested by the visitor". Let's Encrypt's HTTP-01 request to `http://media.example.com/.well-known/acme-challenge/…` therefore reaches Caddy on port 80 through the orange cloud, and issuance works. Source: https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full/ .
* **Except when "Always Use HTTPS" is on.** Cloudflare then returns a 301 to https, LE follows it, and Cloudflare tries the origin on 443, where Caddy has no cert yet (525). TLS-ALPN-01 never works behind the orange cloud. Source: https://developers.cloudflare.com/ssl/edge-certificates/additional-options/always-use-https/ .
* Fixes, best first:
  1. Disable *Always Use HTTPS* (Caddy itself redirects http→https at the origin, which is loop-free in Full mode), or add a rule that skips the redirect for `/.well-known/acme-challenge/*`. This also keeps renewals working.
  2. Temporarily grey-cloud the DNS record (DNS only) until the cert is issued. Renewals about 60 days later will then hit the same problem unless option 1 is in place.
  3. Use a Cloudflare Origin CA certificate with `tls /path/cert.pem /path/key.pem` in Caddy. This needs the files mounted into the Caddy container and only works while the record stays orange-clouded.
* Once Caddy shows a valid LE certificate, set Cloudflare SSL mode to **Full (strict)**.
* If TikTok reports `photo_pull_failed`, make sure Cloudflare Bot Fight Mode / WAF isn't challenging `/media/*`.

## Development / tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -t .     # 31 tests, ~20 s
```

`tests/fake_tiktok.py` is an in-process fake of the TikTok API. Its OAuth token/refresh endpoints rotate refresh tokens. Its user info, creator_info (returns scope error), content/init and status endpoints **actually download the photo URLs** from the running service and validate JPEG/WebP and the 1080p limit. You can make it return injected errors (401/403/429/5xx).
