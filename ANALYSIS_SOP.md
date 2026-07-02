# 卡牌平台鏈上分析 SOP（模板）

> 本文件為通用 SOP 模板（原名 `playkami_analysis_sop.md`，2026-07 更名為 `ANALYSIS_SOP.md`）。
> 分析新平台時，複製整個 `platforms/<name>/` 子目錄並依本 SOP 填寫各章節輸出物。

---

## 階段 0：URL → 自動 Bootstrap

只給平台網址，自動產出 `config.yaml` 與 `platform_profile.md` 草稿。

### 一鍵指令

```bash
export ETHERSCAN_API_KEY="..."
python tools/bootstrap.py --url https://<platform-site> --name <platform-name>
```

### bootstrap.py 做的事

1. **抓頁面** — 首頁 + 常見路徑（`/docs` `/whitepaper` `/contracts` `/about` `/faq`）
2. **偵測鏈別** — 根據頁面內 explorer 域名 / RPC URL 推測
3. **抽合約地址候選** — regex 掃 EVM `0x[a-f0-9]{40}` 或 Solana base58
4. **Explorer enrich** — 對每個候選打 explorer：合約名、deployer、deploy tx、是否 verified、是否 proxy
5. **推合約類型** — 從合約名關鍵字推 `pack_opening` / `nft` / `marketplace` / `token` / `staking`
6. **推付款幣種** — 對 pack_opening 取近 100 筆 tokentx，統計 token symbol
7. **寫檔** — `platforms/<name>/config.yaml` + `platform_profile.md`（自動標 ⚠️ 待複核欄位）

### 人工複核項目（不可省）

bootstrap 只能自動化「**公開可查的部分**」。以下仍需手動：

| 項目 | 為什麼自動不了 |
|------|----------------|
| 合約類型分類 | 合約名可能誤導（marketplace 命名為 `MintHub`） |
| 官方錢包 / treasury | 通常不在公開頁面 |
| Deployer 資金來源 trace | 需要往上 BFS 1-3 跳 |
| Deployer 過往項目 | 比對歷史 + 已知 rug 字典 |
| 多簽簽署人活動 | 需 Safe / Squads 介面查 |

完成後：
- [ ] 全部 ⚠️ 欄位處理完
- [ ] 至少跑過一次 deployer 資金來源 trace
- [ ] 比對 `shared/known_addresses.csv` 命中清單

> 產出物：`platforms/<name>/config.yaml`（pipeline 讀取）+ `platform_profile.md`（人類閱讀）。

### Fallback：bootstrap 抓不到時

- **SPA / JS render**：用 Playwright / 瀏覽器 inspect 把 HTML 存下，未來支援 `--html-file`
- **非 EVM 鏈尚未實作 enrich**：仍會抓地址候選，第 4-5 步跳過，需人工補
- **合約沒 verify**：deployer 仍可取，類型留 `unknown` 人工填

---

## 階段 1：資料庫骨架（一次定義、跨平台復用）

使用 Postgres（搭配 TimescaleDB 處理時序資料更佳），所有表加 `platform_id` 維度欄位，schema 通用於所有平台。

完整建表語句見 `schema/init.sql`。核心表：

```
platforms(id, name, chain, launch_block, profile_json)
contracts(platform_id, address, type, deployer, deploy_block, deploy_tx)
addresses(platform_id, address, label, source, cluster_id)
pack_opens(platform_id, tx_hash, block_time, opener, pack_id, price_raw, price_token, price_usd)
mints(platform_id, tx_hash, block_time, minter, token_id, contract, pack_open_tx)
payments(platform_id, tx_hash, block_time, from_addr, to_addr, token, amount_raw, amount_usd, direction)
nft_transfers(platform_id, tx_hash, block_time, token_id, from_addr, to_addr, price_usd, marketplace)
marketplace_trades(platform_id, tx_hash, token_id, buyer, seller, price_usd, fee_usd, royalty_usd)
address_flows(platform_id, address, day, in_usd, out_usd, net_usd)
```

**關鍵設計原則**
- `pack_open_tx` 在 `mints` 與 `pack_opens` 之間建立外鍵 → 開包/Mint/付款 三件事用同一 tx 串起
- 所有金額同時保存 `raw`（uint256 字串）與 `usd`（numeric），寫入時 freeze 當下匯率
- `addresses.cluster_id` 預留給後續同 funder 分群結果

