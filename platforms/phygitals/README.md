# phygitals

**Phygitals** — Solana 上的「實體卡 RWA」卡包/市場（PSA/Fanatics/Alt 託管）
官網 https://www.phygitals.com/ ｜ 鏈：Solana 主網（地址為 base58，非 0x） ｜ 付款幣：USDC

## 檔案

| 檔案 | 內容 |
|------|------|
| `config.yaml` | 平台設定：合約/program、deployer、官方錢包、分析閾值。 |
| `platform_profile.md` | 平台基本資訊與合約/錢包清單。 |
| `flow_overview.md` | Deployer 與官方錢包金流總覽。 |
| `red_flags.md` / `wash_trading.md` | 紅旗與自刷量彙整。 |
| `bot_clusters.csv` | 機器人/同 funder 群集。 |

## 資料抓取（Ingestion）

**路徑：Solana，需多支併用** —— 付款金流一支、兩個世代的卡片各一支。
全局架構見 [docs/data_pipeline.md](../../docs/data_pipeline.md)。

```bash
# 1. 付款金流（USDC）→ payments
.venv/bin/python ingest_solana.py --platform phygitals          # Solscan Pro v2
#    （phygitals 卡片是 MPL Core / cNFT，非 SPL NFT；--spl-nft 對本平台無效，故只抓 payments）

# 2. 第二代卡片 MPL Core（2026-03 起，collection phygZ…）→ mints / nft_transfers
.venv/bin/python ingest_solana_nft.py --platform phygitals --days 0   # Helius RPC；全量可續

# 3. 第一代卡片 cNFT / Bubblegum（2025 期，collection BSG6Dy，已大量遷移/BURN）→ mints / nft_transfers
.venv/bin/python ingest_solana_cnft.py --platform phygitals --days 0  # Helius Enhanced API（以 tree 分頁）

# 4. 逐筆金額（卡片 ALT 估值）→ card_charts + 回填 nft_transfers.amount_usd
.venv/bin/python ingest_card_charts.py --platform phygitals           # 公開端點 single-nft/chart（免登入、免費）
#    交易金額不上鏈（內部餘額制）；用卡片頁的逐日 ALT 估值，依成交當天定價。→ 實質估值 GMV。

# 5. 開卡金額 + 卡包種類（名目 GMV）→ card_activity
.venv/bin/python ingest_card_activity.py --platform phygitals --from 2026-05-01 --workers 1 --rps 1
#    公開端點 single-nft/activity：每筆 CLAW=開卡(amount=卡包售價)、BUY=賣回；clawId→卡包(vm/available 對照)。
#    ⚠️ 此端點受 Cloudflare 限流極嚴(1015)：務必 --workers 1 --rps 1 涓流(近2月 34K 卡約跑整天)；
#       高並發會被封 IP。只掃近期活躍卡(--from)：端點僅回每卡近 ~5 筆,舊/休眠卡回空。→ 名目 GMV。

# 6. 修正開卡列金額：把開卡(from=62Q9→玩家)的 amount_usd 由 ALT 估值改為「卡包實際售價」
.venv/bin/python fix_open_amounts.py --platform phygitals
#    原本 amount_usd 是卡片 ALT 估值(開卡=賣回同值)；但開卡玩家實付卡包售價。
#    用 card_activity 的 CLAW 金額修正,並在 amount_src 記來源(見下)。需先跑步驟 5。

# 7. 分析 → 報告
.venv/bin/python run_analysis.py --platform phygitals
```

- 資料源：Solscan Pro v2（`SOLANA_API_KEY`，付費，付款金流）＋ Helius（`HELIUS_API_KEY`，NFT 指令/事件）＋ phygitals 公開 API（`single-nft/chart` 逐筆估值、`single-nft/activity` 開卡金額，皆免登入）。
- **三種 GMV 口徑**（皆因「金額不上鏈、走內部餘額制」而需另抓）：
  - **實質估值 GMV** → `nft_transfers.amount_usd`（`ingest_card_charts.py`，每卡逐日 ALT 估值，依 `block_time` 定價）。覆蓋 全期~61%/近一年~97%（價格歷史僅回最近一年、僅 ALT 有估值的卡）。全期約 $237M。
  - **名目 GMV** → `card_activity`（`ingest_card_activity.py`，開卡=卡包售價 mint_price）。近2月開卡 5.6 萬次、約 $8.9M；高價卡包($1000+)僅占開卡量 4% 卻撐 58% 名目額。
  - **鏈上實際 USDC** → `payments`（淨儲值/提領，遠小於上兩者，因交易走內部帳本）。
- ⚠️ `ingest_card_charts.py`（chart 端點）不限流，可高並發；`ingest_card_activity.py`（activity 端點）**受 Cloudflare 嚴格限流(1015)，只能 1 rps 涓流**——兩者別搞混。`--backfill-only` 只重算金額；兩者 `--from/--to` 皆可補指定範圍（冪等）。
- **`nft_transfers.amount_usd` 是混合口徑**，由 `amount_src` 欄標示每列金額來源（`fix_open_amounts.py` 產生）：
  | `amount_src` | 意義 | 查詢用途 |
  |---|---|---|
  | `open_unique` / `open_exact` / `open_mode` | **開卡列=卡包實際售價**（來自 card_activity CLAW；唯一價直接對應／多價精確時間配對／多價眾數近似） | 名目 GMV：`WHERE amount_src LIKE 'open_%'` |
  | `alt` | 卡片 ALT 逐日估值（賣回/二級/未修正開卡列） | 實質估值：`WHERE amount_src='alt'` |
  | `none` | 無任何金額來源（早期、兩資料源皆未覆蓋，`amount_usd` 為 NULL） | 排除 |
  - `fix_open_amounts.py` 只改開卡列(`from=62Q9`→非平台)，不動賣回/二級。**要還原開卡列 ALT 值**：`ingest_card_charts.py --backfill-only`（由 card_charts 重算全部 amount_usd），再視需要重跑 `fix_open_amounts.py`。
  - 覆蓋：近2月開卡已修正 ~40 萬筆(卡包售價口徑 ~$54M)；更早開卡多為 `none`（card_activity 僅回近2月）。
- 注意：MPL Core 資產**不是 SPL token**，不會出現在 `ingest_solana.py` 的轉帳流，必須由 `ingest_solana_nft.py` 解 mpl_core 指令補抓；2025 期 cNFT 則由 `ingest_solana_cnft.py` 補抓。
- 無鏈上市場合約（後端 orderbook + MPL Core 轉移結算），故無 marketplace_trades。
- `--days N`（預設 7）抓近 N 天；`--days 0` 全量 genesis，靠 `ingest_cursors` 可中斷續跑。
- **補洞**：兩次 `--days 7` 間隔 > 7 天會在中間開天窗（floor 游標補不到）。用範圍模式精準重跑、冪等填洞：
  `ingest_solana_nft.py --platform phygitals --from 2026-06-19 --to 2026-06-21`（`ingest_solana_cnft.py` 同）。詳見 [docs/data_pipeline.md](../../docs/data_pipeline.md) §5。

---
> 額外圖表/資料/報告請依慣例放 `charts/`、`data/`、`reports/`（見 [上層 README](../README.md)）。
