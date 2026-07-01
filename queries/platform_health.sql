-- params: :platform_id, :start_date, :end_date
-- 模組 A：平台健康度
-- 輸出：daily_metrics, retention, nft_sinking_rate, price_vs_floor

-- A1. 每日開包數 / 開包金額 / 獨立開包地址數
WITH daily AS (
    SELECT
        DATE(block_time)                AS day,
        COUNT(*)                        AS pack_opens,
        SUM(price_usd)                  AS volume_usd,
        COUNT(DISTINCT opener)          AS unique_openers
    FROM pack_opens
    WHERE platform_id = :platform_id
      AND block_time BETWEEN :start_date AND :end_date
    GROUP BY DATE(block_time)
)
SELECT * FROM daily ORDER BY day;

-- A2. 留存率 D1 / D7 / D30
-- 以「首次開包日」為 cohort，看後續是否仍在開包
WITH first_seen AS (
    SELECT opener, MIN(DATE(block_time)) AS first_day
    FROM pack_opens
    WHERE platform_id = :platform_id
    GROUP BY opener
),
activity AS (
    SELECT DISTINCT opener, DATE(block_time) AS day
    FROM pack_opens
    WHERE platform_id = :platform_id
)
SELECT
    fs.first_day,
    COUNT(DISTINCT fs.opener)                                                       AS cohort_size,
    COUNT(DISTINCT CASE WHEN a.day = fs.first_day + 1  THEN fs.opener END)::FLOAT / NULLIF(COUNT(DISTINCT fs.opener),0) AS d1,
    COUNT(DISTINCT CASE WHEN a.day = fs.first_day + 7  THEN fs.opener END)::FLOAT / NULLIF(COUNT(DISTINCT fs.opener),0) AS d7,
    COUNT(DISTINCT CASE WHEN a.day = fs.first_day + 30 THEN fs.opener END)::FLOAT / NULLIF(COUNT(DISTINCT fs.opener),0) AS d30
FROM first_seen fs
LEFT JOIN activity a ON a.opener = fs.opener
GROUP BY fs.first_day;

-- A3. NFT 沉澱率：mint 後 30 天仍未轉手的比例
WITH minted AS (
    SELECT contract, token_id, MIN(block_time) AS mint_time
    FROM mints
    WHERE platform_id = :platform_id
    GROUP BY contract, token_id
),
moved AS (
    SELECT DISTINCT contract, token_id
    FROM nft_transfers
    WHERE platform_id = :platform_id
      AND block_time <= NOW() - INTERVAL '30 days'
)
SELECT
    COUNT(*) FILTER (WHERE m.contract IS NULL)::FLOAT / COUNT(*) AS sinking_rate_30d
FROM minted mn
LEFT JOIN moved m ON m.contract = mn.contract AND m.token_id = mn.token_id
WHERE mn.mint_time <= NOW() - INTERVAL '30 days';

-- A4. 開包價中位數 vs 二級市場底價（同類比較）
SELECT
    DATE(po.block_time) AS day,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY po.price_usd) AS pack_price_p50,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY mt.price_usd) AS market_price_p50
FROM pack_opens po
LEFT JOIN marketplace_trades mt
       ON mt.platform_id = po.platform_id
      AND DATE(mt.block_time) = DATE(po.block_time)
WHERE po.platform_id = :platform_id
GROUP BY DATE(po.block_time)
ORDER BY day;
