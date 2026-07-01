# playkami

**PlayKami** — 卡包平台（框架的範例平台）
鏈：Monad（EVM L1，由 verifier.playkami.io 確認） ｜ 付款幣：USDC

## 檔案

| 檔案 | 內容 |
|------|------|
| `config.yaml` | 平台設定：合約地址、deployer、官方錢包、分析閾值。 |
| `platform_profile.md` | 平台基本資訊與合約/錢包清單。 |
| `flow_overview.md` | Deployer 與官方錢包金流總覽。 |
| `red_flags.md` / `wash_trading.md` | 紅旗與自刷量彙整。 |
| `bot_clusters.csv` | 機器人/同 funder 群集。 |

## 資料抓取（Ingestion）

**路徑：EVM → adapter 解事件 → silver（用 `adapters/monad_adapter.py`）。**
全局架構見 [docs/data_pipeline.md](../../docs/data_pipeline.md)。

```bash
# 1. 抓鏈上事件 → 語意 domain 表
.venv/bin/python ingest.py --platform playkami

# 2. 分析 → 報告
.venv/bin/python run_analysis.py --platform playkami
```

- 資料源：Etherscan V2（`ETHERSCAN_API_KEY`），Monad（EVM L1）。
- 寫入表：pack_opens / payments / mints / nft_transfers。

---
> 額外圖表/資料/報告請依慣例放 `charts/`、`data/`、`reports/`（見 [上層 README](../README.md)）。
