"""
ingest_card_activity.py — 抓每張卡的「開卡/賣回」活動，記錄真實卡包(clawId)與金額。

資料源(公開,免登入)：GET https://api.phygitals.com/api/marketplace/single-nft/activity?address={token_id}
  回 {activity:[{type, clawId, amount(micro-USD), from, to, time, txid, currency, credits_amount,
     discount_amount, voucher_id, ...}]}。type: CLAW(開卡,amount=卡包售價) / BUY(賣回,amount=回購價)。

⚠️ 限制(實測)：此端點**只回每張卡最近約 5 筆**、不支援分頁 → 拿不到深層歷史；txid 為 hex(非鏈上 base58 簽名)。
   故本表是「真實帳本金額」的近期樣本；欲補歷史開卡價，可用 clawId→卡包定價(vm/available 固定價)推估。

寫入 card_activity(platform_id, token_id, txid, event_time, type, amount_usd, claw_id, from_addr, to_addr,
   currency, credits_amount, discount_amount)。PK=(platform_id, txid)。冪等。

用法：
  .venv/bin/python ingest_card_activity.py --platform phygitals --limit 200   # 小樣本測試
  .venv/bin/python ingest_card_activity.py --platform phygitals               # 全量(每張卡近 5 筆)
  .venv/bin/python ingest_card_activity.py --platform phygitals --from 2026-06-01 --to 2026-07-01  # 只掃該範圍有交易的卡
  旗標：--workers(40)
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

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
from ingest import get_conn, load_config, upsert_platform
from ingest_solana import _upsert_refs
from tools.bootstrap import load_env_file

ACT = "https://api.phygitals.com/api/marketplace/single-nft/activity"
VM = "https://api.phygitals.com/api/vm/available?includeRepacks=true&platform=mainnet"
_tl = threading.local()

# 只有 CLAW = 開卡（amount=卡包售價）；其餘為市場/借貸事件（context 用）。
OPEN_TYPE = "CLAW"

DDL = """
CREATE TABLE IF NOT EXISTS card_activity (
    platform_id    INT,
    token_id       TEXT NOT NULL,
    txid           TEXT NOT NULL,
    event_time     TIMESTAMPTZ,
    type           TEXT,               -- CLAW(開卡) / BUY(賣回) / LIST/DELIST/BID/PACK_PARTY/JUPITER_*…
    amount_usd     NUMERIC(20,6),      -- micro-USD ÷ 1e6（CLAW=卡包售價）
    claw_id        TEXT,               -- 原始值：vm.id(數字) 或 slug；None=非開卡事件
    pack_slug      TEXT,               -- resolve 後的卡包 slug（統一）
    pack_name      TEXT,               -- 卡包顯示名
    pack_price_usd NUMERIC(20,6),      -- vm/available 的 mint_price（卡包定價）
    from_addr      TEXT,
    to_addr        TEXT,
    currency       TEXT,
    credits_amount NUMERIC(20,6),
    discount_amount NUMERIC(20,6),
    ingested_at    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (platform_id, txid)
);
CREATE INDEX IF NOT EXISTS idx_card_activity_token ON card_activity(platform_id, token_id);
CREATE INDEX IF NOT EXISTS idx_card_activity_type  ON card_activity(platform_id, type);
CREATE INDEX IF NOT EXISTS idx_card_activity_pack  ON card_activity(platform_id, pack_slug);
"""


def load_pack_lookup():
    """vm/available(公開) → {clawId(數字id 與 slug 皆可) : (slug, name, price)}。"""
    r = requests.get(VM, headers={"user-agent": "Mozilla/5.0"}, timeout=30)
    packs = r.json()
    lut = {}
    for p in packs:
        slug = p.get("slug"); name = p.get("name")
        try:
            price = float(p.get("mint_price"))
        except (TypeError, ValueError):
            price = None
        rec = (slug, name, price)
        if p.get("id") is not None:
            lut[str(p["id"])] = rec        # 數字型 clawId
        if slug:
            lut[slug] = rec                # slug 型 clawId
    print(f"[packs] vm/available 載入 {len(packs)} 卡包，對照鍵 {len(lut)}")
    return lut


def _sess():
    s = getattr(_tl, "s", None)
    if s is None:
        s = requests.Session(); s.headers["user-agent"] = "Mozilla/5.0"; _tl.s = s
    return s


def _num(v, div=1.0):
    try:
        return float(v) / div
    except (TypeError, ValueError):
        return None


class RateLimited(Exception):
    pass


# 全域限速閘（跨執行緒）：Cloudflare(1015) 對此端點很敏感，須控制「持續」req/s。
_gate_lock = threading.Lock()
_next_slot = [0.0]
_min_interval = [0.0]   # 由 main 依 --rps 設定


def _rate_gate():
    iv = _min_interval[0]
    if iv <= 0:
        return
    with _gate_lock:
        t = max(time.monotonic(), _next_slot[0])
        _next_slot[0] = t + iv
    d = t - time.monotonic()
    if d > 0:
        time.sleep(d)


def fetch_activity(token_id, retries=5):
    """回 (token_id, activity|None)。
    ⚠️ 區分三態：正常(200,含真空[])、暫時失敗(429/5xx→退避重試,不可當空)、放棄。
    Cloudflare 對此端點限流很敏感(429 error 1015)：務必尊重 429、用保守並發。"""
    for a in range(retries):
        try:
            _rate_gate()
            r = _sess().get(ACT, params={"address": token_id}, timeout=15)
            if r.status_code == 429 or r.status_code == 503:
                raise RateLimited()
            if r.status_code != 200:
                return token_id, None
            return token_id, (r.json().get("activity") or [])
        except RateLimited:
            time.sleep(min(30, 2 ** a))   # 1,2,4,8,16,30s 退避
        except Exception:
            if a >= 2:
                return token_id, None
            time.sleep(0.5)
    return token_id, "RATELIMITED"   # 用盡仍被擋：回哨兵,呼叫端可重排不當空


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True)
    ap.add_argument("--from", dest="d1", default=None)
    ap.add_argument("--to", dest="d2", default=None)
    ap.add_argument("--workers", type=int, default=8)   # 保守：Cloudflare 1015 對此端點敏感
    ap.add_argument("--rps", type=float, default=8.0, help="全域持續 req/s 上限(避免 Cloudflare 封;預設 8)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--env-file", default=str(REPO / ".env"))
    args = ap.parse_args()
    load_env_file(Path(args.env_file).expanduser())
    _min_interval[0] = 1.0 / args.rps if args.rps > 0 else 0.0

    cfg = load_config(args.platform)
    pack_lut = load_pack_lookup()
    with get_conn() as conn:
        platform_id = upsert_platform(conn, args.platform, cfg.get("chain", "solana"), cfg.get("launch_block", 0), cfg)
        _upsert_refs(conn, platform_id, cfg)
        with conn.cursor() as c:
            c.execute(DDL)
        conn.commit()

        with conn.cursor() as c:
            cond = "AND block_time>=%s AND block_time<%s" if args.d1 else ""
            a = [platform_id] + ([args.d1, args.d2] if args.d1 else [])
            # 排除「已有 activity 事件」的卡 → 重跑=增量。用 anti-join(比 NOT IN 快很多;
            # NOT IN 在 260 萬列會超慢並卡住 DB)。
            c.execute(f"""WITH scope AS (SELECT DISTINCT token_id FROM nft_transfers
                                         WHERE platform_id=%s {cond}),
                               have AS (SELECT DISTINCT token_id FROM card_activity WHERE platform_id=%s)
                          SELECT s.token_id FROM scope s LEFT JOIN have h USING(token_id)
                          WHERE h.token_id IS NULL""",
                      a + [platform_id])
            todo = [r[0] for r in c.fetchall()]
        if args.limit:
            todo = todo[:args.limit]
        print(f"待抓 activity: {len(todo):,} 卡 (workers={args.workers} rps={args.rps})")

        n_ev = n_claw = n_buy = done = no_data = 0
        blocked = []   # 用盡重試仍 429 的卡,最後再跑一輪
        CHUNK = 400
        for i in range(0, len(todo), CHUNK):
            chunk = todo[i:i+CHUNK]
            rows = []
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futs = {ex.submit(fetch_activity, t): t for t in chunk}
                for f in as_completed(futs):
                    tid, acts = f.result()
                    if acts == "RATELIMITED":
                        blocked.append(tid); continue
                    if not acts:
                        no_data += 1; continue
                    for e in acts:
                        txid = e.get("txid")
                        if not txid:
                            continue
                        t = e.get("type")
                        if t == "CLAW":
                            n_claw += 1
                        elif t == "BUY":
                            n_buy += 1
                        et = e.get("time")
                        try:
                            et = datetime.fromisoformat(et.replace("Z", "+00:00")) if et else None
                        except Exception:
                            et = None
                        claw = e.get("clawId")
                        pslug = pname = pprice = None
                        if claw is not None:
                            rec = pack_lut.get(str(claw))
                            if rec:
                                pslug, pname, pprice = rec
                            else:
                                pslug = str(claw)   # 對不到 vm(已下架卡包)：保留原值當 slug
                        rows.append((platform_id, tid, txid, et, t, _num(e.get("amount"), 1e6),
                                     None if claw is None else str(claw), pslug, pname, pprice,
                                     e.get("from"), e.get("to"), e.get("currency"),
                                     _num(e.get("credits_amount"), 1e6), _num(e.get("discount_amount"), 1e6)))
                        n_ev += 1
            if rows:
                with conn.cursor() as c:
                    c.executemany(
                        "INSERT INTO card_activity(platform_id,token_id,txid,event_time,type,amount_usd,"
                        "claw_id,pack_slug,pack_name,pack_price_usd,from_addr,to_addr,currency,credits_amount,discount_amount) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (platform_id,txid) DO UPDATE "
                        "SET amount_usd=EXCLUDED.amount_usd, claw_id=EXCLUDED.claw_id, pack_slug=EXCLUDED.pack_slug, "
                        "pack_name=EXCLUDED.pack_name, pack_price_usd=EXCLUDED.pack_price_usd, type=EXCLUDED.type", rows)
                conn.commit()
            done += len(chunk)
            print(f"  {done}/{len(todo)}  events={n_ev} CLAW={n_claw} BUY={n_buy} 無資料={no_data} 被擋={len(blocked)}")
        if blocked:
            print(f"\n⚠️ {len(blocked):,} 卡被 429 擋掉（未入庫）。冷卻幾分鐘後重跑本程式即可補（已入庫的會自動略過）。")

        # 報告
        with conn.cursor() as c:
            c.execute("SELECT count(*), count(*) filter(where type='CLAW'), count(distinct token_id) FROM card_activity WHERE platform_id=%s", (platform_id,))
            tot, claw, cards = c.fetchone()
            print(f"\ncard_activity: 事件={tot:,} 其中開卡(CLAW)={claw:,} 涉及卡={cards:,}")
            c.execute("SELECT count(*) FROM card_activity WHERE platform_id=%s AND type='CLAW' AND pack_slug IS NULL", (platform_id,))
            print(f"  開卡但 pack 對不到(無 clawId 或非現行卡包): {c.fetchone()[0]:,}")
            c.execute("""SELECT coalesce(pack_slug,'(未知)') pack, count(*) n, round(avg(amount_usd),2) 開卡均價,
                         max(pack_price_usd) vm定價, round(sum(amount_usd)) 開卡總額
                         FROM card_activity WHERE platform_id=%s AND type='CLAW' GROUP BY 1 ORDER BY 5 DESC NULLS LAST LIMIT 15""", (platform_id,))
            print("開卡(CLAW)卡包分布 top15（開卡均價應=vm定價）:")
            for pack, n, avg, vmp, s in c.fetchall():
                print(f"  {str(pack):28} 次={n:>6,} 均價=${avg} vm定價=${vmp} 總額=${int(s or 0):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
