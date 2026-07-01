# Adapters

每個鏈別（EVM / Solana / TON / Sui）一個檔案，實作 `base_adapter.py` 的 `PlatformAdapter` Protocol。

## 命名規範
- `evm_adapter.py` — 所有 EVM 鏈（以 chain_id 參數區分）
- `solana_adapter.py`
- `ton_adapter.py`
- `sui_adapter.py`

## 新增鏈別步驟

1. 複製 `_template_adapter.py` → `<chain>_adapter.py`
2. 實作 5 個 fetch 方法 + `get_usd_price`
3. 在 `tests/test_<chain>_adapter.py` 用一個已知交易做 fixture 測試
4. 在 `platforms/<name>/config.yaml` 設 `chain: <chain>`，pipeline 會自動選對 adapter

## 為什麼是 Adapter 而不是直接呼叫 RPC

- 抽離「鏈別差異」與「業務分析」：分析者只看 `PackOpen` / `Payment` domain 物件
- 切換資料源（Alchemy → QuickNode → 自架節點）只動 adapter
- 抓取邏輯可獨立做單元測試，不需要跑整條 pipeline

## Trace API 不可省

EVM 平台尤其注意：純 `Transfer` event 會漏掉原生幣（ETH / BNB / MATIC）支付，必須呼叫 `debug_traceTransaction` 或 `trace_transaction` 取 internal tx。Solana 直接看 `meta.preBalances` / `postBalances` 差異。
