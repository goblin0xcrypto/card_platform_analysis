-- params: :platform_id
-- 模組 E：NFT 最終流向
-- 對每張 NFT 分類成：burn / staked / locked_in_official / 散戶長持 / 集中在 cluster / marketplace 掛單

WITH last_holder AS (
    SELECT DISTINCT ON (contract, token_id)
        contract, token_id, to_addr AS holder, block_time AS last_move
    FROM nft_transfers
    WHERE platform_id = :platform_id
    ORDER BY contract, token_id, block_time DESC
),
classified AS (
    SELECT
        lh.contract,
        lh.token_id,
        lh.holder,
        CASE
            WHEN lh.holder IN ('0x0000000000000000000000000000000000000000',
                               '0x000000000000000000000000000000000000dEaD')
                 THEN 'burn'
            WHEN a.label = 'staking_contract'    THEN 'staked'
            WHEN a.label = 'official_treasury'   THEN 'locked_in_official'
            WHEN a.label = 'marketplace_escrow'  THEN 'marketplace_listed'
            WHEN a.cluster_id IS NOT NULL        THEN 'cluster_concentrated'
            ELSE 'retail_long_hold'
        END AS endstate
    FROM last_holder lh
    LEFT JOIN addresses a
      ON a.platform_id = :platform_id AND a.address = lh.holder
)
SELECT
    endstate,
    COUNT(*)                                                 AS nft_count,
    COUNT(*) * 1.0 / SUM(COUNT(*)) OVER ()                   AS pct,
    COUNT(DISTINCT holder)                                   AS distinct_holders
FROM classified
GROUP BY endstate
ORDER BY nft_count DESC;

-- 集中度：前 50 個地址持有的 NFT 佔比
WITH last_holder2 AS (
    SELECT DISTINCT ON (contract, token_id)
        contract, token_id, to_addr AS holder
    FROM nft_transfers
    WHERE platform_id = :platform_id
    ORDER BY contract, token_id, block_time DESC
),
holder_counts AS (
    SELECT holder, COUNT(*) AS n
    FROM last_holder2
    GROUP BY holder
    ORDER BY n DESC
    LIMIT 50
)
SELECT
    SUM(n) AS top50_holdings,
    SUM(n) * 1.0 / (SELECT COUNT(*) FROM last_holder2) AS top50_share
FROM holder_counts;
