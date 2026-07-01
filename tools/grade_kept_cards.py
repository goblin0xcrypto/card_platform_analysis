"""
grade_kept_cards.py — 一次性分析腳本（非管線）。

補「buyback 認 S 卡」的盲點：抓 ERC-721 graded-card metadata，
用「賣回卡 name → buyback 金額」建價值對照，判定「留著沒賣的卡」中有多少 S 卡。

流程：
  1. DB 取 sold token_id→max buyback（建 name→value）與 kept token_id（last holder=玩家）
  2. 對每個 token：eth_call tokenURI → HTTP 取 metadata → 取 name/Set/Grade
  3. S name set = 賣回 >$200 的卡 name；kept 卡 name ∈ S set → 漏算的留卡 S
快取：tools/_card_meta_cache.json（{token_id: {name,set,grade}}），可重入。
"""
import json, os, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import cycle
from pathlib import Path
from threading import Lock

REPO=Path(__file__).resolve().parent.parent; sys.path.insert(0,str(REPO))
import psycopg
URL="postgresql://luowenhong@localhost:5432/card_platform_analysis"
NFT="0xf8646a3ca093e97bb404c3b25e675c0394dd5b30"
OMEGA="0x94e7732b0b2e7c51ffd0d56580067d9c2e2b7910"
RPCS=["https://bsc-dataseed.bnbchain.org","https://bsc-rpc.publicnode.com","https://binance.llamarpc.com",
      "https://bsc-dataseed1.defibit.io","https://bsc.drpc.org","https://bsc-dataseed2.defibit.io"]
rpc_pool=cycle(RPCS)
CACHE=REPO/"tools"/"_card_meta_cache.json"
PLAT=("0x94e7732b0b2e7c51ffd0d56580067d9c2e2b7910","0x0000000000000000000000000000000000000000",
      "0xaab5f5fa75437a6e9e7004c12c9c56cda4b4885a","0xb2891022648c5fad3721c42c05d8d283d4d53080",
      "0xfda4a907d23d9f24271bc47483c5b983831e325e")

def db():
    with psycopg.connect(URL) as c, c.cursor() as cur:
        # sold token_id -> max buyback in its sellback tx
        cur.execute("""
            WITH pay AS (SELECT tx_hash, MAX(amount_usd) amt FROM payments pa JOIN platforms p ON pa.platform_id=p.id
              WHERE p.name='renaiss' AND pa.direction='payout' AND pa.from_addr=%s GROUP BY tx_hash)
            SELECT nt.token_id, MAX(pay.amt)::numeric(10,0)
            FROM pay JOIN nft_transfers nt ON nt.tx_hash=pay.tx_hash JOIN platforms p ON nt.platform_id=p.id
            WHERE p.name='renaiss' AND nt.contract=%s GROUP BY nt.token_id
        """,(OMEGA,NFT))
        sold={r[0]:float(r[1]) for r in cur.fetchall()}
        # kept token_id -> current holder
        cur.execute("""
            WITH lm AS (SELECT DISTINCT ON (token_id) token_id, to_addr FROM nft_transfers nt JOIN platforms p ON nt.platform_id=p.id
              WHERE p.name='renaiss' AND nt.contract=%s ORDER BY token_id, block_number DESC, log_index DESC)
            SELECT token_id, to_addr FROM lm WHERE to_addr <> ALL(%s)
        """,(NFT,list(PLAT)))
        kept={r[0]:r[1] for r in cur.fetchall()}
    return sold, kept

def decstr(h):
    b=bytes.fromhex(h[2:]); off=int.from_bytes(b[0:32],"big"); ln=int.from_bytes(b[off:off+32],"big")
    return b[off+32:off+32+ln].decode("utf-8","replace")

