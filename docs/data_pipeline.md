# 資料抓取與處理流程（Data Pipeline）

> 給「下一次用 AI 工具接手」的參考依據。本檔說明**每個平台的鏈上資料是怎麼抓、怎麼處理、寫進哪張表**。
> 不同平台的抓取路徑不一樣（取決於鏈別與合約是否 verified），下面的決策矩陣決定該跑哪支腳本。
>
> 單一平台的**確切指令**寫在各自的 `platforms/<name>/README.md`「資料抓取」段；本檔是跨平台的全局架構。

## 1. 分層架構（Medallion）

```
鏈上 ──► [ingest 腳本] ──► Postgres ──► [run_analysis.py] ──► platforms/<name>/ 報告
          bronze / silver         (DATABASE_URL)        分析模組        charts/ data/ reports/
```

| 層 | 表 | 產生者 | 說明 |
|----|----|--------|------|
| **bronze（原始）** | `raw_transactions` | `ingest_txs.py` | 不解析 ABI，原樣存每個地址的 native/internal/ERC-20/721/1155 交易。給 unverified 合約用。 |
| **silver（語意 domain）** | `pack_opens` `payments` `mints` `nft_transfers`（含 `amount_usd`）`marketplace_trades` `marketplace_events` | `ingest.py`（EVM verified）/ `ingest_solana*.py`（Solana）/ `derive_from_raw.py`（由 bronze 推導） | 依行為分流的語意化事實，分析直接查這層。 |
| **金額輔助** | `card_charts`（token_id→逐日估值）`ingest_cursors`（抓取游標） | `ingest_card_charts.py` / 各 Solana ingest | 卡片逐日 ALT 估值（回填 `nft_transfers.amount_usd`）；ingest 續跑游標。 |
| **分析 / gold** | `bot_flags` `address_clusters` + 報告檔 | `run_analysis.py` | 讀 silver，跑分析模組，輸出到 `platforms/<name>/`。 |
| **參考資料** | `platforms` `contracts` `addresses` `known_addresses` | 由 config 載入 / 共享字典 | 平台、合約、官方錢包、CEX/Bridge 字典。 |

所有 ingest **冪等**：重跑會從各表 `max(block_number)` 或 cursor 續抓（見 §5）。

## 2. 決策矩陣 — 該平台跑哪條路徑？

| 條件 | 路徑 | 腳本 |
|------|------|------|
| EVM + 合約 **verified**（有事件 ABI） | adapter 解事件 → silver | `ingest.py`（依 `config.chain` 自動選 adapter） |
| EVM + 合約 **unverified**（proxy，拿不到 ABI） | 原始交易 → bronze → 推導 silver | `ingest_txs.py` → `derive_from_raw.py` |
| Solana + 付款幣（USDC）金流 | Solscan 解析轉帳 → `payments` | `ingest_solana.py` |
| Solana + **MPL Core** NFT（新世代卡） | Helius RPC 解 mpl_core 指令 → `mints`/`nft_transfers` | `ingest_solana_nft.py` |
| Solana + **compressed NFT / cNFT**（舊世代卡，Bubblegum） | Helius Enhanced API 以 tree 分頁 → `mints`/`nft_transfers` | `ingest_solana_cnft.py` |
| Solana + **卡片逐筆金額**（金額不上鏈，內部餘額制） | 平台公開卡片價格歷史 → `card_charts` + 回填 `nft_transfers.amount_usd` | `ingest_card_charts.py` |

> 判斷依據是 `platforms/<name>/config.yaml` 的 `chain:` 與合約類型。Solana 平台常需多支併用（payments 一支、各世代 NFT 各一支、逐筆金額一支）。

## 3. ingest 腳本一覽

