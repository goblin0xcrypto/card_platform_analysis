# Card Platform Analysis

跨卡牌/盲盒 NFT 平台的鏈上分析框架。從開包合約抓取、金流追蹤、機器人偵測到自刷量估算，所有平台跑同一套流程。

## 目錄結構

```
card_platform_analysis/
├── playkami_analysis_sop.md   # 主 SOP 模板，所有分析以此為依據
├── schema/
│   └── init.sql               # Postgres 通用建表語句
├── adapters/                  # 鏈別抓取適配器
│   ├── base_adapter.py        # PlatformAdapter Protocol
│   └── README.md              # 新增平台/鏈別說明
├── queries/                   # 參數化 SQL 分析模組
│   ├── platform_health.sql
│   ├── deployer_flow.sql
│   ├── bot_detection.sql
│   ├── wash_trading.sql
│   └── nft_endstate.sql
├── shared/
│   └── known_addresses.csv    # 跨平台共享地址字典 (CEX/Bridge/Mixer)
├── platforms/
│   ├── _template/             # 新平台複製這個資料夾
│   └── playkami/              # 範例平台
├── docs/
│   ├── data_pipeline.md       # ⭐ 各平台資料抓取→處理→寫庫流程 + 決策矩陣
│   ├── clustering.md          # 同 funder 分群演算法
│   └── report_template.md     # 統一報告模板
├── tools/
│   ├── bootstrap.py           # URL → 自動填 config.yaml + platform_profile.md
│   └── README.md
└── notebooks/
    └── report.ipynb           # 自動產生報告 (placeholder)
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
#    Solana：        python ingest_solana.py / ingest_solana_nft.py / ingest_solana_cnft.py --platform <new_platform>

# 4. 跑分析
python run_analysis.py --platform <new_platform>
```

詳細流程請見 [playkami_analysis_sop.md](./playkami_analysis_sop.md)；**資料抓取路徑**見 [docs/data_pipeline.md](./docs/data_pipeline.md)，bootstrap 細節見 [tools/README.md](./tools/README.md)。
