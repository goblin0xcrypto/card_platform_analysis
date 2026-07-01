# Flow Overview — renaiss

> run_analysis.py 產出。觀察期間 2025-11-06 → 2026-06-28

## 開包總體

| 指標 | 數值 |
|------|------|
| 總開包次數 | 220,127 |
| 獨立開包地址數 | 3,869 |
| 平均每人 | 56.9 包 |
| pack_pay (USDT) | $19,598,545.57 (228458 筆) |
| payout (USDT) | $15,859,009.39 (292075 筆) |
| **House net** | **$3,739,536.18** |
| FreePlay coverage gap | 0 opens (0.0%) |
| NFT mints | 177,270 |
| NFT transfers | 898,321 |
| marketplace_events | 0 |

## 集中度

- Top 1 opener 吃 **0.9%**
- Top 5 openers 佔 **3.8%**
- Top 10 openers 佔 **6.3%**

| Opener | Packs |
|--------|-------|
| `0x246962b7b8cd03049677c136c99de7e72a587017` | 2071 |
| `0x3f8af0e0142f6d16e26caea7a2ec1e07a9f824e0` | 1727 |
| `0x310de74ebfcca7cc8bac64916c9cccff39604005` | 1578 |
| `0xf8a568db90b52a7f42c9a99b8b7ef96aec476cdb` | 1561 |
| `0x642fb63947a957a029dcdf82aa114216e4367561` | 1438 |
| `0xb67617a7bd531ff0611536e15a54e874a4679eee` | 1205 |
| `0xa5c3d0b8e0cfafc0bb7792fa3dcb8d7b1d57fc4f` | 1179 |
| `0xaf606e778d5338936d80b984b82fa4f57ab09b03` | 1091 |
| `0xef1752c9df544ac49ed96431e5d59915f4cdcddf` | 1044 |
| `0x7b7cad5595415bfd07098bdb687c5ed0eb25691b` | 1007 |

## Sellback / EV

- 全平台 ROI = 0.809（house edge ≈ +19.08%）
- ROI bucket `0.50-0.69` → 806 players, paid_total=$4,138,971
- ROI bucket `0.70-0.84` → 868 players, paid_total=$5,172,568
- ROI bucket `0.85-0.99` → 922 players, paid_total=$9,178,042
- ROI bucket `1.00-1.49` → 269 players, paid_total=$519,716
- ROI bucket `<0.50` → 963 players, paid_total=$533,395
- ROI bucket `>=1.50` → 103 players, paid_total=$55,853

**系統性贏家 (paid ≥ $500 且 ROI ≥ 0.9):**

| Player | Paid | Received | ROI | # opens |
|--------|------|----------|-----|---------|
| `0x63db8c147951a1539eae6fdfd86e55501d096589` | $594 | $1,770 | 2.980 | 6 |
| `0x423c8c2a0cfd8c021ecc057ab307c4738ed3d3ea` | $520 | $1,515 | 2.914 | 10 |
| `0xf0ef70fe3512f96564fec16e9a0a91f70d36870c` | $3,190 | $8,615 | 2.701 | 20 |
| `0xef5411e433a9756ef9f488bba33387d8fa8cbfc1` | $576 | $1,340 | 2.326 | 12 |
| `0x21901cbfc988120c97f102241acedc43097ee5a9` | $576 | $1,322 | 2.295 | 12 |
| `0x343df1310348f469425231ea72e7d1306dc47c0d` | $1,048 | $2,259 | 2.156 | 10 |
| `0x4f7211fb6a671f37b7aadc279b1b958e1a16863e` | $504 | $1,059 | 2.100 | 8 |
| `0x10376bfc9823a8d85bd2f2033e1a447385368c79` | $630 | $1,312 | 2.082 | 2 |
| `0x99d1fc40ce2e22783457d17c7f7e34ac7acb354d` | $1,100 | $2,159 | 1.963 | 9 |
| `0x71078115bdb2bb84a8ab24a3ea90e794808c89ca` | $880 | $1,724 | 1.959 | 4 |

## Marketplace

- 無資料（contracts.marketplace 未設置或無事件）

## 自動 bot 標記

- `burst`: 502 hits
- `cron_interval`: 34 hits
- **共標記 533 個地址**

## Treasury 資金流

**Top 5 非玩家收款方:**

- `0x8894e0a0c962cb723c1976a4421c95949be2d4e3` n=23 total=$1,932,130
- `0xae3e7268ef5a062946216a44f58a8f685ffd11d0` n=3534 total=$272,952
- `0xaab5f5fa75437a6e9e7004c12c9c56cda4b4885a` n=1676 total=$157,740
- `0x94e7732b0b2e7c51ffd0d56580067d9c2e2b7910` n=2 total=$120,000
- `0x7f6c734242316eeca4a55cda1b4514f639ba2eda` n=1 total=$50,000
