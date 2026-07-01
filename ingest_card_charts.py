"""
ingest_card_charts.py — 用 phygitals 公開的「單卡價格歷史」端點，給 nft_transfers 逐筆金額。

端點(公開,免登入)：GET https://api.phygitals.com/api/marketplace/single-nft/chart?address={token_id}
  回 price_history.altValueTimeSeries = {data:[逐日 ALT 估值 366 天], startDate, endDate}(過去一年逐日)。
  可直接吃鏈上 token_id(平台會 resolve 到卡片身分)。→ 每筆交易用「成交當天」的估值定價。

流程：
  1. 取 nft_transfers 的 distinct token_id(可用 --from/--to 限範圍先做該週)。
  2. 並發抓每個 token_id 的 chart,存 card_charts(token_id, start_date, data[]) 。
  3. 回填 nft_transfers.amount_usd = 該卡逐日序列在 block_time 當天的值(超出區間則取端點值)。

冪等：card_charts PK=token_id;重跑覆寫。amount_usd 依 chart 重算。
成本：公開 API,並發即可;distinct token_id 數決定量(該週~1萬,全量~17萬)。

用法：
  .venv/bin/python ingest_card_charts.py --platform phygitals --from 2026-05-01 --to 2026-05-08   # 先做該週
  .venv/bin/python ingest_card_charts.py --platform phygitals                                     # 全量
  .venv/bin/python ingest_card_charts.py --platform phygitals --backfill-only                     # 只重算金額
  旗標：--workers(40) / --limit(N 測試)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, date
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
from ingest import get_conn, load_config, upsert_platform
from ingest_solana import _upsert_refs
from tools.bootstrap import load_env_file

CHART = "https://api.phygitals.com/api/marketplace/single-nft/chart"
_tl = threading.local()

DDL = """
CREATE TABLE IF NOT EXISTS card_charts (
    platform_id INT,
    token_id    TEXT NOT NULL,
    start_date  DATE,
    end_date    DATE,
    data        DOUBLE PRECISION[],
    name        TEXT,
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (platform_id, token_id)
);
"""


def _sess():
    s = getattr(_tl, "s", None)
    if s is None:
        s = requests.Session(); s.headers["user-agent"] = "Mozilla/5.0"; _tl.s = s
    return s


def fetch_chart(token_id, retries=4):
    for a in range(retries):
        try:
            r = _sess().get(CHART, params={"address": token_id}, timeout=25)
            if r.status_code == 429:
                time.sleep(1.0 * (a + 1)); continue
            if r.status_code != 200:
                return None
            j = r.json()
            ts = (((j.get("price_history") or {}).get("altValueTimeSeries")) or {})
            data = ts.get("data")
            if not isinstance(data, list) or not data:
                return {"empty": True}
            return {"start": ts.get("startDate"), "end": ts.get("endDate"),
                    "data": data, "name": (j.get("price_data") or {}).get("name")}
        except Exception:
            time.sleep(0.5 * (a + 1))
    return None


def value_on(start_date: date, data: list, d: date):
    if not data:
        return None
    idx = (d - start_date).days
    if idx < 0:
        idx = 0
    if idx >= len(data):
        idx = len(data) - 1
    v = data[idx]
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


def backfill(conn, platform_id, D1, D2):
    """用 card_charts 逐日序列,依 block_time 當天回填 nft_transfers.amount_usd。"""
    with conn.cursor() as c:
        c.execute("ALTER TABLE nft_transfers ADD COLUMN IF NOT EXISTS amount_usd NUMERIC(20,6)")
        c.execute("SELECT token_id, start_date, data FROM card_charts WHERE platform_id=%s AND data IS NOT NULL", (platform_id,))
        charts = {t: (sd, dat) for t, sd, dat in c.fetchall()}
    if not charts:
        print("[backfill] card_charts 無資料"); return
    with conn.cursor() as c:
        cond = "AND block_time>=%s AND block_time<%s" if D1 else ""
        args = [platform_id] + ([D1, D2] if D1 else [])
        c.execute(f"SELECT tx_hash, log_index, token_id, block_time FROM nft_transfers "
                  f"WHERE platform_id=%s {cond}", args)
        rows = c.fetchall()
    updates = []
    for tx, li, tid, bt in rows:
        ch = charts.get(tid)
        if not ch or not ch[0]:
            continue
        v = value_on(ch[0], ch[1], bt.date())
        if v:
            updates.append((v, platform_id, tx, li))
    with conn.cursor() as c:
        c.executemany("UPDATE nft_transfers SET amount_usd=%s WHERE platform_id=%s AND tx_hash=%s AND log_index=%s", updates)
    conn.commit()
    print(f"[backfill] 回填 {len(updates):,} 筆 amount_usd")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True)
    ap.add_argument("--from", dest="d1", default=None)
    ap.add_argument("--to", dest="d2", default=None)
    ap.add_argument("--workers", type=int, default=40)
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 個 token_id(測試)")
    ap.add_argument("--backfill-only", action="store_true")
    ap.add_argument("--env-file", default=str(REPO / ".env"))
    args = ap.parse_args()
    load_env_file(Path(args.env_file).expanduser())

    cfg = load_config(args.platform)
    with get_conn() as conn:
        platform_id = upsert_platform(conn, args.platform, cfg.get("chain", "solana"), cfg.get("launch_block", 0), cfg)
        _upsert_refs(conn, platform_id, cfg)
        with conn.cursor() as c:
            c.execute(DDL)
        conn.commit()

        if not args.backfill_only:
            with conn.cursor() as c:
                cond = "AND block_time>=%s AND block_time<%s" if args.d1 else ""
                a = [platform_id] + ([args.d1, args.d2] if args.d1 else [])
                c.execute(f"SELECT DISTINCT token_id FROM nft_transfers WHERE platform_id=%s {cond} "
                          f"AND token_id NOT IN (SELECT token_id FROM card_charts WHERE platform_id=%s)",
                          a + [platform_id])
                todo = [r[0] for r in c.fetchall()]
            if args.limit:
                todo = todo[:args.limit]
            print(f"待抓 chart 的 distinct token_id: {len(todo):,} (workers={args.workers})")
            done = ok = empty = 0
            buf = []
            for i in range(0, len(todo), 500):
                chunk = todo[i:i+500]
                with ThreadPoolExecutor(max_workers=args.workers) as ex:
                    futs = {ex.submit(fetch_chart, t): t for t in chunk}
                    for f in as_completed(futs):
                        t = futs[f]; res = f.result()
                        if res and res.get("data"):
                            buf.append((platform_id, t, res["start"], res["end"], res["data"], (res.get("name") or "")[:200]))
                            ok += 1
                        else:
                            buf.append((platform_id, t, None, None, None, None)); empty += 1
                with conn.cursor() as c:
                    c.executemany("INSERT INTO card_charts(platform_id,token_id,start_date,end_date,data,name) "
                                  "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (platform_id,token_id) DO UPDATE "
                                  "SET start_date=EXCLUDED.start_date,end_date=EXCLUDED.end_date,data=EXCLUDED.data,"
                                  "name=EXCLUDED.name,updated_at=NOW()", buf)
                conn.commit(); buf.clear()
                done += len(chunk)
                print(f"  {done}/{len(todo)}  有價 {ok} 無價 {empty}")

        backfill(conn, platform_id, args.d1, args.d2)
        # 覆蓋率報告
        with conn.cursor() as c:
            cond = "AND block_time>=%s AND block_time<%s" if args.d1 else ""
            a = [platform_id] + ([args.d1, args.d2] if args.d1 else [])
            c.execute(f"SELECT count(*), count(amount_usd), round(coalesce(sum(amount_usd),0)) "
                      f"FROM nft_transfers WHERE platform_id=%s {cond}", a)
            tot, val, s = c.fetchone()
            rng = f"{args.d1}~{args.d2}" if args.d1 else "全期"
            print(f"\n[{rng}] nft_transfers {tot:,} 筆，有金額 {val:,} ({val*100//max(tot,1)}%)，GMV=${int(s):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
