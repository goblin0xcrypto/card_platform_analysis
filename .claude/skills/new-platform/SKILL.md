---
name: new-platform
description: 新增一個卡包平台分析資料夾。給平台網址（可選平台名稱），自動偵測鏈別、用 deployer 反查並分類所有合約（開包 pack / 市場 marketplace / NFT / 付款幣 USDT or USDC / 質押），抓出 deployer 與官方錢包，複製 platforms/_template 並填好 platforms/<name>/config.yaml。當使用者說「新增平台」「加一個平台」「幫我查這個平台的合約」或只丟一個平台網址時觸發。
---

# 新增平台 → 自動填 config.yaml

目標：使用者只給一個**平台網址**（可選平台名稱、鏈別），就把 `platforms/<name>/config.yaml`
填到接近 `platforms/renaiss/config.yaml` 的品質（多個 pack、官方錢包、付款幣、deployer、NFT）。

## 0. 環境與工具

- API key（**先判斷鏈別再選 key/路徑**）：
  - **EVM 鏈**（ethereum / base / arbitrum / polygon / optimism / bsc / monad / megaeth）→
    用 `.env` 的 `ETHERSCAN_API_KEY`。Etherscan V2 統一端點 `https://api.etherscan.io/v2/api`，用 `chainid` 切鏈。
  - **Solana** → 依「查什麼」分工（2026-06 實測定案）：
    - **交易明細 / getTransaction / NFT 指令解析 → 用 RPC：`HELIUS_API_KEY`**
      （`https://mainnet.helius-rpc.com/?api-key=<key>`，並發單筆 getTransaction ~60-150/s、免費）。
      ⚠️ free Helius **批次是付費限定**；**公開 RPC 幾乎全 429 不可用**（僅適合零星唯讀查證）。
    - **帳戶級索引查詢**（某地址全部轉帳/餘額變化）→ `SOLANA_API_KEY`（Solscan Pro v2，付費）。
    - **NFT 當前持有快照** → Helius DAS `getAssetsByGroup(collection)`（一次按 collection 批量）。
  先用第 2 步偵測出鏈別，再據此挑 key 與查詢路徑。
- 既有可重用工具：`tools/explorer.py` 的 `EtherscanV2Client`，已實作分頁、限流、重試、
  `get_contract_creation` / `txlist` / `tokentx` / `tokennfttx` / `token1155tx` / `get_logs`。
  優先用它，不要重寫 HTTP 迴圈。在 repo 根目錄用 `.venv` 跑 Python：
  ```bash
  source .venv/bin/activate 2>/dev/null
  python - <<'PY'
  import os; from pathlib import Path
  from tools.explorer import EtherscanV2Client, CHAIN_IDS
  # 讀 .env
  for line in Path(".env").read_text().splitlines():
      if "=" in line and not line.strip().startswith("#"):
          k,v=line.split("=",1); os.environ.setdefault(k.strip(), v.strip().strip('"\''))
  cli = EtherscanV2Client(os.environ["ETHERSCAN_API_KEY"], "bsc")  # 換成偵測到的鏈
  print(cli.get_contract_creation("0x..."))
  PY
  ```
- EVM 支援鏈別見 `CHAIN_IDS`：ethereum / base / arbitrum / polygon / optimism / bsc / monad / megaeth；
  這些走 `EtherscanV2Client` + `ETHERSCAN_API_KEY`。
- **Solana**（地址 base58、非 0x，**大小寫敏感 → 全程勿轉小寫**）：
  - ingest：
    - **USDC 金流 → `ingest_solana.py`**（Solscan `/account/transfer`→payments；`tools/solscan.py` 的 `SolscanClient`，
      header `token`、分頁/限流/重試/時間窗 resume、逐錢包 cursor、cluster 封閉法找平台錢包）。
    - **MPL Core 卡片 → `ingest_solana_nft.py`（Helius RPC 並發單筆，不用 Solscan detail）**：
      `getSignaturesForAddress` 列舉 + 並發 `getTransaction(jsonParsed)` 解 mpl_core 指令；
      依 data 首位元組 disc 分類 **disc20=createV2(mint)→mints、disc14=transferV1→nft_transfers**，
      asset=accounts[0]/collection=[1]/to=accounts[4]，`from` 用 SQL `LAG over(token_id order by block_time)` 推。
      `--days N`(預設7)/`--days 0`(全量,floor cursor 可續)。
  - 一次性合約查證（program/mint/collection 身分、upgrade/update authority）用 Helius RPC（或公開 RPC 零星查）
    `getAccountInfo`/`getMultipleAccounts` jsonParsed、`getSignaturesForAddress`。
  - 分類邏輯（開包 program、NFT collection、付款幣 USDC/USDT-SPL、deployer=upgrade/update authority、官方錢包）
    對照下面 EVM 步驟比照處理。
