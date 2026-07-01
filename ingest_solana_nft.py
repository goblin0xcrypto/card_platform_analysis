"""
ingest_solana_nft.py — 解析 MPL Core 指令，把卡片鑄造/交付寫進 mints / nft_transfers。

抓法（2026-06 實測選定）：**Helius RPC 並發單筆**（免費 key、不耗 Solscan CU、最快最穩）：
  - 列舉：`getSignaturesForAddress(collection, before=游標, limit=1000)`(降冪，一次 1000 筆，附 slot/blockTime)。
  - 明細：並發 `getTransaction(sig, jsonParsed)`(free Helius 不支援批次，但並發單筆 ~150-216/s、零 429)。
    公開 RPC 幾乎全 429 不可用；Solscan 逐筆 detail 慢且耗 CU。
  - 解碼：掃 message.instructions(+inner)，programId==MPL Core 者依 **data 首位元組(discriminator)** 分類：
      disc 20 = createV2(mint)   → mints       (asset=accounts[0], owner=accounts[4])
      disc 14 = transferV1       → nft_transfers(asset=accounts[0], to=accounts[4])
    accounts[1] 應==collection(否則跳過)。其餘 mpl_core 指令(collect/update/burn…)略過。
  - `from`(前手)不在指令裡 → ingest 時留空，最後 SQL LAG over(token_id order by block_time) 補；
    每 asset 首筆 transfer 的 from = 其 mint owner。
  - floor cursor(ingest_cursors, source=mpl_core, last_block_time=已處理到的最舊 blockTime)：全量(--days 0)可中斷續跑。

成本：getTransaction 走 free Helius(扣 Helius credit,不扣 Solscan CU)。近 7 天 ~5 分;全量(180-360萬筆)~2-5 小時,
      ⚠️ 全量可能吃爆 free Helius 月 credit,跑前看額度。

用法：
  .venv/bin/python ingest_solana_nft.py --platform phygitals --days 7      # 近 7 天(預設)
  .venv/bin/python ingest_solana_nft.py --platform phygitals --days 0      # 全量 genesis(floor 可續)
  .venv/bin/python ingest_solana_nft.py --platform phygitals --from 2026-06-19 --to 2026-06-21  # 範圍模式：補洞
  .venv/bin/python ingest_solana_nft.py --platform phygitals --finalize-only
  旗標：--workers(50)/--chunk(3000)/--reset/--rps(每秒上限,預設不限靠 workers 控)

範圍模式（--from/--to，YYYY-MM-DD 或 unix 秒）：精準重跑任意窗口，ON CONFLICT 冪等填洞。
  ⚠️ 為何需要：floor 游標是單一水位線（只記「最舊已處理」），--days 0 會跳過所有「比 floor 新」
     的交易當作已完成——若 floor 之上有缺漏（例：兩次 --days 7 間隔>7 天漏掉的某天），--days 0 補不到。
     範圍模式不讀也不寫 floor 游標，專門用來補這種洞。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from ingest import get_conn, insert_mints, insert_nft_transfers, load_config, upsert_platform
from ingest_solana import CURSOR_DDL, _upsert_refs
from tools.bootstrap import load_env_file

MPL_CORE = "CoREENxT6tW1HoK8ypY1SxRMZTcVPm7R94rH4PZNhX7d"
DISC_CREATE = 20      # createV2 (mint)
DISC_TRANSFER = 14    # transferV1
CURSOR_SRC = "mpl_core"
FLUSH = 1000

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_tlocal = threading.local()


def _b58_first_byte(s: str) -> int:
    """解 base58 指令 data 的首位元組(= MPL Core discriminator)。"""
    if not s:
        return -1
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    return (n.to_bytes((n.bit_length() + 7) // 8 or 1, "big"))[0]


def _ts(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _parse_when(s: str, *, end_of_day: bool = False) -> int:
    """解析 --from/--to：YYYY-MM-DD（UTC 當日起/迄）或 unix 秒。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(d.timestamp()) + (86399 if end_of_day else 0)


def _session() -> requests.Session:
    s = getattr(_tlocal, "s", None)
    if s is None:
        s = requests.Session(); _tlocal.s = s
    return s


def _rpc(url: str, method: str, params, retries: int = 6):
    for a in range(retries):
        try:
            r = _session().post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
            if r.status_code == 429:
                time.sleep(min(10, 0.5 * (a + 1))); continue
            j = r.json()
            if "error" in j:
                raise RuntimeError(str(j["error"])[:80])
            return j.get("result")
        except Exception:
            if a == retries - 1:
                return None
            time.sleep(0.4 * (a + 1))


