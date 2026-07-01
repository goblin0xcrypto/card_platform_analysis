# Platform Profile — playkami

> 由 WebFetch + 人工分析自動產生（bootstrap.py 因 SPA 限制失敗，改用 LLM-assisted 流程）。
> 分析日期：2026-05-25。**所有合約欄位待平台正式上線後從 Monad explorer 反查補上**。

## 基本資訊

| 項目 | 內容 |
|------|------|
| 平台名稱 | PlayKami |
| 鏈別 | **Monad** (EVM-compatible L1) |
| 啟動區塊 | ⚠️ TBD — 平台 pre-launch 中 |
| 上線狀態 | ⚠️ "APP COMING SOON" |
| 官網 | https://playkami.io |
| 文件 | https://docs.playkami.io |
| 公平性驗證器 | https://verifier.playkami.io |
| Twitter / X | https://x.com/PlayKamiApp |
| Instagram | https://www.instagram.com/playkami.io |
| TikTok | https://www.tiktok.com/@playkami |

## 鏈別判定依據

`verifier.playkami.io` 明確標示「Monad blockchain」+「Dual-Seed Randomness」。
其他頁面只提到 "EVM" 與 "USDC"，未直接指明 Monad，但這三項組合（EVM + USDC + Monad）一致。

Monad 主要 explorer 候選：
- https://monadscan.com
- https://monad.socialscan.io
- https://monadvision.com

> ⚠️ 注意：`tools/bootstrap.py` 與 `EXPLORER_API` dict 目前**尚未支援 Monad**，需另寫 adapter（見「待辦」）。

## 合約清單

| 類型 | 地址 | Deployer | Deploy Block | Verified | 備註 |
|------|------|----------|--------------|----------|------|
| pack_opening | ⚠️ 未公開 | ⚠️ | ⚠️ | ⚠️ | Gacha 機制 |
| nft (Pokemon) | ⚠️ 未公開 | ⚠️ | ⚠️ | ⚠️ | "Coming soon" |
| nft (One Piece) | ⚠️ 未公開 | ⚠️ | ⚠️ | ⚠️ | "Coming soon" |
| nft (Mixed) | ⚠️ 未公開 | ⚠️ | ⚠️ | ⚠️ | "Coming soon" |
| marketplace | ⚠️ 未公開 | ⚠️ | ⚠️ | ⚠️ | 有自有市場，「以 90% 價格回賣」機制 |
| staking | ⚠️ 不確定是否上鏈 | — | — | — | Points / Referrals 系統，可能 off-chain |
| token | ⚠️ 不確定是否上鏈 | — | — | — | 平台積分 |

## 付款 / 錢包資訊

| 項目 | 內容 |
|------|------|
| 付款幣種 | **USDC**（Monad 上的 USDC） |
| 錢包方案 | **Privy**（embedded wallet，自動為使用者建立） |
| 多鏈儲值 | 支援（有 "Multichain Deposits & Bridging" 頁，細節未公開） |

## 產品 / 定價

| 卡包等級 | 價格（USD） |
|---------|-------------|
| Starter | $25 |
| Premium | $100 |
| Elite | $250 |

支援的收藏品類別：Pokémon Cards、One Piece Cards、Mixed Cards（皆 "Coming soon"）。

## 開包機制（Proof of Fairness）

- **Dual-Seed**：server seed 先以 hash commit、client seed 由使用者提供
- 開包後 reveal server seed + 提供 snapshot hash（鎖定當時 card pool + odds）
- 結果可在 `verifier.playkami.io` 重算驗證
- ⚠️ 未公開鏈上 VRF 來源（Chainlink VRF / Pyth Entropy / 自製 commit-reveal 皆有可能）

## 兌換 / 出場

- **保留**：NFT 留在錢包
- **交易**：在內部 marketplace 出售
- **實體兌換**：vault 中的實卡可寄送
- **回賣**：以該物件估值的 **90%** 賣回給平台 → 這是分析自刷量時的關鍵錨點

## 官方錢包

| 用途 | 地址 | 備註 |
|------|------|------|
| treasury | ⚠️ 未公開 | |
| operations | ⚠️ 未公開 | |
| airdrop | ⚠️ 未公開 | 有 Referrals / Points 系統，可能涉及空投 |
| marketplace_escrow | ⚠️ 未公開 | 90% sellback → 平台有資金池接盤 |

## Deployer 背景

- **Contract Creator**：`0xee0d642e9a11ec928f43c6ff26733a40223c792a`（EOA，nonce 146）
- 此 EOA 透過 **EIP-7702** 委派代碼給 `0x63c0c19a282a1b52b07dd5a65b58948a07dae32b`（11k bytes smart account implementation）
- 推測為 **Privy embedded wallet** 升級版（與 docs 中提到的 Privy 整合一致）
- ⚠️ 待 trace 此 EOA 的資金上游（≤ 3 跳）
- ⚠️ 待確認該 implementation 是 Privy / Coinbase Smart Wallet / 自製

> 注意：funder clustering 演算法必須把 `0x63c0c19a...` 這類 smart account implementation 列入 funder 排除清單，否則所有同 Privy 用戶會被誤歸成一個 cluster。

## 紅旗預警（pre-launch 階段觀察）

| 觀察 | 嚴重度 | 說明 |
|------|--------|------|
| 90% sellback 機制 | 中 | 平台主動接盤 → 自刷量風險中等偏高 |
| 無公開合約地址 | 中 | 上線前正常，但持續無揭露要追蹤 |
| Embedded wallet (Privy) | 中 | 使用者錢包行為與真正 EOA 差異大，bot 偵測規則需調整 |
| Multichain 儲值 | 中 | 資金混淆面變大，funder cluster 演算法需處理跨鏈源 |
| Points / Referrals 系統 | 低 | 標準功能，需留意是否被刷 |

## 待辦（人工 / 工程）

- [ ] 平台上線後從 `monadscan.com` 找出 pack_opening 合約（追蹤官方 Twitter 公告）
- [ ] 補 `contracts.pack_opening` 與 `contracts.nft[]`
- [ ] Trace deployer 資金來源（≤ 3 跳）
- [ ] 把 Monad 加入 `tools/bootstrap.py` 的 `EXPLORER_API`（需找 Monad explorer 對應 API endpoint 與格式）
- [ ] 撰寫 `adapters/monad_adapter.py`（EVM-compatible 可從 `evm_adapter` fork）
- [ ] 確認 Points / Token / Staking 是否上鏈
- [ ] 觀察 90% sellback 是合約自動還是 off-chain 報帳

## 檢查清單

- [x] 確認鏈別（Monad）
- [x] 確認付款幣種（USDC）
- [x] 確認是否有自有 marketplace（有，含 90% sellback）
- [ ] 所有合約都解出 deployer
- [ ] 所有官方錢包都已標記
- [ ] deployer 資金來源已追蹤
- [ ] 已比對 `shared/known_addresses.csv`
