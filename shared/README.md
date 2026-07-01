# shared/

跨平台共享資源。新增平台分析時自動 import，避免重複維護。

## known_addresses.csv

跨平台共享的 CEX / Bridge / Mixer / 知名地址字典。

**欄位**
- `chain` — ethereum / solana / base / arbitrum ...
- `address` — 原始大小寫保留（EVM 比對前 lower-case）
- `label` — 人類可讀名稱
- `category` — `cex` / `bridge` / `mixer` / `mev` / `token` / `defi`
- `source` — `public` / `chainalysis` / `internal`
- `added_at` — 加入字典的日期

## 使用方式

```sql
-- 載入到 Postgres
COPY known_addresses FROM '/path/to/shared/known_addresses.csv' CSV HEADER;

-- 在 queries 中 JOIN 標記下游身分
SELECT p.to_addr, ka.label, ka.category
FROM payments p
LEFT JOIN known_addresses ka ON ka.address = p.to_addr
WHERE p.platform_id = :platform_id;
```

## 更新原則

- 新發現的混淆器、rug deployer 等都加進來
- 不收錄具體平台合約（那是 platform_profile 的範疇）
- 每筆都要附 source，可審計