---

## 階段 2：資料抓取流水線

順序具有依賴關係，**不可亂跳**：

1. **定位 Pack Opening 合約**
   - 從 deployer 的 tx 找出第一個被高頻互動的合約
   - 或從 NFT 合約反查 `MINTER_ROLE`
2. **抓開包事件**：解 `PackOpened` / `Mint` / `Transfer(from=0x0)` event
3. **抓對應金流**：同 tx 內的 ERC-20 Transfer **以及 internal tx**（必須用 trace API，光看 Transfer event 會漏 ETH/原生幣支付）
4. **抓 NFT 後續流向**：以 token_id 為單位，沿 transfer 鏈一路追到「最終持有者 / 銷毀 / 質押」
5. **抓 deployer / 官方錢包金流**：以 deployer 為起點 BFS 1～2 層，找出資金集散地與 CEX 充值點

### 工具層
| 鏈 | RPC | Trace | Backfill |
|----|-----|-------|----------|
| EVM | Alchemy / QuickNode | `debug_traceTransaction` / Reth trace | Dune / Flipside |
| Solana | Helius | Helius Enhanced TX | Flipside |
| TON | tonapi.io | tonapi traces | DTon |
| Sui | Sui RPC | Sui transaction blocks | Flipside |

> 每新增一個平台只換 client（在 adapter 層），下游 pipeline 不變。

---

## 階段 3：分析模組（每個平台跑同一套）

把分析拆成獨立查詢模組，每個都是一個 SQL view 或 notebook，**全部以 `platform_id` 為參數**。

### A. 平台健康度（`queries/platform_health.sql`）
- 每日開包數、開包金額、獨立開包地址數
- 留存率：D1 / D7 / D30 仍在開包或交易的比例
- NFT 沉澱率：mint 後 30 天仍未轉手的比例
- 開包價中位數 vs 二級市場底價

### B. Deployer 金流追蹤（`queries/deployer_flow.sql`）
- Deployer 收到的開包分潤路徑（多跳追到 CEX 充值地址或混淆地址）
- 是否有資金回流到「疑似機器人地址」→ 直接的自刷信號
- 多簽簽署人地址的歷史活動（過去是否操盤過其他項目）

### C. 機器人行為偵測（`queries/bot_detection.sql`）
**規則層先打標籤**
- 開包時間間隔變異數極小（cron-like，σ/μ < 0.1）
- gas price 完全一致 / nonce 連續且時間集中
- 資金來源為同一個分發地址（funder cluster）
- NFT 收到後 N 分鐘內全部轉到同一個 sink 地址
- 同一批地址永遠在新合約上線後 X 分鐘內出現

**分群層**：跑「同 funder 圖」，把同一個資金源的地址縮成一個「實體」，再以實體為單位看行為。詳細演算法見 `docs/clustering.md`。

### D. 自刷量偵測（`queries/wash_trading.sql`）
- Marketplace trade 中 buyer / seller 屬於同一 funder cluster
- A→B→A 的環狀轉移（含經過混淆地址，跳數 ≤ 5）
- 成交價顯著偏離同類 NFT 中位價（過高自抬、過低洗手續費返利）
- 手續費 / 版稅回流比例：若平台對自家地址退費，淨流出 < 名義價 → 標出

### E. NFT 最終流向（`queries/nft_endstate.sql`）
對每張 NFT 分類成：`burn` / `staked` / `locked_in_official` / `散戶長持` / `集中在 cluster` / `marketplace 掛單`，畫 Sankey 圖最直覺。

---

## 階段 4：產出物（每個平台一份）

統一模板，**檔名固定**方便跨平台對比：

| 檔案 | 內容 |
|------|------|
| `platform_profile.md` | 合約、地址、代幣（階段 0 產出） |
| `flow_overview.md` | 總開包額、總分潤、deployer 提現路徑 |
| `bot_clusters.csv` | 機器人實體清單 + 證據（地址、規則命中數、funder） |
| `wash_trading.md` | 自刷量估算（金額、佔比、手法分類） |
| `nft_endstate.png` | NFT 流向 Sankey 圖 |
| `red_flags.md` | 結論：平台健康度、主要風險點 |

