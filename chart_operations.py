"""
chart_operations.py — 營運資料的多種視覺化。

口徑與 analyze_operations.py 一致(重用其 fetch_*,已排除官方/測試/上線價/dust)。

圖型(--chart):
  revenue-mom       每月毛收入(長條)+ MoM 成長率(折線)         [預設]
  pack-stack        每月收入「各 pack 堆疊」— 看哪台卡機在帶動成長
  revenue-vs-users  毛收入(長條) vs 實動人數(折線)— 看「量增但人沒增」的背離
  margin            毛收入 / 回購 / 淨現金流 並排 — 看薄利與抽水
  market            市場成交量 + MoM — 看二級市場萎縮
  all               一次輸出全部

用法:
  .venv/bin/python chart_operations.py --platform renaiss --chart all
  .venv/bin/python chart_operations.py --platform renaiss --chart pack-stack
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.ticker import FuncFormatter

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from analyze_operations import (
    _official_addrs, fetch_market_monthly, fetch_pack_by_month, fetch_pack_monthly,
)
from ingest import get_conn, load_config
from tools.bootstrap import load_env_file

BLUE, RED, GREEN, ORANGE = "#4C72B0", "#C44E52", "#55A868", "#DD8452"
PALETTE = ["#4C72B0", "#DD8452", "#55A868", "#C44E52", "#8172B3", "#937860"]


def _set_cjk_font():
    for name in ("PingFang HK", "Arial Unicode MS", "Heiti TC",
                 "Hiragino Sans GB", "STHeiti", "Songti SC"):
        if any(f.name == name for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = name
            break
    plt.rcParams["axes.unicode_minus"] = False


def _mom(vals):
    out = [None]
    for i in range(1, len(vals)):
        p = vals[i - 1]
        out.append((vals[i] - p) / p * 100 if p else None)
    return out


def _usd(v):
    return f"${v/1e6:.2f}M" if abs(v) >= 1e6 else f"${v/1e3:.0f}K"


# --------------------------------------------------------------------------- #
def chart_revenue_mom(d, sym, title):
    """專業雙軸:長條=毛收入(左軸,低飽和藍);折線=MoM 成長率(右軸,高對比橘)。
    左右軸標籤與資料同色對應;0% 基準線灰虛線凸顯逆勢月份。"""
    import numpy as np
    BAR = "#5B7FA6"      # 低飽和沉穩藍(規模)
    LINE = "#E8703A"     # 高對比亮橘(速度/趨勢)
    months, rev = d["months"], d["revenue"]
    mom = _mom(rev)
    x = np.arange(len(months))
    fig, ax1 = plt.subplots(figsize=(11, 6))

    # 左軸:毛收入長條
    bars = ax1.bar(x, [v / 1e6 for v in rev], color=BAR, alpha=0.9, width=0.62,
                   label="開包毛收入", zorder=2)
    ax1.set_ylabel(f"開包毛收入(百萬 {sym})", color=BAR, fontsize=12, fontweight="bold")
    ax1.tick_params(axis="y", labelcolor=BAR)
    ax1.set_ylim(0, max(rev) / 1e6 * 1.16)
    ax1.set_xticks(x); ax1.set_xticklabels(months)
    ax1.set_xlabel("月份", fontsize=12)
    ax1.grid(axis="y", linestyle="--", alpha=0.25, zorder=0)
    for b, v in zip(bars, rev):
        ax1.text(b.get_x() + b.get_width() / 2, b.get_height(), _usd(v),
                 ha="center", va="bottom", fontsize=9.5, color="#33415C")

    # 右軸:MoM 成長率折線
    ax2 = ax1.twinx()
    xs = [xi for xi, v in zip(x, mom) if v is not None]
    ys = [v for v in mom if v is not None]
    ax2.plot(xs, ys, color=LINE, marker="o", markersize=7, linewidth=2.4,
             label="MoM 成長率", zorder=3)
    ax2.set_ylabel("MoM 成長率 (%)", color=LINE, fontsize=12, fontweight="bold")
    ax2.tick_params(axis="y", labelcolor=LINE)
    ax2.axhline(0, color="#888888", linestyle="--", linewidth=1.4, zorder=1)  # 0% 基準
    pad = (max(ys) - min(ys)) * 0.18 or 10
    ax2.set_ylim(min(ys) - pad, max(ys) + pad)
    for xi, v in zip(xs, ys):
        ax2.annotate(f"{v:+.0f}%", (xi, v), textcoords="offset points",
                     xytext=(0, 10 if v >= 0 else -16), ha="center",
                     fontsize=9.5, color=LINE, fontweight="bold")

    ax1.set_title("每月開包毛收入與 MoM 成長率", fontsize=14, fontweight="bold")
    h1, l1 = ax1.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=10, framealpha=0.9)
    fig.tight_layout()
    return fig


def chart_pack_stack(d, sym, title):
    months, packs, data, labels = d["by_pack"]
    fig, ax = plt.subplots(figsize=(11, 6))
    bottom = [0.0] * len(months)
    for i, p in enumerate(packs):
        vals = [float(data[m].get(p, (0, 0))[1]) / 1e6 for m in months]
        ax.bar(months, vals, bottom=bottom, label=labels[p], color=PALETTE[i % len(PALETTE)])
        bottom = [b + v for b, v in zip(bottom, vals)]
    for x, tot in zip(months, bottom):
        ax.text(x, tot, f"${tot:.1f}M", ha="center", va="bottom", fontsize=9, color="#33415C")
    ax.set_ylabel(f"開包毛收入(百萬 {sym})", fontsize=12)
    ax.set_ylim(0, max(bottom) * 1.15)
    ax.legend(title="卡機(單價)", fontsize=9, loc="upper left")
    _finish(fig, ax, None, title, "每月收入 — 各卡機堆疊")
    return fig


def chart_revenue_vs_users(d, sym, title):
    months, rev, active = d["months"], d["revenue"], d["active"]
    fig, ax1 = plt.subplots(figsize=(11, 6))
    ax1.bar(months, [v / 1e6 for v in rev], color=BLUE, alpha=0.8, label="毛收入")
    ax1.set_ylabel(f"開包毛收入(百萬 {sym})", color=BLUE, fontsize=12)
    ax1.tick_params(axis="y", labelcolor=BLUE)
    ax2 = ax1.twinx()
    ax2.plot(months, active, color=ORANGE, marker="s", linewidth=2, label="實動人數")
    ax2.set_ylabel("實動人數(獨立開包者)", color=ORANGE, fontsize=12)
    ax2.tick_params(axis="y", labelcolor=ORANGE)
    ax2.set_ylim(0, max(active) * 1.3)
    for x, y in zip(months, active):
        ax2.annotate(f"{y:,}", (x, y), textcoords="offset points",
                     xytext=(0, 8), ha="center", fontsize=8, color=ORANGE)
    _finish(fig, ax1, ax2, title, "毛收入 vs 實動人數(背離=量增但人沒增)")
    return fig


def chart_margin(d, sym, title):
    months = d["months"]
    payout, net = d["payout"], d["net"]
    # 玩家付款用 pack_pay(=淨+回購)以維持「付款 − 回購 = 淨」內部自洽
    pay_in = [n + p for n, p in zip(net, payout)]
    import numpy as np
    x = np.arange(len(months)); w = 0.27
    fig, ax = plt.subplots(figsize=(11, 6))
    ax.bar(x - w, [v / 1e6 for v in pay_in], w, label="玩家付款", color=BLUE)
    ax.bar(x, [v / 1e6 for v in payout], w, label="回購付出", color=ORANGE)
    ax.bar(x + w, [v / 1e6 for v in net], w, label="淨現金流", color=GREEN)
    ax.set_xticks(x); ax.set_xticklabels(months)
    ax.set_ylabel(f"百萬 {sym}", fontsize=12)
    ax.axhline(0, color="#888", linewidth=0.8)
    ax.legend(fontsize=10, loc="upper left")
    _finish(fig, ax, None, title, "玩家付款 / 回購 / 淨現金流(薄利)")
    return fig


def chart_market(d, sym, title):
    if not d["market"]:
        return None
    months = [r[0] for r in d["market"]]
    vol = [float(r[2]) for r in d["market"]]
    mom = _mom(vol)
    fig, ax1 = plt.subplots(figsize=(11, 6))
    bars = ax1.bar(months, [v / 1e6 for v in vol], color="#8172B3", alpha=0.85, label="市場成交量")
    ax1.set_ylabel(f"市場成交量(百萬 {sym})", color="#8172B3", fontsize=12)
    ax1.tick_params(axis="y", labelcolor="#8172B3")
    for b, v in zip(bars, vol):
        ax1.text(b.get_x() + b.get_width() / 2, b.get_height(), _usd(v),
                 ha="center", va="bottom", fontsize=9, color="#33415C")
    ax2 = ax1.twinx()
    xs = [m for m, v in zip(months, mom) if v is not None]
    ys = [v for v in mom if v is not None]
    ax2.plot(xs, ys, color=RED, marker="o", linewidth=2, label="MoM")
    ax2.set_ylabel("MoM 環比成長率 (%)", color=RED, fontsize=12)
    ax2.tick_params(axis="y", labelcolor=RED)
    ax2.axhline(0, color=RED, linestyle=":", linewidth=0.8, alpha=0.5)
    _finish(fig, ax1, ax2, title, "市場成交量與 MoM")
    return fig


def _market_maps(d):
    """市場資料 → {月:成交量}、{月:交易人數(買+賣)}。"""
    vol = {r[0]: float(r[2] or 0) for r in d["market"]}
    traders = {r[0]: int(r[3] or 0) + int(r[4] or 0) for r in d["market"]}
    return vol, traders


def _dual_line(months, y1, y2, y1lab, y2lab, sub,
               y1fmt=None, y2fmt=None, y1unit=1.0, y2unit=1.0):
    """通用雙軸折線:Y1 左(低飽和藍)、Y2 右(高對比橘),同色軸標籤。"""
    BL, OR = "#3A6EA5", "#E8703A"
    fig, ax1 = plt.subplots(figsize=(11, 6))
    ax1.plot(months, [v / y1unit for v in y1], color=BL, marker="o",
             markersize=7, linewidth=2.4, label=y1lab, zorder=3)
    ax1.set_ylabel(y1lab, color=BL, fontsize=12, fontweight="bold")
    ax1.tick_params(axis="y", labelcolor=BL)
    ax1.set_xlabel("月份", fontsize=12)
    ax1.grid(axis="y", linestyle="--", alpha=0.25)
    ax1.set_ylim(0, max(v / y1unit for v in y1) * 1.18)
    for m, v in zip(months, y1):
        ax1.annotate(y1fmt(v), (m, v / y1unit), textcoords="offset points",
                     xytext=(0, 9), ha="center", fontsize=8.5, color=BL)

    ax2 = ax1.twinx()
    ax2.plot(months, [v / y2unit for v in y2], color=OR, marker="s",
             markersize=7, linewidth=2.4, label=y2lab, zorder=3)
    ax2.set_ylabel(y2lab, color=OR, fontsize=12, fontweight="bold")
    ax2.tick_params(axis="y", labelcolor=OR)
    ax2.set_ylim(0, max(v / y2unit for v in y2) * 1.18)
    for m, v in zip(months, y2):
        ax2.annotate(y2fmt(v), (m, v / y2unit), textcoords="offset points",
                     xytext=(0, -16), ha="center", fontsize=8.5, color=OR)

    ax1.set_title(sub, fontsize=14, fontweight="bold")
    h1, l1 = ax1.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=10, framealpha=0.9)
    fig.tight_layout()
    return fig


def chart_rev_vs_market(d, sym, title):
    """雙軸折線:Y1 營運毛收入 / Y2 市場成交量。"""
    months = d["months"]
    volmap, _ = _market_maps(d)
    rev = d["revenue"]
    vol = [volmap.get(m, 0) for m in months]
    return _dual_line(
        months, rev, vol,
        f"營運毛收入(百萬 {sym})", f"市場成交量(百萬 {sym})",
        "營運毛收入 vs 市場成交量",
        y1fmt=_usd, y2fmt=_usd, y1unit=1e6, y2unit=1e6)


def chart_users_vs_traders(d, sym, title):
    """雙軸折線:Y1 實動人數(開包) / Y2 交易人數(市場買+賣)。"""
    months = d["months"]
    _, trmap = _market_maps(d)
    active = d["active"]
    traders = [trmap.get(m, 0) for m in months]
    f = lambda v: f"{int(v):,}"
    return _dual_line(
        months, active, traders,
        "實動人數(開包)", "交易人數(市場)",
        "實動人數 vs 市場交易人數",
        y1fmt=f, y2fmt=f)


def _finish(fig, ax1, ax2, platform_title, subtitle):
    ax1.set_title(subtitle, fontsize=14, fontweight="bold")
    ax1.set_xlabel("月份", fontsize=12)
    ax1.grid(axis="y", linestyle="--", alpha=0.3)
    if ax2 is not None:
        h1, l1 = ax1.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        ax1.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=10)
    fig.tight_layout()


CHARTS = {
    "revenue-mom": chart_revenue_mom,
    "pack-stack": chart_pack_stack,
    "revenue-vs-users": chart_revenue_vs_users,
    "margin": chart_margin,
    "market": chart_market,
    "rev-vs-market": chart_rev_vs_market,
    "users-vs-traders": chart_users_vs_traders,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True)
    ap.add_argument("--chart", default="revenue-mom",
                    choices=list(CHARTS) + ["all"])
    ap.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    ap.add_argument("--outdir", help="輸出資料夾(預設 platforms/<name>/)")
    args = ap.parse_args()
    load_env_file(Path(args.env_file).expanduser())
    cfg = load_config(args.platform)
    sym = cfg.get("payment_token_symbol", "USDT")

    with get_conn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT id FROM platforms WHERE name=%s", (args.platform,))
            row = c.fetchone()
        if not row:
            print(f"找不到 platform={args.platform}"); return 1
        pid = row[0]
        official = _official_addrs(cfg)
        rows = fetch_pack_monthly(conn, pid, official)
        by_pack = fetch_pack_by_month(conn, pid)
        market, _ = fetch_market_monthly(conn, pid, official)
        with conn.cursor() as c:
            c.execute("SELECT MAX(block_time)::date FROM raw_transactions WHERE platform_id=%s", (pid,))
            cutoff = c.fetchone()[0]
    if not rows:
        print("無 pack_opens 資料,無法繪圖"); return 1

    # 排除部分月份(資料未到月底者,如進行中的當月)避免 MoM 假性暴跌
    drop = cutoff.strftime("%Y-%m") if (cutoff and cutoff.day < 28) else None
    if drop:
        rows = [r for r in rows if r[0] != drop]
        bm, bp, bdata, blab = by_pack
        by_pack = ([m for m in bm if m != drop], bp,
                   {m: v for m, v in bdata.items() if m != drop}, blab)
        market = [r for r in market if r[0] != drop]
        print(f"[chart] 已排除部分月份 {drop}(資料僅到 {cutoff})")

    d = {
        "months": [r[0] for r in rows],
        "revenue": [float(r[2]) for r in rows],
        "active": [int(r[3]) for r in rows],
        "payout": [float(r[5]) for r in rows],
        "net": [float(r[6]) for r in rows],
        "by_pack": by_pack,
        "market": market,
    }
    title = f"{args.platform.upper()} 營運視覺化"
    _set_cjk_font()
    outdir = Path(args.outdir) if args.outdir else (REPO_ROOT / "platforms" / args.platform)
    outdir.mkdir(parents=True, exist_ok=True)

    todo = list(CHARTS) if args.chart == "all" else [args.chart]
    for name in todo:
        fig = CHARTS[name](d, sym, title)
        if fig is None:
            print(f"[chart] {name}: 無資料,略過"); continue
        out = outdir / f"{args.platform}_{name}.png"
        fig.savefig(out, dpi=150)
        plt.close(fig)
        print(f"[chart] 已輸出: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