def _parse_tx(tx: dict, collection: str):
    """從 getTransaction 結果抽 [(kind, asset, to, ins_index)]；kind=mint/transfer。"""
    out = []
    if not tx:
        return out
    msg = tx.get("transaction", {}).get("message", {})
    groups = [msg.get("instructions", [])]
    for inner in (tx.get("meta", {}) or {}).get("innerInstructions", []) or []:
        groups.append(inner.get("instructions", []))
    for ins_list in groups:
        for idx, ins in enumerate(ins_list):
            if ins.get("programId") != MPL_CORE:
                continue
            accs = ins.get("accounts") or []
            if len(accs) < 5 or accs[1] != collection:
                continue
            disc = _b58_first_byte(ins.get("data", ""))
            if disc == DISC_CREATE:
                out.append(("mint", accs[0], accs[4], idx))
            elif disc == DISC_TRANSFER:
                out.append(("transfer", accs[0], accs[4], idx))
    return out


def finalize_from(conn, platform_id):
    """補 nft_transfers.from_addr：同 asset 上一手 to(LAG)；首筆用該 asset 的 mint owner。"""
    with conn.cursor() as c:
        c.execute("""
            WITH seq AS (
              SELECT tx_hash, log_index,
                     LAG(to_addr) OVER (PARTITION BY token_id ORDER BY block_time, log_index) AS prev_to
              FROM nft_transfers WHERE platform_id=%s)
            UPDATE nft_transfers n SET from_addr = seq.prev_to
            FROM seq WHERE n.platform_id=%s AND n.tx_hash=seq.tx_hash AND n.log_index=seq.log_index
              AND seq.prev_to IS NOT NULL AND n.from_addr=''
        """, (platform_id, platform_id))
        c.execute("""
            UPDATE nft_transfers n SET from_addr = m.minter
            FROM (SELECT DISTINCT ON (token_id) token_id, minter FROM mints
                  WHERE platform_id=%s ORDER BY token_id, block_time) m
            WHERE n.platform_id=%s AND n.token_id=m.token_id AND n.from_addr=''
        """, (platform_id, platform_id))
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True)
    ap.add_argument("--days", type=int, default=7, help="抓近 N 天(0=全量 genesis)")
    ap.add_argument("--from", dest="from_when", default=None,
                    help="重跑範圍下界(含)：YYYY-MM-DD 或 unix 秒。指定後進入範圍模式，不動 floor 游標")
    ap.add_argument("--to", dest="to_when", default=None,
                    help="重跑範圍上界(含)：YYYY-MM-DD 或 unix 秒。預設到最新")
    ap.add_argument("--workers", type=int, default=50, help="getTransaction 並發數")
    ap.add_argument("--chunk", type=int, default=3000, help="每批並發/處理/commit 筆數")
    ap.add_argument("--reset", action="store_true", help="清 mpl_core cursor 重抓")
    ap.add_argument("--finalize-only", action="store_true")
    ap.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    args = ap.parse_args()

    load_env_file(Path(args.env_file).expanduser())
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        print("[nft] HELIUS_API_KEY 未設定"); return 1
    url = f"https://mainnet.helius-rpc.com/?api-key={key}"

    cfg = load_config(args.platform)
    if (cfg.get("chain") or "").lower() != "solana":
        print("[nft] 僅處理 solana"); return 1
    collections = [a for a in (cfg["contracts"].get("nft") or []) if a]
    if not collections:
        print("[nft] contracts.nft 為空"); return 1

    # 範圍模式：明確指定 [--from, --to]，重跑任意窗口（補洞用），不讀也不寫 floor 游標。
    range_mode = bool(args.from_when or args.to_when)
    range_top = None
    if range_mode:
        cutoff = _parse_when(args.from_when) if args.from_when else 0
        range_top = _parse_when(args.to_when, end_of_day=True) if args.to_when else None
    else:
        cutoff = 0 if args.days == 0 else int(time.time()) - args.days * 86400
    with get_conn() as conn:
        platform_id = upsert_platform(conn, args.platform, "solana", cfg.get("launch_block", 0), cfg)
        _upsert_refs(conn, platform_id, cfg)
        with conn.cursor() as c:
            c.execute(CURSOR_DDL)
            if args.reset:
                c.execute("DELETE FROM ingest_cursors WHERE platform_id=%s AND source=%s", (platform_id, CURSOR_SRC))
        conn.commit()
        if range_mode:
            print(f"[nft] platform_id={platform_id} 範圍模式 workers={args.workers} "
                  f"from={_ts(cutoff).date() if cutoff else 'genesis'} "
                  f"to={_ts(range_top).date() if range_top else 'latest'}（不動 floor 游標）")
        else:
            print(f"[nft] platform_id={platform_id} days={args.days or 'ALL'} workers={args.workers} "
                  f"cutoff={_ts(cutoff).date() if cutoff else 'genesis'}")

        if args.finalize_only:
            finalize_from(conn, platform_id); print("[nft] finalize 完成"); return 0

        mint_batch, tr_batch = [], []
        n_mint = n_tr = n_seen = 0

        def flush():
            if mint_batch:
                insert_mints(conn, platform_id, mint_batch); mint_batch.clear()
            if tr_batch:
                insert_nft_transfers(conn, platform_id, tr_batch); tr_batch.clear()
            conn.commit()

        for col in collections:
            floor = None
            if args.days == 0 and not range_mode:   # 全量才用 floor 續跑；範圍模式不讀游標
                with conn.cursor() as c:
                    c.execute("SELECT last_block_time FROM ingest_cursors WHERE platform_id=%s AND address=%s AND source=%s",
                              (platform_id, col, CURSOR_SRC))
                    r = c.fetchone(); floor = r[0] if r else None
            # 上界跳過門檻：範圍模式用 --to，否則用 resume floor（兩者皆 None 代表從最新開始）
            skip_above = range_top if range_mode else floor
            print(f"\n[nft] === {col} === floor={_ts(floor).date() if floor else 'none'}")

            def handle(buf):
                """buf: [(blockTime, sig, slot)] → 並發抓 tx、解析、寫入。回 min blockTime。"""
                nonlocal n_mint, n_tr, n_seen
                txs = {}
                with ThreadPoolExecutor(max_workers=args.workers) as ex:
                    futs = {ex.submit(_rpc, url, "getTransaction",
                                      [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]): sig
                            for _, sig, _ in buf}
                    for f in as_completed(futs):
                        txs[futs[f]] = f.result()
                bt_low = buf[0][0]
                for bt, sig, slot in buf:
                    n_seen += 1
                    if bt < bt_low:
                        bt_low = bt
                    for kind, asset, to, li in _parse_tx(txs.get(sig), col):
                        if kind == "mint":
                            mint_batch.append((platform_id, sig, li, _ts(bt), slot, to, asset, col, None))
                            n_mint += 1
                        else:
                            tr_batch.append((platform_id, sig, li, _ts(bt), slot, col, asset, "", to, None, None))
                            n_tr += 1
                    if len(mint_batch) + len(tr_batch) >= FLUSH:
                        flush()
                flush()
                return bt_low

            buf, done = [], 0
            before = None
            stop = False
            while not stop:
                sigs = _rpc(url, "getSignaturesForAddress", [col, {"limit": 1000, **({"before": before} if before else {})}])
                if not sigs:
                    break
                before = sigs[-1]["signature"]
                for s in sigs:
                    bt = s.get("blockTime") or 0
                    if cutoff and bt < cutoff:
                        stop = True; break
                    if skip_above is not None and bt > skip_above:
                        continue   # 跳過上界以上：resume 已完成段（floor）或 --to 之後（範圍模式）
                    buf.append((bt, s["signature"], s.get("slot") or 0))
                    if len(buf) >= args.chunk:
                        bt_low = handle(buf); done += len(buf); buf = []
                        if args.days == 0 and not range_mode:   # 全量推進 floor；範圍模式不動游標
                            with conn.cursor() as c:
                                c.execute("INSERT INTO ingest_cursors(platform_id,address,source,last_block_time) VALUES(%s,%s,%s,%s) "
                                          "ON CONFLICT (platform_id,address,source) DO UPDATE SET "
                                          "last_block_time=LEAST(ingest_cursors.last_block_time,EXCLUDED.last_block_time), updated_at=NOW()",
                                          (platform_id, col, CURSOR_SRC, bt_low))
                            conn.commit()
                        print(f"[nft]   累計 {done}  mints={n_mint} transfers={n_tr}  至 {_ts(bt_low).date()}")
                if len(sigs) < 1000:
                    break
            if buf:
                handle(buf); done += len(buf)
                print(f"[nft]   尾批 累計 {done}  mints={n_mint} transfers={n_tr}")

        print("\n[nft] 補算 from_addr ...")
        finalize_from(conn, platform_id)
        with conn.cursor() as c:
            for tbl in ("mints", "nft_transfers"):
                c.execute(f"SELECT COUNT(*) FROM {tbl} WHERE platform_id=%s", (platform_id,))
                print(f"[nft] {tbl} 總計: {c.fetchone()[0]}")
            c.execute("SELECT COUNT(*) FROM nft_transfers WHERE platform_id=%s AND from_addr=''", (platform_id,))
            print(f"[nft] from 仍空(前手在範圍外): {c.fetchone()[0]}")
    print(f"\n[nft] done. 本次 掃 {n_seen} 交易  mints={n_mint} transfers={n_tr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
