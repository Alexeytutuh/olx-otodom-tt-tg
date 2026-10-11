"""Token lifecycle: store after code exchange, refresh before expiry, revoke.

Per https://developers.tiktok.com/doc/oauth-user-access-token-management :
access_token is valid 24h (expires_in=86400), refresh_token 365 days
(refresh_expires_in=31536000); a refresh may return a NEW refresh_token which
must replace the old one.
"""

from __future__ import annotations

import logging
import time

from .config import Config
from .storage import TokenStore
from .tiktok_api import TikTokClient, TikTokError

log = logging.getLogger(__name__)


def mask(value: str | None, keep: int = 4) -> str:
    if not value:
        return "-"
    return value[:keep] + "…" if len(value) > keep else "…"


class NotConnected(RuntimeError):
    pass


class TokenManager:
    def __init__(self, config: Config, client: TikTokClient | None = None, store: TokenStore | None = None):
        self.config = config
        self.client = client or TikTokClient(config.api_base, config.client_key, config.client_secret,
                                             config.http_timeout)
        self.store = store or TokenStore(config.tokens_file, config.lock_file)

    @staticmethod
    def _record_from_token_response(resp: dict, now: int | None = None) -> dict:
        now = int(now or time.time())
        return {
            "open_id": resp["open_id"],
            "access_token": resp["access_token"],
            "refresh_token": resp.get("refresh_token", ""),
            "scope": resp.get("scope", ""),
            "token_type": resp.get("token_type", "Bearer"),
            "access_expires_at": now + int(resp.get("expires_in", 86400)),
            "refresh_expires_at": now + int(resp.get("refresh_expires_in", 31536000)),
        }

    def connect_with_code(self, code: str) -> dict:
        resp = self.client.exchange_code(code, self.config.redirect_uri)
        if not resp.get("open_id"):
            raise TikTokError("invalid_response", "token response without open_id")
        record = self._record_from_token_response(resp)
        try:
            user = self.client.user_info(record["access_token"])
            record["display_name"] = user.get("display_name", "")
            record["avatar_url"] = user.get("avatar_url", "")
        except TikTokError as exc:
            log.warning("user.info failed after connect: %s", exc.describe())
        self.store.save_account(record, make_default=True)
        log.info("Connected TikTok account open_id=%s scopes=%s", mask(record["open_id"], 6), record["scope"])
        return record

    def refresh_account(self, open_id: str, force: bool = False) -> dict:
        """Refresh under the cross-process lock (refresh tokens may rotate)."""
        with self.store.locked():
            account = self.store.get_account(open_id)
            if not account:
                raise NotConnected(f"no stored account {mask(open_id, 6)}")
            now = int(time.time())
            if not force and account["access_expires_at"] - now > self.config.refresh_margin_seconds:
                return account
            if account.get("refresh_expires_at", 0) <= now:
                raise NotConnected("refresh token expired — reconnect via the web page")
            resp = self.client.refresh(account["refresh_token"])
            record = self._record_from_token_response(resp, now)
            record["open_id"] = account["open_id"]  # keep the key stable
            if not record["refresh_token"]:
                record["refresh_token"] = account["refresh_token"]
            self.store.save_account(record, make_default=False)
            log.info("Refreshed access token for open_id=%s (rotated refresh token: %s)",
                     mask(open_id, 6), record["refresh_token"] != account["refresh_token"])
            return self.store.get_account(open_id)  # type: ignore[return-value]

    def get_valid_account(self, open_id: str | None = None) -> dict:
        account = self.store.get_account(open_id)
        if not account:
            raise NotConnected("no connected TikTok account — open the web page and connect first")
        if account["access_expires_at"] - time.time() <= self.config.refresh_margin_seconds:
            account = self.refresh_account(account["open_id"])
        return account

    def refresh_all_due(self) -> None:
        data = self.store.load()
        for oid in list(data["accounts"]):
            try:
                self.refresh_account(oid)
            except (TikTokError, NotConnected) as exc:
                log.warning("Background refresh failed for open_id=%s: %s", mask(oid, 6), exc)

    def revoke_and_forget(self, open_id: str) -> None:
        account = self.store.get_account(open_id)
        if not account:
            raise NotConnected("unknown account")
        try:
            self.client.revoke(account["access_token"])
        finally:
            self.store.remove_account(open_id)
