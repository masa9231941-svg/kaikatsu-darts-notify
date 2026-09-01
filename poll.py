#!/usr/bin/env python3
"""快活CLUB ダーツ席 ポーラー.

config.json の各店について空席照会 API を1回ずつ叩き、
  - data/log/YYYY-MM.csv に1行ずつ追記
  - data/state.json を更新（差分検出・cooldown・ダーツ台数推定）
  - 満席→空き の遷移を検出したら ntfy にプッシュ

Python 3 標準ライブラリのみ。GitHub Actions (ubuntu-latest) 上での実行を想定。
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- 定数 ---------------------------------------------------------------

API_URL = "https://jx5rl6ilkg.execute-api.ap-northeast-1.amazonaws.com/prd/empty_seat"
API_KEY = "VBVkOEaMZR5WKLi7mpKiAaFS5INR2rAR6Bgw7aOs"  # 公式サイトの JS に平文で埋め込まれている値
USER_AGENT = "kaikatsu-darts-notify/1.0 (personal use; polls once per ~10min)"
DARTS_CATEGORY_ID = "10"          # seat_type[].category_id が "10" のものがダーツ枠
VACANCY_PAGE = "https://www.kaikatsu.jp/shop/detail/vacancy.html?store_code={code}"

JST = timezone(timedelta(hours=9))

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
LOG_DIR = ROOT / "data" / "log"
STATE_PATH = ROOT / "data" / "state.json"

CSV_FIELDS = [
    "ts_jst", "ts_slot", "store_code", "store_name",
    "seat_status", "status_no", "state", "free_count",
]

# state 値
FULL, AVAILABLE, FEW, UNKNOWN, ERROR = "FULL", "AVAILABLE", "FEW", "UNKNOWN", "ERROR"
# ここに含まれる直前状態からの遷移だけを「空きが出た」とみなす
NOTIFY_FROM = {FULL, UNKNOWN, None}


# --- ユーティリティ ---------------------------------------------------

def now_jst() -> datetime:
    return datetime.now(JST)


def data_slot(now: datetime) -> datetime:
    """この時点で参照できる空席データが表す10分スロット.

    公式ページの updateClock() と同じ計算（現在時刻 -2分 を10分で切り捨て）。
    """
    shifted = now - timedelta(minutes=2)
    return shifted.replace(minute=(shifted.minute // 10) * 10, second=0, microsecond=0)


def parse_free_count(seat_status: str):
    """seat_status 文字列 → 空き台数(int) or None."""
    if not seat_status:
        return None
    s = seat_status.strip()
    if s == "満席":
        return 0
    if "以上" in s:                       # 「残10席以上」など
        m = re.search(r"(\d+)", s)
        return int(m.group(1)) if m else None
    m = re.search(r"(\d+)", s)            # 「残N席」
    return int(m.group(1)) if m else None


def classify(darts: dict | None) -> tuple[str, str, str, object]:
    """ダーツ枠の dict → (state, seat_status, status_no, free_count)."""
    if not darts:
        return UNKNOWN, "", "", None
    seat_status = str(darts.get("seat_status", ""))
    status_no = str(darts.get("status_no", ""))
    free = parse_free_count(seat_status)
    if status_no == "4" or seat_status == "満席":
        state = FULL
    elif status_no == "1":
        state = AVAILABLE
    elif status_no in ("2", "3"):
        state = FEW
    else:
        state = UNKNOWN
    return state, seat_status, status_no, free


def fetch_darts(code: str) -> tuple[str, str, str, object]:
    """1店ぶん取得して classify した結果を返す。失敗時は state=ERROR."""
    url = f"{API_URL}?store_cd={code}"
    req = urllib.request.Request(url, headers={
        "x-api-key": API_KEY,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "ja",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        # 意図的に broad except: http.client.RemoteDisconnected / IncompleteRead など
        # OSError の派生でない一過性のネットワーク例外もあり、それで実行全体を落としたくない。
        # 1店の失敗は ERROR 扱いにしてスキップするだけ（次の cron が10分後に来る）。
        print(f"  [{code}] fetch error: {exc!r}", file=sys.stderr)
        return ERROR, "", "", None

    if str(payload.get("status")) not in ("0", "0.0"):
        print(f"  [{code}] api status={payload.get('status')} msg={payload.get('message')!r}",
              file=sys.stderr)
        return ERROR, "", "", None

    darts = next(
        (s for s in payload.get("seat_type", []) if str(s.get("category_id")) == DARTS_CATEGORY_ID),
        None,
    )
    return classify(darts)


def in_quiet_hours(now: datetime, quiet: dict | None) -> bool:
    if not quiet:
        return False
    try:
        sh, sm = map(int, str(quiet["start"]).split(":"))
        eh, em = map(int, str(quiet["end"]).split(":"))
    except (KeyError, ValueError):
        return False
    start = sh * 60 + sm
    end = eh * 60 + em
    cur = now.hour * 60 + now.minute
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end          # 日をまたぐ (例 01:00-09:00 は跨がないが 23:00-06:00 用)


def prospect_rank(free_count, data_delay_min: float, travel_min, margin_per_free: float) -> str | None:
    """「取れる見込み」を 高/中/低 で返す。travel_min 未設定なら None."""
    if travel_min is None:
        return None
    free = free_count or 0
    total_delay = data_delay_min + float(travel_min)
    margin = free * margin_per_free - total_delay
    if margin >= 10:
        return "高"
    if margin >= -6:
        return "中"
    return "低"


# --- ntfy ------------------------------------------------------------

def send_ntfy(title: str, body: str, click: str, actions: str | None) -> bool:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("  NTFY_TOPIC 未設定 — 送信スキップ（ログのみ）", file=sys.stderr)
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    url = f"{server}/{topic}" if not topic.startswith("http") else topic
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": "high",
        "Tags": "dart",
        "Click": click,
        "User-Agent": USER_AGENT,
    }
    if actions:
        headers["Actions"] = actions.encode("utf-8")
    token = os.environ.get("NTFY_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        return True
    except Exception as exc:               # 送信失敗で実行全体を落とさない（fetch_darts と同じ理由）
        print(f"  ntfy 送信失敗: {exc!r}", file=sys.stderr)
        return False


def process_store(store, code, state, now, slot, data_delay_min, ts_jst, ts_slot,
                   notify_on, cooldown_min, quiet, margin_per_free) -> None:
    """1店ぶん取得→CSV追記→state更新→(必要なら)ntfy送信. state は in-place で更新."""
    name = store.get("name", code)

    cur_state, seat_status, status_no, free = fetch_darts(code)
    print(f"[{code}] {name}: {cur_state} {seat_status!r} free={free}")

    append_csv({
        "ts_jst": ts_jst, "ts_slot": ts_slot,
        "store_code": code, "store_name": name,
        "seat_status": seat_status, "status_no": status_no,
        "state": cur_state, "free_count": "" if free is None else free,
    })

    if cur_state == ERROR:
        return                                          # 取得失敗は state を触らない（遷移を取りこぼさない）

    st = state.setdefault(code, {})
    prev_state = st.get("last_state")

    # ダーツ台数の推定: config の公式台数 / 過去の最大 free / 今回の free の最大値
    cap_candidates = [st.get("capacity_est") or 0]
    if store.get("darts_units"):
        cap_candidates.append(int(store["darts_units"]))
    if free is not None:
        cap_candidates.append(free)
    st["capacity_est"] = max(cap_candidates)
    st["last_seen_ts"] = int(now.timestamp())

    # --- 通知判定 ---
    want_notify = bool(store.get("notify"))
    is_open_edge = prev_state in NOTIFY_FROM and cur_state in notify_on
    last_notified = st.get("last_notified_ts", 0)
    cooled = (now.timestamp() - last_notified) >= cooldown_min * 60
    quiet_now = in_quiet_hours(now, quiet)

    if want_notify and is_open_edge and cooled and not quiet_now:
        travel = store.get("travel_min")
        rank = prospect_rank(free, data_delay_min, travel, margin_per_free)
        title = f"空き: {name}（{seat_status or '空きあり'}）"
        parts = [f"{slot.strftime('%H:%M')}時点",
                 f"データ遅延~{round(data_delay_min)}分"]
        if travel is not None:
            parts.append(f"移動{travel}分 ⇒ 見込み: {rank}")
        else:
            parts.append("移動時間未設定")
        body = " / ".join(parts)
        click = VACANCY_PAGE.format(code=code)
        maps = "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(
            f"快活CLUB {name}")
        actions = f"view, 地図で開く, {maps}"
        if send_ntfy(title, body, click, actions):
            st["last_notified_ts"] = int(now.timestamp())
            print(f"  -> ntfy 送信: {title} | {body}")
    elif want_notify and is_open_edge:
        reason = "cooldown中" if not cooled else "quiet_hours中" if quiet_now else "?"
        print(f"  -> 空き遷移だが通知抑制（{reason}）")

    st["last_state"] = cur_state


# --- メイン --------------------------------------------------------

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def append_csv(row: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{row['ts_jst'][:7]}.csv"        # YYYY-MM.csv
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(row)


def main() -> int:
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        print(f"config.json が読めません: {CONFIG_PATH}", file=sys.stderr)
        return 1

    poll_cfg = cfg.get("poll", {})
    notify_on = set(poll_cfg.get("notify_on", [AVAILABLE]))
    cooldown_min = float(poll_cfg.get("cooldown_min", 30))
    quiet = poll_cfg.get("quiet_hours")
    gap = float(poll_cfg.get("request_gap_sec", 1.0))
    margin_per_free = float(poll_cfg.get("hindsight_margin_per_free_min", 6))

    state = load_json(STATE_PATH, {})
    now = now_jst()
    slot = data_slot(now)
    data_delay_min = (now - slot).total_seconds() / 60.0
    ts_jst = now.strftime("%Y-%m-%dT%H:%M:%S")
    ts_slot = slot.strftime("%Y-%m-%dT%H:%M")

    stores = cfg.get("stores", [])
    for i, store in enumerate(stores):
        code = str(store.get("code", f"#{i}"))
        if i:
            time.sleep(gap)                            # 店間ディレイ（並列アクセスしない）
        try:
            process_store(
                store, code, state, now, slot, data_delay_min, ts_jst, ts_slot,
                notify_on, cooldown_min, quiet, margin_per_free,
            )
        except Exception as exc:
            # 1店の処理でどんな例外が起きても他の店・CSV追記・state保存は続行する
            # （broad except は意図的: cron が10分後にまた来るので今回はスキップで十分）。
            print(f"[{code}] unexpected error, skipping this store: {exc!r}", file=sys.stderr)

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
                          encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
