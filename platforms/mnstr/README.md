# mnstr

**MNSTR** — 卡包/市場平台
鏈：MegaETH L2（chainId 6342，explorer https://mega.etherscan.io） ｜ 付款幣：USDm

## 檔案

| 檔案 | 內容 |
|------|------|
| `config.yaml` | 平台設定：合約地址、deployer、官方錢包、分析閾值。 |
| `platform_profile.md` | 平台基本資訊與合約/錢包清單。 |
| `flow_overview.md` | Deployer 與官方錢包金流總覽。 |
| `results.json` | 分析流程產出的結果資料。 |
| `red_flags.md` / `wash_trading.md` | 紅旗與自刷量彙整。 |
| `bot_clusters.csv` | 機器人/同 funder 群集。 |

## 資料抓取（Ingestion）

**路徑：EVM verified（有事件 ABI）→ adapter 解事件 → silver。**
全局架構見 [docs/data_pipeline.md](../../docs/data_pipeline.md)。

```bash
# 1. 抓鏈上事件 → 語意 domain 表（用 adapters/mnstr_adapter.py）
.venv/bin/python ingest.py --platform mnstr        # 預設 --mode explorer

# 2. 分析 → 報告
.venv/bin/python run_analysis.py --platform mnstr
```

- 資料源：Etherscan V2（`ETHERSCAN_API_KEY`），MegaETH chainId 6342。
- 三個 OffchainGacha 合約皆 verified，由 `adapters/mnstr_adapter.py` 解事件。
- 寫入表：pack_opens / payments / mints / nft_transfers / marketplace_events（mnstr 為 string-key 市場）。

---
> 額外圖表/資料/報告請依慣例放 `charts/`、`data/`、`reports/`（見 [上層 README](../README.md)）。
