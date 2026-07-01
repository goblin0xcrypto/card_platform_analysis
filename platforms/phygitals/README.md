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

# 4. 分析 → 報告
.venv/bin/python run_analysis.py --platform phygitals
```

- 資料源：Solscan Pro v2（`SOLANA_API_KEY`，付費，付款金流）＋ Helius（`HELIUS_API_KEY`，NFT 指令/事件）。
- 注意：MPL Core 資產**不是 SPL token**，不會出現在 `ingest_solana.py` 的轉帳流，必須由 `ingest_solana_nft.py` 解 mpl_core 指令補抓；2025 期 cNFT 則由 `ingest_solana_cnft.py` 補抓。
- 無鏈上市場合約（後端 orderbook + MPL Core 轉移結算），故無 marketplace_trades。
- `--days N`（預設 7）抓近 N 天；`--days 0` 全量 genesis，靠 `ingest_cursors` 可中斷續跑。
- **補洞**：兩次 `--days 7` 間隔 > 7 天會在中間開天窗（floor 游標補不到）。用範圍模式精準重跑、冪等填洞：
  `ingest_solana_nft.py --platform phygitals --from 2026-06-19 --to 2026-06-21`（`ingest_solana_cnft.py` 同）。詳見 [docs/data_pipeline.md](../../docs/data_pipeline.md) §5。

---
> 額外圖表/資料/報告請依慣例放 `charts/`、`data/`、`reports/`（見 [上層 README](../README.md)）。
