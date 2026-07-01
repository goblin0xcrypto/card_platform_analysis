"""
ingest_txs.py — 抓取 platforms/<name>/config.yaml 內所有地址的「全部交易紀錄」，
寫入 DATABASE_URL 的 raw_transactions 表。用 ETHERSCAN_API_KEY（Etherscan V2 多鏈）。

與 ingest.py 的差異：
  ingest.py     依賴 per-chain adapter 解析特定事件（pack_open / payment / nft）。
                需要已知合約 ABI / 事件 topic，適合 verified 合約。
  ingest_txs.py 不依賴 ABI，直接打 Etherscan account 端點，把每個地址的
                native / internal / ERC-20 / ERC-721 / ERC-1155 交易原樣存下。
                適合 unverified proxy 合約（如 renaiss）或想保留完整足跡。

地址來源（config.yaml）：contracts.{pack_opening,marketplace,staking,token,nft}
                        + deployers + official_wallets.*

冪等：PK = (platform_id, address, kind, tx_hash, seq)，ON CONFLICT DO NOTHING。
      seq 為同 (address,kind,tx_hash) 內依抓取順序（sort=asc，穩定）的序號，可重入。

用法：
  .venv/bin/python ingest_txs.py --platform renaiss
  .venv/bin/python ingest_txs.py --platform renaiss --from-block 66766326
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from ingest import (  # 重用既有 config / upsert 邏輯，保持 DB 一致
    _addr_norm,
    _as_addr_list,
    get_conn,
    load_config,
    upsert_addresses,
    upsert_contracts,
    upsert_platform,
)
from tools.bootstrap import load_env_file
from tools.explorer import EtherscanV2Client

DDL = """
CREATE TABLE IF NOT EXISTS raw_transactions (
    platform_id     INT REFERENCES platforms(id),
    address         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    tx_hash         TEXT NOT NULL,
    seq             INT NOT NULL,
    block_number    BIGINT NOT NULL,
    block_time      TIMESTAMPTZ NOT NULL,
    from_addr       TEXT,
    to_addr         TEXT,
    value_raw       NUMERIC(78,0),
    token           TEXT,
    token_symbol    TEXT,
    token_decimals  INT,
    token_id        TEXT,
    ingested_at     TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (platform_id, address, kind, tx_hash, seq)
);
CREATE INDEX IF NOT EXISTS idx_raw_tx_hash  ON raw_transactions(platform_id, tx_hash);
CREATE INDEX IF NOT EXISTS idx_raw_tx_from  ON raw_transactions(platform_id, from_addr);
CREATE INDEX IF NOT EXISTS idx_raw_tx_to    ON raw_transactions(platform_id, to_addr);
CREATE INDEX IF NOT EXISTS idx_raw_tx_token ON raw_transactions(platform_id, token);
CREATE INDEX IF NOT EXISTS idx_raw_tx_time  ON raw_transactions(platform_id, block_time);
"""

INSERT_SQL = """
INSERT INTO raw_transactions(platform_id, address, kind, tx_hash, seq, block_number,
    block_time, from_addr, to_addr, value_raw, token, token_symbol, token_decimals, token_id)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (platform_id, address, kind, tx_hash, seq) DO NOTHING
