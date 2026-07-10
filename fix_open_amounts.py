"""
fix_open_amounts.py — 把 nft_transfers 開卡列(from=62Q9→玩家)的 amount_usd 從「卡片 ALT 估值」
                       修正為「卡包實際售價」(來自 card_activity 的 CLAW 金額)。

背景：amount_usd 原由 card_charts(卡片逐日 ALT 估值)回填，故開卡與賣回同卡同日會同值。
      但開卡玩家實付的是「卡包售價」(如 trainer-pack $10)，應以 card_activity CLAW 金額為準。

策略(依序，只動 from=62Q9 且 to 非平台的開卡列；每列記來源到 amount_src)：
  1. 唯一價卡(84%)：該 token_id 的 CLAW 金額唯一 → token_id 直接對應。            src='open_unique'
  2. 多價卡(16%) 精確配對：(token_id + 收卡玩家 to + 時間±300s) 最近的 CLAW。       src='open_exact'
  3. 多價卡 仍未配到：用該 token_id 的眾數(最常見)CLAW 金額近似。                   src='open_mode'
  其餘(card_activity 未覆蓋的早期開卡列)：保持原 ALT 估值。                         src='alt'

安全：新增 amount_src 欄稽核來源；不動賣回/二級列(仍為 ALT)。要還原開卡列 ALT 值，
      重跑 ingest_card_charts.py --backfill-only 即可(它由 card_charts 重算全部 amount_usd)。

用法：.venv/bin/python fix_open_amounts.py --platform phygitals
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
from ingest import get_conn, load_config
from tools.bootstrap import load_env_file

V = "62Q9eeDY3eM8A5CnprBGYMPShdBjAzdpBdr71QHsS8dS"   # vmNftOwner：開卡交付源


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", default="phygitals")
    ap.add_argument("--match-window", type=int, default=300, help="精確配對時間窗(秒)")
    ap.add_argument("--env-file", default=str(REPO / ".env"))
    args = ap.parse_args()
    load_env_file(Path(args.env_file).expanduser())

    cfg = load_config(args.platform)
    ow = cfg["official_wallets"]
    cluster = [ow["operations"], ow["fee_payer"]] + list(ow["usdc_cluster"])
    pid_name = args.platform

    with get_conn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT id FROM platforms WHERE name=%s", (pid_name,))
            platform_id = c.fetchone()[0]

            # 0) amount_src 欄 + baseline='alt'
            c.execute("ALTER TABLE nft_transfers ADD COLUMN IF NOT EXISTS amount_src TEXT")
            c.execute("UPDATE nft_transfers SET amount_src='alt' WHERE platform_id=%s AND amount_usd IS NOT NULL AND amount_src IS NULL",
                      (platform_id,))
            conn.commit()

            OPEN = "n.platform_id=%s AND n.from_addr=%s AND NOT (n.to_addr = ANY(%s))"

            # 1) 唯一價卡：token_id 直接對應
            c.execute(f"""
                WITH uniq AS (
                  SELECT token_id, MAX(amount_usd) amt FROM card_activity
                  WHERE platform_id=%s AND type='CLAW' AND amount_usd IS NOT NULL
                  GROUP BY token_id HAVING COUNT(DISTINCT amount_usd)=1)
                UPDATE nft_transfers n SET amount_usd=u.amt, amount_src='open_unique'
                FROM uniq u WHERE {OPEN} AND n.token_id=u.token_id
            """, (platform_id, platform_id, V, cluster))
            n1 = c.rowcount; conn.commit()
            print(f"1) 唯一價卡 token_id 直接對應: 更新 {n1:,} 開卡列 (src=open_unique)")

            # 2) 多價卡 精確時間配對
            c.execute(f"""
                WITH multi AS (
                  SELECT token_id FROM card_activity WHERE platform_id=%s AND type='CLAW' AND amount_usd IS NOT NULL
                  GROUP BY token_id HAVING COUNT(DISTINCT amount_usd)>1),
                m AS (
                  SELECT DISTINCT ON (n.tx_hash, n.log_index) n.tx_hash, n.log_index, a.amount_usd amt
                  FROM nft_transfers n
                  JOIN card_activity a ON a.platform_id=%s AND a.type='CLAW'
                       AND a.token_id=n.token_id AND a.to_addr=n.to_addr
                       AND abs(extract(epoch FROM a.event_time - n.block_time)) < %s
                  WHERE {OPEN} AND n.token_id IN (SELECT token_id FROM multi)
                  ORDER BY n.tx_hash, n.log_index, abs(extract(epoch FROM a.event_time - n.block_time)))
                UPDATE nft_transfers n SET amount_usd=m.amt, amount_src='open_exact'
                FROM m WHERE n.platform_id=%s AND n.tx_hash=m.tx_hash AND n.log_index=m.log_index
            """, (platform_id, platform_id, args.match_window, platform_id, V, cluster, platform_id))
            n2 = c.rowcount; conn.commit()
            print(f"2) 多價卡 精確時間配對(±{args.match_window}s): 更新 {n2:,} 開卡列 (src=open_exact)")

            # 3) 多價卡 仍未配到 → 眾數近似
            c.execute(f"""
                WITH cnt AS (
                  SELECT token_id, amount_usd, COUNT(*) k FROM card_activity
                  WHERE platform_id=%s AND type='CLAW' AND amount_usd IS NOT NULL GROUP BY token_id, amount_usd),
                mode AS (
                  SELECT DISTINCT ON (token_id) token_id, amount_usd amt
                  FROM cnt ORDER BY token_id, k DESC, amount_usd DESC)
                UPDATE nft_transfers n SET amount_usd=mode.amt, amount_src='open_mode'
                FROM mode WHERE {OPEN} AND n.token_id=mode.token_id AND n.amount_src='alt'
            """, (platform_id, platform_id, V, cluster))
            n3 = c.rowcount; conn.commit()
            print(f"3) 多價卡 眾數近似: 更新 {n3:,} 開卡列 (src=open_mode)")

            # 報告
            print("\n=== 開卡列(62Q9→玩家) amount_src 分布 ===")
            c.execute(f"SELECT amount_src, count(*), round(sum(amount_usd)) FROM nft_transfers n WHERE {OPEN} GROUP BY 1 ORDER BY 2 DESC",
                      (platform_id, V, cluster))
            for src, n, s in c.fetchall():
                print(f"  {str(src):12} {n:>9,} 列  ${int(s or 0):,}")
            print("\n=== 修正效果：近2月(2026-05~) 開卡金額 前後對比 ===")
            c.execute(f"""SELECT
                count(*) filter(where amount_src like 'open_%%') 已修正,
                round(sum(amount_usd) filter(where amount_src like 'open_%%')) 修正後開卡額
                FROM nft_transfers n WHERE {OPEN} AND n.block_time>='2026-05-01'""",
                      (platform_id, V, cluster))
            fixed, amt = c.fetchone()
            print(f"  近2月開卡列已修正 {fixed:,} 筆，修正後開卡金額(卡包售價口徑) ${int(amt or 0):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
