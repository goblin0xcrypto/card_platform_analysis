# renaiss

**Renaiss — Collectible Finance Network**（PSA/BGS 評級卡 gacha + 市場）
官網 https://www.renaiss.xyz/ ｜ 鏈：BSC (BNB Smart Chain, chainId 56) ｜ 付款幣：USDT

本平台 6 個合約皆由同一 deployer（`0x12639a…E3A3`）部署。詳細合約清單見 `config.yaml`。

## 資料抓取（Ingestion）

**路徑：EVM unverified（proxy，拿不到事件 ABI）→ bronze 原始交易 → 推導 silver。**
全局架構見 [docs/data_pipeline.md](../../docs/data_pipeline.md)。

```bash
# 1. 抓 config 內所有地址的完整交易 → raw_transactions（bronze）
.venv/bin/python ingest_txs.py --platform renaiss
#    （可選）只抓某區塊後：--from-block 66766326

# 2. 由 raw_transactions 推導語意表（silver）
.venv/bin/python derive_from_raw.py --platform renaiss
#    → payments / pack_opens / mints / marketplace_trades / nft_transfers

# 3. 分析 → 報告
.venv/bin/python run_analysis.py --platform renaiss
```

- 資料源：Etherscan V2（`ETHERSCAN_API_KEY`），BSC chainId 56。
- 為何不用 `ingest.py`：合約是 ERC-1967 可升級 proxy、未 verified，拿不到事件 topic，故走「原始交易 + 推導」。
- 寫入表：`raw_transactions`（bronze）→ `payments` / `pack_opens` / `mints` / `marketplace_trades` / `nft_transfers`（silver）。

## 目錄結構

| 路徑 | 內容 |
|------|------|
| `config.yaml` | 平台設定：合約地址、deployer、官方錢包、分析閾值。 |
| `platform_profile.md` | 平台基本資訊與合約/錢包清單。 |
| `flow_overview.md` | Deployer 與官方錢包金流總覽。 |
| `red_flags.md` / `wash_trading.md` | 紅旗與自刷量彙整。 |
| `bot_clusters.csv` | 機器人/同 funder 群集。 |
| `reports/` | 客製分析報告 |
| `charts/` | 分析圖表 PNG |
| `data/` | 資料匯出 CSV / JSON |

### reports/
- `renaiss_bot_report.md` — OMEGA $48 卡機 BOT 嫌疑地址重點報告（21 個）。

### charts/
- `renaiss_revenue-mom.png` — 月營收（MoM）
- `renaiss_revenue-vs-users.png` — 營收 vs 用戶數
- `renaiss_users-vs-traders.png` — 用戶 vs 交易者
- `renaiss_rev-vs-market.png` — 營收 vs 市場成交
- `renaiss_market.png` — 市場成交
- `renaiss_margin.png` — 利潤
- `renaiss_pack-stack.png` — 各卡機開包堆疊

### data/
- `renaiss_omega_s_winners_june.csv` — 六月 OMEGA S 卡贏家
- `renaiss_omega_s_winners_2mo.csv` — 近兩月 OMEGA S 卡贏家
- `renaiss_omega_s_june_pnl.csv` — 六月 OMEGA S 卡損益
- `renaiss_kept_s_cards.json` — 抽到 S 卡留著不賣（收藏型）的玩家明細，bot 報告中引用。
