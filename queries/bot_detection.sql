-- params: :platform_id, :interval_cv_threshold (default 0.1), :cluster_min_size (default 3)
-- 模組 C：機器人行為偵測
-- 規則命中結果寫入 bot_flags(platform_id, address, rule_id, evidence_json)

-- C1. cron_interval：開包時間間隔變異係數 σ/μ 極小
WITH intervals AS (
    SELECT
        opener,
        EXTRACT(EPOCH FROM (block_time - LAG(block_time) OVER (PARTITION BY opener ORDER BY block_time))) AS dt
    FROM pack_opens
    WHERE platform_id = :platform_id
),
stats AS (
    SELECT
        opener,
        COUNT(dt)            AS n,
        AVG(dt)              AS mean_dt,
        STDDEV_POP(dt)       AS sd_dt
    FROM intervals
    WHERE dt IS NOT NULL
    GROUP BY opener
    HAVING COUNT(dt) >= 10
)
INSERT INTO bot_flags (platform_id, address, rule_id, evidence_json)
SELECT
    :platform_id, opener, 'cron_interval',
    JSONB_BUILD_OBJECT('n', n, 'mean_dt', mean_dt, 'sd_dt', sd_dt, 'cv', sd_dt / NULLIF(mean_dt,0))
FROM stats
WHERE mean_dt > 0
  AND sd_dt / mean_dt < :interval_cv_threshold
ON CONFLICT DO NOTHING;

-- C2. shared_funder：資金來源為同一個分發地址
WITH first_funding AS (
    SELECT DISTINCT ON (to_addr)
        to_addr   AS funded_addr,
        from_addr AS funder,
        block_time
    FROM payments
    WHERE platform_id = :platform_id
    ORDER BY to_addr, block_time
),
funder_groups AS (
    SELECT funder, ARRAY_AGG(funded_addr) AS members, COUNT(*) AS member_count
    FROM first_funding
    GROUP BY funder
    HAVING COUNT(*) >= :cluster_min_size
)
INSERT INTO bot_flags (platform_id, address, rule_id, evidence_json)
SELECT
    :platform_id, UNNEST(members), 'shared_funder',
    JSONB_BUILD_OBJECT('funder', funder, 'cluster_size', member_count)
FROM funder_groups
ON CONFLICT DO NOTHING;

-- C3. fast_dump：NFT 收到後 N 分鐘內全部轉到同一 sink 地址
WITH receive AS (
    SELECT to_addr AS holder, contract, token_id, block_time AS recv_time
    FROM nft_transfers
    WHERE platform_id = :platform_id
),
send_out AS (
    SELECT from_addr AS holder, contract, token_id, to_addr AS sink, block_time AS send_time
    FROM nft_transfers
    WHERE platform_id = :platform_id
),
hold_times AS (
    SELECT
        r.holder,
        r.contract,
        r.token_id,
        s.sink,
        EXTRACT(EPOCH FROM (s.send_time - r.recv_time)) AS hold_seconds
    FROM receive r
    JOIN send_out s
      ON s.holder = r.holder
     AND s.contract = r.contract
     AND s.token_id = r.token_id
     AND s.send_time > r.recv_time
),
fast_dumpers AS (
    SELECT
        holder,
        COUNT(*)                                AS nft_count,
        AVG(hold_seconds)                       AS avg_hold_seconds,
        MODE() WITHIN GROUP (ORDER BY sink)     AS dominant_sink
    FROM hold_times
    GROUP BY holder
    HAVING AVG(hold_seconds) < 600              -- 10 分鐘
       AND COUNT(*) >= 5
)
INSERT INTO bot_flags (platform_id, address, rule_id, evidence_json)
SELECT
    :platform_id, holder, 'fast_dump_to_sink',
    JSONB_BUILD_OBJECT('avg_hold_s', avg_hold_seconds, 'sink', dominant_sink, 'n', nft_count)
FROM fast_dumpers
ON CONFLICT DO NOTHING;

-- C4. gas_pattern：gas price 完全一致 + nonce 連續（需 adapter 補 nonce/gas_price 欄位）
-- 此處保留 placeholder，請在 schema 擴充 pack_opens.tx_meta JSONB 後實作

-- C5. just_after_launch：永遠在新合約上線後 X 分鐘內出現
-- 同樣依賴 contracts.deploy_block 與 pack_opens.block_number diff
