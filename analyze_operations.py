"""
analyze_operations.py — 平台「整體營運狀況」月度儀表板。

讀 domain 表（pack_opens / payments / marketplace_trades 或 marketplace_events），
輸出每月:開包毛收入、開包次數、實動人數、新進玩家、回購、淨利、
         市場成交量、市場成交筆數、市場交易人數，以及各項 MoM 環比成長率。

跨平台通用:
  - 市場優先讀 marketplace_trades（ERC-721 token_id 市場，如 renaiss）；
    若空則讀 marketplace_events 的 card_bought（字串 key 市場，如 mnstr）。

用法:
  .venv/bin/python analyze_operations.py --platform renaiss
  .venv/bin/python analyze_operations.py --platform renaiss --csv out.csv
"""
from __future__ import annotations

import argparse
import csv as csvmod
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from ingest import _addr_norm, get_conn, load_config
from tools.bootstrap import load_env_file


def _mom(cur, prev):
    """環比成長率字串。"""
    if prev in (None, 0) or cur is None:
        return "  —  "
    pct = (float(cur) - float(prev)) / float(prev) * 100
    return f"{pct:+6.0f}%"


def _platform_id(conn, name):
    with conn.cursor() as c:
        c.execute("SELECT id FROM platforms WHERE name=%s", (name,))
        r = c.fetchone()
    return r[0] if r else None


def _official_addrs(cfg: dict) -> list[str]:
    """official_wallets + deployers 的地址,計算時要排除(非真實玩家)。
    用 _addr_norm 正規化:YAML 會把無引號 0x… 當 int 解析,需轉回 hex。"""
    out: list[str] = []
    for v in (cfg.get("official_wallets") or {}).values():
        if isinstance(v, list):
            out += [_addr_norm(a) for a in v if a]
        elif v:
            out.append(_addr_norm(v))
    out += [_addr_norm(a) for a in (cfg.get("deployers") or []) if a]
    return list({a for a in out if a})


def fetch_pack_monthly(conn, pid, official):
    """每月:開包毛收入/次數/實動/新進/回購/淨利。
    pack_opens 已在 deriver 排除官方與 exclude.pack_prices;回購排除官方收款,
    淨利 = 乾淨開包收入 − (排除官方的)回購。"""
    with conn.cursor() as c:
        c.execute("""
            WITH opens AS (
                SELECT date_trunc('month', block_time) AS mo, opener, price_usd,
                       COALESCE(quantity,1) AS qty
                FROM pack_opens WHERE platform_id=%(pid)s
            ),
            firsts AS (
                SELECT opener, date_trunc('month', MIN(block_time)) AS fm
                FROM pack_opens WHERE platform_id=%(pid)s GROUP BY opener
            ),
            pay AS (
                -- 淨利採對稱現金流:玩家付款(pack_pay,已排除官方內部轉帳)−
                -- 玩家回購(payout,排除官方收款)。不受價位排除單邊影響。
                SELECT date_trunc('month', block_time) AS mo,
                       SUM(amount_usd) FILTER (WHERE direction='pack_pay') AS rev,
                       SUM(amount_usd) FILTER (
                           WHERE direction='payout' AND to_addr <> ALL(%(official)s)
                       ) AS payout
                FROM payments WHERE platform_id=%(pid)s GROUP BY 1
            )
            SELECT to_char(o.mo,'YYYY-MM') ym,
                   SUM(o.qty) opens,
                   SUM(o.price_usd) revenue,
                   COUNT(DISTINCT o.opener) active,
                   COUNT(DISTINCT o.opener) FILTER (WHERE f.fm = o.mo) newp,
                   COALESCE(MAX(p.payout),0) payout,
                   COALESCE(MAX(p.rev),0) - COALESCE(MAX(p.payout),0) net
            FROM opens o
            JOIN firsts f ON f.opener=o.opener
            LEFT JOIN pay p ON p.mo=o.mo
            GROUP BY o.mo ORDER BY o.mo
        """, {"pid": pid, "official": official})
        return c.fetchall()


