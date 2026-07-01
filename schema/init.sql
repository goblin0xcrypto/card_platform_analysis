-- Card Platform Analysis - Postgres schema
-- 所有表加 platform_id 維度，schema 通用於所有平台

CREATE TABLE IF NOT EXISTS platforms (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    chain           TEXT NOT NULL,                -- ethereum / solana / ton / sui ...
    launch_block    BIGINT,
    profile_json    JSONB,                        -- 階段 0 收集的所有情報
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS contracts (
    platform_id     INT REFERENCES platforms(id),
    address         TEXT NOT NULL,
    type            TEXT NOT NULL,                -- pack_opening / marketplace / staking / token
    deployer        TEXT,
    deploy_block    BIGINT,
    deploy_tx       TEXT,
    abi_json        JSONB,
    PRIMARY KEY (platform_id, address)
);

CREATE TABLE IF NOT EXISTS addresses (
    platform_id     INT REFERENCES platforms(id),
    address         TEXT NOT NULL,
    label           TEXT,                         -- official_treasury / suspected_bot / unknown ...
    source          TEXT,                         -- public / rule_hit / manual
    cluster_id      INT,                          -- 同 funder 分群結果
    first_seen      TIMESTAMPTZ,
    last_seen       TIMESTAMPTZ,
    PRIMARY KEY (platform_id, address)
);
CREATE INDEX IF NOT EXISTS idx_addresses_cluster ON addresses(platform_id, cluster_id);
CREATE INDEX IF NOT EXISTS idx_addresses_label   ON addresses(platform_id, label);

CREATE TABLE IF NOT EXISTS pack_opens (
    platform_id     INT REFERENCES platforms(id),
    tx_hash         TEXT NOT NULL,
    log_index       INT NOT NULL,
    block_time      TIMESTAMPTZ NOT NULL,
    block_number    BIGINT NOT NULL,
    opener          TEXT NOT NULL,
    pack_id         TEXT,
    price_raw       NUMERIC(78,0),
    price_token     TEXT,                         -- USDT / USDC / ETH ...
    price_usd       NUMERIC(20,6),                -- freeze 當下匯率
    quantity        INT DEFAULT 1,                -- 此列代表幾包:事件型平台=1;金額推導(unverified)=amount/單價(整買展開)
    ingested_at     TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (platform_id, tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS idx_pack_opens_opener ON pack_opens(platform_id, opener);
CREATE INDEX IF NOT EXISTS idx_pack_opens_time   ON pack_opens(platform_id, block_time);

CREATE TABLE IF NOT EXISTS mints (
    platform_id     INT REFERENCES platforms(id),
    tx_hash         TEXT NOT NULL,
    log_index       INT NOT NULL,
    block_time      TIMESTAMPTZ NOT NULL,
    block_number    BIGINT NOT NULL,
    minter          TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    contract        TEXT NOT NULL,
    pack_open_tx    TEXT,                         -- 串回 pack_opens
    ingested_at     TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (platform_id, tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS idx_mints_token ON mints(platform_id, contract, token_id);
CREATE INDEX IF NOT EXISTS idx_mints_pack  ON mints(platform_id, pack_open_tx);

CREATE TABLE IF NOT EXISTS payments (
    platform_id     INT REFERENCES platforms(id),
    tx_hash         TEXT NOT NULL,
    log_index       INT NOT NULL,
    block_time      TIMESTAMPTZ NOT NULL,
    block_number    BIGINT NOT NULL,
    from_addr       TEXT NOT NULL,
    to_addr         TEXT NOT NULL,
    token           TEXT NOT NULL,
    amount_raw      NUMERIC(78,0),
    amount_usd      NUMERIC(20,6),
    direction       TEXT,                         -- pack_pay / royalty / fee / payout / refund
    ingested_at     TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (platform_id, tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS idx_payments_from ON payments(platform_id, from_addr);
CREATE INDEX IF NOT EXISTS idx_payments_to   ON payments(platform_id, to_addr);

CREATE TABLE IF NOT EXISTS nft_transfers (
    platform_id     INT REFERENCES platforms(id),
    tx_hash         TEXT NOT NULL,
    log_index       INT NOT NULL,
    block_time      TIMESTAMPTZ NOT NULL,
    block_number    BIGINT NOT NULL,
    contract        TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    from_addr       TEXT NOT NULL,
    to_addr         TEXT NOT NULL,
    price_usd       NUMERIC(20,6),
    marketplace     TEXT,                         -- opensea / blur / native / null(p2p)
    ingested_at     TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (platform_id, tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS idx_nft_transfers_token ON nft_transfers(platform_id, contract, token_id);

CREATE TABLE IF NOT EXISTS marketplace_trades (
    platform_id     INT REFERENCES platforms(id),
    tx_hash         TEXT NOT NULL,
    log_index       INT NOT NULL,
    block_time      TIMESTAMPTZ NOT NULL,
    contract        TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    buyer           TEXT NOT NULL,
    seller          TEXT NOT NULL,
    price_usd       NUMERIC(20,6),
    fee_usd         NUMERIC(20,6),
    royalty_usd     NUMERIC(20,6),
    ingested_at     TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (platform_id, tx_hash, log_index)
);

-- Marketplace 完整事件流（list / bid / buy / sellback / redeem / delist / price-update）。
-- 給「非 ERC-721 字串 key」(e.g. mnstr.xyz 用 certNumber) 的市場用。
CREATE TABLE IF NOT EXISTS marketplace_events (
    platform_id     INT REFERENCES platforms(id),
    tx_hash         TEXT NOT NULL,
    log_index       INT NOT NULL,
    block_time      TIMESTAMPTZ NOT NULL,
    block_number    BIGINT NOT NULL,
    contract        TEXT NOT NULL,            -- marketplace 合約地址
    kind            TEXT NOT NULL,            -- card_listed / card_bought / bid_placed / ...
    item_key        TEXT NOT NULL,            -- cert_number 或 pack_id（string key）
    actor           TEXT,                     -- buyer / bidder / seller / redeemer
    counterparty    TEXT,
    price_raw       NUMERIC(78,0),
    price_usd       NUMERIC(20,6),
    quantity        INT,                      -- pack 事件才用
    ingested_at     TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (platform_id, tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS idx_market_events_item  ON marketplace_events(platform_id, item_key);
CREATE INDEX IF NOT EXISTS idx_market_events_actor ON marketplace_events(platform_id, actor);
CREATE INDEX IF NOT EXISTS idx_market_events_kind  ON marketplace_events(platform_id, kind, block_time);

-- 通用「所有交易紀錄」表：直接存 Etherscan account 端點原始交易
-- （native txlist / internal / ERC-20 / ERC-721 / ERC-1155），不依賴合約 ABI。
-- 用於 unverified 合約 / 非標準事件平台，或想保留完整鏈上足跡時。
CREATE TABLE IF NOT EXISTS raw_transactions (
    platform_id     INT REFERENCES platforms(id),
    address         TEXT NOT NULL,            -- 抓取此列所針對的 config 地址
    kind            TEXT NOT NULL,            -- native / internal / erc20 / erc721 / erc1155
    tx_hash         TEXT NOT NULL,
    seq             INT NOT NULL,             -- 同 (address,kind,tx_hash) 內的序號（抓取順序，穩定可重入）
    block_number    BIGINT NOT NULL,
    block_time      TIMESTAMPTZ NOT NULL,
    from_addr       TEXT,
    to_addr         TEXT,
    value_raw       NUMERIC(78,0),            -- native/internal:wei；erc20:amount；erc1155:tokenValue
    token           TEXT,                     -- token 合約地址（native/internal 為 NULL）
    token_symbol    TEXT,
    token_decimals  INT,
    token_id        TEXT,                     -- erc721/erc1155 的 tokenID
    ingested_at     TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (platform_id, address, kind, tx_hash, seq)
);
CREATE INDEX IF NOT EXISTS idx_raw_tx_hash  ON raw_transactions(platform_id, tx_hash);
CREATE INDEX IF NOT EXISTS idx_raw_tx_from  ON raw_transactions(platform_id, from_addr);
CREATE INDEX IF NOT EXISTS idx_raw_tx_to    ON raw_transactions(platform_id, to_addr);
CREATE INDEX IF NOT EXISTS idx_raw_tx_token ON raw_transactions(platform_id, token);
CREATE INDEX IF NOT EXISTS idx_raw_tx_time  ON raw_transactions(platform_id, block_time);

-- 物化視圖：地址日金流，加速圖表
CREATE MATERIALIZED VIEW IF NOT EXISTS address_flows AS
SELECT
    platform_id,
    address,
    day,
    SUM(in_usd)  AS in_usd,
    SUM(out_usd) AS out_usd,
    SUM(in_usd) - SUM(out_usd) AS net_usd
FROM (
    SELECT platform_id, to_addr   AS address, DATE(block_time) AS day, amount_usd AS in_usd,  0::NUMERIC AS out_usd FROM payments
    UNION ALL
    SELECT platform_id, from_addr AS address, DATE(block_time) AS day, 0::NUMERIC AS in_usd,  amount_usd AS out_usd FROM payments
) t
GROUP BY platform_id, address, day;

CREATE INDEX IF NOT EXISTS idx_address_flows ON address_flows(platform_id, address, day);

-- 規則命中結果（用於審計：為什麼某地址被標為機器人）
CREATE TABLE IF NOT EXISTS bot_flags (
    platform_id     INT REFERENCES platforms(id),
    address         TEXT NOT NULL,
    rule_id         TEXT NOT NULL,                -- e.g. 'cron_interval' / 'shared_funder'
    hit_at          TIMESTAMPTZ DEFAULT NOW(),
    evidence_json   JSONB,                        -- 留具體證據以便回溯
    PRIMARY KEY (platform_id, address, rule_id)
);

-- 已知地址（CEX / bridge / mixer / infra），跨平台共用
CREATE TABLE IF NOT EXISTS known_addresses (
    address         TEXT PRIMARY KEY,         -- lower-case
    chain           TEXT,                     -- ethereum / base / megaeth ... 留 NULL 表跨鏈通用
    label           TEXT NOT NULL,            -- e.g. 'Binance hot wallet', 'Across bridge'
    category        TEXT NOT NULL,            -- cex / bridge / mixer / infra / oracle / mev / other
    source          TEXT,                     -- 'arkham' / 'manual' / 'etherscan' / ...
    notes           TEXT,
    added_at        TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_known_addresses_cat ON known_addresses(category);

-- Funder cluster 結果
CREATE TABLE IF NOT EXISTS address_clusters (
    platform_id     INT REFERENCES platforms(id),
    cluster_id      INT NOT NULL,
    funder          TEXT,                         -- 共同上游
    member_count    INT,
    total_in_usd    NUMERIC(20,6),
    suspicion_score NUMERIC(5,2),                 -- 0-100
    PRIMARY KEY (platform_id, cluster_id)
);