def fetch(tid):
    data="0xc87b56dd"+format(int(tid),"064x")
    body=json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":NFT,"data":data},"latest"]}).encode()
    uri=None
    for _ in range(4):
        rp=next(rpc_pool)
        try:
            req=urllib.request.Request(rp,body,{"Content-Type":"application/json"})
            res=json.load(urllib.request.urlopen(req,timeout=15))
            if res.get("result") and res["result"]!="0x":
                uri=decstr(res["result"]); break
        except Exception: continue
    if not uri: return tid, None
    for _ in range(3):
        try:
            req=urllib.request.Request(uri,headers={"User-Agent":"Mozilla/5.0"})
            m=json.load(urllib.request.urlopen(req,timeout=20))
            at={a["trait_type"]:a["value"] for a in (m.get("attributes") or [])}
            return tid, {"name":m.get("name"),"set":at.get("Set"),"grade":at.get("Grade")}
        except Exception: continue
    return tid, None

def main():
    sold, kept = db()
    print(f"sold(有buyback)={len(sold)}  kept(留卡)={len(kept)}")
    sold_s=[t for t,v in sold.items() if v>200]
    print(f"sold S(>200)={len(sold_s)}")
    cache=json.loads(CACHE.read_text()) if CACHE.exists() else {}
    need=[t for t in (sold_s+list(kept)) if t not in cache]
    print(f"待抓 metadata={len(need)} (快取已有 {len(cache)})")
    lock=Lock(); done=[0]
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs={ex.submit(fetch,t):t for t in need}
        for f in as_completed(futs):
            tid,meta=f.result()
            with lock:
                cache[tid]=meta; done[0]+=1
                if done[0]%200==0:
                    CACHE.write_text(json.dumps(cache)); print(f"  {done[0]}/{len(need)}")
    CACHE.write_text(json.dumps(cache))
    ok=sum(1 for t in (sold_s+list(kept)) if cache.get(t))
    print(f"成功取得 metadata: {ok}/{len(sold_s)+len(kept)}")
    # S name set + value
    from collections import defaultdict
    sname_val=defaultdict(list)
    for t in sold_s:
        m=cache.get(t)
        if m and m.get("name"): sname_val[m["name"]].append(sold[t])
    s_names={n:(sum(v)/len(v)) for n,v in sname_val.items()}
    print(f"S 卡 name 種類={len(s_names)}")
    # classify kept
    kept_s=[]; kept_unknown=0
    for t,holder in kept.items():
        m=cache.get(t)
        if not m or not m.get("name"): kept_unknown+=1; continue
        if m["name"] in s_names:
            kept_s.append((t,holder,m["name"],s_names[m["name"]]))
    print(f"\n=== 結果 ===")
    print(f"留卡總數={len(kept)}  無法取得metadata={kept_unknown}")
    print(f"留卡中是 S(name 對到賣回>$200)={len(kept_s)} 張")
    byh=defaultdict(lambda:[0,0.0])
    for t,h,n,v in kept_s: byh[h][0]+=1; byh[h][1]+=v
    print(f"持有留卡S 的地址數={len(byh)}")
    print(f"\nTop 持有留卡S 的地址:")
    for h,(c,v) in sorted(byh.items(),key=lambda x:-x[1][0])[:15]:
        print(f"  {h}  {c} 張  est ${v:,.0f}")
    # 校正後 S 總數
    print(f"\n=== 校正 ===")
    print(f"原 buyback 法 S 卡(賣回>200)={len(sold_s)} 張")
    print(f"+ 留卡 S(本次補)={len(kept_s)} 張")
    print(f"= 校正後最少 S 抽出={len(sold_s)+len(kept_s)} 張 (低估,未含無metadata與name未對到的稀有留卡)")
    json.dump([{"token_id":t,"holder":h,"name":n,"est_value":v} for t,h,n,v in kept_s],
              open(REPO/"platforms"/"renaiss"/"renaiss_kept_s_cards.json","w"),ensure_ascii=False,indent=2)

if __name__=="__main__":
    main()