def fetch_market_monthly(conn, pid, official):
    """每月市場:成交量/筆數/交易人數。排除官方買賣方。
    先試 marketplace_trades,再退回 marketplace_events。"""
    with conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM marketplace_trades WHERE platform_id=%s", (pid,))
        if c.fetchone()[0] > 0:
            c.execute("""
                SELECT to_char(date_trunc('month',block_time),'YYYY-MM') ym,
                       COUNT(*) trades, SUM(price_usd) vol,
                       COUNT(DISTINCT buyer) buyers,
                       COUNT(DISTINCT seller) sellers
                FROM marketplace_trades
                WHERE platform_id=%(pid)s
                  AND buyer  <> ALL(%(official)s) AND seller <> ALL(%(official)s)
                GROUP BY 1 ORDER BY 1
            """, {"pid": pid, "official": official})
            return c.fetchall(), "marketplace_trades"
        c.execute("SELECT COUNT(*) FROM marketplace_events WHERE platform_id=%s AND kind='card_bought'", (pid,))
        if c.fetchone()[0] > 0:
            c.execute("""
                SELECT to_char(date_trunc('month',block_time),'YYYY-MM') ym,
                       COUNT(*) trades, SUM(price_usd) vol,
                       COUNT(DISTINCT actor) buyers,
                       COUNT(DISTINCT counterparty) sellers
                FROM marketplace_events
                WHERE platform_id=%(pid)s AND kind='card_bought'
                  AND actor <> ALL(%(official)s) AND counterparty <> ALL(%(official)s)
                GROUP BY 1 ORDER BY 1
            """, {"pid": pid, "official": official})
            return c.fetchall(), "marketplace_events(card_bought)"
        return [], None


def fetch_pack_by_month(conn, pid):
    """各 pack（pack_id）每月開包數與收入。回傳 (months, packs, data, labels)。
    packs 依首次開包時間排序;label = 短地址 + 推估單價(該 pack 最小單筆)。"""
    with conn.cursor() as c:
        # 標籤用「最新單價」:取各 pack 最近活躍月份「最常見的 6 個價位」中的最小值。
        # 整買($480=10×$48)是單價的倍數,取最小即還原單價;排除 <$1 的 dust。
        c.execute("""
            WITH lo AS (
                SELECT pack_id, MIN(block_time) AS first_seen, MAX(block_time) AS mx
                FROM pack_opens WHERE platform_id=%(pid)s GROUP BY pack_id
            )
            SELECT lo.pack_id, lo.first_seen,
                   (SELECT MIN(price_usd) FROM (
                        SELECT p.price_usd, COUNT(*) ct
                        FROM pack_opens p
                        WHERE p.platform_id=%(pid)s AND p.pack_id=lo.pack_id
                          AND p.price_usd >= 1
                          AND p.block_time >= date_trunc('month', lo.mx)
                        GROUP BY p.price_usd ORDER BY ct DESC LIMIT 6
                    ) t) AS unit
            FROM lo ORDER BY lo.first_seen
        """, {"pid": pid})
        meta = c.fetchall()
        packs = [m[0] for m in meta]
        labels = {}
        for addr, _fs, unit in meta:
            tag = f"(${unit:.0f})" if unit else ""
            labels[addr] = f"{addr[:6]}…{tag}"
        c.execute("""
            SELECT to_char(date_trunc('month',block_time),'YYYY-MM') ym, pack_id,
                   SUM(COALESCE(quantity,1)) n, SUM(price_usd) v
            FROM pack_opens WHERE platform_id=%s GROUP BY 1,2
        """, (pid,))
        data = {}
        for ym, addr, n, v in c.fetchall():
            data.setdefault(ym, {})[addr] = (n, v)
    months = sorted(data)
    return months, packs, data, labels


def print_by_pack(months, packs, data, labels):
    if not packs:
        print("\n【各 pack 每月拆解】無 pack_opens 資料"); return
    w = 15
    hdr = "  月份    " + "".join(f"{labels[p]:>{w}}" for p in packs)
    print("\n【各 pack 每月開包數】(依上線時間排序)")
    print(hdr)
    for m in months:
        row = f"  {m} " + "".join(f"{data[m].get(p,(0,0))[0]:>{w},}" for p in packs)
        print(row)
    print("\n【各 pack 每月收入】")
    print(hdr)
    for m in months:
        row = f"  {m} " + "".join(f"{data[m].get(p,(0,0))[1]:>{w},.0f}" for p in packs)
        print(row)


def print_pack(rows, sym):
    print(f"\n【開包 / 營運】(幣別 {sym})")
    print(f"  {'月份':<7} {'毛收入':>12} {'MoM':>7} {'開包數':>8} {'實動人數':>8} {'MoM':>7} {'新進':>6} {'淨現金流':>11}")
    prev_rev = prev_act = None
    for ym, opens, rev, active, newp, payout, net in rows:
        print(f"  {ym:<7} ${rev:>11,.0f} {_mom(rev,prev_rev):>7} {opens:>8,} "
              f"{active:>8,} {_mom(active,prev_act):>7} {newp:>6,} ${net:>10,.0f}")
        prev_rev, prev_act = rev, active