- 其他非 EVM（ton / sui）目前無 client，能填多少填多少、其餘標 ⚠️ 待人工。

### Solana 實戰補充（phygitals 驗證心得）
- **SPA 不露地址時挖前端**：Next.js 站把地址藏在 `/_next/static/chunks/*.js`。抓首頁列出 chunk →
  全部下載 → grep `new PublicKey("…")`、`*_MINT_ADDRESS`、`*_PROGRAM_ID`、`*_COLLECTION_ADDRESS`、
  `NEXT_PUBLIC_*`。注意：minify 後的**變數標籤不可信**（曾見 USDC/USDT/meme 幣互相錯標），
  地址一律以鏈上 metadata 為準。env 讀取（`x=env.NEXT_PUBLIC_…`）的值多半**未 baked**進 bundle，抓不到。
- **provider = Solscan Pro API v2**（`SOLANA_API_KEY` 是 Solscan key，從 solscan.io profile 取得；**需付費方案**，
  free key 對 v2 端點全回 401「upgrade your api key level」）。封裝在 `tools/solscan.py`，直接用 `SolscanClient`：
  - base `https://pro-api.solscan.io/v2.0`，header **`token: <key>`**（非 RPC、非 `x-api-key`）。
  - 規格坑：`page_size` 只能用允許值（帳戶 10/20/30/40/60/100、NFT 12/24/36）；`balance_change` 是**底線**非連字號；
    `sort_by=block_time&sort_order=asc` 可穩定排序→resume；支援 `token=` 與 `block_time[]=[from,to]` 過濾。
  - ⚠️ **Solscan 只用於帳戶級索引查詢（/account/transfer 等）；交易明細(getTransaction)改走 Helius RPC**
    （實測 Solscan 逐筆 /transaction/detail ~13/s 慢又耗 CU；Helius 並發單筆 ~60-150/s 免費）。
- **交易明細/指令解析 = Helius RPC（`HELIUS_API_KEY`）**：`https://mainnet.helius-rpc.com/?api-key=<key>`，
  `getTransaction(sig,{encoding:jsonParsed,maxSupportedTransactionVersion:0})` 並發單筆(free 批次=付費限定)；
  唯讀合約查證(getAccountInfo/getMultipleAccounts/getSignaturesForAddress)亦走此或公開 RPC(後者易 429,僅零星用)。
- **分類**：candidate base58 先驗 32-byte 合法性 → `getMultipleAccounts` 一次分類
  （executable=program、spl-token/spl-token-2022 mint、owner=`CoREENxT…`=Metaplex Core 資產/collection、
  owner=System=錢包）。剔除標準 program/sysvar 與 minify 雜訊（鏈上不存在者）。
- **NFT/collection**：MPL Core collection 解 account data：key(1)+updateAuthority(32)+name+uri+num_minted+current_size；
  **updateAuthority 即部署/權限錢包**（≈ deployer / operations）。舊版 spl-token NFT 用 Metaplex Token Metadata PDA 取 symbol。
  - **卡片轉移/鑄造**：MPL Core 非 SPL→不在 /account/transfer；走 Helius `getTransaction` 解 mpl_core 指令
    （disc20=createV2/disc14=transferV1，asset=acc[0]/collection=acc[1]/to=acc[4]）。
  - **當前持有快照**（誰握有哪些卡+評級/Cert）：Helius DAS `getAssetsByGroup(collection)`，~48s 拿全 collection，免逐筆。
