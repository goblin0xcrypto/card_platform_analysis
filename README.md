# Card Platform Analysis

跨卡牌/盲盒 NFT 平台的鏈上分析框架。從開包合約抓取、金流追蹤、機器人偵測到自刷量估算，所有平台跑同一套流程。

> 本 README 已統整 `ANALYSIS_SOP.md`、`docs/data_pipeline.md`、`tools/README.md` 三份文件的核心流程。原始文件仍保留作為深入參考。

---

## 目錄

1. [目錄結構](#目錄結構)
2. [快速開始](#快速開始)
3. [完整分析流程（階段 0–4）](#完整分析流程階段-04)
4. [資料 Pipeline 架構](#資料-pipeline-架構)
5. [Bootstrap 工具細節](#bootstrap-工具細節)
6. [跨平台復用設計](#跨平台復用設計)
7. [紅旗速查表](#紅旗速查表)

---

## 目錄結構

```
card_platform_analysis/
├── ANALYSIS_SOP.md            # 主 SOP 模板，所有分析以此為依據（原 playkami_analysis_sop.md）
├── schema/init.sql            # Postgres 通用建表語句
│
│   # --- ingest 腳本（依鏈別/合約選用，見下方決策矩陣）---
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
│   ├── data_pipeline.md       # 各平台資料抓取→處理→寫庫流程 + 決策矩陣
│   ├── clustering.md          # 同 funder 分群演算法
│   └── report_template.md     # 統一報告模板
└── tools/
    ├── bootstrap.py           # URL → 自動填 config.yaml + platform_profile.md
    ├── explorer.py            # Etherscan V2 多鏈 client
    ├── solscan.py             # Solscan Pro v2 client
    └── README.md
```

---

## 快速開始

```bash
# 0. 一次性：建表
psql "$DATABASE_URL" -f schema/init.sql

# 1. 給網址，自動填 config.yaml + platform_profile.md
export ETHERSCAN_API_KEY="..."
python tools/bootstrap.py --url https://<platform-site> --name <new_platform>

# 2. 人工複核 ⚠️ 標記欄位（不可省略）
edit platforms/<new_platform>/platform_profile.md

# 3. 抓鏈上資料 —— 路徑依鏈別/合約而異，見下方決策矩陣
#    EVM verified：  python ingest.py --platform <new_platform>
#    EVM unverified：python ingest_txs.py --platform <new_platform> && python derive_from_raw.py --platform <new_platform>
#    Solana：        ingest_solana.py（金流）+ ingest_solana_nft.py / ingest_solana_cnft.py（各世代卡片）
#                    + ingest_card_charts.py（逐筆金額，金額不上鏈的平台）

# 4. 跑分析
python run_analysis.py --platform <new_platform>

# 5. 產報告
jupyter nbconvert --execute notebooks/report.ipynb --output platforms/<new_platform>/red_flags.md
```

---

## 完整分析流程（階段 0–4）

### 階段 0：URL → 自動 Bootstrap

只給平台網址，自動產出 `config.yaml` 與 `platform_profile.md` 草稿：

```bash
export ETHERSCAN_API_KEY="..."
python tools/bootstrap.py --url https://<platform-site> --name <platform-name>
```

bootstrap 自動完成 6 步：抓頁面（首頁 + `/docs` `/whitepaper` `/contracts` `/about` `/faq`）→ 偵測鏈別 → regex 抽合約地址候選 → 打 explorer enrich（合約名、deployer、是否 verified/proxy）→ 推合約類型與付款幣種 → 寫檔並標 ⚠️。

**人工複核項目（不可省）** — bootstrap 只能自動化「公開可查的部分」：

| 項目 | 為什麼自動不了 |
|------|----------------|
| 合約類型分類 | 合約名可能誤導（marketplace 命名為 `MintHub`） |
| 官方錢包 / treasury | 通常不在公開頁面 |
| Deployer 資金來源 trace | 需要往上 BFS 1-3 跳 |
| Deployer 過往項目 | 比對歷史 + 已知 rug 字典 |
| 多簽簽署人活動 | 需 Safe / Squads 介面查 |

複核 checklist：
- [ ] 全部 ⚠️ 欄位處理完
- [ ] 至少跑過一次 deployer 資金來源 trace
- [ ] 比對 `shared/known_addresses.csv` 命中清單

### 階段 1：資料庫骨架（一次定義、跨平台復用）

Postgres（搭配 TimescaleDB 更佳），所有表加 `platform_id` 維度欄位。完整建表語句見 `schema/init.sql`。核心表：

```
platforms / contracts / addresses
pack_opens / mints / payments / nft_transfers / marketplace_trades / address_flows
```

關鍵設計原則：
- `pack_open_tx` 在 `mints` 與 `pack_opens` 之間建立外鍵 → 開包/Mint/付款三件事用同一 tx 串起
- 所有金額同時保存 `raw`（uint256 字串）與 `usd`（numeric），寫入時 freeze 當下匯率
- `addresses.cluster_id` 預留給後續同 funder 分群結果

### 階段 2：資料抓取（順序有依賴，不可亂跳）

1. **定位 Pack Opening 合約** — 從 deployer 的 tx 找高頻互動合約，或從 NFT 合約反查 `MINTER_ROLE`
2. **抓開包事件** — 解 `PackOpened` / `Mint` / `Transfer(from=0x0)` event
3. **抓對應金流** — 同 tx 內的 ERC-20 Transfer **以及 internal tx**（必須用 trace API，光看 Transfer event 會漏 ETH/原生幣支付）
4. **抓 NFT 後續流向** — 以 token_id 為單位，沿 transfer 鏈追到「最終持有者 / 銷毀 / 質押」
5. **抓 deployer / 官方錢包金流** — 以 deployer 為起點 BFS 1～2 層，找資金集散地與 CEX 充值點

各鏈工具層：

| 鏈 | RPC | Trace | Backfill |
|----|-----|-------|----------|
| EVM | Alchemy / QuickNode | `debug_traceTransaction` / Reth trace | Dune / Flipside |
| Solana | Helius | Helius Enhanced TX | Flipside |
| TON | tonapi.io | tonapi traces | DTon |
| Sui | Sui RPC | Sui transaction blocks | Flipside |

### 階段 3：分析模組（每個平台跑同一套）

每個模組是一個參數化 SQL（全部以 `platform_id` 為參數）：

| 模組 | 檔案 | 重點 |
|------|------|------|
| A. 平台健康度 | `queries/platform_health.sql` | 每日開包數/金額/獨立地址、D1/D7/D30 留存、NFT 沉澱率、開包價 vs 二級底價 |
| B. Deployer 金流 | `queries/deployer_flow.sql` | 分潤路徑多跳追到 CEX、資金是否回流疑似機器人地址（直接自刷信號）、多簽簽署人歷史 |
| C. 機器人偵測 | `queries/bot_detection.sql` | 規則層打標籤（開包間隔 σ/μ < 0.1、gas/nonce 特徵、同 funder、NFT 秒轉 sink）→ 分群層跑同 funder 圖（見 `docs/clustering.md`） |
| D. 自刷量偵測 | `queries/wash_trading.sql` | 同 cluster 買賣、A→B→A 環狀轉移（跳數 ≤ 5）、成交價偏離中位、手續費回流比例 |
| E. NFT 最終流向 | `queries/nft_endstate.sql` | 分類為 burn / staked / locked / 散戶長持 / cluster 集中 / 掛單，畫 Sankey 圖 |

### 階段 4：產出物（每個平台一份，檔名固定）

| 檔案 | 內容 |
|------|------|
| `platform_profile.md` | 合約、地址、代幣（階段 0 產出） |
| `flow_overview.md` | 總開包額、總分潤、deployer 提現路徑 |
| `bot_clusters.csv` | 機器人實體清單 + 證據 |
| `wash_trading.md` | 自刷量估算（金額、佔比、手法分類） |
| `nft_endstate.png` | NFT 流向 Sankey 圖 |
| `red_flags.md` | 結論：平台健康度、主要風險點 |

---

## 資料 Pipeline 架構

### 分層架構（Medallion）

```
鏈上 ──► [ingest 腳本] ──► Postgres ──► [run_analysis.py] ──► platforms/<name>/ 報告
          bronze / silver         (DATABASE_URL)        分析模組        charts/ data/ reports/
```

| 層 | 表 | 產生者 | 說明 |
|----|----|--------|------|
| **bronze（原始）** | `raw_transactions` | `ingest_txs.py` | 不解析 ABI，原樣存交易。給 unverified 合約用。 |
| **silver（語意 domain）** | `pack_opens` `payments` `mints` `nft_transfers` `marketplace_trades` `marketplace_events` | `ingest.py` / `ingest_solana*.py` / `derive_from_raw.py` | 語意化事實，分析直接查這層。 |
| **金額輔助** | `card_charts` `ingest_cursors` | `ingest_card_charts.py` / 各 Solana ingest | 卡片逐日估值（回填 `nft_transfers.amount_usd`）；續跑游標。 |
| **分析 / gold** | `bot_flags` `address_clusters` + 報告檔 | `run_analysis.py` | 讀 silver，輸出到 `platforms/<name>/`。 |
| **參考資料** | `platforms` `contracts` `addresses` `known_addresses` | config 載入 / 共享字典 | 平台、合約、官方錢包、CEX/Bridge 字典。 |

### 決策矩陣 — 該平台跑哪條路徑？

判斷依據是 `platforms/<name>/config.yaml` 的 `chain:` 與合約類型。Solana 平台常需多支併用。

| 條件 | 路徑 | 腳本 |
|------|------|------|
| EVM + 合約 **verified**（有事件 ABI） | adapter 解事件 → silver | `ingest.py` |
| EVM + 合約 **unverified**（proxy，拿不到 ABI） | 原始交易 → bronze → 推導 silver | `ingest_txs.py` → `derive_from_raw.py` |
| Solana + 付款幣（USDC）金流 | Solscan 解析轉帳 → `payments` | `ingest_solana.py` |
| Solana + **MPL Core** NFT（新世代卡） | Helius RPC 解 mpl_core 指令 | `ingest_solana_nft.py` |
| Solana + **cNFT**（舊世代卡，Bubblegum） | Helius Enhanced API 以 tree 分頁 | `ingest_solana_cnft.py` |
| Solana + **卡片逐筆金額**（金額不上鏈） | 平台公開價格歷史 → `card_charts` + 回填 | `ingest_card_charts.py` |

### ingest 腳本一覽

| 腳本 | 鏈 | 資料源（env key） | 寫入表 | 重點 |
|------|----|-------------------|--------|------|
| `ingest.py` | EVM verified | Etherscan V2（`ETHERSCAN_API_KEY`） | pack_opens / payments / mints / nft_transfers / marketplace_events | `--mode explorer`（預設，目前唯一實作）；靠 `adapters/<chain>_adapter.py` 解事件 |
| `ingest_txs.py` | EVM（尤其 unverified） | Etherscan V2 | `raw_transactions` | 不需 ABI，抓 config 內所有地址的完整交易足跡 |
| `derive_from_raw.py` | （後處理） | 讀 DB `raw_transactions` | payments / pack_opens / mints / marketplace_trades / nft_transfers | bronze → silver；轉帳類事實不需 ABI 即可重建 |
| `ingest_solana.py` | Solana | Solscan Pro v2（`SOLANA_API_KEY`，付費） | payments（SPL NFT 需 `--spl-nft`） | **抓不到 MPL Core**，需搭配 `ingest_solana_nft.py` |
| `ingest_solana_nft.py` | Solana | Helius RPC（`HELIUS_API_KEY`） | mints / nft_transfers | 解 MPL Core `createV2`/`transferV1`；`from` 由 SQL LAG 補 |
| `ingest_solana_cnft.py` | Solana | Helius Enhanced API（`HELIUS_API_KEY`） | mints / nft_transfers | 解 Bubblegum cNFT，以各 merkle tree 分頁 |
| `ingest_card_charts.py` | Solana（金額層） | 平台公開 API（免登入） | `card_charts` + 回填 `nft_transfers.amount_usd` | 金額不上鏈的平台用；覆蓋率 ~61%（僅近一年 + 僅 ALT 估值卡）。**實質估值 GMV**，與機構名目 GMV 約差 2 倍 |

### 環境變數（`.env`，由 `.env.example` 複製）

| 變數 | 用途 | 誰用 |
|------|------|------|
| `ETHERSCAN_API_KEY` | Etherscan V2 多鏈（一把 key 走 chainId 切鏈） | `ingest.py` / `ingest_txs.py` |
| `HELIUS_API_KEY` | Solana getTransaction / mpl_core / cNFT | `ingest_solana_nft.py` / `ingest_solana_cnft.py` |
| `SOLANA_API_KEY` | Solscan Pro v2（付費） | `ingest_solana.py` |
| `DATABASE_URL` | Postgres 連線 | 全部 ingest + `run_analysis.py` |
| `ANTHROPIC_API_KEY` | （可選）用 Claude 解 docs HTML 找合約 | `tools/bootstrap.py` |

### 冪等與續跑

所有 ingest **冪等**，重跑會自動續抓：

- **EVM**：從各表 `max(block_number)` 續抓；`ingest_txs.py` 用 PK ON CONFLICT DO NOTHING。
- **Solana**：`ingest_cursors` 記 floor（最舊已處理 blockTime / per-tree）。`--days N`（預設 7）抓近 N 天，`--days 0` 全量可中斷續跑，`--reset` 清 cursor 重抓。
- **補洞用範圍模式 `--from/--to`**：明確指定窗口重跑（`YYYY-MM-DD` 或 unix 秒），不讀不寫 floor 游標。
  ⚠️ **不能用 `--days 0` 補洞**：floor 是單一水位線，`--days 0` 會把比 floor 新的交易當已完成而跳過。若兩次 `--days 7` 間隔超過 7 天造成中間缺漏，必須用範圍模式，例如：
  ```bash
  python ingest_solana_nft.py --platform phygitals --from 2026-06-19 --to 2026-06-21
  ```
  偵測缺漏方法：對高量平台看「每日筆數」，找突然掉到 0 或異常低的日子。

---

## Bootstrap 工具細節

### 用法

```bash
pip install requests pyyaml

# 推薦用 .env，免每次 export
cp .env.example .env
edit .env                          # 填入 ETHERSCAN_API_KEY 等

python tools/bootstrap.py --url https://<platform-site> --name <name>
python tools/bootstrap.py --url ... --name ... --env-file .env.local   # 指定別的 env 檔
```

### API Key 載入優先順序

1. **shell 環境變數** — 最高優先，會覆寫 .env
2. **`--env-file` 指定的檔案**
3. **專案根目錄的 `.env`**（預設；已加入 `.gitignore`）

### 限制與 fallback

| 限制 | 對策 |
|------|------|
| SPA / JS-render 頁面抓不到內容 | 手動貼 HTML 給 `--html-file` 參數（TODO） |
| 合約類型誤判 | enrich 只看合約名稱，必要時人工改 `config.yaml` |
| 合約沒 verify | deployer 仍可取，類型留 `unknown`，人工補 |
| 非 EVM 鏈未支援 enrich | Solana / TON / Sui 會跳過 enrich 步驟，需人工補 |
| Multi-chain 平台 | 多次跑 bootstrap，每條鏈一個資料夾 |

> bootstrap 的目的是把繁瑣的「找地址 + 查 explorer」自動化，**不取代調查**。輸出的 `platform_profile.md` 所有 ⚠️ 欄位必須人工確認後才能進入後續分析。

---

## 跨平台復用設計

1. **Adapter Pattern（資料抓取層）** — 所有抓取邏輯實作同一個 `PlatformAdapter` Protocol（見 `adapters/base_adapter.py`）。新增平台只寫 adapter，下游消費 `PackOpen` / `Payment` / `Transfer` domain 物件，與鏈無關。
2. **參數化 SQL** — `queries/` 底下所有 `.sql` 第一行標 `-- params: :platform_id, :start_date, :end_date`。禁止 hardcode 地址，一律 JOIN `addresses` 表。
3. **共享地址字典**（`shared/known_addresses.csv`）— 跨平台共用的 CEX / Bridge / Mixer / MEV 地址，新平台分析時自動 import。
4. **設定檔驅動**（`platforms/<name>/config.yaml`）— 抓取參數與分析閾值（`bot_interval_cv` / `wash_price_deviation` / `cluster_min_size`）都在 yaml，分析者不改 code。
5. **統一報告產生器** — `notebooks/report.ipynb` 依 `docs/report_template.md` 自動產生報告，跨平台格式一致。
6. **版本控制與審計** — 抓取結果帶 `ingested_at`；規則命中存 `bot_flags(..., evidence_json)` 可回溯；規則放 `queries/rules/` 版本化，調閾值留 commit message。

---

## 紅旗速查表

| 紅旗 | 判定 | 嚴重度 |
|------|------|--------|
| Deployer 資金來自 Tornado / 混淆器 | trace 上游 ≤ 3 跳 | 高 |
| 開包高峰時段 > 60% 流量來自單一 funder cluster | bot_detection rule 命中 | 高 |
| Marketplace 50% 以上成交為同 cluster 內 | wash_trading 命中 | 高 |
| 部署者過去部署過 rug 項目 | 共享地址字典比對 | 高 |
| NFT > 80% 集中在 < 50 個地址 | nft_endstate 統計 | 中 |
| 官方錢包對特定地址有非公開空投 | payments 直接 outflow 比對 | 中 |

---

## 延伸閱讀

- [ANALYSIS_SOP.md](./ANALYSIS_SOP.md) — 完整 SOP 原文（含資料庫核心表欄位定義、adapter interface 程式碼）
- [docs/data_pipeline.md](./docs/data_pipeline.md) — Pipeline 原文（各平台確切指令見各自 `platforms/<name>/README.md`）
- [docs/clustering.md](./docs/clustering.md) — 同 funder 分群演算法
- [tools/README.md](./tools/README.md) — bootstrap 工具原文
