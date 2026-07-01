-- params: :platform_id, :price_deviation_threshold (default 0.4), :cycle_max_hops (default 5)
-- 模組 D：自刷量偵測

-- D1. 同 cluster 內 buy-sell（最強信號）
SELECT
    mt.tx_hash,
    mt.block_time,
    mt.buyer,
    mt.seller,
    mt.price_usd,
    ab.cluster_id AS buyer_cluster,
    asl.cluster_id AS seller_cluster
FROM marketplace_trades mt
JOIN addresses ab  ON ab.platform_id = mt.platform_id AND ab.address = mt.buyer
JOIN addresses asl ON asl.platform_id = mt.platform_id AND asl.address = mt.seller
WHERE mt.platform_id = :platform_id
  AND ab.cluster_id IS NOT NULL
  AND ab.cluster_id = asl.cluster_id;

-- D2. A→B→A 環狀（含中介，最多 :cycle_max_hops 跳）
WITH RECURSIVE chain AS (
    SELECT
        contract, token_id,
        from_addr AS origin,
        to_addr   AS current,
        block_time,
        1 AS hops,
        ARRAY[from_addr, to_addr] AS path
    FROM nft_transfers
    WHERE platform_id = :platform_id

    UNION ALL

    SELECT
        c.contract, c.token_id, c.origin, n.to_addr, n.block_time, c.hops + 1,
        c.path || n.to_addr
    FROM nft_transfers n
    JOIN chain c
      ON n.contract = c.contract
     AND n.token_id = c.token_id
     AND n.from_addr = c.current
     AND n.block_time > c.block_time
    WHERE n.platform_id = :platform_id
      AND c.hops < :cycle_max_hops
      AND NOT (n.to_addr = ANY(c.path[2:]))    -- 防止非環的重複
)
SELECT contract, token_id, origin AS wash_address, path, hops
FROM chain
WHERE current = origin
  AND hops >= 2;

-- D3. 成交價偏離中位數
WITH median_per_collection AS (
    SELECT
        contract,
        DATE(block_time) AS day,
        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price_usd) AS p50
    FROM marketplace_trades
    WHERE platform_id = :platform_id
    GROUP BY contract, DATE(block_time)
)
SELECT
    mt.tx_hash, mt.contract, mt.token_id, mt.buyer, mt.seller, mt.price_usd, m.p50,
    ABS(mt.price_usd - m.p50) / NULLIF(m.p50, 0) AS deviation
FROM marketplace_trades mt
JOIN median_per_collection m
  ON m.contract = mt.contract AND m.day = DATE(mt.block_time)
WHERE mt.platform_id = :platform_id
  AND m.p50 > 0
  AND ABS(mt.price_usd - m.p50) / m.p50 > :price_deviation_threshold;

-- D4. fee/royalty 回流：賣方淨流出 < 名義價（平台對自家地址退費）
SELECT
    mt.tx_hash,
    mt.seller,
    mt.price_usd                                AS gross_price,
    COALESCE(refund.refund_usd, 0)              AS refund_usd,
    mt.price_usd - COALESCE(refund.refund_usd, 0) AS net_outflow,
    1.0 - (mt.price_usd - COALESCE(refund.refund_usd, 0)) / NULLIF(mt.price_usd, 0) AS refund_ratio
FROM marketplace_trades mt
LEFT JOIN (
    SELECT tx_hash, to_addr, SUM(amount_usd) AS refund_usd
    FROM payments
    WHERE platform_id = :platform_id
      AND direction   = 'refund'
    GROUP BY tx_hash, to_addr
) refund ON refund.tx_hash = mt.tx_hash AND refund.to_addr = mt.seller
WHERE mt.platform_id = :platform_id
  AND COALESCE(refund.refund_usd, 0) > 0
ORDER BY refund_ratio DESC;

-- D5. 自刷量總體估算（合併 D1 + D2 結果）
WITH suspicious_trades AS (
    SELECT tx_hash FROM marketplace_trades mt
    JOIN addresses ab  ON ab.platform_id = mt.platform_id AND ab.address = mt.buyer
    JOIN addresses asl ON asl.platform_id = mt.platform_id AND asl.address = mt.seller
    WHERE mt.platform_id = :platform_id
      AND ab.cluster_id IS NOT NULL
      AND ab.cluster_id = asl.cluster_id
)
SELECT
    COUNT(*)                                                   AS suspicious_count,
    SUM(price_usd)                                             AS suspicious_volume_usd,
    SUM(price_usd) / NULLIF(
        (SELECT SUM(price_usd) FROM marketplace_trades WHERE platform_id = :platform_id), 0
    )                                                          AS wash_volume_ratio
FROM marketplace_trades
WHERE platform_id = :platform_id
  AND tx_hash IN (SELECT tx_hash FROM suspicious_trades);