- **compressed NFT (cNFT / Bubblegum)**：平台可能有「更早一代」用 cNFT（不是 SPL、也不是 MPL Core；走 Bubblegum + Account Compression）。
  徵兆：玩家早期交易出現 program `BGUMAp9…`(Bubblegum)+`cmtDvXum…`(Account Compression)+`noopb9bk…`(Noop)；payments 早於現有 NFT collection。
  抓法 = `ingest_solana_cnft.py`：Helius **Enhanced Transactions API**(`/v0/addresses/{tree}/transactions?before=`，**對 merkle tree 分頁**，一個 collection 可能多棵 tree)，
  `events.compressed[]` 直接給 type(MINT/TRANSFER/BURN)/assetId/oldLeafOwner(from)/newLeafOwner(to)→from/to 直接有。
  ⚠️ Enhanced API 中途會回短頁，**只有「確實成功且回空 []」才算到底**(勿用 len<100 break，否則漏抓早期)。
  找 collection/tree：DAS `getAssetsByOwner`(平台庫存錢包) 看 compressed 資產的 grouping(collection) 與 compression.tree。
- **無自訂程式很常見**：RWA 卡平台常無鏈上 pack/marketplace program，撮合/回購走中心化後端，
  鏈上只有 NFT 轉移 + USDC 轉帳 + gasless fee-payer。此時 pack_opening/marketplace 留空並註明後端撮合。
- **找平台收/付款錢包 = cluster 封閉法**（後端撮合平台的「開包金流」就是玩家 USDC ↔ 平台錢包）：
  1. 從錨點(authority/fee_payer)追不到 USDC 時，改**順著玩家追**：取 NFT 持有者→看其 USDC 對手方；
  2. 對候選錢包做 profile：**付給很多不同地址(高 out_cp/distinct 非 cluster payee≥~20)=平台 hub**；
     只付回 cluster(≤3)=重度玩家/farmer(這些是 bot/wash 分析標的，不是平台錢包)；
  3. 把確認的 hub 加進 `official_wallets.usdc_cluster`，跑 ingest_solana，再重做封閉檢查；
  4. 反覆到「與 cluster 大量互動者全是玩家、無新 hub」為止 = cluster 收斂，金流才算抓全。
  分腿：非cluster→cluster=開包/儲值；cluster→非cluster=buyback；兩端 cluster=內部。(phygitals 範例：11 顆)
- `tools/bootstrap.py` 是舊版機械式產生器：可拿來**快速抽地址候選與偵測鏈別**參考，但
  **不要**讓它寫最終 config（它會用 yaml.safe_dump 洗掉 `_template` 的註解、且不填官方錢包）。

## 1. 決定平台名稱與資料夾

- 平台名稱：使用者沒給就從網址 domain 推 kebab-case（如 `https://www.renaiss.xyz/` → `renaiss`）。
- 複製模板（**保留註解**，逐欄 Edit 填寫，不要整檔重寫）：
  ```bash
  cp -r platforms/_template platforms/<name>
  ```
- 若資料夾已存在，先看內容；不要覆蓋使用者既有調查結果，改用 Edit 補欄位。

## 2. 抓站找候選地址 + 偵測鏈別

- 用 WebFetch 抓首頁與常見子頁（`/docs` `/whitepaper` `/litepaper` `/contracts` `/about` `/faq`），
  也看 footer / Etherscan 連結。SPA 抓不到內容時請使用者貼合約地址或 explorer 連結。
- 鏈別：看頁面出現的 explorer 域名 / RPC（bscscan→bsc、basescan→base、arbiscan→arbitrum、
  polygonscan→polygon、etherscan→ethereum…）。抓不到就問使用者或先標 ⚠️。
- 地址候選：regex `0x[a-fA-F0-9]{40}`，去掉 0x0 與 0xdead。

## 3. enrich 每個候選合約

對每個候選打 explorer：
- `getsourcecode` → 合約名稱 `ContractName`、是否 verified（有 SourceCode）、是否 proxy
  （`Proxy==1` 時記 `Implementation`，開包/市場常是 ERC-1967 代理）。
- `get_contract_creation` → **deployer** 與 deploy tx / block。
- 初步分類（名稱關鍵字）：
  - pack_opening：pack / mint / box / gacha / loot / opener
  - marketplace：market / exchange / trade / auction
  - nft：erc721 / erc1155 / nft / card / collection（用 `supportsInterface` 0x80ac58cd=721、
    0xd9b67a26=1155 確認）
  - token：token / erc20 / coin（多半就是付款穩定幣，見第 5 步）
  - staking：stak

## 4. 用 deployer 反查所有兄弟合約（關鍵步驟）

