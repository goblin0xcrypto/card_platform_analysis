"""
run_analysis.py — read DB, run analysis modules, print + write reports.

Modules:
  A. health             — daily / cumulative metrics + FreePlay coverage gap
  B. concentration      — top openers + Gini-ish + single-whale flag
  C. funder_cluster     — first-funder grouping from payments
  D. bot_flags          — cron_interval / shared_funder / burst rules
  E. endstate           — where NFTs ended up (skips on platforms with no on-chain NFT)
  G. sellback           — pack_pay → payout pairing, per-player ROI, hot-potato signal
  H. marketplace        — marketplace_events analysis (mnstr-style string-key markets)
  I. deployer_flow      — top non-player recipients of treasury USDm

CLI:
  --platform NAME            (required)
  --modules health,sellback  (default: all)
  --format text|json|both    (default: both)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import yaml

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
from tools.bootstrap import load_env_file


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def load_config(platform: str) -> dict:
    with open(REPO_ROOT / "platforms" / platform / "config.yaml") as f:
        return yaml.safe_load(f)


def get_conn():
    return psycopg.connect(os.environ["DATABASE_URL"])


def get_platform_id(conn, name: str) -> int:
    with conn.cursor() as c:
        c.execute("SELECT id FROM platforms WHERE name=%s", (name,))
        r = c.fetchone()
        if not r:
            raise SystemExit(f"platform '{name}' not in DB — run ingest first")
        return r[0]


def _addr_norm(v) -> str:
    if not v:
        return ""
    if isinstance(v, int):
        return "0x" + format(v, "040x")
    return str(v).lower()


def _as_addr_list(v) -> list[str]:
    if not v:
        return []
    if isinstance(v, list):
        return [_addr_norm(a) for a in v if a]
    s = _addr_norm(v)
    return [s] if s else []


# --------------------------------------------------------------------------- #
# Context — passed to every module, lets module pull cfg/related/excluded sets
# --------------------------------------------------------------------------- #
class Ctx:
    def __init__(self, platform: str, pid: int, cfg: dict):
        self.platform = platform
        self.pid = pid
        self.cfg = cfg
        self.token_symbol = cfg.get("payment_token_symbol", "USDC")

        # treasury / deployer / pack / marketplace addresses 都不該被當 funder
        self.contract_addrs = (
            _as_addr_list(cfg["contracts"].get("pack_opening"))
            + _as_addr_list(cfg["contracts"].get("marketplace"))
            + _as_addr_list(cfg["contracts"].get("token"))
            + _as_addr_list(cfg["contracts"].get("staking"))
        )
        self.official_addrs: list[str] = []
        for v in (cfg.get("official_wallets") or {}).values():
            if isinstance(v, list):
                self.official_addrs.extend(_addr_norm(a) for a in v if a)
            elif v:
                self.official_addrs.append(_addr_norm(v))
        self.deployer_addrs = [
            _addr_norm(a) for a in (cfg.get("deployers") or []) if a
        ]
        self.infrastructure_addrs = [
            _addr_norm(a) for a in (cfg.get("infrastructure_addresses") or []) if a
        ]

        # funder cluster 要排除的：所有平台自家地址 + 已知 infra
        self.excluded_funders = list({
            *self.contract_addrs,
            *self.official_addrs,
            *self.deployer_addrs,
            *self.infrastructure_addrs,
        })

        # 沒有鏈上 NFT 的平台跳過 endstate
        self.has_onchain_nft = bool(_as_addr_list(cfg["contracts"].get("nft")))

        self.thresholds = cfg.get("thresholds") or {}


# --------------------------------------------------------------------------- #
# Module A: health (+ FreePlay coverage gap)
# --------------------------------------------------------------------------- #
def module_health(conn, ctx: Ctx) -> dict:
    out: dict = {}
    with conn.cursor() as c:
        c.execute("""
            SELECT MIN(block_time)::date, MAX(block_time)::date,
                   COUNT(*), COUNT(DISTINCT opener), SUM(price_usd)::numeric
            FROM pack_opens WHERE platform_id=%s
        """, (ctx.pid,))
        first, last, total, uniq, gmv = c.fetchone()
        out.update(first_day=first, last_day=last,
                   total_packs=total or 0, unique_openers=uniq or 0,
                   pack_gmv_usd=gmv or Decimal(0))

        c.execute("""
            SELECT direction, COUNT(*), ROUND(SUM(amount_usd)::numeric, 2)
            FROM payments WHERE platform_id=%s GROUP BY direction
        """, (ctx.pid,))
        out["payment_breakdown"] = c.fetchall()

        c.execute("SELECT COUNT(*) FROM mints WHERE platform_id=%s", (ctx.pid,))
        out["mints"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM nft_transfers WHERE platform_id=%s", (ctx.pid,))
        out["nft_transfers"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM marketplace_events WHERE platform_id=%s", (ctx.pid,))
        out["marketplace_events"] = c.fetchone()[0]

        c.execute("""
            SELECT DATE(block_time), COUNT(*) FROM pack_opens
            WHERE platform_id=%s GROUP BY 1 ORDER BY 1 DESC LIMIT 14
        """, (ctx.pid,))
        out["daily_last14"] = c.fetchall()

        # FreePlay coverage gap：pack_opens 找不到同 tx 的 pack_pay = FreePlay 或補貼
        c.execute("""
            WITH pp AS (
                SELECT DISTINCT tx_hash FROM payments
                WHERE platform_id=%s AND direction='pack_pay'
            )
            SELECT
              COUNT(*) FILTER (WHERE po.tx_hash NOT IN (SELECT tx_hash FROM pp))
                  AS freeplay_opens,
              COUNT(*) AS total_opens
            FROM pack_opens po WHERE po.platform_id=%s
        """, (ctx.pid, ctx.pid))
        fp, tot = c.fetchone()
        out["freeplay_opens"] = fp or 0
        out["freeplay_pct"] = (fp / tot * 100) if tot else 0.0

        # House net P&L —「玩家面向」(排除 treasury ↔ Safe ↔ operator 內部轉帳)
        # 條件：pack_pay 必須 from_addr 不在內部、payout 必須 to_addr 不在內部
        c.execute("""
            SELECT DATE(block_time) AS d,
                   ROUND(SUM(CASE
                       WHEN direction='pack_pay' AND from_addr <> ALL(%s)
                            THEN amount_usd
                       WHEN direction='payout'   AND to_addr   <> ALL(%s)
                            THEN -amount_usd
                       ELSE 0 END)::numeric, 2) AS net_usd
            FROM payments WHERE platform_id=%s
            GROUP BY 1 ORDER BY 1 DESC LIMIT 14
        """, (ctx.excluded_funders, ctx.excluded_funders, ctx.pid))
        out["daily_net_last14"] = c.fetchall()

        c.execute("""
            SELECT ROUND(SUM(CASE
                       WHEN direction='pack_pay' AND from_addr <> ALL(%s)
                            THEN amount_usd
                       WHEN direction='payout'   AND to_addr   <> ALL(%s)
                            THEN -amount_usd
                       ELSE 0 END)::numeric, 2)
            FROM payments WHERE platform_id=%s
        """, (ctx.excluded_funders, ctx.excluded_funders, ctx.pid))
        out["house_net_usd"] = c.fetchone()[0] or Decimal(0)

        # 內部資金流（treasury → Safe → operator → 外部）— 真正的「平台抽水出場」
        c.execute("""
            SELECT ROUND(SUM(amount_usd)::numeric, 2)
            FROM payments
            WHERE platform_id=%s
              AND direction='payout'
              AND to_addr = ANY(%s)
              AND from_addr <> ALL(%s)
        """, (ctx.pid, ctx.excluded_funders, ctx.excluded_funders))
        out["internal_inflow_usd"] = c.fetchone()[0] or Decimal(0)

    sym = ctx.token_symbol
    print(f"=== A. Health ===")
    if out["first_day"]:
        print(f"  期間: {out['first_day']} → {out['last_day']}")
    print(f"  total_packs: {out['total_packs']:,}  unique_openers: {out['unique_openers']:,}")
    print(f"  mints: {out['mints']:,}  nft_transfers: {out['nft_transfers']:,}  "
          f"marketplace_events: {out['marketplace_events']:,}")
    print(f"  payments:")
    for row in out["payment_breakdown"]:
        print(f"    {row[0]:12s} count={row[1]:6d} volume_{sym}=${row[2]:,.2f}")
    print(f"  FreePlay coverage gap: {out['freeplay_opens']:,} opens "
          f"({out['freeplay_pct']:.1f}%) 找不到對應 pack_pay")
    print(f"  House net P&L (全期): ${out['house_net_usd']:,.2f} {sym}")
    print(f"  最近 14 天:")
    daily_map = dict(out["daily_last14"])
    net_map = dict(out["daily_net_last14"])
    for d in sorted(set(daily_map) | set(net_map), reverse=True)[:14]:
        n = daily_map.get(d, 0)
        net = net_map.get(d, Decimal(0))
        print(f"    {d}  packs={n:5d}  net=${net:>12,.2f}")
    return out


# --------------------------------------------------------------------------- #
# Module B: concentration (+ single-whale)
# --------------------------------------------------------------------------- #
def module_concentration(conn, ctx: Ctx) -> dict:
    with conn.cursor() as c:
        c.execute("""
            SELECT opener, COUNT(*) AS packs, SUM(price_usd)::numeric AS po_volume
            FROM pack_opens WHERE platform_id=%s
            GROUP BY opener ORDER BY packs DESC LIMIT 20
        """, (ctx.pid,))
        top = c.fetchall()
        c.execute(
            "SELECT COUNT(*), COUNT(DISTINCT opener) FROM pack_opens WHERE platform_id=%s",
            (ctx.pid,),
        )
        total, uniq = c.fetchone()

    top1 = top[0][1] if top else 0
    top5 = sum(r[1] for r in top[:5])
    top10 = sum(r[1] for r in top[:10])
    out = dict(
        total=total or 0, unique=uniq or 0,
        top1_pct=(top1 / total * 100) if total else 0,
        top5_pct=(top5 / total * 100) if total else 0,
        top10_pct=(top10 / total * 100) if total else 0,
        top20=top,
    )

    print(f"\n=== B. Concentration ===")
    print(f"  top  1 / total = {top1} / {total or 0}  ({out['top1_pct']:.1f}%)")
    print(f"  top  5 / total = {top5} / {total or 0}  ({out['top5_pct']:.1f}%)")
    print(f"  top 10 / total = {top10} / {total or 0}  ({out['top10_pct']:.1f}%)")
    print(f"  Top 20 openers:")
    for addr, packs, vol in top:
        print(f"    {addr}  packs={packs:5d}  vol=${vol:>10,.0f}")
    return out


# --------------------------------------------------------------------------- #
# Module C: funder cluster
# --------------------------------------------------------------------------- #
def module_funder_cluster(conn, ctx: Ctx) -> dict:
    cluster_min = int(ctx.thresholds.get("cluster_min_size", 3))
    with conn.cursor() as c:
        c.execute("""
            WITH first_in AS (
                SELECT DISTINCT ON (to_addr)
                       to_addr AS funded_addr, from_addr AS funder, block_time
                FROM payments
                WHERE platform_id=%s
                ORDER BY to_addr, block_time
            )
            SELECT funder, COUNT(*) AS n,
                   ARRAY_AGG(funded_addr ORDER BY funded_addr) AS members
            FROM first_in
            WHERE funder <> ALL(%s)
            GROUP BY funder
            HAVING COUNT(*) >= %s
            ORDER BY n DESC LIMIT 20
        """, (ctx.pid, ctx.excluded_funders, cluster_min))
        clusters = c.fetchall()

    print(f"\n=== C. Funder clusters (≥{cluster_min} members) ===")
    print(f"  排除清單: {len(ctx.excluded_funders)} 個地址（自家合約 + treasury + deployer + infra）")
    for funder, n, members in clusters[:10]:
        print(f"  funder {funder}  members={n}")
        for m in members[:5]:
            print(f"    {m}")
        if n > 5:
            print(f"    ... and {n-5} more")
    return dict(clusters=clusters)


# --------------------------------------------------------------------------- #
# Module D: bot_flags (cron_interval + shared_funder + burst)
# --------------------------------------------------------------------------- #
def module_bot_flags(conn, ctx: Ctx) -> dict:
    cv_threshold = float(ctx.thresholds.get("bot_interval_cv", 0.35))
    cluster_min = int(ctx.thresholds.get("cluster_min_size", 3))
    burst_n = int(ctx.thresholds.get("burst_n", 5))
    burst_window = int(ctx.thresholds.get("burst_window_sec", 60))

    with conn.cursor() as c:
        c.execute("DELETE FROM bot_flags WHERE platform_id=%s", (ctx.pid,))

        # cron_interval: 開包間隔變異係數低
        c.execute("""
            WITH intervals AS (
                SELECT opener,
                       EXTRACT(EPOCH FROM (block_time -
                            LAG(block_time) OVER (PARTITION BY opener ORDER BY block_time))) AS dt
                FROM pack_opens WHERE platform_id=%s
            ),
            stats AS (
                SELECT opener, COUNT(dt) AS n, AVG(dt) AS mean_dt, STDDEV_POP(dt) AS sd_dt
                FROM intervals WHERE dt IS NOT NULL
                GROUP BY opener HAVING COUNT(dt) >= 10
            )
            INSERT INTO bot_flags(platform_id, address, rule_id, evidence_json)
            SELECT %s, opener, 'cron_interval',
                   jsonb_build_object('n', n, 'mean_dt', mean_dt, 'sd_dt', sd_dt,
                                      'cv', (sd_dt / NULLIF(mean_dt, 0))::numeric)
            FROM stats
            WHERE mean_dt > 0 AND sd_dt / mean_dt < %s
            ON CONFLICT DO NOTHING
        """, (ctx.pid, ctx.pid, cv_threshold))

        # shared_funder: 排除清單動態
        c.execute("""
            WITH first_in AS (
                SELECT DISTINCT ON (to_addr) to_addr AS funded, from_addr AS funder
                FROM payments WHERE platform_id=%s
                ORDER BY to_addr, block_time
            ),
            groups AS (
                SELECT funder, array_agg(funded) AS members, COUNT(*) AS n
                FROM first_in
                WHERE funder <> ALL(%s)
                GROUP BY funder HAVING COUNT(*) >= %s
            )
            INSERT INTO bot_flags(platform_id, address, rule_id, evidence_json)
            SELECT %s, unnest(members), 'shared_funder',
                   jsonb_build_object('funder', funder, 'cluster_size', n)
            FROM groups
            ON CONFLICT DO NOTHING
        """, (ctx.pid, ctx.excluded_funders, cluster_min, ctx.pid))

        # burst: 同 opener N opens within W seconds
        c.execute("""
            WITH ordered AS (
                SELECT opener, block_time,
                       LEAD(block_time, %s - 1) OVER
                            (PARTITION BY opener ORDER BY block_time) AS later_t
                FROM pack_opens WHERE platform_id=%s
            ),
            bursters AS (
                SELECT opener,
                       COUNT(*) AS burst_windows,
                       MIN(EXTRACT(EPOCH FROM (later_t - block_time))) AS min_span_sec
                FROM ordered
                WHERE later_t IS NOT NULL
                  AND EXTRACT(EPOCH FROM (later_t - block_time)) <= %s
                GROUP BY opener
            )
            INSERT INTO bot_flags(platform_id, address, rule_id, evidence_json)
            SELECT %s, opener, 'burst',
                   jsonb_build_object('burst_windows', burst_windows,
                                      'min_span_sec', min_span_sec,
                                      'n', %s, 'window_sec', %s)
            FROM bursters
            ON CONFLICT DO NOTHING
        """, (burst_n, ctx.pid, burst_window, ctx.pid, burst_n, burst_window))

        c.execute("""
            SELECT rule_id, COUNT(*)
            FROM bot_flags WHERE platform_id=%s GROUP BY rule_id ORDER BY 2 DESC
        """, (ctx.pid,))
        summary = c.fetchall()

        c.execute("SELECT COUNT(DISTINCT address) FROM bot_flags WHERE platform_id=%s",
                  (ctx.pid,))
        flagged_addrs = c.fetchone()[0]

        c.execute("""
            WITH top AS (
                SELECT opener, COUNT(*) AS packs
                FROM pack_opens WHERE platform_id=%s
                GROUP BY opener ORDER BY packs DESC LIMIT 20
            )
            SELECT t.opener, t.packs,
                   array_agg(DISTINCT bf.rule_id) FILTER (WHERE bf.rule_id IS NOT NULL) AS rules
            FROM top t
            LEFT JOIN bot_flags bf ON bf.platform_id=%s AND bf.address=t.opener
            GROUP BY t.opener, t.packs
            ORDER BY t.packs DESC
        """, (ctx.pid, ctx.pid))
        top_with_rules = c.fetchall()

    conn.commit()
    print(f"\n=== D. Bot flags (cv<{cv_threshold}, min_cluster={cluster_min}, "
          f"burst {burst_n} in {burst_window}s) ===")
    for rule, n in summary:
        print(f"  {rule}: {n} hits")
    print(f"  total flagged addresses: {flagged_addrs}")
    print(f"  Top 20 with rule hits:")
    for opener, packs, rules in top_with_rules:
        tag = ",".join(rules) if rules else ""
        print(f"    {opener}  packs={packs:5d}  rules=[{tag}]")
    return dict(rule_summary=summary, flagged_addrs=flagged_addrs,
                top_with_rules=top_with_rules)


# --------------------------------------------------------------------------- #
# Module E: NFT endstate — only if platform has on-chain NFT
# --------------------------------------------------------------------------- #
def module_endstate(conn, ctx: Ctx) -> dict:
    if not ctx.has_onchain_nft:
        print(f"\n=== E. NFT endstate (skipped: contracts.nft is empty) ===")
        return dict(skipped=True)

    with conn.cursor() as c:
        c.execute("""
            WITH last_holder AS (
                SELECT DISTINCT ON (contract, token_id)
                    contract, token_id, to_addr AS holder
                FROM nft_transfers WHERE platform_id=%s
                ORDER BY contract, token_id, block_time DESC
            ),
            classified AS (
                SELECT lh.contract, lh.token_id, lh.holder,
                       CASE WHEN lh.holder IN
                                ('0x0000000000000000000000000000000000000000',
                                 '0x000000000000000000000000000000000000dead')
                            THEN 'burn' ELSE 'held' END AS endstate
                FROM last_holder lh
            )
            SELECT endstate, COUNT(*) FROM classified GROUP BY endstate ORDER BY 2 DESC
        """, (ctx.pid,))
        endstate = c.fetchall()

        c.execute("""
            WITH last_holder AS (
                SELECT DISTINCT ON (contract, token_id)
                    contract, token_id, to_addr AS holder
                FROM nft_transfers WHERE platform_id=%s
                ORDER BY contract, token_id, block_time DESC
            )
            SELECT holder, COUNT(*) FROM last_holder
            WHERE holder NOT IN ('0x0000000000000000000000000000000000000000',
                                 '0x000000000000000000000000000000000000dead')
            GROUP BY holder ORDER BY 2 DESC LIMIT 10
        """, (ctx.pid,))
        top_holders = c.fetchall()

        c.execute("SELECT COUNT(*) FROM mints WHERE platform_id=%s", (ctx.pid,))
        total_minted = c.fetchone()[0]

    print(f"\n=== E. NFT endstate ===")
    for state, n in endstate:
        pct = n / total_minted * 100 if total_minted else 0
        print(f"  {state}: {n} ({pct:.1f}%)")
    print(f"  Top 10 持有者:")
    for h, n in top_holders:
        print(f"    {h}  {n}")
    return dict(endstate=endstate, top_holders=top_holders,
                total_minted=total_minted, skipped=False)


# --------------------------------------------------------------------------- #
# Module G: sellback pairing + per-player ROI
# --------------------------------------------------------------------------- #
def module_sellback(conn, ctx: Ctx) -> dict:
    fast_dump_sec = int(ctx.thresholds.get("fast_dump_seconds", 600))
    sym = ctx.token_symbol
    # 操作者 / treasury / Safe 等內部地址：不該被當「玩家」算 ROI
    excl = ctx.excluded_funders
    with conn.cursor() as c:
        # 每 player 的 pack_pay 與 payout 總額／ROI
        c.execute("""
            WITH pp AS (
                SELECT from_addr AS player, COUNT(*) AS pay_n,
                       SUM(amount_usd) AS paid_out
                FROM payments WHERE platform_id=%s AND direction='pack_pay'
                  AND from_addr <> ALL(%s)
                GROUP BY from_addr
            ), po AS (
                SELECT to_addr AS player, COUNT(*) AS rcv_n,
                       SUM(amount_usd) AS received
                FROM payments WHERE platform_id=%s AND direction='payout'
                  AND to_addr <> ALL(%s)
                GROUP BY to_addr
            )
            SELECT COALESCE(pp.player, po.player) AS player,
                   COALESCE(pp.pay_n, 0)         AS pay_n,
                   COALESCE(pp.paid_out, 0)      AS paid_usd,
                   COALESCE(po.rcv_n, 0)         AS rcv_n,
                   COALESCE(po.received, 0)      AS recv_usd,
                   CASE WHEN COALESCE(pp.paid_out, 0) > 0
                        THEN COALESCE(po.received, 0)::numeric / pp.paid_out
                        ELSE NULL END             AS roi
            FROM pp FULL OUTER JOIN po ON pp.player = po.player
        """, (ctx.pid, excl, ctx.pid, excl))
        roi_rows = c.fetchall()

        # ROI 分佈
        c.execute("""
            WITH per_player AS (
                SELECT from_addr AS player, SUM(amount_usd) AS paid,
                       (SELECT SUM(amount_usd) FROM payments po
                         WHERE po.platform_id=%s AND po.direction='payout'
                           AND po.to_addr = p.from_addr
                           AND po.to_addr <> ALL(%s)) AS recv
                FROM payments p
                WHERE platform_id=%s AND direction='pack_pay'
                  AND from_addr <> ALL(%s)
                GROUP BY from_addr
            )
            SELECT
              CASE WHEN paid <= 0 THEN 'no_pay'
                   WHEN COALESCE(recv,0)/paid <  0.5  THEN '<0.50'
                   WHEN COALESCE(recv,0)/paid <  0.7  THEN '0.50-0.69'
                   WHEN COALESCE(recv,0)/paid <  0.85 THEN '0.70-0.84'
                   WHEN COALESCE(recv,0)/paid <  1.0  THEN '0.85-0.99'
                   WHEN COALESCE(recv,0)/paid <  1.5  THEN '1.00-1.49'
                   ELSE '>=1.50' END AS bucket,
              COUNT(*), SUM(paid) AS paid_total
            FROM per_player
            GROUP BY 1 ORDER BY 1
        """, (ctx.pid, excl, ctx.pid, excl))
        roi_buckets = c.fetchall()

        # 即時 sellback (hot potato): 同 player pack_pay 後 < fast_dump_sec 收到 payout
        c.execute("""
            WITH events AS (
                SELECT from_addr AS player, block_time AS t,
                       amount_usd, 'pay' AS kind
                FROM payments WHERE platform_id=%s AND direction='pack_pay'
                  AND from_addr <> ALL(%s)
                UNION ALL
                SELECT to_addr AS player, block_time AS t, amount_usd, 'recv' AS kind
                FROM payments WHERE platform_id=%s AND direction='payout'
                  AND to_addr <> ALL(%s)
            ),
            ordered AS (
                SELECT player, t, amount_usd, kind,
                       LAG(t)         OVER (PARTITION BY player ORDER BY t) AS prev_t,
                       LAG(kind)      OVER (PARTITION BY player ORDER BY t) AS prev_kind,
                       LAG(amount_usd) OVER (PARTITION BY player ORDER BY t) AS prev_amt
                FROM events
            ),
            paired AS (
                SELECT player, prev_amt AS paid, amount_usd AS recv,
                       EXTRACT(EPOCH FROM (t - prev_t)) AS dt_sec
                FROM ordered
                WHERE prev_kind = 'pay' AND kind = 'recv'
            )
            SELECT
              CASE WHEN dt_sec <=  10 THEN '0-10s'
                   WHEN dt_sec <=  60 THEN '10-60s'
                   WHEN dt_sec <= 300 THEN '60-300s'
                   WHEN dt_sec <= %s  THEN '300s-fastdump'
                   ELSE 'slow' END AS bucket,
              COUNT(*) AS n,
              ROUND(AVG(recv / NULLIF(paid, 0))::numeric, 3) AS avg_recv_ratio
            FROM paired
            GROUP BY 1 ORDER BY 1
        """, (ctx.pid, excl, ctx.pid, excl, fast_dump_sec))
        timing = c.fetchall()

        # Top 10 ROI > 0.9 且 paid > $500 的「系統性贏家」（已排除內部地址）
        c.execute("""
            WITH pp AS (
                SELECT from_addr AS player, SUM(amount_usd) AS paid, COUNT(*) AS n
                FROM payments WHERE platform_id=%s AND direction='pack_pay'
                  AND from_addr <> ALL(%s)
                GROUP BY from_addr
            ), po AS (
                SELECT to_addr AS player, SUM(amount_usd) AS recv
                FROM payments WHERE platform_id=%s AND direction='payout'
                  AND to_addr <> ALL(%s)
                GROUP BY to_addr
            )
            SELECT pp.player, pp.paid, COALESCE(po.recv,0) AS recv,
                   pp.n, COALESCE(po.recv,0)/pp.paid AS roi
            FROM pp LEFT JOIN po ON pp.player=po.player
            WHERE pp.paid >= 500 AND COALESCE(po.recv,0)/pp.paid >= 0.9
            ORDER BY roi DESC, pp.paid DESC LIMIT 10
        """, (ctx.pid, excl, ctx.pid, excl))
        winners = c.fetchall()

    total_paid = sum(r[2] for r in roi_rows) or Decimal(0)
    total_recv = sum(r[4] for r in roi_rows) or Decimal(0)
    print(f"\n=== G. Sellback / EV ===")
    print(f"  players w/ pack_pay or payout : {len(roi_rows):,}")
    print(f"  全期 pack_pay : ${total_paid:>12,.2f} {sym}")
    print(f"  全期 payout   : ${total_recv:>12,.2f} {sym}")
    if total_paid:
        print(f"  全平台 ROI    : {float(total_recv/total_paid):.3f}  "
              f"(house edge ≈ {float(1 - total_recv/total_paid)*100:+.2f}%)")
    print(f"  ROI 分桶（per player）:")
    for bucket, n, paid in roi_buckets:
        print(f"    {bucket:>13s}  players={n:>6,}  paid_total=${paid:>12,.0f}")
    print(f"  pack_pay → payout 時間差（hot-potato 偵測）:")
    for bucket, n, ratio in timing:
        ratio_s = f"{ratio:.2f}" if ratio is not None else "n/a"
        print(f"    {bucket:>15s}  n={n:>6,}  avg recv/paid={ratio_s}")
    print(f"  系統性贏家 (paid≥$500 且 ROI≥0.9):")
    for p, paid, recv, n, roi in winners:
        print(f"    {p}  paid=${paid:>8,.0f} recv=${recv:>8,.0f} ROI={float(roi):.3f} n={n}")
    return dict(
        total_paid=total_paid, total_recv=total_recv,
        roi_buckets=roi_buckets, timing=timing, winners=winners,
    )


# --------------------------------------------------------------------------- #
# Module H: marketplace events
# --------------------------------------------------------------------------- #
def module_marketplace_trades(conn, ctx: Ctx) -> dict:
    """ERC-721 token_id 市場（如 renaiss）：成交記在 marketplace_trades，
    無 list/bid 等事件細類（unverified 合約推導而來，只有已成交）。"""
    sym = ctx.token_symbol
    with conn.cursor() as c:
        c.execute("""
            SELECT COUNT(*), COUNT(DISTINCT buyer), COUNT(DISTINCT seller),
                   COUNT(DISTINCT token_id), SUM(price_usd), AVG(price_usd),
                   PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price_usd),
                   MAX(price_usd), SUM(fee_usd)
            FROM marketplace_trades WHERE platform_id=%s
        """, (ctx.pid,))
        n, nb, ns, ncards, gmv, avg_p, med_p, max_p, fee = c.fetchone()
        c.execute("""
            SELECT to_char(date_trunc('month', block_time),'YYYY-MM') m,
                   COUNT(*) trades, SUM(price_usd) vol, COUNT(DISTINCT buyer) buyers
            FROM marketplace_trades WHERE platform_id=%s GROUP BY 1 ORDER BY 1
        """, (ctx.pid,))
        monthly = c.fetchall()
        c.execute("""
            SELECT buyer, COUNT(*) n, SUM(price_usd) v FROM marketplace_trades
            WHERE platform_id=%s GROUP BY buyer ORDER BY v DESC LIMIT 10
        """, (ctx.pid,))
        top_buyers = c.fetchall()
    print(f"\n=== H. Marketplace trades (ERC-721 token_id 市場) ===")
    print(f"  成交筆數={n:,}  獨立買家={nb:,}  獨立賣家={ns:,}  成交卡種={ncards:,}")
    print(f"  GMV=${gmv:,.0f} ({sym})  平均=${avg_p:,.2f}  中位=${med_p:,.2f}  最大=${max_p:,.2f}")
    print(f"  合約層手續費留存=${fee or 0:,.0f}")
    print(f"  每月成交量:")
    for m, t, v, b in monthly:
        print(f"    {m}  成交={t:>6,}  量=${v:>11,.0f}  買家={b:>5,}")
    print(f"  成交額 Top10 買家:")
    for a, cnt, v in top_buyers:
        print(f"    {a}  ${v:>10,.0f}  ({cnt:,}筆)")
    return dict(source="marketplace_trades", trades=n, buyers=nb, sellers=ns,
                gmv=gmv, avg_price=avg_p, median_price=med_p, max_price=max_p,
                fee=fee, monthly=monthly, top_buyers=top_buyers)


def module_marketplace(conn, ctx: Ctx) -> dict:
    with conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM marketplace_events WHERE platform_id=%s", (ctx.pid,))
        total = c.fetchone()[0]
        if total == 0:
            # 事件流為空 → 改看 token_id 市場的成交表（marketplace_trades）
            c.execute("SELECT COUNT(*) FROM marketplace_trades WHERE platform_id=%s", (ctx.pid,))
            if c.fetchone()[0] > 0:
                return module_marketplace_trades(conn, ctx)
            print(f"\n=== H. Marketplace (none ingested) ===")
            return dict(total=0, skipped=True)

        c.execute("""
            SELECT kind, COUNT(*) FROM marketplace_events WHERE platform_id=%s
            GROUP BY kind ORDER BY 2 DESC
        """, (ctx.pid,))
        kind_breakdown = c.fetchall()

        # liquidity proxy: 成交 vs 掛單／改價
        c.execute("""
            SELECT
              SUM(CASE WHEN kind='card_bought'        THEN 1 ELSE 0 END) AS bought,
              SUM(CASE WHEN kind='card_listed'        THEN 1 ELSE 0 END) AS listed,
              SUM(CASE WHEN kind='card_price_updated' THEN 1 ELSE 0 END) AS updated,
              SUM(CASE WHEN kind='bid_placed'         THEN 1 ELSE 0 END) AS bids,
              SUM(CASE WHEN kind LIKE 'pack_%%'       THEN 1 ELSE 0 END) AS pack_market
            FROM marketplace_events WHERE platform_id=%s
        """, (ctx.pid,))
        bought, listed, updated, bids, pack_market = c.fetchone()

        # 改價狂熱：同 actor 跨 cert 改價
        c.execute("""
            SELECT actor, COUNT(*) AS updates,
                   COUNT(DISTINCT item_key) AS distinct_certs
            FROM marketplace_events
            WHERE platform_id=%s AND kind='card_price_updated' AND actor <> ''
            GROUP BY actor ORDER BY updates DESC LIMIT 10
        """, (ctx.pid,))
        actor_updates = c.fetchall()
        # 同 cert 改價軌跡：volatility = max/min
        c.execute("""
            SELECT item_key,
                   COUNT(*) AS n_updates,
                   MIN(price_usd) AS min_p, MAX(price_usd) AS max_p,
                   ROUND((MAX(price_usd)/NULLIF(MIN(price_usd),0))::numeric, 2) AS volatility
            FROM marketplace_events
            WHERE platform_id=%s AND kind='card_price_updated' AND price_usd > 0
            GROUP BY item_key
            HAVING COUNT(*) >= 5 AND MIN(price_usd) > 0
            ORDER BY volatility DESC NULLS LAST LIMIT 10
        """, (ctx.pid,))
        volatile_certs = c.fetchall()

        # 成交統計
        c.execute("""
            SELECT COUNT(DISTINCT actor) AS distinct_buyers,
                   SUM(price_usd) AS gmv_usd,
                   AVG(price_usd) AS avg_price,
                   MAX(price_usd) AS max_price
            FROM marketplace_events
            WHERE platform_id=%s AND kind='card_bought'
        """, (ctx.pid,))
        buyer_stats = c.fetchone()

    trade_to_list_ratio = (bought / listed) if listed else None
    trade_to_update_ratio = (bought / updated) if updated else None
    sym = ctx.token_symbol
    print(f"\n=== H. Marketplace events ===")
    print(f"  total events: {total:,}")
    for kind, n in kind_breakdown:
        print(f"    {kind:<25s} {n:>8,}")
    print(f"  liquidity proxies:")
    print(f"    bought / listed         = {bought}/{listed}  "
          f"→ {trade_to_list_ratio:.3f}" if listed else
          f"    bought / listed         = {bought}/0")
    print(f"    bought / price_updated  = {bought}/{updated}  "
          f"→ {trade_to_update_ratio:.4f}" if updated else
          f"    bought / price_updated  = {bought}/0")
    if buyer_stats and buyer_stats[0]:
        n_buyers, gmv, avg_p, max_p = buyer_stats
        print(f"  成交統計：buyers={n_buyers}  GMV=${gmv:,.2f}  "
              f"avg=${avg_p:,.2f}  max=${max_p:,.2f}")
    print(f"  改價狂熱 (Top 10 actor by # price updates):")
    for actor, n, distinct in actor_updates:
        print(f"    {actor}  updates={n:5d}  distinct_certs={distinct:4d}")
    print(f"  價格波動 Top 10 cert (≥5 updates, max/min):")
    for cert, n, mn, mx, vol in volatile_certs:
        print(f"    cert={cert:<15s} n={n:3d}  ${mn:>7.2f}→${mx:>9.2f}  vol={vol}x")
    return dict(
        total=total, kind_breakdown=kind_breakdown,
        bought=bought, listed=listed, updated=updated,
        bids=bids, pack_market=pack_market,
        trade_to_list_ratio=trade_to_list_ratio,
        trade_to_update_ratio=trade_to_update_ratio,
        actor_updates=actor_updates, volatile_certs=volatile_certs,
        buyer_stats=buyer_stats,
    )


# --------------------------------------------------------------------------- #
# Module I: deployer / treasury flow — top non-player recipients of treasury USDm
# --------------------------------------------------------------------------- #
def module_deployer_flow(conn, ctx: Ctx) -> dict:
    sym = ctx.token_symbol
    if not ctx.official_addrs and not ctx.deployer_addrs:
        print(f"\n=== I. Deployer / Treasury flow (no official/deployer in config) ===")
        return dict(skipped=True)
    src_addrs = list({*ctx.official_addrs, *ctx.deployer_addrs})

    with conn.cursor() as c:
        # Top recipients of payouts (excluding addresses that opened any pack — those are players)
        c.execute("""
            WITH players AS (
                SELECT DISTINCT opener AS addr FROM pack_opens WHERE platform_id=%s
            )
            SELECT p.to_addr,
                   COUNT(*) AS n,
                   SUM(p.amount_usd)::numeric AS total_usd,
                   MIN(p.block_time)::date AS first_seen,
                   MAX(p.block_time)::date AS last_seen,
                   EXISTS(SELECT 1 FROM known_addresses k
                          WHERE k.address = p.to_addr) AS is_known
            FROM payments p
            WHERE p.platform_id=%s
              AND p.from_addr = ANY(%s)
              AND p.to_addr NOT IN (SELECT addr FROM players)
              AND p.to_addr <> ALL(%s)         -- 排除其它自家地址
            GROUP BY p.to_addr
            ORDER BY total_usd DESC NULLS LAST LIMIT 20
        """, (ctx.pid, ctx.pid, src_addrs, src_addrs))
        non_player_out = c.fetchall()

        # Top inbound funders to treasury (excluding players)
        c.execute("""
            WITH players AS (
                SELECT DISTINCT opener AS addr FROM pack_opens WHERE platform_id=%s
            )
            SELECT p.from_addr,
                   COUNT(*) AS n,
                   SUM(p.amount_usd)::numeric AS total_usd,
                   MIN(p.block_time)::date, MAX(p.block_time)::date,
                   EXISTS(SELECT 1 FROM known_addresses k
                          WHERE k.address = p.from_addr) AS is_known
            FROM payments p
            WHERE p.platform_id=%s
              AND p.to_addr = ANY(%s)
              AND p.from_addr NOT IN (SELECT addr FROM players)
              AND p.from_addr <> ALL(%s)
            GROUP BY p.from_addr
            ORDER BY total_usd DESC NULLS LAST LIMIT 20
        """, (ctx.pid, ctx.pid, src_addrs, src_addrs))
        non_player_in = c.fetchall()

    print(f"\n=== I. Deployer / Treasury flow ===")
    print(f"  treasury / deployer 源頭：{len(src_addrs)} 個地址")
    print(f"  Top 20 non-player recipients (treasury → X):")
    for addr, n, total, first, last, known in non_player_out:
        tag = " [known]" if known else ""
        print(f"    {addr}  n={n:5d}  total=${total:>10,.0f}  "
              f"{first}→{last}{tag}")
    print(f"  Top 20 non-player funders (X → treasury):")
    for addr, n, total, first, last, known in non_player_in:
        tag = " [known]" if known else ""
        print(f"    {addr}  n={n:5d}  total=${total:>10,.0f}  "
              f"{first}→{last}{tag}")
    return dict(non_player_out=non_player_out, non_player_in=non_player_in)


# --------------------------------------------------------------------------- #
# JSON serializer
# --------------------------------------------------------------------------- #
def _to_json_safe(obj):
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_json_safe(v) for v in obj]
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj


# --------------------------------------------------------------------------- #
# reports
# --------------------------------------------------------------------------- #
def write_reports(ctx: Ctx, results: dict, fmt: str):
    out_dir = REPO_ROOT / "platforms" / ctx.platform
    sym = ctx.token_symbol

    if fmt in ("json", "both"):
        (out_dir / "results.json").write_text(json.dumps(
            _to_json_safe(dict(platform=ctx.platform, results=results)),
            indent=2, ensure_ascii=False))

    if fmt in ("text", "both"):
        h = results.get("health", {})
        c = results.get("concentration", {})
        bot = results.get("bot_flags", {})
        sb = results.get("sellback", {})
        mk = results.get("marketplace", {})
        df = results.get("deployer_flow", {})
        es = results.get("endstate", {})

        pay = dict((row[0], (row[1], row[2])) for row in h.get("payment_breakdown", []))
        pack_pay = pay.get("pack_pay", (0, Decimal(0)))
        payout = pay.get("payout", (0, Decimal(0)))

        flow = []
        flow.append(f"# Flow Overview — {ctx.platform}")
        flow.append("")
        if h.get("first_day"):
            flow.append(f"> run_analysis.py 產出。觀察期間 {h['first_day']} → {h['last_day']}")
        flow.append("")
        flow.append("## 開包總體")
        flow.append("")
        flow.append("| 指標 | 數值 |")
        flow.append("|------|------|")
        flow.append(f"| 總開包次數 | {h.get('total_packs', 0):,} |")
        flow.append(f"| 獨立開包地址數 | {h.get('unique_openers', 0):,} |")
        if h.get("unique_openers"):
            flow.append(f"| 平均每人 | {h['total_packs']/h['unique_openers']:.1f} 包 |")
        flow.append(f"| pack_pay ({sym}) | ${pack_pay[1]:,.2f} ({pack_pay[0]} 筆) |")
        flow.append(f"| payout ({sym}) | ${payout[1]:,.2f} ({payout[0]} 筆) |")
        flow.append(f"| **House net** | **${h.get('house_net_usd', 0):,.2f}** |")
        flow.append(f"| FreePlay coverage gap | {h.get('freeplay_opens', 0):,} opens "
                    f"({h.get('freeplay_pct', 0):.1f}%) |")
        flow.append(f"| NFT mints | {h.get('mints', 0):,} |")
        flow.append(f"| NFT transfers | {h.get('nft_transfers', 0):,} |")
        flow.append(f"| marketplace_events | {h.get('marketplace_events', 0):,} |")
        flow.append("")
        flow.append("## 集中度")
        flow.append("")
        flow.append(f"- Top 1 opener 吃 **{c.get('top1_pct', 0):.1f}%**")
        flow.append(f"- Top 5 openers 佔 **{c.get('top5_pct', 0):.1f}%**")
        flow.append(f"- Top 10 openers 佔 **{c.get('top10_pct', 0):.1f}%**")
        flow.append("")
        flow.append("| Opener | Packs |")
        flow.append("|--------|-------|")
        for addr, packs, _ in (c.get("top20") or [])[:10]:
            flow.append(f"| `{addr}` | {packs} |")
        flow.append("")
        flow.append("## Sellback / EV")
        flow.append("")
        if sb.get("total_paid"):
            roi = float(sb["total_recv"]) / float(sb["total_paid"])
            flow.append(f"- 全平台 ROI = {roi:.3f}（house edge ≈ {(1-roi)*100:+.2f}%）")
        for bucket, n, paid in sb.get("roi_buckets", []):
            flow.append(f"- ROI bucket `{bucket}` → {n} players, paid_total=${paid:,.0f}")
        if sb.get("winners"):
            flow.append("")
            flow.append("**系統性贏家 (paid ≥ $500 且 ROI ≥ 0.9):**")
            flow.append("")
            flow.append("| Player | Paid | Received | ROI | # opens |")
            flow.append("|--------|------|----------|-----|---------|")
            for p, paid, recv, n, r in sb["winners"]:
                flow.append(f"| `{p}` | ${paid:,.0f} | ${recv:,.0f} | {float(r):.3f} | {n} |")
        flow.append("")
        flow.append("## Marketplace")
        flow.append("")
        if mk.get("total"):
            flow.append(f"- 事件總數 {mk['total']:,}（成交 {mk['bought']}，掛單 {mk['listed']}, "
                        f"改價 {mk['updated']}, 出價 {mk['bids']}）")
            if mk.get("trade_to_update_ratio") is not None:
                flow.append(f"- 成交/改價 = {mk['trade_to_update_ratio']:.4f}（流動性指標）")
        else:
            flow.append("- 無資料（contracts.marketplace 未設置或無事件）")
        flow.append("")
        flow.append("## 自動 bot 標記")
        flow.append("")
        for rule, n in bot.get("rule_summary", []):
            flow.append(f"- `{rule}`: {n} hits")
        flow.append(f"- **共標記 {bot.get('flagged_addrs', 0)} 個地址**")
        flow.append("")
        flow.append("## Treasury 資金流")
        flow.append("")
        if df.get("non_player_out"):
            flow.append("**Top 5 非玩家收款方:**")
            flow.append("")
            for addr, n, total, first, last, known in df["non_player_out"][:5]:
                tag = " [known]" if known else ""
                flow.append(f"- `{addr}` n={n} total=${total:,.0f}{tag}")
        flow.append("")
        (out_dir / "flow_overview.md").write_text("\n".join(flow))

        # red_flags.md
        rf = [
            f"# Red Flags — {ctx.platform}",
            "",
            "## 數據摘要",
            "",
            f"- 期間: {h.get('first_day')} → {h.get('last_day')}",
            f"- 開包: **{h.get('total_packs', 0):,}** 筆 / **{h.get('unique_openers', 0):,}** 地址",
            f"- pack_pay: ${pack_pay[1]:,.2f} {sym}",
            f"- payout:   ${payout[1]:,.2f} {sym}",
            f"- House net P&L: ${h.get('house_net_usd', 0):,.2f} {sym}",
            "",
            "## 紅旗",
            "",
        ]
        if c.get("top1_pct", 0) > 5:
            rf.append(f"- 🚩 **single-whale** — Top 1 持有者吃 {c['top1_pct']:.1f}% 開包量")
        if c.get("top5_pct", 0) > 25:
            rf.append(f"- 🚩 **Top 5 集中度** — {c['top5_pct']:.1f}%")
        if bot.get("flagged_addrs", 0) > 0:
            rf.append(f"- 🚩 **{bot['flagged_addrs']} 個地址命中 bot 規則**")
        if h.get("freeplay_pct", 0) > 20:
            rf.append(f"- ⚠️ FreePlay coverage gap {h['freeplay_pct']:.1f}% "
                      f"({h['freeplay_opens']:,} 筆) — 平台補貼或 admin grant 比例高")
        if h.get("house_net_usd") is not None and abs(float(h["house_net_usd"])) < float(pack_pay[1] or 1) * 0.02:
            rf.append(f"- ⚠️ House net 接近 0 ({h['house_net_usd']:,.2f}) "
                      f"— 收進來的錢幾乎全退回去")
        if sb.get("winners"):
            rf.append(f"- 🚩 {len(sb['winners'])} 個系統性贏家（paid ≥ $500 且 ROI ≥ 0.9）")
        if mk.get("trade_to_update_ratio") is not None and mk["trade_to_update_ratio"] < 0.05:
            rf.append(f"- ⚠️ marketplace 流動性極低 "
                      f"（成交/改價 = {mk['trade_to_update_ratio']:.4f}）")
        if not es.get("skipped") and es.get("endstate"):
            burn = next((n for s, n in es["endstate"] if s == "burn"), 0)
            if burn:
                rf.append(f"- ℹ️ {burn} 個 NFT 已銷毀")
        rf.append("")
        rf.append("## 待後續分析")
        rf.append("")
        rf.append("- [ ] deployer 資金最終去向（橋 / CEX / 自家集中地址）")
        rf.append("- [ ] known_addresses 表標記 funder / 收款方")
        (out_dir / "red_flags.md").write_text("\n".join(rf))

    print(f"\n[reports] wrote to {out_dir}/")


# --------------------------------------------------------------------------- #
# Module registry
# --------------------------------------------------------------------------- #
MODULES = {
    "health":         ("A", module_health),
    "concentration":  ("B", module_concentration),
    "funder_cluster": ("C", module_funder_cluster),
    "bot_flags":      ("D", module_bot_flags),
    "endstate":       ("E", module_endstate),
    "sellback":       ("G", module_sellback),
    "marketplace":    ("H", module_marketplace),
    "deployer_flow":  ("I", module_deployer_flow),
}


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True)
    ap.add_argument("--modules", default="all",
                    help=f"comma-separated subset of: {','.join(MODULES)} or 'all'")
    ap.add_argument("--format", choices=["text", "json", "both"], default="both")
    ap.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    args = ap.parse_args()

    load_env_file(Path(args.env_file).expanduser())
    cfg = load_config(args.platform)
    selected = list(MODULES) if args.modules == "all" else [
        m.strip() for m in args.modules.split(",") if m.strip()
    ]
    unknown = [m for m in selected if m not in MODULES]
    if unknown:
        raise SystemExit(f"unknown modules: {unknown}; available: {list(MODULES)}")

    with get_conn() as conn:
        pid = get_platform_id(conn, args.platform)
        ctx = Ctx(args.platform, pid, cfg)
        print(f"[analysis] platform_id={pid} chain={cfg.get('chain')} "
              f"token={ctx.token_symbol} modules={selected}")
        results: dict = {}
        for name in selected:
            _, fn = MODULES[name]
            results[name] = fn(conn, ctx)

    write_reports(ctx, results, args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
