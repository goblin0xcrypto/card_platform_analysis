"""
funder_cluster_omega.py — 一次性分析腳本（非管線）。

反查 OMEGA $48 卡機近兩月所有 opener(>=MIN_PACKS 包) 的「第一筆 BNB 金主」，
做 union-find 分群，合併同金主錢包後重算 S 卡命中率，並做 Poisson 上尾檢定。

目的：檢驗「有人用多個錢包分散開包、規避單地址偵測來刷 S 卡」的假說。
funder 結果快取到 tools/_funder_cache_omega.json，可重跑不重抓。
"""
import json, os, sys
from math import exp, factorial
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import psycopg
from tools.bootstrap import load_env_file
from tools.explorer import EtherscanV2Client

load_env_file(REPO / ".env")
URL = os.environ["DATABASE_URL"]
KEY = os.environ["ETHERSCAN_API_KEY"]
OMEGA = "0x94e7732b0b2e7c51ffd0d56580067d9c2e2b7910"
SINCE = "2026-04-22"
MIN_PACKS = 10
CACHE = REPO / "tools" / "_funder_cache_omega.json"

# 不能當 funder 的地址：平台自家 + USDT + 0x0
EXCLUDE = {
    OMEGA, "0x0000000000000000000000000000000000000000",
    "0x55d398326f99059ff775485246999027b3197955",  # USDT
    "0xaab5f5fa75437a6e9e7004c12c9c56cda4b4885a",
    "0xb2891022648c5fad3721c42c05d8d283d4d53080",
    "0xfda4a907d23d9f24271bc47483c5b983831e325e",
    "0xae3e7268ef5a062946216a44f58a8f685ffd11d0",
    "0x12639a4147af7c777bb9a21fdaefa5ea91e2e3a3",
    "0x2e7c38fd92cacab8f218cd529c2a3fe70b4201e9",
    "0x9c84bd30a694cb2b0b3cc3810d621973bd3dab9d",
    "0x48fbb6eaa5f8ba3068562b7518b970b32ca7fa8e",
}

def fetch_openers():
    q = """
    WITH opens AS (
      SELECT opener addr, SUM(quantity) packs FROM pack_opens po JOIN platforms p ON po.platform_id=p.id
      WHERE p.name='renaiss' AND po.pack_id=%(o)s AND po.block_time>=%(s)s GROUP BY 1),
    s AS (
      SELECT to_addr addr, COUNT(*) FILTER (WHERE amount_usd>200) s_hits FROM payments pa JOIN platforms p ON pa.platform_id=p.id
      WHERE p.name='renaiss' AND pa.direction='payout' AND pa.from_addr=%(o)s AND pa.block_time>=%(s)s GROUP BY 1)
    SELECT o.addr, o.packs::int, COALESCE(s.s_hits,0)
    FROM opens o LEFT JOIN s USING(addr) WHERE o.packs>=%(m)s;
    """
    with psycopg.connect(URL) as c, c.cursor() as cur:
        cur.execute(q, {"o": OMEGA, "s": SINCE, "m": MIN_PACKS})
        return {r[0]: {"packs": r[1], "s": r[2]} for r in cur.fetchall()}

def first_funder(ex, addr):
    """最早一筆「收到 BNB」的 from。先看 normal tx，再 fallback internal。"""
    best = None
    try:
        for t in ex.txlist(addr, 0, 999999999):
            if t.to_addr == addr and t.value_raw > 0 and t.from_addr:
                best = (t.block_number, t.from_addr); break
    except Exception as e:
        print(f"  txlist err {addr[:10]}: {e}")
    if best is None:
        try:
            for t in ex.txlistinternal(addr, 0, 999999999):
                if t.to_addr == addr and t.value_raw > 0 and t.from_addr:
                    best = (t.block_number, t.from_addr); break
        except Exception as e:
            print(f"  internal err {addr[:10]}: {e}")
    return best[1] if best else None

def main():
    openers = fetch_openers()
    print(f"openers(>= {MIN_PACKS} packs) = {len(openers)}")
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    ex = EtherscanV2Client(KEY, "bsc")
    todo = [a for a in openers if a not in cache]
    print(f"need fetch funder = {len(todo)} (cached {len(cache)})")
    for i, a in enumerate(todo, 1):
        cache[a] = first_funder(ex, a)
        if i % 25 == 0:
            print(f"  {i}/{len(todo)} ...")
            CACHE.write_text(json.dumps(cache))
    CACHE.write_text(json.dumps(cache))

    # union-find：addr 與其 funder 同群；同 funder 的 addr 自動同群
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb: parent[ra] = rb
    for a in openers:
        f = cache.get(a)
        if f and f not in EXCLUDE:
            union(a, f)
        else:
            find(a)  # 自成一群

    # 聚合 cluster（只算 opener 成員的 packs/S）
    from collections import defaultdict
    cl = defaultdict(lambda: {"packs": 0, "s": 0, "members": [], "funder": None})
    for a in openers:
        root = find(a)
        cl[root]["packs"] += openers[a]["packs"]
        cl[root]["s"] += openers[a]["s"]
        cl[root]["members"].append(a)
        f = cache.get(a)
        if f and f not in EXCLUDE:
            cl[root]["funder"] = f

    totp = sum(v["packs"] for v in cl.values())
    tots = sum(v["s"] for v in cl.values())
    rate = tots / totp
    print(f"\nclusters={len(cl)}  total_packs={totp}  total_S={tots}  baseline={rate*100:.3f}%")

    from math import lgamma, log
    def pois_tail(k, lam):  # P(X>=k)，log-space 穩定
        if k <= 0: return 1.0
        if lam <= 0: return 0.0
        cdf = sum(exp(-lam + i*log(lam) - lgamma(i+1)) for i in range(k))
        return max(0.0, 1 - cdf)

    rows = []
    for root, v in cl.items():
        lam = v["packs"] * rate
        p = pois_tail(v["s"], lam)
        rows.append((p, len(v["members"]), v["packs"], v["s"], lam, v["funder"], v["members"]))
    multi = [r for r in rows if r[1] >= 2]  # 多錢包群才有「分散規避」意義
    multi.sort()
    N = len(rows)
    bonf = 0.05 / N
    print(f"多錢包群(>=2 wallets)={len(multi)}  Bonferroni門檻 p<{bonf:.2e}")
    print(f"\n=== 多錢包群 最異常前 15 ===")
    print(f"{'p_value':>10} {'wal':>4} {'packs':>6} {'S':>3} {'expS':>6} {'rate%':>6}  funder")
    for p, nm, packs, s, lam, funder, members in multi[:15]:
        fmark = (funder[:12] if funder else "(no funder)")
        print(f"{p:>10.2e} {nm:>4} {packs:>6} {s:>3} {lam:>6.2f} {100*s/packs:>6.2f}  {fmark}  pass={'Y' if p<bonf else ''}")
    # 最大的幾個多錢包群（看是不是 CEX 把人併在一起）
    big = sorted(multi, key=lambda r: -r[1])[:8]
    print(f"\n=== 成員最多的多錢包群（檢查是否 CEX） ===")
    for p, nm, packs, s, lam, funder, members in big:
        print(f" wallets={nm:>3} packs={packs:>6} S={s:>3} rate={100*s/packs:>5.2f}% p={p:.2e} funder={funder}")

if __name__ == "__main__":
    main()