平台合約常由**同一個 deployer** 部署（renaiss 的 7 個合約都是）。這是補齊清單的最強訊號：

1. 取第 3 步已確認的主要合約（pack 或 nft）的 deployer。
2. 掃該 deployer 的 `txlist`，挑出**合約建立交易**（`to` 為空、或 Etherscan 回傳 `contractAddress`），
   收集它部署過的所有合約地址。
3. 對這些新地址重跑第 3 步 enrich + 分類。把屬於本平台的補進清單。
4. 多個 pack 合約要全部收進 `pack_opening`（做成 list，像 renaiss 那樣）。
5. NFT 若官網沒列：用 `tokennfttx` / `token1155tx`（傳 `contractaddress`）或 pack 的 mint event
   反查實際發卡的 NFT 合約。

## 5. 判定付款幣（USDT / USDC）

- 對每個 pack_opening 合約取近 N 筆 `tokentx`，統計 token 合約與 symbol。
- 以「付進合約」(to==pack) 的主流穩定幣為付款幣；對照下表確認是 USDT 還是 USDC：

  | 鏈 | USDT | USDC |
  |----|------|------|
  | bsc | 0x55d398326f99059ff775485246999027b3197955 | 0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d |
  | ethereum | 0xdac17f958d2ee523a2206206994597c13d831ec7 | 0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48 |
  | base | — | 0x833589fcd6edb6e08f4c7c32d4f71b54bda02913 |
  | arbitrum | 0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9 | 0xaf88d065e77c8cc2239327c5edb3a432268e5831 |
  | polygon | 0xc2132d05d31c914a87c6611c10748aeb04b58e8f | 0x3c499c542cef5e3811e1192ce70d8cc03d5c3359 |

- 把該幣合約填進 `contracts.token`，`payment_token_symbol` 填 USDT / USDC。
- 順手由「付進金額的眾數 / 整數倍」推單價，寫進 pack 的行內註解（renaiss 風格）。

## 6. 找 deployer 與官方錢包

- `deployers`：第 4 步的 deployer（通常一個，全部合約共用）。
- `official_wallets`（多半不在官網，用資金流推，**全部標 ⚠️ 待人工複核**）：
  - treasury：被 pack 合約以整數金額大量撥入、自己幾乎不付出的冷錢包。
  - operations：deployer / NFT owner 那顆營運 EOA。
  - liquidity / market_maker：對平台注資、或在二級市場異常活躍（首日開包、整數種子）的地址。
- `infrastructure_addresses`：Privy / Coinbase Smart Wallet 等 factory（funder 分群要排除），找到才填。

## 7. 寫入 config.yaml

逐欄 Edit `platforms/<name>/config.yaml`（從 `_template` 複製來的那份，**保留所有註解**）：
- `platform` / `chain` / `payment_token_symbol`
- `launch_block`：最早部署合約的區塊；`ingest.from_block` 設成同值、`to_block: latest`、`batch_size: 5000`
- `contracts.pack_opening`（list，每個 pack 加行內註解：名稱、推估單價）
- `contracts.marketplace` / `staking` / `token` / `nft`（list）
- `deployers` / `official_wallets` / `infrastructure_addresses`
- `thresholds` / `report` 用模板預設即可
- 每個合約地址後面加簡短行內註解（部署 block、類型、proxy、verified、單價），像 renaiss。
- 任何用推測得到、未經鏈上確認的欄位，註解標 ⚠️。

## 8. 收尾

- 用 `python -c "import yaml,sys; yaml.safe_load(open('platforms/<name>/config.yaml'))"` 確認 YAML 合法。
- 回報摘要：偵測到的鏈、找到幾個 pack / nft / 市場、付款幣、deployer、哪些欄位是 ⚠️ 待人工複核。
- 不要主動跑 ingest / 分析；那是後續步驟。

## 設計原則

- 寧可標 ⚠️ 留白，不要填錯地址。鏈上能驗證的（getsourcecode / creation / supportsInterface / tokentx）就驗證。
- 官方錢包與資金來源 trace 是判斷重點，自動推測後務必提醒使用者人工複核。
- 鏈上查詢按鏈別選 key：EVM 走 `ETHERSCAN_API_KEY` + `tools/explorer.py`；Solana 走 `SOLANA_API_KEY`。
  只抓與平台地址互動的資料，不用 RPC。
