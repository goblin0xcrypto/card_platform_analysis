"""
ingest_solana.py — Solana 平台的 silver-layer ingest。

對齊 ingest.py 的設計：不是把所有東西塞進一張 raw 表，而是「依行為分流」寫入
domain 表（payments / nft_transfers / …），讓後續分析直接查語意化資料。

差別在資料來源：
  ingest.py        EVM：Etherscan getLogs + per-chain adapter 解析事件 topic。
  ingest_solana.py Solana：Solscan Pro v2 /account/transfer（已解析的轉帳），
                   依「付款幣 mint / NFT」分類，分別寫 payments / nft_transfers。

分類規則（每列 /account/transfer）：
  - token == contracts.token（付款幣 mint，如 USDC）  → payments
  - token_decimals == 0 且 amount == 1（單一 SPL NFT）→ nft_transfers
  - 其餘（原生 SOL gas、ATA create、其他代幣）          → 略過（非平台金流/NFT 行為）

⚠️ MPL Core 限制：MPL Core 資產「不是 SPL token」(無 token account)，其 mint/transfer 走
   mpl_core_program 指令，**不會出現在 /account/transfer**。所以對 MPL Core 平台(如 phygitals)，
   上面的 NFT 分類抓不到任何卡片轉移；MPL Core 的 mint/transfer 改由 **ingest_solana_nft.py**
   解 mpl_core createV2/transferV1 指令，寫 mints / nft_transfers。payments(USDC)由本檔負責，不受影響。

地址來源：與 ingest_txs 相同 _collect_addresses（pack/marketplace/staking/nft +
          deployers + official_wallets）。Solana 付款金流多走 official_wallets
          （treasury / buyback / fee_payer），務必先在 config 填好這些地址。

log_index：Solscan 轉帳列無「指令索引」。同一 signature 內、被不同 address 抓到的
          「同一筆轉帳」必須對到同一 PK 才能冪等去重，故 log_index 由轉帳本身的
          內在欄位 (token,from,to,amount,flow) 決定（穩定、跨 address 一致）。
          代價：同一 tx 內若有兩筆完全相同的轉帳會被視為一筆（罕見，已知取捨）。

冪等：domain 表 PK = (platform_id, tx_hash, log_index)，ON CONFLICT DO NOTHING。
      resume：以各表 max(block_time) 為 from_time，避免重抓。

用法：
  .venv/bin/python ingest_solana.py --platform phygitals
  .venv/bin/python ingest_solana.py --platform phygitals --from-time 1730000000
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from ingest import (  # 重用 config / domain insert / platform upsert（不涉地址大小寫）
    get_conn,
    insert_nft_transfers,
    insert_payments,
    load_config,
    upsert_platform,
)
from tools.bootstrap import load_env_file
from tools.solscan import SolscanClient

FLUSH = 1000
CURSOR_SOURCE = "solscan_transfer"

CURSOR_DDL = """
CREATE TABLE IF NOT EXISTS ingest_cursors (
    platform_id     INT,
    address         TEXT NOT NULL,
    source          TEXT NOT NULL,        -- 抓取來源/串流，如 solscan_transfer
    last_block_time BIGINT NOT NULL,      -- 該地址已完整抓到的最新 block_time（unix 秒）
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (platform_id, address, source)
);
"""


def _ts(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _get_cursor(conn, platform_id: int, address: str) -> int:
    """該地址上次完整抓到的 block_time（無則 0）。逐地址記錄,避免用全表 max 漏抓未掃過的錢包。"""
    with conn.cursor() as c:
        c.execute("SELECT last_block_time FROM ingest_cursors "
                  "WHERE platform_id=%s AND address=%s AND source=%s",
                  (platform_id, address, CURSOR_SOURCE))
        r = c.fetchone()
        return r[0] if r else 0


def _set_cursor(conn, platform_id: int, address: str, block_time: int):
    with conn.cursor() as c:
        c.execute(
            "INSERT INTO ingest_cursors(platform_id,address,source,last_block_time,updated_at) "
            "VALUES (%s,%s,%s,%s,NOW()) "
            "ON CONFLICT (platform_id,address,source) DO UPDATE "
            "SET last_block_time=GREATEST(ingest_cursors.last_block_time,EXCLUDED.last_block_time), "
            "    updated_at=NOW()",
            (platform_id, address, CURSOR_SOURCE, block_time))
    conn.commit()


# Solana base58 大小寫敏感 → 全程保留原樣，不可套用 EVM 的 _addr_norm（會轉小寫）。
def _collect_addresses(cfg: dict) -> list[str]:
    """config 內所有要抓的地址（保留大小寫、去重保序）。"""
    out, seen = [], set()

    def add(v):
        for a in (v if isinstance(v, list) else [v]):
            a = (a or "").strip() if isinstance(a, str) else a
            if a and a not in seen:
                seen.add(a); out.append(a)

    contracts = cfg.get("contracts") or {}
    for typ in ("pack_opening", "marketplace", "staking", "nft"):
        add(contracts.get(typ))
    add(cfg.get("deployers") or [])
    for v in (cfg.get("official_wallets") or {}).values():
        add(v)
    return out


def _upsert_refs(conn, platform_id: int, cfg: dict):
    """case-preserving 寫入 contracts / addresses 參考表（Solana 專用）。"""
    contracts = cfg.get("contracts") or {}
    crows = []
    for typ in ("pack_opening", "marketplace", "staking", "token"):
        v = contracts.get(typ)
        for a in (v if isinstance(v, list) else [v]):
            if a:
                crows.append((platform_id, a, typ))
    for a in (contracts.get("nft") or []):
        if a:
            crows.append((platform_id, a, "nft"))
    arows = []
    for label, v in (cfg.get("official_wallets") or {}).items():
        for a in (v if isinstance(v, list) else [v]):
            if a:
                arows.append((platform_id, a, label, "config"))
    for a in (cfg.get("deployers") or []):
        if a:
            arows.append((platform_id, a, "deployer", "config"))
    with conn.cursor() as c:
        if crows:
            c.executemany(
                "INSERT INTO contracts(platform_id,address,type) VALUES (%s,%s,%s) "
                "ON CONFLICT (platform_id,address) DO UPDATE SET type=EXCLUDED.type", crows)
        if arows:
            c.executemany(
                "INSERT INTO addresses(platform_id,address,label,source) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (platform_id,address) DO UPDATE SET label=EXCLUDED.label", arows)


def _synthetic_log_index(t) -> int:
    """由轉帳內在欄位算出穩定的 31-bit log_index（跨 address 一致 → 可冪等去重）。"""
    key = f"{t.token}|{t.from_addr}|{t.to_addr}|{t.amount_raw}|{t.flow}".encode()
    return int.from_bytes(hashlib.blake2b(key, digest_size=4).digest(), "big") & 0x7FFFFFFF


def _amount_usd(t, decimals: int) -> float | None:
    """優先用 Solscan 估值；否則對穩定幣以 amount/10^decimals 近似（USDC≈$1）。"""
    if t.value_usd:
        return float(t.value_usd)
    if decimals is not None:
        return t.amount_raw / (10 ** decimals)
    return None




def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True)
    ap.add_argument("--from-time", type=int, help="覆寫 resume（unix 秒）；0=全量")
    ap.add_argument("--spl-nft", action="store_true",
                    help="額外掃 SPL NFT 轉移(decimals=0,amount=1)。MPL Core 平台無效，預設關")
    ap.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    args = ap.parse_args()

    load_env_file(Path(args.env_file).expanduser())
    api_key = os.environ.get("SOLANA_API_KEY")
    if not api_key:
        print("[ingest_solana] SOLANA_API_KEY 未設定"); return 1

    cfg = load_config(args.platform)
    chain = (cfg.get("chain") or "").lower()
    if chain != "solana":
        print(f"[ingest_solana] config.chain={chain!r}，此腳本僅處理 solana"); return 1

    # Solana mint 大小寫敏感 → 保留原樣比對，勿經 _addr_norm
    pay_token_raw = (cfg["contracts"].get("token") or "")
    addresses = _collect_addresses(cfg)
    if not addresses:
        print("[ingest_solana] config 內沒有可抓的地址（pack/nft/deployers/official_wallets 皆空）")
        return 1

    client = SolscanClient(api_key)
    # 付款幣 decimals
    pay_decimals = 6
    pay_symbol = cfg.get("payment_token_symbol") or "?"
    if pay_token_raw:
        try:
            meta = client.token_meta(pay_token_raw)
            pay_decimals = meta.get("decimals", 6)
            pay_symbol = meta.get("symbol") or pay_symbol
        except Exception as e:
            print(f"[ingest_solana] token_meta 取 decimals 失敗，預設 6：{e}")

    print(f"[ingest_solana] platform={args.platform} chain=solana "
          f"payToken={pay_token_raw[:8]}…({pay_symbol},{pay_decimals}dp) addresses={len(addresses)}")

    with get_conn() as conn:
        platform_id = upsert_platform(conn, args.platform, "solana", cfg.get("launch_block", 0), cfg)
        _upsert_refs(conn, platform_id, cfg)
        with conn.cursor() as c:
            c.execute(CURSOR_DDL)
        conn.commit()
        print(f"[ingest_solana] platform_id={platform_id}")

        pay_batch, nft_batch = [], []
        n_pay = n_nft = n_skip = 0

        def flush():
            if pay_batch:
                insert_payments(conn, platform_id, pay_batch); pay_batch.clear()
            if nft_batch:
                insert_nft_transfers(conn, platform_id, nft_batch); nft_batch.clear()
            conn.commit()

        for addr in addresses:
            # 逐地址 resume：用該地址自己的 cursor（--from-time 可全域覆寫）
            addr_from = args.from_time if args.from_time is not None else _get_cursor(conn, platform_id, addr)
            max_bt = addr_from
            ok = True
            print(f"\n[ingest_solana] === {addr} === from_time={addr_from} "
                  f"({_ts(addr_from).date() if addr_from else 'genesis'})")
            a_pay = a_nft = 0
            # --- payments：直接用付款幣 token 過濾抓取（高效，避免掃 SOL/雜訊海）---
            if pay_token_raw:
                try:
                    for t in client.account_transfers(addr, from_time=addr_from, token=pay_token_raw):
                        li = _synthetic_log_index(t)
                        direction = "in" if t.to_addr == addr else "out"  # 進平台=in、出=out（語意細分留後續）
                        pay_batch.append((
                            platform_id, t.signature, li, _ts(t.block_time), t.slot,
                            t.from_addr, t.to_addr, t.token,
                            t.amount_raw, _amount_usd(t, pay_decimals), direction))
                        a_pay += 1
                        if t.block_time > max_bt:
                            max_bt = t.block_time
                        if len(pay_batch) >= FLUSH:
                            flush()
                except Exception as e:
                    print(f"  payments ERROR: {type(e).__name__}: {e}")
                    flush(); ok = False
            # --- SPL NFT 轉移（可選；MPL Core 平台無效，見檔頭說明）---
            if args.spl_nft:
                try:
                    for t in client.account_transfers(addr, from_time=addr_from):
                        if not (t.token_decimals == 0 and t.amount_raw == 1
                                and t.activity_type.startswith("ACTIVITY_SPL")):
                            n_skip += 1; continue
                        li = _synthetic_log_index(t)
                        if t.block_time > max_bt:
                            max_bt = t.block_time
                        nft_batch.append((  # Solana：每個 NFT 自成 mint → contract=token_id=mint
                            platform_id, t.signature, li, _ts(t.block_time), t.slot,
                            t.token, t.token, t.from_addr, t.to_addr, None, None))
                        a_nft += 1
                        if len(nft_batch) >= FLUSH:
                            flush()
                except Exception as e:
                    print(f"  spl-nft ERROR: {type(e).__name__}: {e}")
                    flush(); ok = False
            flush()
            n_pay += a_pay; n_nft += a_nft
            # 僅在該地址完整掃完(無錯)才推進 cursor，避免下次漏抓中斷段
            if ok and max_bt > addr_from:
                _set_cursor(conn, platform_id, addr, max_bt)
            if a_pay or a_nft:
                print(f"  payments={a_pay} nft_transfers={a_nft} cursor→{_ts(max_bt).date() if ok else '(未推進:有錯)'}")

        with conn.cursor() as c:
            for tbl in ("payments", "nft_transfers"):
                c.execute(f"SELECT COUNT(*) FROM {tbl} WHERE platform_id=%s", (platform_id,))
                print(f"[ingest_solana] {tbl} 總計（含先前）: {c.fetchone()[0]}")

    print(f"\n[ingest_solana] done. 本次 payments={n_pay} nft_transfers={n_nft} skipped={n_skip}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
