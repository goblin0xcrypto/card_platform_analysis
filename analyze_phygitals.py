"""phygitals 平台鏈上分析 — 直接讀 DB(payments / nft_transfers+amount_src / card_activity / mints)。"""
import os, yaml
from pathlib import Path
for line in Path(".env").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip().strip('"\''))
import psycopg

cfg = yaml.safe_load(open("platforms/phygitals/config.yaml")); ow = cfg["official_wallets"]
CL = [ow["operations"], ow["fee_payer"]] + list(ow["usdc_cluster"])
V = "62Q9eeDY3eM8A5CnprBGYMPShdBjAzdpBdr71QHsS8dS"
conn = psycopg.connect(os.environ["DATABASE_URL"]); c = conn.cursor()
def q(sql, a=()): c.execute(sql, a); return c.fetchall()
def q1(sql, a=()): c.execute(sql, a); return c.fetchone()
PID = q1("select id from platforms where name='phygitals'")[0]
def d(n): return f"${n:,.0f}" if n else "$0"

print("="*72); print("PHYGITALS 平台分析報告"); print("="*72)

# 1) 總覽
print("\n【1. 平台總覽】")
n_pay = q1("select count(*),min(block_time)::date,max(block_time)::date from payments where platform_id=%s",(PID,))
n_nft = q1("select count(*),min(block_time)::date,max(block_time)::date from nft_transfers where platform_id=%s",(PID,))
n_mint = q1("select count(*),count(distinct token_id) from mints where platform_id=%s",(PID,))
n_open = q1("select count(*) from nft_transfers where platform_id=%s and from_addr=%s and not(to_addr=any(%s))",(PID,V,CL))[0]
n_players = q1("select count(distinct a) from (select from_addr a from payments where platform_id=%s and not(from_addr=any(%s)) union select to_addr from payments where platform_id=%s and not(to_addr=any(%s))) t",(PID,CL,PID,CL))[0]
print(f"  payments(USDC金流): {n_pay[0]:,} 筆  ({n_pay[1]} ~ {n_pay[2]})")
print(f"  nft_transfers(卡片轉移): {n_nft[0]:,} 筆  ({n_nft[1]} ~ {n_nft[2]})")
print(f"  mints(鑄造): {n_mint[0]:,} 筆 / {n_mint[1]:,} 張卡")
print(f"  開卡交付(62Q9→玩家): {n_open:,} 筆")
print(f"  獨立玩家(碰過 USDC): {n_players:,}")

# 2) GMV(月度) — 定義：非 burn 且非平台內部 的轉移加總(開卡+賣回+玩家互轉)
#    每列用其 amount_usd(開卡=卡包售價 open_%、賣回/互轉=ALT 估值)；對照鏈上實際 USDC 入金。
print("\n【2. GMV 月度】定義=非burn且非平台內部的轉移加總(開卡+賣回+玩家互轉)；對照鏈上USDC入金")
NOTINT = "marketplace is distinct from 'burn' and not(from_addr=any(%s) and to_addr=any(%s))"
rows = q(f"""
  with gmv as (select date_trunc('month',block_time) m, sum(amount_usd) v,
                      count(*) filter(where amount_usd is null) nulln, count(*) tot
               from nft_transfers where platform_id=%s and {NOTINT} group by 1),
       usdc as (select date_trunc('month',block_time) m, sum(amount_usd) v from payments
               where platform_id=%s and not(from_addr=any(%s)) and to_addr=any(%s) group by 1)
  select to_char(coalesce(gmv.m,usdc.m),'YYYY-MM'), coalesce(gmv.v,0), coalesce(usdc.v,0),
         coalesce(gmv.nulln,0), coalesce(gmv.tot,0)
  from gmv full join usdc using(m) order by 1""",(PID,CL,CL,PID,CL,CL))
print(f"  {'月份':8}{'GMV(交易量)':>16}{'鏈上USDC入金':>16}{'GMV有估值%':>12}")
for m,gmv,uc,nulln,tot in rows:
    if m and m>='2025-08':
        cov = f"{(tot-nulln)*100//tot}%" if tot else "-"
        print(f"  {m:8}{d(float(gmv)):>16}{d(float(uc)):>16}{cov:>12}")
