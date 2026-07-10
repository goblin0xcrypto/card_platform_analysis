"""
funder_cluster_phygitals.py — 找「同一 funder 資助多個刷量地址」的 sybil 分群。

背景：payments 表只收與平台互動的 USDC,看不到「操盤者→其多個 farmer 錢包」的轉帳。
      故改從鏈上抓每個刷量錢包的『第一筆 SOL 資金來源(funder)』(Solscan Pro),再按 funder 分群。

刷量地址定義：2026-04~06 開卡(62Q9→它)且賣回(它→平台)各 >= MIN 次。
funder：該錢包最早一筆 inbound 轉帳的 from(通常是撥 SOL 開帳的金主)。
輸出：資助 >=3 個刷量地址的 funder(疑操盤者),及其資助地址的總刷量。

快取：tools/_funder_cache_phygitals.json(可重跑不重抓)。
用法：.venv/bin/python funder_cluster_phygitals.py [--min 50] [--limit N]
"""
import argparse, json, os, sys, time
from collections import defaultdict
from pathlib import Path
import requests

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import psycopg, yaml
from tools.bootstrap import load_env_file

load_env_file(REPO / ".env")
BASE = "https://pro-api.solscan.io/v2.0"
KEY = os.environ["SOLANA_API_KEY"]
CACHE = REPO / "tools" / "_funder_cache_phygitals.json"
V = "62Q9eeDY3eM8A5CnprBGYMPShdBjAzdpBdr71QHsS8dS"


def get_conn():
    return psycopg.connect(os.environ["DATABASE_URL"])


def first_funder(addr, sess):
    """該地址最早一筆 inbound 轉帳的 from = 資金來源。"""
    for a in range(4):
        try:
            r = sess.get(f"{BASE}/account/transfer", headers={"token": KEY},
                         params={"address": addr, "flow": "in", "sort_by": "block_time",
                                 "sort_order": "asc", "page": 1, "page_size": 10}, timeout=30)
            if r.status_code == 429:
                time.sleep(1.5 * (a + 1)); continue
            data = r.json().get("data") or []
            for row in data:
                frm = row.get("from_address")
                if frm and frm != addr:
                    return frm
            return None
        except Exception:
            time.sleep(1)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=50, help="開卡與賣回各至少幾次才算刷量地址")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    cfg = yaml.safe_load(open(REPO / "platforms/phygitals/config.yaml"))
    ow = cfg["official_wallets"]; CL = [ow["operations"], ow["fee_payer"]] + list(ow["usdc_cluster"])

    conn = get_conn(); c = conn.cursor()
    c.execute("""
      with o as (select to_addr a, count(*) opens from nft_transfers where platform_id=16 and from_addr=%s
                 and not(to_addr=any(%s)) and block_time>='2026-04-01' and block_time<'2026-07-01' group by 1),
           s as (select from_addr a, count(*) sells from nft_transfers where platform_id=16 and to_addr=any(%s)
                 and not(from_addr=any(%s)) and block_time>='2026-04-01' and block_time<'2026-07-01' group by 1)
      select a, coalesce(opens,0)+coalesce(sells,0) vol from o full join s using(a)
      where coalesce(opens,0)>=%s and coalesce(sells,0)>=%s order by 2 desc
    """, (V, CL, CL, CL, args.min, args.min))
    farmers = [(r[0], r[1]) for r in c.fetchall()]
    if args.limit:
        farmers = farmers[:args.limit]
    vol = dict(farmers)
    print(f"刷量地址(開卡+賣回各>={args.min}): {len(farmers):,} → 抓 funder…")

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    sess = requests.Session()
    done = 0
    for addr, _ in farmers:
        if addr not in cache:
            cache[addr] = first_funder(addr, sess)
            time.sleep(0.2)
            done += 1
            if done % 100 == 0:
                CACHE.write_text(json.dumps(cache)); print(f"  抓了 {done} 個…")
    CACHE.write_text(json.dumps(cache))

    # 依 funder 分群
    grp = defaultdict(list)
    for addr, _ in farmers:
        f = cache.get(addr)
        if f and f not in CL:               # 排除平台錢包當 funder
            grp[f].append(addr)
    clusters = sorted(((f, addrs) for f, addrs in grp.items() if len(addrs) >= 3),
                      key=lambda x: -len(x[1]))
    print(f"\n=== 資助 >=3 個刷量地址的 funder(疑 sybil 操盤者): {len(clusters)} 組 ===")
    print(f"{'funder':16}{'資助刷量地址數':>14}{'該群總刷量(開卡+賣回)':>20}")
    for f, addrs in clusters[:25]:
        tvol = sum(vol.get(a, 0) for a in addrs)
        print(f"  {f[:14]}…{len(addrs):>14}{tvol:>20,}")
    # 覆蓋:多少刷量地址落在某個 sybil 群
    in_cluster = sum(len(a) for _, a in clusters)
    print(f"\n落在 sybil 群(同funder>=3)的刷量地址: {in_cluster:,}/{len(farmers):,}")
    conn.close()


if __name__ == "__main__":
    main()
