# kaikatsu-darts-notify

快活CLUB のダーツ席の空き状況を GitHub Actions で10分ごとにチェックし、

1. 近所の店で **満席 → 空き** になったらスマホにプッシュ通知（[ntfy](https://ntfy.sh) 経由・無料）
2. 全監視店の空き状況を CSV に長期ログ収集 → 曜日×時間帯のヒートマップ／将来の予測に使う

を行う個人用の仕組み。**非公式 API を使うので個人利用・低頻度・自己責任**。

## 仕組み

| ファイル | 役割 |
|---|---|
| `poll.py` | 空席照会 API を各店1回ずつ叩く → `data/log/YYYY-MM.csv` 追記 → `data/state.json` 更新 → 空き遷移で ntfy 送信 |
| `config.json` | 監視店・`travel_min`（自宅からの所要分）・閾値 |
| `.github/workflows/poll.yml` | cron `*/10`。`poll.py` 実行後、`data/` の差分を commit |
| `build_heatmap.py` | `data/log/*.csv` を集計 → `heatmap/heatmap.json` と `heatmap.md` |
| `.github/workflows/heatmap.yml` | 毎日 JST 0:17 に `build_heatmap.py` 実行 |
| `tools/generate-darts-stores.ps1` | `shop.js` からダーツ設置全店リスト＋台数を生成（監視店選定の参照用） |
| `tools/darts_stores.json` | 上記の出力（248 店） |

空席照会 API（`common/js/shop_vacancy.js` から判明。応答に時刻は無く、更新は壁時計10分刻み・反映は境界の1〜2分後）:

```
GET https://jx5rl6ilkg.execute-api.ap-northeast-1.amazonaws.com/prd/empty_seat?store_cd=<code>
Header: x-api-key: <公式JSに平文で入っている値>
→ seat_type[] の category_id=="10" がダーツ枠。seat_status は「満席 / 残N席 / 残10席以上」。
```

## セットアップ

### 1. スマホ側（ntfy）
1. **ntfy** アプリを入れる（Google Play / App Store / F-Droid）。
2. 「＋ → トピックを購読」。トピック名は**長くランダム**に（例 `kaikatsu-darts-8f3k2p9q`）。サーバはデフォルト。
3. 動作テスト: `curl -d test https://ntfy.sh/kaikatsu-darts-8f3k2p9q` → 1秒でスマホに通知。
4. ntfy アプリを電池最適化から除外、通知を「ポップアップ＋音」に。

### 2. GitHub 側
1. この一式を **public** リポジトリとして push（public なら Actions 無料無制限。ログは非機微）。
2. Settings → Secrets and variables → Actions →
   - `NTFY_TOPIC` = 上で決めたトピック名（必須）
   - `NTFY_TOKEN` = ntfy アクセストークン（任意。トピック保護する場合）
   - Variables: `NTFY_SERVER` = 自前サーバを使う場合のみ（既定 `https://ntfy.sh`）
3. Actions タブで `poll` ワークフローを有効化。まず **Run workflow**（`workflow_dispatch`）で手動実行し、
   `data/log/` に4行の CSV が commit されることを確認。
4. 問題なければ cron（10分ごと）が自動で回る。

### 3. config.json の調整
- `stores[].travel_min` を**自宅からの実際の片道所要時間（分）**に直す（現在は仮値）。
- `notify: true` の店だけプッシュ対象（既定: 調布南口・登戸駅前・百合ヶ丘）。横浜北山田はログのみ。
- `poll.quiet_hours` で通知を止める時間帯（既定 01:00–09:00 JST）。
- `poll.cooldown_min` 同一店の連続通知の間隔（既定30分）。

## 通知の「見込み（高/中/低）」について

`見込み = 空き台数 × 6分 − (データ遅延 + 移動時間)` の符号でざっくり判定（`config.hindsight_margin_per_free_min` で調整）。
ダーツ席は**予約不可・先着**で、公式データは最大12分遅れる。**通知が来ても取れないことはある**。
分単位の正確な予測は原理的に不可能（10分粒度＋遅延＋待ち行列が見えない）。数週間ログを貯めた
ヒートマップと合わせて「狙い目の時間帯」を掴むのが現実的な使い方。

## ローカル実行（任意・Python 3 が要る）

```
python3 poll.py           # NTFY_TOPIC 未設定なら送信せずログのみ
python3 build_heatmap.py  # heatmap/ を再生成
```
