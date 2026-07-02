# platforms/

各卡牌/盲盒 NFT 平台的分析資料夾。每個子目錄是一個平台，所有平台共用同一套分析流程（見上層 [ANALYSIS_SOP.md](../ANALYSIS_SOP.md)）。新平台請複製 [`_template/`](./_template/)。

> **不同平台的資料抓取流程不一樣**（取決於鏈別與合約是否 verified）。全局架構與決策矩陣見
> [docs/data_pipeline.md](../docs/data_pipeline.md)；單一平台的確切指令見各平台 README 的「資料抓取」段。

## 平台一覽

| 平台 | 鏈 | 付款幣 | 抓取路徑 | 說明 |
|------|----|--------|----------|------|
| [renaiss](./renaiss/) | BSC (BNB Smart Chain) | USDT | `ingest_txs.py` → `derive_from_raw.py`（unverified） | Renaiss — Collectible Finance Network，PSA/BGS 評級卡 gacha + 市場。分析最完整。 |
| [phygitals](./phygitals/) | Solana | USDC | `ingest_solana.py` + `ingest_solana_nft.py` + `ingest_solana_cnft.py` | Phygitals — 實體卡 RWA 卡包/市場（PSA/Fanatics/Alt 託管）。 |
| [mnstr](./mnstr/) | MegaETH L2 | USDm | `ingest.py`（verified, mnstr_adapter） | MNSTR 卡包/市場。 |
| [playkami](./playkami/) | Monad (EVM L1) | USDC | `ingest.py`（monad_adapter） | PlayKami 卡包平台（範例平台）。 |
| [`_template`](./_template/) | — | — | — | 新平台複製此資料夾。 |

## 每個平台的標準檔案

複製自 `_template/`，是各分析階段的固定產出物：

| 檔案 | 內容 |
|------|------|
| `config.yaml` | 平台設定：鏈別、合約地址、deployer、官方錢包、分析閾值。所有 ingest/分析的輸入。 |
| `platform_profile.md` | 階段 0 產出：平台基本資訊、合約清單、官方錢包、deployer 背景。 |
| `flow_overview.md` | 階段 3-B 產出：deployer 與官方錢包的整體金流圖。 |
| `red_flags.md` | 紅旗清單彙整。 |
| `wash_trading.md` | 自刷量分析。 |
| `bot_clusters.csv` | 偵測到的機器人/同 funder 群集。 |

## 額外產出物的擺放慣例

標準檔案以外、分析過程產生的圖表、資料匯出、客製報告，請放進子資料夾，不要散落在平台根目錄（參考 `renaiss/`）：

| 子資料夾 | 放什麼 |
|----------|--------|
| `charts/` | 圖表 PNG（營收、市場、利潤等）。 |
| `data/`   | 資料匯出 CSV / JSON（winners、PnL、原始明細等）。 |
| `reports/`| 客製的 Markdown 分析報告。 |
