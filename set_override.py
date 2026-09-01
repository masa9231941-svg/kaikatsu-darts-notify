#!/usr/bin/env python3
"""通知トグルの状態 data/override.json を書き換える.

環境変数:
  INPUT_ACTION : "on" | "off"（既定 on）
  INPUT_HOURS  : on にする時間数（空なら config.json の poll.override_hours、なければ 3）
  NTFY_TOPIC / NTFY_SERVER / NTFY_TOKEN : あれば確認プッシュを送る

on  → data/override.json = {"notify_until": "<now+hours>", ...}  （poll.py がこの時刻まで通知する）
off → data/override.json = {}                                    （既定＝通知しない状態に戻す）

Python 3 標準ライブラリのみ。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
OVERRIDE_PATH = ROOT / "data" / "override.json"
CONFIG_PATH = ROOT / "config.json"
USER_AGENT = "kaikatsu-darts-notify/1.0 (override)"


def cfg_hours(default: float = 3.0) -> float:
    try:
        c = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return float(c.get("poll", {}).get("override_hours", default))
    except Exception:
        return default


def send_ntfy(text: str) -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    url = topic if topic.startswith("http") else f"{server}/{topic}"
    headers = {
        "Title": "ダーツ通知トグル".encode("utf-8"),
        "Tags": "bell",
        "User-Agent": USER_AGENT,
    }
    token = os.environ.get("NTFY_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=text.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except Exception as exc:
        print(f"ntfy 送信失敗: {exc!r}", file=sys.stderr)


def write_off(reason: str) -> int:
    OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDE_PATH.write_text("{}\n", encoding="utf-8")
    msg = f"通知を OFF にしました（{reason}）" if reason else "通知を OFF にしました"
    print(msg)
    send_ntfy(msg)
    return 0


def main() -> int:
    action = (os.environ.get("INPUT_ACTION") or "on").strip().lower()
    now = datetime.now(JST)
    OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)

    if action == "off":
        return write_off("")

    raw = (os.environ.get("INPUT_HOURS") or "").strip()
    try:
        hours = float(raw) if raw else cfg_hours()
    except ValueError:
        hours = cfg_hours()
    if hours <= 0:
        return write_off("hours<=0")

    until = now + timedelta(hours=hours)
    payload = {
        "notify_until": until.strftime("%Y-%m-%dT%H:%M:%S"),
        "set_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "hours": hours,
    }
    OVERRIDE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    msg = f"通知を ON にしました（{until.strftime('%m-%d %H:%M')} まで / {hours:g}時間）"
    print(msg)
    send_ntfy(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
