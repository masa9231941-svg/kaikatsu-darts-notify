# kaikatsu-darts-notify

快活CLUB のダーツ席の空き状況を GitHub Actions で10分ごとにチェックし、

1. 近所の店で **満席 → 空き** になったらスマホにプッシュ通知（[ntfy](https://ntfy.sh) 経由・無料）
   — ただし **通知は既定 OFF**。遊びに行く前に手動で ON にした時だけ届く（3時間で自動 OFF）
2. 全監視店の空き状況を CSV に長期ログ収集 → 曜日×時間帯のヒートマップ／将来の予測に使う

を行う個人用の仕組み。**非公式 API を使うので個人利用・低頻度・自己責任**。

## 仕組み

| ファイル | 役割 |
|---|---|
| `poll.py` | 空席照会 API を各店1回ずつ叩く → `data/log/YYYY-MM.csv` 追記 → `data/state.json` 更新 → 通知ON中なら空き遷移で ntfy 送信 |
| `config.json` | 監視店・`travel_min`（自宅からの所要分）・閾値・`default_notify` |
| `set_override.py` | `data/override.json` を書いて通知を ON(3時間)/OFF する。確認プッシュも送る |
| `data/override.json` | 通知トグルの状態（`{}`＝OFF、`{"notify_until": …}`＝その時刻まで ON） |
| `.github/workflows/poll.yml` | cron `*/10`。`poll.py` 実行後、`data/` の差分を commit |
| `.github/workflows/override.yml` | 手動 `workflow_dispatch`。`set_override.py` で ON/OFF、ON時はその場で `poll.py` も実行 |
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
- `poll.default_notify` 既定 `false`（通知は常に OFF、手動 ON した時だけ届く）。常時 ON にしたいなら `true`。
- `poll.override_hours` 手動 ON の持続時間（既定 3）。
- `poll.cooldown_min` 同一店の連続通知の間隔（既定30分）。

## 通知を ON/OFF する（既定 OFF）

ログ収集は常時。**プッシュ通知だけが既定 OFF**で、`override` ワークフローで切り替える。
ON にすると `data/override.json` に期限が入り、その時刻まで（既定3時間）だけ通知。寝る前などは手動で OFF。

### A. GitHub モバイルアプリ（設定ほぼ不要・数タップ）
1. 「GitHub」アプリを入れてログイン（Google Play）。
2. 遊びに行く前: アプリ → このリポジトリ → **Actions** → `override` → **Run workflow** →
   `action` を `on`（`hours` は空欄で既定3）→ Run。
3. 寝る時など早めに止める: 同じ手順で `action` を `off`。
- PAT 不要。ブラウザで github.com の Actions 画面からでも同じ操作ができる。

### B. ホーム画面からワンタップ（HTTP Shortcuts ＋ PAT）
1. Google Play で無料の **HTTP Shortcuts**（作者 Waboodoo）を入れる。
2. GitHub → Settings → Developer settings → **Fine-grained personal access tokens** → Generate。
   - Repository access: **Only select repositories** → `kaikatsu-darts-notify` のみ
   - Permissions: **Actions → Read and write** だけ
   - 期限は任意（切れたら作り直す）
3. HTTP Shortcuts で2つ作成し、ホーム画面に配置:
   - **ダーツ通知ON**
     - Method: `POST`
     - URL: `https://api.github.com/repos/masa9231941-svg/kaikatsu-darts-notify/actions/workflows/override.yml/dispatches`
     - Headers: `Authorization: Bearer <PAT>` / `Accept: application/vnd.github+json`
     - Body (JSON): `{"ref":"main","inputs":{"action":"on","hours":"3"}}`
   - **ダーツ通知OFF**: 上と同じで Body の `"action"` を `"off"` に
4. 成功すると GitHub が HTTP 204 を返し、数秒後に「通知を ON にしました…」の確認プッシュが届く。
- PAT は端末内（アプリ内）に保存される。単一リポジトリ＋Actions のみのトークンなので影響範囲は限定。
- Tasker / MacroDroid / Automate でも同じ POST を組めば可。

## 通知の「見込み（高/中/低）」について

`見込み = 空き台数 × 6分 − (データ遅延 + 移動時間)` の符号でざっくり判定（`config.hindsight_margin_per_free_min` で調整）。
ダーツ席は**予約不可・先着**で、公式データは最大12分遅れる。**通知が来ても取れないことはある**。
分単位の正確な予測は原理的に不可能（10分粒度＋遅延＋待ち行列が見えない）。数週間ログを貯めた
ヒートマップと合わせて「狙い目の時間帯」を掴むのが現実的な使い方。

## ローカル実行（任意・Python 3 が要る）

```
python3 poll.py                        # NTFY_TOPIC 未設定なら送信せずログのみ
python3 build_heatmap.py               # heatmap/ を再生成
INPUT_ACTION=on  python3 set_override.py   # 通知を3時間ONにする（data/override.json を書く）
INPUT_ACTION=off python3 set_override.py   # 通知をOFFに戻す
```