| 腳本 | 鏈 | 資料源（env key） | 寫入表 | 重點 |
|------|----|-------------------|--------|------|
| `ingest.py` | EVM verified | Etherscan V2（`ETHERSCAN_API_KEY`） | pack_opens / payments / mints / nft_transfers / marketplace_events | `--mode explorer`（預設，列舉 tx；目前唯一實作）。`--mode getlogs` 尚未實作，會提示改用 explorer。靠 `adapters/<chain>_adapter.py` 解事件。 |
| `ingest_txs.py` | EVM（任意，尤其 unverified） | Etherscan V2（`ETHERSCAN_API_KEY`） | `raw_transactions` | 不需 ABI，抓 config 內所有地址的完整交易足跡。 |
| `derive_from_raw.py` | （後處理） | 讀 DB `raw_transactions` | `payments` / `pack_opens` / `mints` / `marketplace_trades` / `nft_transfers` | 把 bronze 推導成 silver。轉帳類事實不需 ABI 即可重建（USDT 付款依方向分 pack_pay/payout/fee… 等）。 |
| `ingest_solana.py` | Solana | Solscan Pro v2（`SOLANA_API_KEY`，付費） | payments（+ SPL NFT，需 `--spl-nft`） | 解 `/account/transfer`：付款幣 mint→payments。SPL NFT 需加 `--spl-nft`（預設關）。**抓不到 MPL Core**（非 SPL token，需 `ingest_solana_nft.py`）。 |
| `ingest_solana_nft.py` | Solana | Helius RPC（`HELIUS_API_KEY`，免費並發單筆） | mints / nft_transfers | 解 MPL Core `createV2`/`transferV1` 指令。`from` 由 SQL LAG 補。 |
| `ingest_solana_cnft.py` | Solana | Helius Enhanced API（`HELIUS_API_KEY`） | mints / nft_transfers | 解 Bubblegum cNFT，**以各 merkle tree 位址分頁**；from/to 直接有。 |
| `ingest_card_charts.py` | Solana（金額層） | 平台公開 API `marketplace/single-nft/chart`（免登入、免費） | `card_charts` + 回填 `nft_transfers.amount_usd` | 金額不上鏈；抓每張卡逐日 ALT 估值，依成交當天定價。`--from/--to` 限範圍、`--backfill-only` 只重算。覆蓋率受限「價格歷史僅近一年」＋「僅 ALT 有估值的卡」（全期 ~61%）。**實質估值 GMV**，與機構名目 GMV（卡包售價）約差 2 倍。 |

## 4. 環境變數（`.env`，由 `.env.example` 複製）

| 變數 | 用途 | 誰用 |
|------|------|------|
| `ETHERSCAN_API_KEY` | Etherscan V2 多鏈（一把 key 走 chainId 切鏈） | `ingest.py` / `ingest_txs.py` |
| `HELIUS_API_KEY` | Solana getTransaction / mpl_core 指令 / cNFT 事件 | `ingest_solana_nft.py` / `ingest_solana_cnft.py` |
| `SOLANA_API_KEY` | Solscan Pro v2 帳戶級轉帳查詢（付費） | `ingest_solana.py` |
| `DATABASE_URL` | Postgres 連線 | 全部 ingest + `run_analysis.py` |
| `ANTHROPIC_API_KEY` | （可選）用 Claude 解 docs HTML 找合約 | `tools/bootstrap.py` |

## 5. 冪等與續跑

- **EVM**：重跑從各表 `max(block_number)` 續抓（getlogs），或 pack_opens/payments 的最大區塊（explorer）。`ingest_txs.py` 用 PK `(platform_id, address, kind, tx_hash, seq)` ON CONFLICT DO NOTHING。
- **Solana NFT / cNFT**：`ingest_cursors` 記 floor（最舊已處理 blockTime / per-tree），`--days N`（預設 7）抓近 N 天，`--days 0` 全量 genesis 可中斷續跑。`--reset` 清 cursor 重抓。
- **範圍模式 `--from/--to`（補洞）**：`ingest_solana_nft.py` / `ingest_solana_cnft.py` 支援明確指定窗口（`YYYY-MM-DD` 或 unix 秒）重跑，`ON CONFLICT` 冪等，**不讀也不寫 floor 游標**。
  ⚠️ **為何不能用 `--days 0` 補洞**：floor 是單一水位線（只記「最舊已處理」），`--days 0` 會把所有「比 floor 新」的交易當已完成而**跳過**。若 floor 之上出現缺漏（典型成因：兩次 `--days 7` 間隔 > 7 天，中間某天兩邊都沒掃到），`--days 0` 補不到——必須用範圍模式。
  例：`ingest_solana_nft.py --platform phygitals --from 2026-06-19 --to 2026-06-21`。
  偵測缺漏：對高量平台用「每日筆數」找突然掉到 0／異常低的日子（見本次 phygitals 06-20 案例）。

## 6. 標準操作順序

```bash
# 0. 一次性：建表
psql "$DATABASE_URL" -f schema/init.sql

# 1. 抓資料（依平台選路徑，見 §2 與各平台 README）
#    EVM verified：   .venv/bin/python ingest.py --platform <name>
#    EVM unverified： .venv/bin/python ingest_txs.py --platform <name>
#                     .venv/bin/python derive_from_raw.py --platform <name>
#    Solana：         .venv/bin/python ingest_solana.py --platform <name>
#                     .venv/bin/python ingest_solana_nft.py  --platform <name> --days 0
#                     .venv/bin/python ingest_solana_cnft.py --platform <name> --days 0

# 2. 跑分析 → 產報告
.venv/bin/python run_analysis.py --platform <name>
```

## 7. 新增平台 / 新增鏈別

- **新平台**：複製 `platforms/_template/`，填 `config.yaml`（鏈別、合約、deployer、官方錢包），再依 §2 決策矩陣選 ingest 路徑。bootstrap 細節見 [`tools/README.md`](../tools/README.md)。
- **新鏈別**：在 `adapters/` 新增 `<chain>_adapter.py` 實作 `PlatformAdapter` Protocol（見 [`adapters/README.md`](../adapters/README.md)）；下游 SQL 與分析不變。