tg=q1(f"select round(sum(amount_usd)),count(*) from nft_transfers where platform_id=%s and {NOTINT}",(PID,CL,CL))
print(f"  → 全期 GMV(非burn非內部) = {d(float(tg[0]))}  ({tg[1]:,} 筆)")

# 3) Wash / 刷量
print("\n【3. Wash / 刷量分析】")
issued = q1("select count(*) from nft_transfers where platform_id=%s and from_addr=any(%s) and not(to_addr=any(%s))",(PID,CL,CL))[0]
soldback = q1("select count(*) from nft_transfers where platform_id=%s and not(from_addr=any(%s)) and to_addr=any(%s)",(PID,CL,CL))[0]
print(f"  平台發卡 {issued:,} vs 玩家賣回 {soldback:,} → 賣回率 {soldback*100//issued}%")
# 同卡開卡後多快賣回(近2月,有開卡真值)
fast = q1("""
  with o as (select token_id,to_addr,block_time from nft_transfers where platform_id=%s and from_addr=%s and not(to_addr=any(%s)) and block_time>='2026-05-01'),
       s as (select token_id,from_addr,block_time from nft_transfers where platform_id=%s and to_addr=%s and block_time>='2026-05-01')
  select count(*) filter(where sec<60), count(*) filter(where sec<600), count(*)
  from (select extract(epoch from s.block_time-o.block_time) sec from o join s on s.token_id=o.token_id and s.from_addr=o.to_addr and s.block_time>o.block_time
        and s.block_time<o.block_time+interval '1 hour') t""",(PID,V,CL,PID,V))
if fast: print(f"  開卡後快速賣回(近2月配對 {fast[2]:,} 對): <60秒 {fast[0]:,} ({(fast[0]*100//max(fast[2],1))}%), <10分 {fast[1]:,} ({fast[1]*100//max(fast[2],1)}%)")

# 4) 玩家經濟 — 淨流(付款 vs 回購)
print("\n【4. 玩家經濟】(玩家付款進平台 vs 平台回購付玩家)")
pin = q1("select count(*),round(sum(amount_usd)) from payments where platform_id=%s and not(from_addr=any(%s)) and to_addr=any(%s)",(PID,CL,CL))
pout = q1("select count(*),round(sum(amount_usd)) from payments where platform_id=%s and from_addr=any(%s) and not(to_addr=any(%s))",(PID,CL,CL))
print(f"  玩家→平台(儲值/開卡付款): {pin[0]:,} 筆 {d(float(pin[1]))}")
print(f"  平台→玩家(回購/提領):     {pout[0]:,} 筆 {d(float(pout[1]))}")
print(f"  淨額(玩家淨流出=平台淨收): {d(float(pin[1])-float(pout[1]))}")
print("\n  付款進平台最多的玩家 top8(疑巨鯨/farmer):")
for a,n,s in q("""select from_addr,count(*),round(sum(amount_usd)) from payments
                  where platform_id=%s and not(from_addr=any(%s)) and to_addr=any(%s) group by 1 order by 3 desc limit 8""",(PID,CL,CL)):
    print(f"    {a[:14]}… {n:>5} 筆 {d(float(s))}")

# 5) 卡包分析
print("\n【5. 卡包分析】(近2月,card_activity CLAW)")
for pack,n,pr,s in q("""select coalesce(pack_slug,claw_id,'?'),count(*),max(pack_price_usd),round(sum(amount_usd))
                        from card_activity where platform_id=%s and type='CLAW' group by 1 order by 4 desc limit 12""",(PID,)):
    print(f"    {str(pack):26} 開卡 {n:>6,}次  定價 {d(float(pr)) if pr else '?':>8}  名目額 {d(float(s))}")

# 6) 集中度
print("\n【6. 集中度】")
topn = q("""select to_addr,count(*) from nft_transfers where platform_id=%s and from_addr=%s and not(to_addr=any(%s)) group by 1 order by 2 desc limit 5""",(PID,V,CL))
tot_open = n_open
top5 = sum(n for _,n in topn)
print(f"  開卡量 top5 玩家佔全部開卡: {top5*100//tot_open}% ({top5:,}/{tot_open:,})")
for a,n in topn: print(f"    {a[:14]}… 開卡 {n:,} 次")

conn.close()
print("\n"+"="*72)
