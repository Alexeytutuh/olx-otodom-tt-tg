"""Small admin CLI: list / refresh / revoke accounts, clean old media.

  docker exec tiktok-connect python -m tiktok_connect.admin list
  docker exec tiktok-connect python -m tiktok_connect.admin refresh [--open-id X]
  docker exec tiktok-connect python -m tiktok_connect.admin revoke --open-id X   # also deletes local tokens
  docker exec tiktok-connect python -m tiktok_connect.admin cleanup-media
"""

from __future__ import annotations

import argparse
import sys
import time

from .config import load_config, setup_logging
from .server import App
from .tiktok_api import TikTokError
from .tokens import NotConnected, TokenManager


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tiktok_connect.admin")
    p.add_argument("command", choices=["list", "refresh", "revoke", "cleanup-media"])
    p.add_argument("--open-id")
    args = p.parse_args(argv)
    setup_logging()
    config = load_config(require_secrets=args.command in ("refresh", "revoke"))
    tm = TokenManager(config)
    try:
        if args.command == "list":
            data = tm.store.load()
            now = time.time()
            for oid, a in data["accounts"].items():
                star = "*" if oid == data.get("default_open_id") else " "
                print(f"{star} {oid}  {a.get('display_name') or '?'}  scope={a.get('scope')}  "
                      f"access_left={int((a['access_expires_at'] - now) / 60)}min  "
                      f"refresh_left={int((a['refresh_expires_at'] - now) / 86400)}d")
            if not data["accounts"]:
                print("no connected accounts")
        elif args.command == "refresh":
            acc = tm.get_valid_account(args.open_id)
            acc = tm.refresh_account(acc["open_id"], force=True)
            print(f"refreshed; access token valid for {int((acc['access_expires_at'] - time.time()) / 3600)}h")
        elif args.command == "revoke":
            if not args.open_id:
                print("--open-id required (see `list`)")
                return 2
            tm.revoke_and_forget(args.open_id)
            print("revoked at TikTok and removed locally")
        elif args.command == "cleanup-media":
            print(f"removed {App(config, tm).cleanup_media()} old batch dir(s)")
    except (TikTokError, NotConnected) as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