---

## 跨平台復用設計（強化版）

### 1. Adapter Pattern（資料抓取層）
所有抓取邏輯實作同一個 interface（見 `adapters/base_adapter.py`）：

```python
class PlatformAdapter(Protocol):
    def fetch_pack_opens(self, from_block, to_block) -> Iterable[PackOpen]
    def fetch_payments(self, tx_hashes) -> Iterable[Payment]
    def fetch_nft_transfers(self, contract, from_block, to_block) -> Iterable[Transfer]
    def fetch_deployer_flow(self, address, depth) -> Iterable[FlowEdge]
    def get_usd_price(self, token, block_time) -> Decimal
```

**新增平台只要寫 adapter，不動下游**。下游消費 `PackOpen` / `Payment` / `Transfer` 這些 domain 物件，與鏈無關。

### 2. 參數化 SQL
`queries/` 底下所有 `.sql` 第一行都寫：
```sql
-- params: :platform_id, :start_date, :end_date
```
禁止 hardcode 地址；地址查詢一律 JOIN `addresses` 表。

### 3. 共享地址字典（`shared/known_addresses.csv`）
跨平台共用的 CEX / Bridge / Mixer / 知名 MEV 地址：

```
chain,address,label,category,source,added_at
ethereum,0x28C6c06298d514Db089934071355E5743bf21d60,Binance 14,cex,public,2025-01-01
ethereum,0xDFd5293D8e347dFe59E90eFd55b2956a1343963d,Binance 16,cex,public,2025-01-01
...
```

新平台分析時自動 import，標記 deployer / 機器人 / NFT sink 的下游身分。

### 4. 設定檔驅動（`platforms/<name>/config.yaml`）
每個平台一個 yaml，描述抓取參數與分析閾值：

```yaml
platform: playkami
chain: ethereum
launch_block: 19500000
contracts:
  pack_opening: "0x..."
  marketplace: "0x..."
thresholds:
  bot_interval_cv: 0.1        # 開包間隔變異係數閾值
  wash_price_deviation: 0.4   # 成交價偏離中位數比例
  cluster_min_size: 3         # 機器人實體最小地址數
```

CLI 跑分析時：`python run_analysis.py --platform playkami`，所有閾值與合約地址都從 yaml 讀，分析者不改 code。

### 5. 統一報告產生器（`docs/report_template.md`）
所有平台跑完後，由 notebook（`notebooks/report.ipynb`）依模板自動產生 markdown 報告，避免每個平台手寫格式不一致。

### 6. 版本控制與審計
- 每次抓取結果寫入時帶 `ingested_at` 時間戳
- 規則命中結果存進 `bot_flags(platform_id, address, rule_id, hit_at, evidence_json)`，可回溯為什麼某地址被標
- 規則本身放 `queries/rules/` 並版本化，調閾值要留 commit message

---

## 快速開始（複製貼上即可）

```bash
# 1. URL → 自動填表（取代手動 cp + edit）
python tools/bootstrap.py --url https://<platform-site> --name <new_platform>

# 2. 人工複核 platforms/<new_platform>/platform_profile.md 中所有 ⚠️ 欄位

# 3. 寫該平台 adapter（若為新鏈）
edit adapters/<chain>_adapter.py

# 4. 跑抓取
python ingest.py --platform <new_platform>

# 5. 跑分析
python run_analysis.py --platform <new_platform>

# 6. 產報告
jupyter nbconvert --execute notebooks/report.ipynb --output platforms/<new_platform>/red_flags.md
```

---

## 附錄：紅旗清單（速查）

| 紅旗 | 判定 | 嚴重度 |
|------|------|--------|
| Deployer 資金來自 Tornado / 混淆器 | trace 上游 ≤ 3 跳 | 高 |
| 開包高峰時段 > 60% 流量來自單一 funder cluster | bot_detection rule 命中 | 高 |
| Marketplace 50% 以上成交為同 cluster 內 | wash_trading 命中 | 高 |
| NFT > 80% 集中在 < 50 個地址 | nft_endstate 統計 | 中 |
| 官方錢包對特定地址有非公開空投 | payments 直接 outflow 比對 | 中 |
| 部署者過去部署過 rug 項目 | 共享地址字典比對 | 高 |