def print_market(rows, src, sym):
    if not rows:
        print("\n【市場】無資料(marketplace_trades / marketplace_events 皆空)")
        return
    print(f"\n【市場】(來源 {src})")
    print(f"  {'月份':<7} {'成交量':>12} {'MoM':>7} {'成交筆數':>8} {'交易人數':>8} {'MoM':>7}")
    prev_vol = prev_traders = None
    for ym, trades, vol, buyers, sellers in rows:
        traders = (buyers or 0) + (sellers or 0)  # 買+賣（去重於各自,合計為參與人次）
        print(f"  {ym:<7} ${vol:>11,.0f} {_mom(vol,prev_vol):>7} {trades:>8,} "
              f"{traders:>8,} {_mom(traders,prev_traders):>7}")
        prev_vol, prev_traders = vol, traders


def write_csv(path, pack_rows, mkt_rows):
    with open(path, "w", newline="") as f:
        w = csvmod.writer(f)
        w.writerow(["section","month","col2","col3","col4","col5","col6","col7"])
        w.writerow(["pack","month","opens","revenue","active","new_players","payout","net"])
        for r in pack_rows:
            w.writerow(["pack", *r])
        w.writerow(["market","month","trades","volume","buyers","sellers"])
        for r in mkt_rows:
            w.writerow(["market", *r])
    print(f"\n[csv] 已寫出 {path}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True)
    ap.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    ap.add_argument("--csv", help="另存月度資料為 CSV")
    args = ap.parse_args()
    load_env_file(Path(args.env_file).expanduser())
    cfg = load_config(args.platform)
    sym = cfg.get("payment_token_symbol", "USDT")

    with get_conn() as conn:
        pid = _platform_id(conn, args.platform)
        if not pid:
            print(f"找不到 platform={args.platform}"); return 1
        official = _official_addrs(cfg)
        pack_rows = fetch_pack_monthly(conn, pid, official)
        mkt_rows, src = fetch_market_monthly(conn, pid, official)
        by_pack = fetch_pack_by_month(conn, pid)   # 各 pack 拆解為預設輸出
        with conn.cursor() as c:
            c.execute("SELECT MAX(block_time)::date FROM raw_transactions WHERE platform_id=%s", (pid,))
            cutoff = c.fetchone()[0]
    # 最後一個月若未到月底 → 部分資料,MoM 會偏低
    partial = None
    if cutoff and cutoff.day < 28:
        partial = cutoff.strftime("%Y-%m")

    print("=" * 78)
    print(f"  {args.platform.upper()} 營運儀表板（月度 + 環比 MoM）")
    print("=" * 78)

    # 總覽
    if pack_rows:
        tot_rev = sum(r[2] for r in pack_rows)
        tot_open = sum(r[1] for r in pack_rows)
        tot_net = sum(r[6] for r in pack_rows)
        tot_new = sum(r[4] for r in pack_rows)
        print(f"\n【總覽】期間 {pack_rows[0][0]} → {pack_rows[-1][0]}")
        print(f"  累積開包毛收入 ${tot_rev:,.0f} | 開包 {tot_open:,} 次 | "
              f"累積淨現金流 ${tot_net:,.0f} (毛收入留存 {tot_net/tot_rev*100:.1f}%) | 總玩家 {tot_new:,}")
    if mkt_rows:
        print(f"  累積市場成交量 ${sum(r[2] for r in mkt_rows):,.0f} | "
              f"成交 {sum(r[1] for r in mkt_rows):,} 筆")

    print_pack(pack_rows, sym)
    if by_pack:
        print_by_pack(*by_pack)
    print_market(mkt_rows, src, sym)
    print(f"\n  ※ 淨現金流 = 玩家付款 − 回購(USDT 進出),已排除官方;為「利潤上限」。")
    print(f"     未計入:玩家持有未賣回的卡(遞延賣回負債)、實體贖回出庫的卡成本"
          f"(鏈上無卡估值,無法量化)。")
    if partial:
        print(f"  ※ 資料截至 {cutoff};最後一月 {partial} 為部分資料,其 MoM 偏低非真實衰退。")

    if args.csv:
        write_csv(args.csv, pack_rows, mkt_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
