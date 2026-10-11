"""Thin TikTok Open API v2 client (stdlib urllib only).

Docs:
  OAuth token / refresh / revoke: https://developers.tiktok.com/doc/oauth-user-access-token-management
  User info:                     https://developers.tiktok.com/doc/tiktok-api-v2-get-user-info
  Photo post (content/init):     https://developers.tiktok.com/doc/content-posting-api-reference-photo-post
  Post status:                   https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status
  Creator info:                  https://developers.tiktok.com/doc/content-posting-api-reference-query-creator-info
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import __version__

log = logging.getLogger(__name__)
USER_AGENT = f"tiktok-connect/{__version__}"


class TikTokError(Exception):
    """Error returned by TikTok (or transport failure). Never contains tokens."""

    def __init__(self, code: str, message: str = "", log_id: str = "", http_status: int | None = None):
        self.code = code or "unknown_error"
        self.message = message or ""
        self.log_id = log_id or ""
        self.http_status = http_status
        super().__init__(self.describe())

    def describe(self) -> str:
        parts = [self.code]
        if self.http_status:
            parts.append(f"HTTP {self.http_status}")
        if self.message:
            parts.append(self.message)
        if self.log_id:
            parts.append(f"log_id={self.log_id}")
        return " | ".join(parts)

    @property
    def retryable(self) -> bool:
        return self.code in {"rate_limit_exceeded", "internal_error", "network_error"} or (
            self.http_status is not None and self.http_status >= 500
        )


class TikTokClient:
    def __init__(self, api_base: str, client_key: str, client_secret: str, timeout: int = 20):
        self.api_base = api_base.rstrip("/")
        self.client_key = client_key
        self._client_secret = client_secret
        self.timeout = timeout

    # ------------------------------------------------------------------ transport
    def _request(self, method: str, path: str, *, form: dict | None = None,
                 json_body: Any = None, token: str | None = None,
                 query: dict | None = None) -> tuple[int, dict]:
        url = f"{self.api_base}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Cache-Control"] = "no-cache"
        elif json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=UTF-8"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TikTokError("network_error", f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}") from None
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise TikTokError("invalid_response", f"non-JSON response ({len(raw)} bytes)", http_status=status) from None
        if not isinstance(body, dict):
            raise TikTokError("invalid_response", "unexpected JSON shape", http_status=status)
        log.debug("TikTok %s %s -> HTTP %s", method, path, status)
        return status, body

    @staticmethod
    def _check_v2(status: int, body: dict) -> dict:
        """Open API v2 envelope: {"data": {...}, "error": {"code": "ok", ...}}."""
        err = body.get("error") or {}
        if isinstance(err, dict):
            code = err.get("code", "ok" if status < 400 else "unknown_error")
            if code != "ok" or status >= 400:
                raise TikTokError(code, err.get("message", ""), err.get("log_id") or err.get("logid", ""), status)
        elif status >= 400:
            raise TikTokError(str(err) or "unknown_error", http_status=status)
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _check_oauth(status: int, body: dict) -> dict:
        """OAuth endpoints return flat JSON; errors as {"error","error_description","log_id"}."""
        if status >= 400 or body.get("error") or "access_token" not in body:
            err = body.get("error")
            if isinstance(err, dict):  # be tolerant to the v2 envelope style
                raise TikTokError(err.get("code", "oauth_error"), err.get("message", ""), err.get("log_id", ""), status)
            raise TikTokError(str(err or "oauth_error"), body.get("error_description", ""), body.get("log_id", ""), status)
        return body

    # ------------------------------------------------------------------ OAuth
    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        status, body = self._request("POST", "/v2/oauth/token/", form={
            "client_key": self.client_key,
            "client_secret": self._client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        })
        return self._check_oauth(status, body)

    def refresh(self, refresh_token: str) -> dict:
        status, body = self._request("POST", "/v2/oauth/token/", form={
            "client_key": self.client_key,
            "client_secret": self._client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })
        return self._check_oauth(status, body)

    def revoke(self, access_token: str) -> None:
        status, body = self._request("POST", "/v2/oauth/revoke/", form={
            "client_key": self.client_key,
            "client_secret": self._client_secret,
            "token": access_token,
        })
        err = body.get("error")
        if isinstance(err, dict) and err.get("code") in (None, "", "ok"):
            err = None
        if status < 400 and not err:
            return
        if isinstance(err, dict):
            raise TikTokError(err.get("code", "revoke_failed"), err.get("message", ""), err.get("log_id", ""), status)
        raise TikTokError(str(err or "revoke_failed"), body.get("error_description", ""), body.get("log_id", ""), status)

    # ------------------------------------------------------------------ APIs
    def user_info(self, access_token: str, fields: str = "open_id,union_id,avatar_url,display_name") -> dict:
        status, body = self._request("GET", "/v2/user/info/", token=access_token, query={"fields": fields})
        return self._check_v2(status, body).get("user", {})

    def creator_info(self, access_token: str) -> dict:
        # Requires video.publish scope (Direct Post). Not needed for MEDIA_UPLOAD.
        status, body = self._request("POST", "/v2/post/publish/creator_info/query/", token=access_token, json_body={})
        return self._check_v2(status, body)

    def init_photo_post(self, access_token: str, payload: dict) -> dict:
        status, body = self._request("POST", "/v2/post/publish/content/init/", token=access_token, json_body=payload)
        return self._check_v2(status, body)

    def fetch_status(self, access_token: str, publish_id: str) -> dict:
        status, body = self._request("POST", "/v2/post/publish/status/fetch/", token=access_token,
                                     json_body={"publish_id": publish_id})
        return self._check_v2(status, body)
