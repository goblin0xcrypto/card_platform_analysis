# {{platform}} 鏈上分析報告

> 觀察期間：{{start_date}} ~ {{end_date}}
> 產出時間：{{generated_at}}
> 鏈別：{{chain}}

---

## 一、平台概覽

{{from platform_profile.md}}

## 二、健康度指標

- 總開包數：{{total_pack_opens}}
- 總開包金額：${{total_volume_usd}}
- 獨立開包地址數：{{unique_openers}}
- D7 留存率：{{d7_retention}}
- NFT 30 天沉澱率：{{sinking_rate_30d}}

## 三、Deployer 與官方金流

{{from flow_overview.md}}

## 四、機器人實體

- 偵測到 cluster 數：{{cluster_count}}
- 高度可疑（score ≥ 80）：{{high_risk_clusters}}
- 涉及地址總數：{{bot_address_count}}
- 機器人開包佔比：{{bot_pack_open_ratio}}

詳細見 `bot_clusters.csv`。

## 五、自刷量

{{from wash_trading.md}}

- 可疑成交佔比：**{{wash_volume_ratio}}**

## 六、NFT 流向

| 狀態 | 數量 | 佔比 |
|------|------|------|
| burn | | |
| staked | | |
| locked_in_official | | |
| cluster_concentrated | | |
| retail_long_hold | | |
| marketplace_listed | | |

前 50 地址持有佔比：{{top50_share}}

## 七、紅旗結論

{{from red_flags.md}}

---

## 附錄：方法說明
- 資料來源：on-chain（RPC + trace）
- 規則命中閾值見 `platforms/{{platform}}/config.yaml`
- 分群演算法：見 `docs/clustering.md`
