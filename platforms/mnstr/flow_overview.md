# Flow Overview — mnstr

> run_analysis.py 產出。觀察期間 2026-04-27 → 2026-06-28

## 開包總體

| 指標 | 數值 |
|------|------|
| 總開包次數 | 93,767 |
| 獨立開包地址數 | 1,430 |
| 平均每人 | 65.6 包 |
| pack_pay (USDm) | $17,343,374.05 (71654 筆) |
| payout (USDm) | $15,800,282.02 (116674 筆) |
| **House net** | **$304,837.03** |
| FreePlay coverage gap | 491 opens (0.5%) |
| NFT mints | 0 |
| NFT transfers | 0 |
| marketplace_events | 25,743 |

## 集中度

- Top 1 opener 吃 **9.7%**
- Top 5 openers 佔 **26.1%**
- Top 10 openers 佔 **38.4%**

| Opener | Packs |
|--------|-------|
| `0x3c93a6fa880d6437255b924de37f532d823039e1` | 9120 |
| `0x646cce4dbc0ab1d37850722f311682c09c4903b6` | 7492 |
| `0x1657fc18749f7dcff9a07ce63206fb431f85d73f` | 2771 |
| `0x198bce5dc6736db79ff5a62bdc9433025a86f769` | 2597 |
| `0x242fe6749f65f494895c1d9f90c34ebb81e9d48d` | 2515 |
| `0xb51b16e99a54062dd075e17a8a2ef296d3d74a47` | 2501 |
| `0x28e1c17550b292729072cae8914622adb7a94159` | 2467 |
| `0x45b6656031571b2d7587e762ab8a3f448137134b` | 2432 |
| `0xb13b5338dc564504bce223d90b2deb7d51642e8d` | 2259 |
| `0x07cce44c4fde27a5debe44091c5d4f1f1114d190` | 1821 |

## Sellback / EV

- 全平台 ROI = 0.981（house edge ≈ +1.95%）
- ROI bucket `0.50-0.69` → 314 players, paid_total=$297,176
- ROI bucket `0.70-0.84` → 387 players, paid_total=$1,178,578
- ROI bucket `0.85-0.99` → 378 players, paid_total=$11,687,723
- ROI bucket `1.00-1.49` → 189 players, paid_total=$2,140,175
- ROI bucket `<0.50` → 143 players, paid_total=$288,411
- ROI bucket `>=1.50` → 40 players, paid_total=$42,464

**系統性贏家 (paid ≥ $500 且 ROI ≥ 0.9):**

| Player | Paid | Received | ROI | # opens |
|--------|------|----------|-----|---------|
| `0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f` | $26,082 | $834,997 | 32.014 | 8 |
| `0x7bbfca9de990af623ccdcea53d494b994e183d40` | $750 | $2,776 | 3.701 | 15 |
| `0xd7949bfdbb1169d7ce2fe675d3780f2f337fca7b` | $1,550 | $3,830 | 2.471 | 7 |
| `0x35995212786db77e95af4872eee5b5c54ddcd75d` | $1,350 | $3,109 | 2.303 | 27 |
| `0x548a31a78e40cbbee70ebf59116eb1a62925b092` | $1,850 | $3,764 | 2.035 | 33 |
| `0x27923c5757d5a3e265867759b83dae7a6ee2e0c0` | $1,350 | $2,713 | 2.010 | 9 |
| `0x9bbb9e1e63b0b1b57e43932a95ed46154a316328` | $550 | $1,070 | 1.945 | 3 |
| `0x99d5b50019af40aa6aed553e80833f0d43f6db60` | $1,400 | $2,611 | 1.865 | 11 |
| `0x7a2d3b42c66aac173b1bc813805318a52c1d467a` | $700 | $1,230 | 1.758 | 14 |
| `0xff7728f90a8cb9e20b63c22794a7510c829fdd3c` | $1,060 | $1,659 | 1.566 | 13 |

## Marketplace

- 事件總數 25,743（成交 156，掛單 0, 改價 25586, 出價 0）
- 成交/改價 = 0.0061（流動性指標）

## 自動 bot 標記

- `burst`: 359 hits
- `cron_interval`: 2 hits
- **共標記 359 個地址**

## Treasury 資金流

**Top 5 非玩家收款方:**

- `0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f` n=34 total=$834,997 [known]
- `0x1f788712a090f70d0933668484546572d440e4fc` n=94 total=$35,566
- `0xbdaa0385ae984170c87950d079823754536e3248` n=3 total=$30,000
- `0x3b6b2a0cc5be24f04e9239e3340e2019c2b5af88` n=2 total=$11,182
- `0xdda0363d87d97a677eea4f180337886fd9cc6593` n=49 total=$6,185
