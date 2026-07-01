"""
derive_from_raw.py — 把 raw_transactions（bronze 原始層）推導成語意 domain 表（silver）。

給「unverified 合約、拿不到事件 ABI」的平台用（如 renaiss）。轉帳類事實（USDT 付款、
NFT mint/轉移、ERC-721 token_id 市場成交）都是標準轉帳，不需要 ABI 即可重建。

目前實作：
  marketplace_trades  ← ERC-721 token_id 市場成交（買方付 USDT 進 marketplace + 同 tx 有卡牌轉移）

設計上可逐步擴充 payments / mints / nft_transfers / pack_opens（皆可從 raw 推導）。

用法：
  .venv/bin/python derive_from_raw.py --platform renaiss
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from ingest import _addr_norm, _as_addr_list, get_conn, load_config
from tools.bootstrap import load_env_file

ZERO = "0x0000000000000000000000000000000000000000"


def _platform_addrs(cfg: dict) -> list[str]:
    """平台自家地址(packs + marketplace + nft + token + deployers + official_wallets
    + infrastructure)。pack_opens 要排除這些之間的內部轉帳,只留外部玩家付款。"""
    addrs: list[str] = []
    contracts = cfg.get("contracts") or {}
    for typ in ("pack_opening", "marketplace", "nft", "token", "staking"):
        addrs += _as_addr_list(contracts.get(typ))
    addrs += [_addr_norm(a) for a in (cfg.get("deployers") or []) if a]
    addrs += [_addr_norm(a) for a in (cfg.get("infrastructure_addresses") or []) if a]
    for v in (cfg.get("official_wallets") or {}).values():
        if isinstance(v, list):
            addrs += [_addr_norm(a) for a in v if a]
        elif v:
            addrs.append(_addr_norm(v))
    return list({a for a in addrs if a})


def _excluded_price_pairs(cfg: dict):
    """exclude.pack_prices → (epacks[], eraws[]) 兩個對位陣列,代表要排除的
    (pack 地址, USDT raw 金額) 配對。raw = price * 1e18(USDT 18 位小數)。"""
    epacks: list[str] = []
    eraws: list[int] = []
    for rule in ((cfg.get("exclude") or {}).get("pack_prices") or []):
        p = _addr_norm(rule.get("pack"))
        if not p:
            continue
        for price in (rule.get("prices") or []):
            epacks.append(p)
            eraws.append(int(round(float(price) * 10**18)))
    return epacks, eraws


def derive_payments(conn, pid: int, cfg: dict) -> None:
    """所有 USDT 轉帳 → payments。direction 分類(pack_pay/payout 給下游 ROI 分析依賴)。
    raw 的 erc20 只會收錄「與平台地址互動」的轉帳,因此皆屬平台金流。"""
    usdt = _addr_norm(cfg["contracts"].get("token"))
    packs = _as_addr_list(cfg["contracts"].get("pack_opening"))
    mkt = _addr_norm(cfg["contracts"].get("marketplace"))
    plat = _platform_addrs(cfg)
    if not usdt:
        print("[derive] 缺 token,略過 payments"); return
    with conn.cursor() as c:
        c.execute("DELETE FROM payments WHERE platform_id=%s", (pid,))
        c.execute("""
            WITH u AS (
                SELECT DISTINCT tx_hash, block_number, block_time, from_addr, to_addr, value_raw
                FROM raw_transactions
                WHERE platform_id=%(pid)s AND kind='erc20' AND token=%(usdt)s
            ),
            r AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY tx_hash
                         ORDER BY from_addr,to_addr,value_raw)-1 AS li FROM u)
            INSERT INTO payments(platform_id, tx_hash, log_index, block_time, block_number,
                from_addr, to_addr, token, amount_raw, amount_usd, direction)
            SELECT %(pid)s, tx_hash, li, block_time, block_number, from_addr, to_addr, %(usdt)s,
                   value_raw, (value_raw/1e18)::numeric(20,6),
                   CASE WHEN from_addr = ANY(%(plat)s) AND to_addr = ANY(%(plat)s) THEN 'internal'
                        WHEN to_addr   = ANY(%(packs)s) THEN 'pack_pay'
                        WHEN from_addr = ANY(%(packs)s) THEN 'payout'
                        WHEN to_addr   = %(mkt)s        THEN 'market_in'
                        WHEN from_addr = %(mkt)s        THEN 'market_out'
                        ELSE 'treasury' END
            FROM r ON CONFLICT (platform_id, tx_hash, log_index) DO NOTHING
        """, {"pid": pid, "usdt": usdt, "packs": packs, "mkt": mkt, "plat": plat})
        c.execute("SELECT direction, COUNT(*), SUM(amount_usd) FROM payments WHERE platform_id=%s GROUP BY direction ORDER BY 2 DESC", (pid,))
        rows = c.fetchall()
    conn.commit()
    print("[derive] payments:")
    for d, n, v in rows:
        print(f"           {d:10s} {n:>8,} 筆  ${v or 0:>14,.0f}")


def derive_pack_opens(conn, pid: int, cfg: dict) -> None:
    """USDT 付進 pack ≈ 一次開包(購買)。註:bulk 一次買多包仍記一列,price 為總額。"""
    usdt = _addr_norm(cfg["contracts"].get("token"))
    packs = _as_addr_list(cfg["contracts"].get("pack_opening"))
    plat = _platform_addrs(cfg)
    epacks, eraws = _excluded_price_pairs(cfg)
    excl = cfg.get("exclude") or {}
    min_raw = int(round(float(excl.get("min_open_usd") or 0) * 10**18))
    drop_frac = bool(excl.get("drop_fractional"))
    sym = cfg.get("payment_token_symbol", "USDT")
    if not (usdt and packs):
        print("[derive] 缺 token/pack,略過 pack_opens"); return
    # 整數美元 = value_raw 可被 1e18 整除;USDT 18 位小數
    frac_cond = "AND value_raw %% 1000000000000000000 = 0" if drop_frac else ""
    with conn.cursor() as c:
        c.execute("DELETE FROM pack_opens WHERE platform_id=%s", (pid,))
        c.execute(f"""
            WITH u AS (
                SELECT DISTINCT tx_hash, block_number, block_time, from_addr, to_addr, value_raw
                FROM raw_transactions
                WHERE platform_id=%(pid)s AND kind='erc20' AND token=%(usdt)s
                  AND to_addr = ANY(%(packs)s)
                  AND from_addr <> ALL(%(plat)s)   -- 排除平台內部注資/轉帳,只留外部玩家
                  AND value_raw >= %(minraw)s       -- 排除 dust(低於 min_open_usd)
                  {frac_cond}                        -- 排除帶小數金額(drop_fractional)
                  AND NOT EXISTS (                 -- 排除 config.exclude.pack_prices 的(pack,金額)配對
                        SELECT 1 FROM unnest(%(epacks)s::text[], %(eraws)s::numeric[]) AS e(p, rw)
                        WHERE e.p = to_addr AND e.rw = value_raw
                  )
            ),
            r AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY tx_hash
                         ORDER BY from_addr,to_addr,value_raw)-1 AS li FROM u)
            INSERT INTO pack_opens(platform_id, tx_hash, log_index, block_time, block_number,
                opener, pack_id, price_raw, price_token, price_usd)
            SELECT %(pid)s, tx_hash, li, block_time, block_number, from_addr, to_addr,
                   value_raw, %(sym)s, (value_raw/1e18)::numeric(20,6)
            FROM r ON CONFLICT (platform_id, tx_hash, log_index) DO NOTHING
        """, {"pid": pid, "usdt": usdt, "packs": packs, "sym": sym, "plat": plat,
              "epacks": epacks, "eraws": eraws, "minraw": min_raw})
        # quantity:金額推導平台的整買展開。單價 = 該 pack 該月「最常見的 6 個價位中的最小值」
        # (=基礎單價,避免整買 $480 比單買 $48 更常見時眾數誤判);金額為單價整數倍才展開,
        # 否則記 1 包(不同檔位/非標準金額)。
        c.execute("""
            WITH freq AS (
                SELECT pack_id, date_trunc('month', block_time) AS mo, price_usd,
                       ROW_NUMBER() OVER (PARTITION BY pack_id, date_trunc('month', block_time)
                                          ORDER BY COUNT(*) DESC) AS rn
                FROM pack_opens WHERE platform_id=%(pid)s AND price_usd >= 1
                GROUP BY 1,2,3
            ),
            unit AS (
                SELECT pack_id, mo, MIN(price_usd) AS u FROM freq WHERE rn <= 6 GROUP BY 1,2
            )
            UPDATE pack_opens po SET quantity = CASE
                WHEN un.u IS NULL OR un.u = 0 THEN 1
                WHEN po.price_usd >= un.u AND mod(po.price_usd, un.u) = 0
                     THEN (po.price_usd / un.u)::int
                ELSE 1 END
            FROM unit un
            WHERE po.platform_id=%(pid)s AND po.pack_id=un.pack_id
              AND date_trunc('month', po.block_time)=un.mo
        """, {"pid": pid})
        c.execute("SELECT COUNT(*), COALESCE(SUM(quantity),0), COUNT(DISTINCT opener), SUM(price_usd) FROM pack_opens WHERE platform_id=%s", (pid,))
        n, q, u, v = c.fetchone()
    conn.commit()
    print(f"[derive] pack_opens: {n:,} 筆購買 / 實際開包 {q:,} 包  獨立 opener={u:,}  總額=${v or 0:,.0f}")


def derive_mints(conn, pid: int, cfg: dict) -> None:
    """NFT from 0x0 → mints(721+1155)。"""
    with conn.cursor() as c:
        c.execute("DELETE FROM mints WHERE platform_id=%s", (pid,))
        c.execute("""
            WITH u AS (
                SELECT DISTINCT tx_hash, block_number, block_time, to_addr, token_id, token
                FROM raw_transactions
                WHERE platform_id=%(pid)s AND kind IN ('erc721','erc1155') AND from_addr=%(zero)s
            ),
            r AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY tx_hash
                         ORDER BY token, token_id, to_addr)-1 AS li FROM u)
            INSERT INTO mints(platform_id, tx_hash, log_index, block_time, block_number,
                minter, token_id, contract, pack_open_tx)
            SELECT %(pid)s, tx_hash, li, block_time, block_number, to_addr, token_id, token, NULL
            FROM r ON CONFLICT (platform_id, tx_hash, log_index) DO NOTHING
        """, {"pid": pid, "zero": ZERO})
        c.execute("SELECT COUNT(*), COUNT(DISTINCT contract), COUNT(DISTINCT minter) FROM mints WHERE platform_id=%s", (pid,))
        n, ct, mn = c.fetchone()
    conn.commit()
    print(f"[derive] mints: {n:,} 筆  合約數={ct}  獨立接收者={mn:,}")


def derive_nft_transfers(conn, pid: int, cfg: dict) -> None:
    """所有 NFT 轉移(721+1155) → nft_transfers。marketplace 欄:成交 tx 標記為市場。
    需 marketplace_trades 先建好。"""
    mkt = _addr_norm(cfg["contracts"].get("marketplace"))
    with conn.cursor() as c:
        c.execute("DELETE FROM nft_transfers WHERE platform_id=%s", (pid,))
        c.execute("""
            WITH u AS (
                SELECT DISTINCT tx_hash, block_number, block_time, token, token_id, from_addr, to_addr
                FROM raw_transactions
                WHERE platform_id=%(pid)s AND kind IN ('erc721','erc1155')
            ),
            mtx AS (SELECT DISTINCT tx_hash FROM marketplace_trades WHERE platform_id=%(pid)s),
            r AS (SELECT u.*, ROW_NUMBER() OVER (PARTITION BY u.tx_hash
                         ORDER BY token,token_id,from_addr,to_addr)-1 AS li,
                         (u.tx_hash IN (SELECT tx_hash FROM mtx)) AS is_mkt
                  FROM u)
            INSERT INTO nft_transfers(platform_id, tx_hash, log_index, block_time, block_number,
                contract, token_id, from_addr, to_addr, price_usd, marketplace)
            SELECT %(pid)s, tx_hash, li, block_time, block_number, token, token_id,
                   from_addr, to_addr, NULL, CASE WHEN is_mkt THEN %(mkt)s ELSE NULL END
            FROM r ON CONFLICT (platform_id, tx_hash, log_index) DO NOTHING
        """, {"pid": pid, "mkt": mkt})
        c.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE marketplace IS NOT NULL) FROM nft_transfers WHERE platform_id=%s", (pid,))
        n, m = c.fetchone()
    conn.commit()
    print(f"[derive] nft_transfers: {n:,} 筆(其中市場成交 {m:,})")


def derive_marketplace_trades(conn, pid: int, cfg: dict) -> None:
    mkt = _addr_norm(cfg["contracts"].get("marketplace"))
    usdt = _addr_norm(cfg["contracts"].get("token"))
    nfts = _as_addr_list(cfg["contracts"].get("nft"))
    if not (mkt and usdt and nfts):
        print("[derive] 缺 marketplace / token / nft，略過"); return

    with conn.cursor() as c:
        # 只用實際在 raw 出現過 erc721 轉移的 nft 合約（renaiss 主卡牌為 ERC-721）
        c.execute("""
            SELECT DISTINCT token FROM raw_transactions
            WHERE platform_id=%s AND kind='erc721' AND token = ANY(%s)
        """, (pid, nfts))
        nft721 = [r[0] for r in c.fetchall()]
        if not nft721:
            print("[derive] 找不到 ERC-721 卡牌合約，略過 marketplace_trades"); return

        # 乾淨重建（冪等）
        c.execute("DELETE FROM marketplace_trades WHERE platform_id=%s", (pid,))

        # 一筆成交 = 「買方付 USDT 進 marketplace」且「同 tx 有 ERC-721 卡牌轉移」。
        # price = 買方付進的 USDT；seller = marketplace 付出 USDT 最多的收款人；
        # fee = price - marketplace 該 tx 總付出（合約留下的部分）。
        # 用 DISTINCT 子查詢消除 raw 逐地址重複；多卡 tx 以 token_id 排序給 log_index。
        c.execute("""
            WITH usdt AS (
                SELECT DISTINCT tx_hash, from_addr, to_addr, value_raw, block_time
                FROM raw_transactions
                WHERE platform_id=%(pid)s AND kind='erc20' AND token=%(usdt)s
            ),
            nft AS (
                SELECT DISTINCT tx_hash, from_addr, to_addr, token_id
                FROM raw_transactions
                WHERE platform_id=%(pid)s AND kind='erc721' AND token = ANY(%(nft721)s)
            ),
            buyin AS (                       -- 買方付款進 marketplace
                SELECT tx_hash,
                       MIN(block_time) AS bt,
                       SUM(value_raw) AS price_raw,
                       (ARRAY_AGG(from_addr ORDER BY value_raw DESC))[1] AS buyer
                FROM usdt WHERE to_addr=%(mkt)s GROUP BY tx_hash
            ),
            payout AS (                      -- marketplace 付出（賣家拿最多）
                SELECT tx_hash,
                       SUM(value_raw) AS out_raw,
                       (ARRAY_AGG(to_addr ORDER BY value_raw DESC))[1] AS seller
                FROM usdt WHERE from_addr=%(mkt)s GROUP BY tx_hash
            ),
            card AS (                        -- 該 tx 成交的卡（含序號處理多卡 tx）
                SELECT tx_hash, token_id,
                       (ARRAY_AGG(from_addr))[1] AS card_from,
                       ROW_NUMBER() OVER (PARTITION BY tx_hash ORDER BY token_id) - 1 AS li
                FROM nft GROUP BY tx_hash, token_id
            )
            INSERT INTO marketplace_trades(platform_id, tx_hash, log_index, block_time,
                contract, token_id, buyer, seller, price_usd, fee_usd, royalty_usd)
            SELECT %(pid)s, b.tx_hash, c.li, b.bt, %(mkt)s, c.token_id,
                   b.buyer,
                   COALESCE(p.seller, NULLIF(c.card_from, %(mkt)s)),
                   (b.price_raw/1e18)::numeric(20,6),
                   GREATEST(b.price_raw - COALESCE(p.out_raw,0), 0)/1e18,
                   0
            FROM buyin b
            JOIN card c   ON c.tx_hash=b.tx_hash      -- 只取真的有搬卡的成交
            LEFT JOIN payout p ON p.tx_hash=b.tx_hash
            ON CONFLICT (platform_id, tx_hash, log_index) DO NOTHING
        """, {"pid": pid, "usdt": usdt, "mkt": mkt, "nft721": nft721})

        c.execute("""
            SELECT COUNT(*), COUNT(DISTINCT buyer), COUNT(DISTINCT seller),
                   SUM(price_usd), SUM(fee_usd), MIN(block_time)::date, MAX(block_time)::date
            FROM marketplace_trades WHERE platform_id=%s
        """, (pid,))
        n, nb, ns, gmv, fee, d0, d1 = c.fetchone()
    conn.commit()
    print(f"[derive] marketplace_trades: {n:,} 筆成交  買家={nb:,} 賣家={ns:,}")
    print(f"         GMV=${gmv:,.0f}  合約留存(fee)=${fee or 0:,.0f}  期間 {d0} → {d1}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True)
    ap.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    args = ap.parse_args()
    load_env_file(Path(args.env_file).expanduser())
    cfg = load_config(args.platform)

    with get_conn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT id FROM platforms WHERE name=%s", (args.platform,))
            row = c.fetchone()
        if not row:
            print(f"[derive] 找不到 platform={args.platform}，請先跑 ingest"); return 1
        pid = row[0]
        print(f"[derive] platform={args.platform} platform_id={pid}")
        derive_payments(conn, pid, cfg)
        derive_pack_opens(conn, pid, cfg)
        derive_mints(conn, pid, cfg)
        derive_marketplace_trades(conn, pid, cfg)   # nft_transfers 依賴它的 tx 標記
        derive_nft_transfers(conn, pid, cfg)
    print("[derive] done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
