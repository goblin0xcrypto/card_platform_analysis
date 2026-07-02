# Card Platform Analysis

跨卡牌/盲盒 NFT 平台的鏈上分析框架。從開包合約抓取、金流追蹤、機器人偵測到自刷量估算，所有平台跑同一套流程。

## 目錄結構

```
card_platform_analysis/
├── ANALYSIS_SOP.md            # 主 SOP 模板，所有分析以此為依據（原 playkami_analysis_sop.md）
├── schema/init.sql            # Postgres 通用建表語句
│
│   # --- ingest 腳本（依鏈別/合約選用，見 docs/data_pipeline.md）---
├── ingest.py                  # EVM verified：adapter 解事件 → silver
├── ingest_txs.py              # EVM：原始交易 → bronze raw_transactions
├── derive_from_raw.py         # bronze → silver 推導（unverified 合約）
├── ingest_solana.py           # Solana 付款金流（Solscan Pro v2）→ payments
├── ingest_solana_nft.py       # Solana MPL Core NFT（Helius RPC）→ mints/nft_transfers
├── ingest_solana_cnft.py      # Solana cNFT/Bubblegum（Helius Enhanced）→ mints/nft_transfers
├── ingest_card_charts.py      # 卡片逐筆金額（平台公開價格頁）→ nft_transfers.amount_usd
├── run_analysis.py            # 讀 silver → 分析 + 報告
│
├── adapters/                  # EVM 鏈別抓取適配器（base_adapter.py = Protocol）
├── queries/                   # 參數化 SQL：platform_health / deployer_flow / bot_detection / wash_trading / nft_endstate
├── shared/known_addresses.csv # 跨平台共享地址字典 (CEX/Bridge/Mixer)
├── platforms/
│   ├── _template/             # 新平台複製這個資料夾
│   ├── playkami/ mnstr/ renaiss/   # EVM 範例平台
│   └── phygitals/             # Solana 範例平台（MPL Core + cNFT + 逐筆金額）
├── docs/
│   ├── data_pipeline.md       # ⭐ 各平台資料抓取→處理→寫庫流程 + 決策矩陣
│   ├── clustering.md          # 同 funder 分群演算法
│   └── report_template.md     # 統一報告模板
└── tools/
    ├── bootstrap.py           # URL → 自動填 config.yaml + platform_profile.md
    ├── explorer.py            # Etherscan V2 多鏈 client
    ├── solscan.py             # Solscan Pro v2 client
    └── README.md
```

## 新平台快速開始

```bash
# 1. 給網址，自動填 config.yaml + platform_profile.md
export ETHERSCAN_API_KEY="..."
python tools/bootstrap.py --url https://<platform-site> --name <new_platform>

# 2. 人工複核 ⚠️ 標記欄位
edit platforms/<new_platform>/platform_profile.md

# 3. 抓鏈上資料 —— 路徑依鏈別/合約而異，見決策矩陣 docs/data_pipeline.md
#    EVM verified：  python ingest.py --platform <new_platform>
#    EVM unverified：python ingest_txs.py --platform <new_platform> && python derive_from_raw.py --platform <new_platform>
#    Solana：        ingest_solana.py（金流）+ ingest_solana_nft.py / ingest_solana_cnft.py（各世代卡片）
#                    + ingest_card_charts.py（逐筆金額，金額不上鏈的平台）

# 4. 跑分析
python run_analysis.py --platform <new_platform>
```

詳細流程請見 [ANALYSIS_SOP.md](./ANALYSIS_SOP.md)；**資料抓取路徑**見 [docs/data_pipeline.md](./docs/data_pipeline.md)，bootstrap 細節見 [tools/README.md](./tools/README.md)。
