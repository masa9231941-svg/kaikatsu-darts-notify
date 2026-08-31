#!/usr/bin/env python3
"""data/log/*.csv を集計して heatmap/heatmap.json と heatmap.md を生成する.

store_code × 曜日 × 30分スロット ごとに:
  - p_open           : state が AVAILABLE/FEW だった割合（空きありの出やすさ）
  - n                : 観測回数
  - avg_free         : 空き台数の平均
  - avg_occupancy    : (推定台数 - 空き台数) の平均
店ごとのスカラー:
  - capacity         : 推定ダーツ台数
  - openings_per_hour: 満席→空き の遷移回数 / 観測時間
  - median_full_streak_min: 満席が連続した時間の中央値

Python 3 標準ライブラリのみ。
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "data" / "log"
STATE_PATH = ROOT / "data" / "state.json"
CONFIG_PATH = ROOT / "config.json"
OUT_DIR = ROOT / "heatmap"

OPEN_STATES = {"AVAILABLE", "FEW"}
WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]
BLOCKS = " ▁▂▃▄▅▆▇█"          # p_open 0..1 を 9 段階で


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def read_rows():
    rows = []
    for path in sorted(LOG_DIR.glob("*.csv")):
        with path.open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    r["_ts"] = datetime.strptime(r["ts_jst"], "%Y-%m-%dT%H:%M:%S")
                except (KeyError, ValueError):
                    continue
                fc = r.get("free_count", "")
                r["_free"] = int(fc) if str(fc).strip().lstrip("-").isdigit() else None
                rows.append(r)
    return rows


def median(values):
    vs = sorted(values)
    if not vs:
        return None
    mid = len(vs) // 2
    return vs[mid] if len(vs) % 2 else (vs[mid - 1] + vs[mid]) / 2


def main() -> int:
    rows = read_rows()
    if not rows:
        print("ログがまだありません。")
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "heatmap.md").write_text("# ヒートマップ\n\nデータ未取得。\n", encoding="utf-8")
        return 0

    state = load_json(STATE_PATH, {})
    cfg = load_json(CONFIG_PATH, {})
    units = {str(s["code"]): s.get("darts_units") for s in cfg.get("stores", [])}
    names = {str(s["code"]): s.get("name", s["code"]) for s in cfg.get("stores", [])}

    by_store: dict[str, list] = {}
    for r in rows:
        by_store.setdefault(r["store_code"], []).append(r)

    out = {"generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "stores": {}}

    for code, srows in sorted(by_store.items()):
        srows.sort(key=lambda r: r["_ts"])
        name = names.get(code) or srows[-1].get("store_name") or code

        observed_free = [r["_free"] for r in srows if r["_free"] is not None]
        capacity = max(
            [state.get(code, {}).get("capacity_est") or 0]
            + ([int(units[code])] if units.get(code) else [])
            + observed_free
        ) or None

        # --- 遷移解析（満席ストリーク / opening 回数）---
        openings = 0
        full_streaks = []
        full_since = None
        prev = None
        for r in srows:
            s = r["state"]
            if s == "ERROR":
                continue
            if s == "FULL" and prev != "FULL":
                full_since = r["_ts"]
            if prev == "FULL" and s in OPEN_STATES:
                openings += 1
                if full_since is not None:
                    full_streaks.append((r["_ts"] - full_since).total_seconds() / 60.0)
                    full_since = None
            prev = s
        span_h = (srows[-1]["_ts"] - srows[0]["_ts"]).total_seconds() / 3600.0

        # --- 曜日 × 30分スロット ---
        grid: dict[int, dict[int, dict]] = {}
        for r in srows:
            if r["state"] == "ERROR":
                continue
            wd = r["_ts"].weekday()
            slot = (r["_ts"].hour * 60 + r["_ts"].minute) // 30
            cell = grid.setdefault(wd, {}).setdefault(slot, {"n": 0, "open": 0, "free_sum": 0, "free_n": 0})
            cell["n"] += 1
            if r["state"] in OPEN_STATES:
                cell["open"] += 1
            if r["_free"] is not None:
                cell["free_sum"] += r["_free"]
                cell["free_n"] += 1

        grid_out = {}
        for wd, slots in grid.items():
            grid_out[wd] = {}
            for slot, c in slots.items():
                p_open = c["open"] / c["n"] if c["n"] else 0.0
                avg_free = c["free_sum"] / c["free_n"] if c["free_n"] else None
                avg_occ = (capacity - avg_free) if (capacity and avg_free is not None) else None
                grid_out[wd][slot] = {
                    "p_open": round(p_open, 3),
                    "n": c["n"],
                    "avg_free": round(avg_free, 2) if avg_free is not None else None,
                    "avg_occupancy": round(avg_occ, 2) if avg_occ is not None else None,
                }

        out["stores"][code] = {
            "name": name,
            "capacity": capacity,
            "openings_per_hour": round(openings / span_h, 3) if span_h > 0 else None,
            "median_full_streak_min": round(median(full_streaks), 1) if full_streaks else None,
            "n_observations": len(srows),
            "grid": grid_out,
        }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "heatmap.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    (OUT_DIR / "heatmap.md").write_text(render_md(out), encoding="utf-8")
    print(f"stores={len(out['stores'])}  rows={len(rows)}  -> heatmap/")
    return 0


def render_md(out: dict) -> str:
    lines = [
        "# ダーツ空き ヒートマップ",
        "",
        f"生成: {out['generated_at']}  ／  セル = その時間帯に「空きあり」だった割合"
        f"（`{BLOCKS.strip()}` 低→高、空白=データ無し）",
        "",
    ]
    for code, s in sorted(out["stores"].items(), key=lambda kv: kv[1]["name"]):
        lines.append(f"## {s['name']}（{code}）")
        cap = s["capacity"]
        lines.append(
            f"- 推定台数: {cap if cap is not None else '不明'} ／ "
            f"満席→空き: {s['openings_per_hour'] if s['openings_per_hour'] is not None else '—'} 回/時 ／ "
            f"満席の続く時間(中央値): {s['median_full_streak_min'] if s['median_full_streak_min'] is not None else '—'} 分 ／ "
            f"観測 {s['n_observations']} 回")
        lines.append("")
        lines.append("```")
        lines.append("時  " + "".join(f"{w} " for w in WEEKDAYS))
        for hour in range(24):
            cells = []
            for wd in range(7):
                slots = s["grid"].get(str(wd)) or s["grid"].get(wd) or {}
                vals, ns = [], 0
                for half in (0, 1):
                    slot = hour * 2 + half
                    c = slots.get(str(slot)) or slots.get(slot)
                    if c and c["n"]:
                        vals.append(c["p_open"] * c["n"])
                        ns += c["n"]
                if ns:
                    p = sum(vals) / ns
                    cells.append(BLOCKS[min(8, round(p * 8))] + " ")
                else:
                    cells.append("  ")
            lines.append(f"{hour:02d}  " + "".join(cells))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
