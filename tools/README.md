# tools/

## bootstrap.py — URL → 自動填表

給平台網址，自動偵測鏈別、抓合約、推測類型、填寫 `config.yaml` 與 `platform_profile.md`。

### 用法

```bash
# 安裝依賴
pip install requests pyyaml

# 設定 API key（推薦用 .env，免每次 export）
cp .env.example .env
edit .env                          # 填入 ETHERSCAN_API_KEY 等

# 跑 bootstrap（自動讀專案根 .env）
python tools/bootstrap.py --url https://playkami.example --name playkami

# 或指定別的 env 檔
python tools/bootstrap.py --url ... --name ... --env-file .env.local
```

### API Key 載入優先順序

1. **shell 環境變數**（`export ETHERSCAN_API_KEY=...`）— 最高優先，會覆寫 .env
2. **`--env-file` 指定的檔案**
3. **專案根目錄的 `.env`**（預設）

`.env` 不會覆寫已存在的 shell 變數，方便 CI / 臨時切換。`.env` 已加入 `.gitignore`，不會被 commit。

### 流程（6 步）

1. **抓頁面**：首頁 + `/docs` `/whitepaper` `/contracts` `/about` `/faq`
2. **偵測鏈別**：根據頁面中出現的 explorer 域名、RPC URL 推測
3. **抽地址候選**：regex 抓所有 `0x[a-fA-F0-9]{40}`（EVM）或 base58（Solana）
4. **打 explorer enrich**：取得合約名稱、ABI、deployer、deploy tx、是否 verified、是否 proxy
5. **推付款幣種**：對 pack_opening 合約取近 100 筆 tokentx，統計 token symbol
6. **寫檔**：輸出 `platforms/<name>/config.yaml` + `platform_profile.md`，標 ⚠️ 處待人工複核

### 輸出物標記

所有自動產出的 `platform_profile.md` 開頭都有：

> 由 tools/bootstrap.py 自動產生。**請人工複核**所有欄位。

不確定的欄位用 ⚠️ 標出，必須人工確認後才能進入後續分析。

### 限制與 fallback

| 限制 | 對策 |
|------|------|
| SPA / JS-render 頁面抓不到內容 | 手動貼 HTML 給 `--html-file` 參數（TODO） |
| 合約類型誤判 | 第 4 步只看合約名稱，必要時人工改 `config.yaml` |
| 合約沒 verify | deployer 仍可取，類型留 `unknown`，人工補 |
| 非 EVM 鏈未支援 | 目前 Solana / TON / Sui 還沒 enrich，會跳過第 4-5 步 |
| Multi-chain 平台 | 多次跑 bootstrap，每條鏈一個資料夾 |

### 為什麼還需要人工複核

- 合約名稱可能誤導（例如有人把 marketplace 叫 `MintHub`）
- 官方錢包 / treasury 通常不會出現在公開頁面
- Deployer 資金來源 trace 是分析的關鍵，仍需手動跑
- 已知地址比對（CEX / mixer）要 join `shared/known_addresses.csv`

bootstrap 的目的是把繁瑣的「找地址 + 查 explorer」自動化，**不取代調查**。
