# Funder Clustering 演算法

## 目的

把「同一個資金源出來的多個錢包」歸併成一個「實體（cluster）」，後續所有機器人 / 自刷量分析以實體為單位，而不是地址。

## 直覺

一個操盤者要操控 100 個錢包，總得從某個源頭把 ETH / USDT 分發出去。這個源頭就是 funder。同 funder 的地址有極高機率是同一個實體。

## 演算法

### Step 1：建立 funder 邊
對每個地址，找出**第一筆收到 gas / 收到計價幣**的來源：

```python
def first_funder(addr) -> Optional[str]:
    # 取第一筆 payments / native_transfer 的 from_addr
    # 排除 CEX、橋、官方錢包（這些是合理的多人共同來源，不算同源）
```

排除清單來自 `shared/known_addresses.csv`（category in `cex / bridge / mixer`）。

### Step 2：建立並查集（Union-Find）
- 節點：所有出現過的錢包地址
- 邊：`addr → first_funder(addr)`，當 funder 不在排除清單時，union(addr, funder)
- 也加入：**同一筆批量轉帳的所有 receiver**（multisend / disperse 模式）

### Step 3：分群結果寫入
寫入 `address_clusters` 表：
```
cluster_id, funder, member_count, total_in_usd, suspicion_score
```

並 backfill `addresses.cluster_id`。

## 可疑度評分（suspicion_score 0-100）

加權加總以下信號：

| 信號 | 權重 |
|------|------|
| 成員數 ≥ 10 | +15 |
| 成員數 ≥ 50 | +15（累計） |
| 所有成員首次活動時間集中在 1 小時內 | +20 |
| funder 的上游是 mixer | +25 |
| cluster 內有命中 `cron_interval` 規則的地址 | +15 |
| cluster 內地址多為僅與本平台互動的「乾淨地址」 | +10 |

> 80 分以上視為高度可疑機器人實體。

## 已知限制

1. **CEX 直充誤判**：如果使用者直接從 CEX 出金到多個錢包，看起來像同源但其實是不同人。所以一定要排除 CEX。
2. **多跳資金分發**：操盤者可能用兩跳（A → B → C₁..Cₙ）來規避一階分群。對策：union-find 跑兩輪，第二輪把 B 也視為 funder。
3. **誤判洗牌**：被當作 funder 的地址若本身也是受害者（被釣魚的人），需要人工 review。產出 `bot_clusters.csv` 時保留 `notes` 欄位給 reviewer。

## 跑法

```bash
python -m analysis.clustering --platform <name>
# 寫入 address_clusters 與 addresses.cluster_id
```