"""


def _collect_addresses(cfg: dict) -> list[str]:
    """config.yaml 裡所有要抓的地址，去重、lower-case。

    刻意排除 contracts.token（付款幣本身）：它通常是全網共用的 ERC-20（如 BSC USDT），
    抓它的「所有交易」會是數百萬筆無關資料。各業務地址的 USDT 金流已由它們自己的
    tokentx（erc20 串流）涵蓋。
    """
    addrs: list[str] = []
    contracts = cfg.get("contracts") or {}
    for typ in ("pack_opening", "marketplace", "staking", "nft"):
        addrs += _as_addr_list(contracts.get(typ))
    addrs += [_addr_norm(a) for a in (cfg.get("deployers") or []) if a]
    for v in (cfg.get("official_wallets") or {}).values():
        if isinstance(v, list):
            addrs += [_addr_norm(a) for a in v if a]
        elif v:
            addrs.append(_addr_norm(v))
    # 去重但保序
    seen, out = set(), []
    for a in addrs:
        if a and a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _ts(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _ingest_stream(rows_iter, platform_id, address, kind, batch, conn):
    """把一個 account 端點的 TxRecord/dict 串流轉成 raw row 並批次寫入。"""
    seq_by_tx: dict[str, int] = {}
    count = 0
    for r in rows_iter:
        if kind in ("native", "internal", "erc20"):
            tx = r.tx_hash
            seq = seq_by_tx.get(tx, 0)
            seq_by_tx[tx] = seq + 1
            row = (platform_id, address, kind, tx, seq, r.block_number, _ts(r.timestamp),
                   r.from_addr, r.to_addr, r.value_raw, r.token, r.token_symbol,
                   r.token_decimals, None)
        else:  # erc721 / erc1155 — explorer 回原始 dict
            tx = r["hash"]
            seq = seq_by_tx.get(tx, 0)
            seq_by_tx[tx] = seq + 1
            token = (r.get("contractAddress") or "").lower()
            token_id = r.get("tokenID")
            # 1155 數量在 tokenValue；721 無，記 1
            value = r.get("tokenValue") or ("1" if kind == "erc721" else "0")
            decimals = r.get("tokenDecimal")
            row = (platform_id, address, kind, tx, seq, int(r["blockNumber"]),
                   _ts(int(r["timeStamp"])), (r.get("from") or "").lower(),
                   (r.get("to") or "").lower(), int(value) if str(value).isdigit() else 0,
                   token, r.get("tokenName") or r.get("tokenSymbol"),
                   int(decimals) if decimals and str(decimals).isdigit() else None,
                   token_id)
        batch.append(row)
        count += 1
        if len(batch) >= 1000:
            _flush(conn, batch)
    return count


def _flush(conn, batch):
    if not batch:
        return
    with conn.cursor() as c:
        c.executemany(INSERT_SQL, batch)
    conn.commit()
    batch.clear()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True)
    ap.add_argument("--from-block", type=int, help="覆寫 config.launch_block")
    ap.add_argument("--to-block", type=int, default=999999999)
    ap.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    args = ap.parse_args()

    load_env_file(Path(args.env_file).expanduser())
    api_key = os.environ.get("ETHERSCAN_API_KEY")
    if not api_key:
        print("[ingest_txs] ETHERSCAN_API_KEY 未設定"); return 1

    cfg = load_config(args.platform)
    chain = cfg["chain"]
    start_block = args.from_block if args.from_block is not None else (cfg.get("launch_block") or 0)
    end_block = args.to_block

    addresses = _collect_addresses(cfg)
    print(f"[ingest_txs] platform={args.platform} chain={chain} "
          f"blocks {start_block}→{end_block} addresses={len(addresses)}")

    explorer = EtherscanV2Client(api_key, chain)

    with get_conn() as conn:
        platform_id = upsert_platform(conn, args.platform, chain, cfg.get("launch_block", 0), cfg)
        upsert_contracts(conn, platform_id, cfg)
        upsert_addresses(conn, platform_id, cfg)
        with conn.cursor() as c:
            c.execute(DDL)
        conn.commit()
        print(f"[ingest_txs] platform_id={platform_id}; raw_transactions 表就緒")

        nft_set = set(_as_addr_list((cfg.get("contracts") or {}).get("nft")))

        def streams_for(a: str):
            """每個地址的 5 種串流。NFT 合約用 contractaddress 模式抓「該合約全部
            721/1155 轉移」（token 合約不會是自己轉帳的參與者，address 模式抓不到）。"""
            s = [
                ("native",   lambda: explorer.txlist(a, start_block, end_block)),
                ("internal", lambda: explorer.txlistinternal(a, start_block, end_block)),
                ("erc20",    lambda: explorer.tokentx(a, start_block, end_block)),
            ]
            if a in nft_set:
                s += [
                    ("erc721",  lambda: explorer.tokennfttx(contractaddress=a,
                                  start_block=start_block, end_block=end_block)),
                    ("erc1155", lambda: explorer.token1155tx(contractaddress=a,
                                  start_block=start_block, end_block=end_block)),
                ]
            else:
                s += [
                    ("erc721",  lambda: explorer.tokennfttx(a, start_block, end_block)),
                    ("erc1155", lambda: explorer.token1155tx(a, start_block, end_block)),
                ]
            return s

        grand_total = 0
        for addr in addresses:
            print(f"\n[ingest_txs] === {addr} ===")
            batch: list = []
            for kind, fn in streams_for(addr):
                try:
                    n = _ingest_stream(fn(), platform_id, addr, kind, batch, conn)
                except Exception as e:
                    print(f"  {kind:8s} ERROR: {type(e).__name__}: {e}")
                    continue
                if n:
                    print(f"  {kind:8s}: {n}")
                grand_total += n
            _flush(conn, batch)

        with conn.cursor() as c:
            c.execute("SELECT kind, COUNT(*) FROM raw_transactions WHERE platform_id=%s GROUP BY kind ORDER BY 1",
                      (platform_id,))
            print("\n[ingest_txs] raw_transactions 統計（含先前已存）：")
            for k, n in c.fetchall():
                print(f"  {k:8s}: {n}")
            c.execute("SELECT COUNT(*), COUNT(DISTINCT tx_hash) FROM raw_transactions WHERE platform_id=%s",
                      (platform_id,))
            total, distinct = c.fetchone()
            print(f"  total rows={total}  distinct tx={distinct}")

    print(f"\n[ingest_txs] done. 本次抓取 {grand_total} 列。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
