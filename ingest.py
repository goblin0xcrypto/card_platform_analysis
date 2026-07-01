"""
ingest.py — read platforms/<name>/config.yaml, fetch on-chain data, write to Postgres.

Two modes:
  --mode explorer  (default) — use Etherscan V2 to enumerate relevant tx hashes,
                                then fetch each tx receipt and parse logs.
                                Fast for historical sweeps.
  --mode getlogs   — use eth_getLogs block sweep. Useful for chains without
                                explorer support, or for tail-following.

Idempotent: re-running resumes from max(block_number) per table (getlogs mode)
or from max(block_number) of pack_opens / payments (explorer mode).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import psycopg
import yaml

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from adapters import get_adapter
from tools.bootstrap import load_env_file
from tools.explorer import EtherscanV2Client


def _addr_to_topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower().removeprefix("0x")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _addr_norm(v) -> str:
    if not v:
        return ""
    if isinstance(v, int):
        return "0x" + format(v, "040x")
    return str(v).lower()


def _as_addr_list(v) -> list[str]:
    """
    Config 欄位可能是 string / list / int / None；統一成 lower-case 地址 list。

    YAML 把無引號的 0xABC... 解成 int，所以單值情況一律走 _addr_norm（已處理 int）。
    """
    if not v:
        return []
    if isinstance(v, list):
        return [_addr_norm(a) for a in v if a]
    s = _addr_norm(v)
    return [s] if s else []


def load_config(platform: str) -> dict:
    cfg_path = REPO_ROOT / "platforms" / platform / "config.yaml"
    with open(cfg_path) as f:
        return yaml.safe_load(f)


def get_conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg.connect(url)


# --------------------------------------------------------------------------- #
# upserts
# --------------------------------------------------------------------------- #
def upsert_platform(conn, name, chain, launch_block, profile) -> int:
    with conn.cursor() as c:
        c.execute(
            """
            INSERT INTO platforms(name, chain, launch_block, profile_json)
            VALUES (%s, %s, %s, %s::jsonb)
            ON CONFLICT (name) DO UPDATE
              SET chain = EXCLUDED.chain,
                  launch_block = EXCLUDED.launch_block,
                  profile_json = EXCLUDED.profile_json
            RETURNING id
            """,
            (name, chain, launch_block, json.dumps(profile, default=str)),
        )
        return c.fetchone()[0]


def upsert_contracts(conn, platform_id, cfg):
    """支援 pack_opening / marketplace 可為 str 或 list；nft 為 list。"""
    rows = []
    for typ in ("pack_opening", "marketplace", "staking", "token"):
        for a in _as_addr_list(cfg["contracts"].get(typ)):
            rows.append((platform_id, a, typ))
    for a in _as_addr_list(cfg["contracts"].get("nft")):
        rows.append((platform_id, a, "nft"))
    if rows:
        with conn.cursor() as c:
            c.executemany(
                """
                INSERT INTO contracts(platform_id, address, type)
                VALUES (%s, %s, %s)
                ON CONFLICT (platform_id, address) DO UPDATE SET type = EXCLUDED.type
                """,
                rows,
            )


def upsert_addresses(conn, platform_id, cfg):
    rows = []
    for label, v in (cfg.get("official_wallets") or {}).items():
        if not v:
            continue
        for addr in (v if isinstance(v, list) else [v]):
            if addr:
                rows.append((platform_id, _addr_norm(addr), label, "config"))
    for a in cfg.get("deployers") or []:
        if a:
            rows.append((platform_id, _addr_norm(a), "deployer", "config"))
    if rows:
        with conn.cursor() as c:
            c.executemany(
                """
                INSERT INTO addresses(platform_id, address, label, source)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (platform_id, address) DO UPDATE
                  SET label = EXCLUDED.label
                """,
                rows,
            )


# --------------------------------------------------------------------------- #
# batch insert (idempotent on PK)
# --------------------------------------------------------------------------- #
def insert_pack_opens(conn, platform_id, rows):
    with conn.cursor() as c:
        c.executemany(
            """
            INSERT INTO pack_opens(platform_id, tx_hash, log_index, block_time,
                block_number, opener, pack_id, price_raw, price_token, price_usd)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (platform_id, tx_hash, log_index) DO NOTHING
            """, rows,
        )


def insert_mints(conn, platform_id, rows):
    with conn.cursor() as c:
        c.executemany(
            """
            INSERT INTO mints(platform_id, tx_hash, log_index, block_time, block_number,
                minter, token_id, contract, pack_open_tx)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (platform_id, tx_hash, log_index) DO NOTHING
            """, rows,
        )


def insert_nft_transfers(conn, platform_id, rows):
    with conn.cursor() as c:
        c.executemany(
            """
            INSERT INTO nft_transfers(platform_id, tx_hash, log_index, block_time, block_number,
                contract, token_id, from_addr, to_addr, price_usd, marketplace)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (platform_id, tx_hash, log_index) DO NOTHING
            """, rows,
        )


def insert_payments(conn, platform_id, rows):
    with conn.cursor() as c:
        c.executemany(
            """
            INSERT INTO payments(platform_id, tx_hash, log_index, block_time, block_number,
                from_addr, to_addr, token, amount_raw, amount_usd, direction)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (platform_id, tx_hash, log_index) DO NOTHING
            """, rows,
        )


def insert_marketplace_events(conn, platform_id, rows):
    with conn.cursor() as c:
        c.executemany(
            """
            INSERT INTO marketplace_events(platform_id, tx_hash, log_index, block_time, block_number,
                contract, kind, item_key, actor, counterparty, price_raw, price_usd, quantity)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (platform_id, tx_hash, log_index) DO NOTHING
            """, rows,
        )


# --------------------------------------------------------------------------- #
# explorer-driven ingest
# --------------------------------------------------------------------------- #
def run_explorer_mode(conn, platform_id: int, cfg: dict, adapter,
                      explorer: EtherscanV2Client, start_block: int, end_block: int):
    """
    Pure getLogs-based ingest. 4 independent log streams:
      A) Pack-open events on each pack_opening contract
      B) ERC-721 Transfer on each NFT contract       (skipped if adapter.has_onchain_nft=False)
      C) Payment-token Transfer where pack/marketplace is from OR to
      D) Marketplace events on each marketplace contract (if adapter exposes topics)
    All log fields come from Etherscan (no RPC needed).
    """
    pack_openings = _as_addr_list(cfg["contracts"].get("pack_opening"))
    marketplaces  = _as_addr_list(cfg["contracts"].get("marketplace"))
    token         = _addr_norm(cfg["contracts"].get("token"))
    nft_list      = _as_addr_list(cfg["contracts"].get("nft"))

    # Resume points per table — pack_opens 用 max(block_number)，按 contract 區分意義不大
    def _resume(table: str) -> int:
        with conn.cursor() as c:
            c.execute(
                f"SELECT COALESCE(MAX(block_number), 0) FROM {table} WHERE platform_id=%s",
                (platform_id,),
            )
            return c.fetchone()[0]

    resume_po  = _resume("pack_opens")
    resume_tr  = _resume("nft_transfers")
    resume_pay = _resume("payments")
    resume_me  = _resume("marketplace_events")
    fp_po  = max(start_block, resume_po + 1)  if resume_po  else start_block
    fp_tr  = max(start_block, resume_tr + 1)  if resume_tr  else start_block
    fp_pay = max(start_block, resume_pay + 1) if resume_pay else start_block
    fp_me  = max(start_block, resume_me + 1)  if resume_me  else start_block

    po_batch, mint_batch, tr_batch, pay_batch, me_batch = [], [], [], [], []
    FLUSH = 1000

    def flush():
        if po_batch:
            insert_pack_opens(conn, platform_id, po_batch); po_batch.clear()
        if mint_batch:
            insert_mints(conn, platform_id, mint_batch); mint_batch.clear()
        if tr_batch:
            insert_nft_transfers(conn, platform_id, tr_batch); tr_batch.clear()
        if pay_batch:
            insert_payments(conn, platform_id, pay_batch); pay_batch.clear()
        if me_batch:
            insert_marketplace_events(conn, platform_id, me_batch); me_batch.clear()
        conn.commit()

    transfer_topic = adapter.transfer_topic0
    pack_topics    = list(adapter.pack_event_topics)
    market_topics  = list(getattr(adapter, "market_event_topics", []) or [])
    has_nft        = bool(getattr(adapter, "has_onchain_nft", True))

    # --- A. Pack-open events on each pack contract -----------------------
    for pack in pack_openings:
        for topic in pack_topics:
            print(f"[ingest] PackOpen topic={topic[:10]}.. on {pack[:10]}.. from {fp_po}")
            count = 0
            for raw_log in explorer.get_logs(pack, topic0=topic,
                                              from_block=fp_po, to_block=end_block):
                po_batch.append(_pack_open_row(platform_id, adapter.parse_pack_opened(raw_log)))
                count += 1
                if len(po_batch) >= FLUSH:
                    flush()
                    print(f"  flushed; cumulative pack_opens for this topic: {count}")
            print(f"  total pack_opens on {pack[:10]}.. topic {topic[:10]}..: {count}")
            flush()

    # --- B. ERC-721 Transfer on NFT contracts ----------------------------
    if has_nft:
        for nft in nft_list:
            print(f"[ingest] Transfer on nft {nft[:10]}.. from {fp_tr}")
            count_tr = count_mint = 0
            for raw_log in explorer.get_logs(nft, topic0=transfer_topic,
                                              from_block=fp_tr, to_block=end_block):
                if len(raw_log.get("topics", [])) != 4:
                    continue
                tr, mint = adapter.parse_nft_transfer(raw_log)
                if tr is None:
                    continue
                tr_batch.append(_transfer_row(platform_id, tr))
                count_tr += 1
                if mint:
                    mint_batch.append(_mint_row(platform_id, mint))
                    count_mint += 1
                if len(tr_batch) >= FLUSH:
                    flush()
                    print(f"  flushed; cumulative transfers={count_tr} mints={count_mint}")
            print(f"  total transfers={count_tr} mints={count_mint}")
            flush()
    elif nft_list:
        print(f"[ingest] adapter.has_onchain_nft=False; skipping {len(nft_list)} nft addr(s)")

    # --- C. Payment-token Transfer where any pack/marketplace/official wallet is from/to -
    # 重要：mnstr 之類「合約只發事件、USDm 直進 paymentWallet」的設計，pack_opening
    # 本身不在金流路徑上。需要把 deployers + official_wallets 也納入 related set。
    treasury_addrs: list[str] = []
    for v in (cfg.get("official_wallets") or {}).values():
        if isinstance(v, list):
            treasury_addrs.extend(_addr_norm(a) for a in v if a)
        elif v:
            treasury_addrs.append(_addr_norm(v))
    treasury_addrs.extend(_addr_norm(a) for a in (cfg.get("deployers") or []) if a)

    related = list({a for a in (pack_openings + marketplaces + treasury_addrs) if a})
    if token and related:
        for r in related:
            r_topic = _addr_to_topic(r)
            # to = r  (pack_pay / inbound to marketplace escrow)
            print(f"[ingest] payment-token → {r[:10]}.. from {fp_pay}")
            count_in = 0
            for raw_log in explorer.get_logs(token, topic0=transfer_topic,
                                              topic2=r_topic,
                                              from_block=fp_pay, to_block=end_block):
                pay_batch.append(_payment_row(platform_id, adapter.parse_usdc_payment(raw_log, r)))
                count_in += 1
                if len(pay_batch) >= FLUSH:
                    flush()
                    print(f"  flushed; cumulative inbound: {count_in}")
            print(f"  total inbound payments to {r[:10]}..: {count_in}")
            flush()

            # from = r  (payouts / refunds / sellback)
            print(f"[ingest] payment-token ← {r[:10]}.. from {fp_pay}")
            count_out = 0
            for raw_log in explorer.get_logs(token, topic0=transfer_topic,
                                              topic1=r_topic,
                                              from_block=fp_pay, to_block=end_block):
                pay_batch.append(_payment_row(platform_id, adapter.parse_usdc_payment(raw_log, r)))
                count_out += 1
                if len(pay_batch) >= FLUSH:
                    flush()
                    print(f"  flushed; cumulative outbound: {count_out}")
            print(f"  total outbound payments from {r[:10]}..: {count_out}")
            flush()

    # --- D. Marketplace events ------------------------------------------
    if market_topics and marketplaces and hasattr(adapter, "parse_market_event"):
        for market in marketplaces:
            for topic in market_topics:
                print(f"[ingest] market topic={topic[:10]}.. on {market[:10]}.. from {fp_me}")
                count = 0
                for raw_log in explorer.get_logs(market, topic0=topic,
                                                  from_block=fp_me, to_block=end_block):
                    ev = adapter.parse_market_event(raw_log)
                    if ev is None:
                        continue
                    me_batch.append(_market_event_row(platform_id, market, ev))
                    count += 1
                    if len(me_batch) >= FLUSH:
                        flush()
                        print(f"  flushed; cumulative market events: {count}")
                print(f"  total market events on {market[:10]}.. topic {topic[:10]}..: {count}")
                flush()
    elif marketplaces and not market_topics:
        print(f"[ingest] adapter exposes no market_event_topics; skipping {len(marketplaces)} marketplace(s)")


# row-flatten helpers
def _pack_open_row(platform_id, po):
    return (platform_id, po.tx_hash, po.log_index, po.block_time, po.block_number,
            po.opener, po.pack_id, po.price_raw, po.price_token, po.price_usd)

def _mint_row(platform_id, m):
    return (platform_id, m.tx_hash, m.log_index, m.block_time, m.block_number,
            m.minter, m.token_id, m.contract, m.pack_open_tx)

def _transfer_row(platform_id, tr):
    return (platform_id, tr.tx_hash, tr.log_index, tr.block_time, tr.block_number,
            tr.contract, tr.token_id, tr.from_addr, tr.to_addr, tr.price_usd, tr.marketplace)

def _payment_row(platform_id, p):
    return (platform_id, p.tx_hash, p.log_index, p.block_time, p.block_number,
            p.from_addr, p.to_addr, p.token, p.amount_raw, p.amount_usd, p.direction)

def _market_event_row(platform_id, contract, ev):
    return (platform_id, ev.tx_hash, ev.log_index, ev.block_time, ev.block_number,
            contract, ev.kind, ev.cert_number, ev.actor, ev.counterparty,
            ev.price_raw, ev.price_usd, ev.quantity)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True)
    ap.add_argument("--from-block", type=int, help="覆寫 config.launch_block")
    ap.add_argument("--to-block", type=int, help="預設 latest")
    ap.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    ap.add_argument("--rpc", default="https://rpc3.monad.xyz")
    ap.add_argument("--mode", choices=["explorer", "getlogs"], default="explorer")
    ap.add_argument("--max-txs", type=int, default=0,
                    help="cap candidate tx count (smoke test)")
    args = ap.parse_args()

    load_env_file(Path(args.env_file).expanduser())

    cfg = load_config(args.platform)
    chain = cfg["chain"]
    print(f"[ingest] platform={args.platform} chain={chain} mode={args.mode}")

    pack_openings = _as_addr_list(cfg["contracts"].get("pack_opening"))
    token = _addr_norm(cfg["contracts"].get("token"))
    if not pack_openings:
        print("[ingest] ⚠️ contracts.pack_opening empty"); return 1
    if not token:
        print("[ingest] ⚠️ contracts.token empty — payments will be skipped")

    adapter = get_adapter(
        chain, platform_name=args.platform, rpc_url=args.rpc,
        usdc_address=token or "0x0000000000000000000000000000000000000000",
    )

    latest = args.to_block or adapter.latest_block()
    launch = args.from_block or cfg.get("launch_block", 0) or 0
    print(f"[ingest] block range: {launch} → {latest}  (span {latest - launch + 1:,} blocks)")
    print(f"[ingest] pack contracts: {len(pack_openings)}, marketplaces: "
          f"{len(_as_addr_list(cfg['contracts'].get('marketplace')))}")

    with get_conn() as conn:
        platform_id = upsert_platform(conn, args.platform, chain, cfg.get("launch_block", 0), cfg)
        upsert_contracts(conn, platform_id, cfg)
        upsert_addresses(conn, platform_id, cfg)
        conn.commit()
        print(f"[ingest] platform_id={platform_id}")

        if args.mode == "explorer":
            api_key = os.environ.get("ETHERSCAN_API_KEY")
            if not api_key:
                print("[ingest] ETHERSCAN_API_KEY required for explorer mode"); return 1
            explorer = EtherscanV2Client(api_key, chain)
            run_explorer_mode(conn, platform_id, cfg, adapter, explorer, launch, latest)
        else:
            print("[ingest] getlogs mode not implemented in this rewrite; use --mode explorer")
            return 1

        with conn.cursor() as c:
            for tbl in ("pack_opens", "mints", "nft_transfers", "payments", "marketplace_events"):
                c.execute(f"SELECT COUNT(*) FROM {tbl} WHERE platform_id=%s", (platform_id,))
                print(f"[ingest] {tbl}: {c.fetchone()[0]}")

    print("[ingest] done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
