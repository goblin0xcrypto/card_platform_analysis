# Flow Overview — renaiss

> run_analysis.py 產出。觀察期間 2025-11-06 → 2026-07-24

## 開包總體

| 指標 | 數值 |
|------|------|
| 總開包次數 | 236,106 |
| 獨立開包地址數 | 4,191 |
| 平均每人 | 56.3 包 |
| pack_pay (USDT) | $23,671,124.06 (244436 筆) |
| payout (USDT) | $20,950,001.46 (400129 筆) |
| **House net** | **$2,721,122.60** |
| FreePlay coverage gap | 0 opens (0.0%) |
| NFT mints | 186,884 |
| NFT transfers | 1,064,797 |
| marketplace_events | 0 |

## 集中度

- Top 1 opener 吃 **0.9%**
- Top 5 openers 佔 **3.5%**
- Top 10 openers 佔 **5.9%**

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

- 全平台 ROI = 0.885（house edge ≈ +11.50%）
- ROI bucket `0.50-0.69` → 548 players, paid_total=$774,123
- ROI bucket `0.70-0.84` → 909 players, paid_total=$4,100,967
- ROI bucket `0.85-0.99` → 1401 players, paid_total=$17,458,331
- ROI bucket `1.00-1.49` → 318 players, paid_total=$783,407
- ROI bucket `<0.50` → 960 players, paid_total=$480,645
- ROI bucket `>=1.50` → 117 players, paid_total=$73,651

**系統性贏家 (paid ≥ $500 且 ROI ≥ 0.9):**

| Player | Paid | Received | ROI | # opens |
|--------|------|----------|-----|---------|
| `0x00665c9aa4bab6b770daf533ad159cef94ce4864` | $800 | $3,343 | 4.178 | 8 |
| `0xacc198fcb465081ff4e900df31f796eb5b1158b7` | $1,482 | $4,815 | 3.249 | 15 |
| `0x63db8c147951a1539eae6fdfd86e55501d096589` | $594 | $1,770 | 2.980 | 6 |
| `0x423c8c2a0cfd8c021ecc057ab307c4738ed3d3ea` | $520 | $1,515 | 2.914 | 10 |
| `0xf0ef70fe3512f96564fec16e9a0a91f70d36870c` | $3,190 | $8,811 | 2.762 | 20 |
| `0xef5411e433a9756ef9f488bba33387d8fa8cbfc1` | $576 | $1,340 | 2.326 | 12 |
| `0x21901cbfc988120c97f102241acedc43097ee5a9` | $576 | $1,322 | 2.295 | 12 |
| `0x10376bfc9823a8d85bd2f2033e1a447385368c79` | $630 | $1,395 | 2.214 | 2 |
| `0xa80ce42f379ef52c2e189c1e601a8d515f18d70a` | $3,436 | $7,386 | 2.150 | 8 |
| `0x4f7211fb6a671f37b7aadc279b1b958e1a16863e` | $504 | $1,059 | 2.100 | 8 |

## Marketplace

- 無資料（contracts.marketplace 未設置或無事件）

## 自動 bot 標記

- `burst`: 510 hits
- `cron_interval`: 35 hits
- **共標記 542 個地址**

## Treasury 資金流

**Top 5 非玩家收款方:**

- `0x8894e0a0c962cb723c1976a4421c95949be2d4e3` n=30 total=$2,335,131
- `0xae3e7268ef5a062946216a44f58a8f685ffd11d0` n=3605 total=$285,794
- `0xaab5f5fa75437a6e9e7004c12c9c56cda4b4885a` n=1676 total=$157,740
- `0x94e7732b0b2e7c51ffd0d56580067d9c2e2b7910` n=2 total=$120,000
- `0x7f6c734242316eeca4a55cda1b4514f639ba2eda` n=1 total=$50,000
