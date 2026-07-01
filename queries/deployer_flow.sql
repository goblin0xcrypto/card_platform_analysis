-- params: :platform_id, :deployer_address, :max_depth
-- 模組 B：Deployer 金流追蹤
-- 前置：執行 ingest 時已用 adapter.fetch_deployer_flow 把多跳結果寫入 payments 表
--      （或寫入專屬 flow_edges 表；本檔以 payments 簡化示意）

-- B1. Deployer 直接收益（pack_pay / royalty / fee 累計）
SELECT
    direction,
    COUNT(*)               AS tx_count,
    SUM(amount_usd)        AS total_usd
FROM payments
WHERE platform_id = :platform_id
  AND to_addr     = :deployer_address
GROUP BY direction
ORDER BY total_usd DESC;

-- B2. Deployer 提現多跳路徑（最多 :max_depth 跳，找 CEX 充值地址）
-- 思路：BFS payments 表，標記終點是否為已知 CEX
WITH RECURSIVE flow AS (
    SELECT
        from_addr, to_addr, amount_usd, tx_hash, 1 AS depth, ARRAY[from_addr, to_addr] AS path
    FROM payments
    WHERE platform_id = :platform_id
      AND from_addr   = :deployer_address

    UNION ALL

    SELECT
        p.from_addr, p.to_addr, p.amount_usd, p.tx_hash, f.depth + 1, f.path || p.to_addr
    FROM payments p
    JOIN flow f ON p.from_addr = f.to_addr
    WHERE p.platform_id = :platform_id
      AND f.depth < :max_depth
      AND NOT (p.to_addr = ANY(f.path))            -- 防環
)
SELECT
    f.path,
    f.amount_usd,
    a.label  AS endpoint_label,
    ka.category AS endpoint_known_category
FROM flow f
LEFT JOIN addresses a       ON a.platform_id = :platform_id AND a.address = f.to_addr
LEFT JOIN known_addresses ka ON ka.address  = f.to_addr     -- 共享地址字典 import 表
WHERE ka.category = 'cex' OR a.label IS NOT NULL
ORDER BY f.amount_usd DESC;

-- B3. Deployer 是否回流到「疑似機器人地址」（直接的自刷信號）
SELECT
    p.to_addr,
    SUM(p.amount_usd)            AS received_usd,
    COUNT(DISTINCT bf.rule_id)   AS bot_rule_hits,
    ARRAY_AGG(DISTINCT bf.rule_id) AS rules
FROM payments p
JOIN bot_flags bf
  ON bf.platform_id = p.platform_id
 AND bf.address     = p.to_addr
WHERE p.platform_id = :platform_id
  AND p.from_addr   = :deployer_address
GROUP BY p.to_addr
ORDER BY received_usd DESC;
