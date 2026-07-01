"""
ingest_card_values.py — 建 card_values(token_id→FMV) 並回填 nft_transfers.amount_usd。

背景：phygitals 逐筆交易金額不在鏈上(內部餘額制)、平台也不開放歷史逐筆價。
      唯一可大量取得的是「平台錢包當前庫存」的卡片 FMV(altFmv，graded 卡)。
      wash 卡大量在 62Q9 等平台錢包進出，故掃平台庫存能覆蓋相當比例的交易。

資料源：GET https://api.phygitals.com/api/users/i/{wallet}?itemsPerPage=100&page=N
        每張卡：address(=token_id) / altFmv(ALT 估值,USD,graded 卡才有) / slug / certnumber / name / graded。
        需登入 cookie(privy-token + cf_clearance)，存 /tmp/phy_cookie.txt(讀取,不寫死)。

流程：
  1. 對 config official_wallets 全部地址分頁掃庫存 → upsert card_values(platform_id,token_id,fmv,slug,graded,name)。
  2. 回填 nft_transfers.amount_usd = card_values.fmv(join token_id)。amount_usd 欄不存在則自動加。
  3. 報覆蓋率(整體 + 指定週)。

⚠️ 限制：altFmv 是「當前」估值(近似歷史)；只 graded 卡有值；賣給玩家未回流的卡抓不到 → 部分覆蓋。
用法：
  .venv/bin/python ingest_card_values.py --platform phygitals            # 掃庫存+回填
  .venv/bin/python ingest_card_values.py --platform phygitals --wallets 62Q9...  # 只掃指定錢包
  .venv/bin/python ingest_card_values.py --platform phygitals --backfill-only    # 只重算 amount_usd
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
from ingest import get_conn, load_config, upsert_platform
from ingest_solana import _upsert_refs
from tools.bootstrap import load_env_file

API = "https://api.phygitals.com"
COOKIE_FILE = Path("/tmp/phy_cookie.txt")

DDL = """
CREATE TABLE IF NOT EXISTS card_values (
    platform_id INT,
    token_id    TEXT NOT NULL,
    fmv         NUMERIC(20,6),
    slug        TEXT,
    graded      BOOLEAN,
    name        TEXT,
    src_wallet  TEXT,
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (platform_id, token_id)
);
"""


def _cookie_header() -> str:
    if not COOKIE_FILE.exists():
        raise SystemExit("需要 /tmp/phy_cookie.txt(登入 cookie)。請貼上目前 cookie 讓我寫入。")
    return COOKIE_FILE.read_text().strip()


def _token(cookie: str) -> str:
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("privy-token="):
            return part[len("privy-token="):]
    return ""


def _collect_wallets(cfg: dict) -> list[str]:
    ow = cfg.get("official_wallets") or {}
    out = []
    for v in ow.values():
        for a in (v if isinstance(v, list) else [v]):
            if a and a not in out:
                out.append(a)
    return out


def scan_wallet(sess, cookie, token, wallet, platform_id, conn, page_size=100):
    """分頁掃一個錢包庫存 → upsert card_values。回 (筆數, 有fmv數)。"""
    hdr = {"authorization": f"Bearer {token}", "cookie": cookie,
           "user-agent": "Mozilla/5.0", "accept": "application/json"}
    n = fmvn = 0
    page = 1
    while True:
        try:
            r = sess.get(f"{API}/api/users/i/{wallet}",
                         params={"itemsPerPage": page_size, "page": page}, headers=hdr, timeout=30)
            if r.status_code == 429:
                time.sleep(2); continue
            if r.status_code != 200:
                print(f"    page {page} HTTP {r.status_code}，停"); break
            j = r.json()
        except Exception as e:
            print(f"    page {page} err {str(e)[:80]}"); break
        items = j if isinstance(j, list) else (j.get("items") or j.get("data") or [])
        if not items:
            break
        rows = []
        for it in items:
            tid = it.get("address")
            if not tid:
                continue
            # 價格：優先 altFmv(ALT 估值,graded)；否則 lastSale(micro-USD,÷1e6,成交過的卡)
            fmv = it.get("altFmv")
            fmv = float(fmv) if isinstance(fmv, (int, float)) and fmv > 0 else None
            if fmv is None:
                ls = it.get("lastSale")
                try:
                    ls = float(ls)
                except (TypeError, ValueError):
                    ls = 0
                if ls > 0:
                    fmv = ls / 1e6
            if fmv:
                fmvn += 1
            rows.append((platform_id, tid, fmv, it.get("slug"), bool(it.get("graded")),
                         (it.get("name") or "")[:200], wallet))
            n += 1
        with conn.cursor() as c:
            c.executemany(
                "INSERT INTO card_values(platform_id,token_id,fmv,slug,graded,name,src_wallet) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (platform_id,token_id) DO UPDATE "
                "SET fmv=COALESCE(EXCLUDED.fmv,card_values.fmv), slug=EXCLUDED.slug, "
                "graded=EXCLUDED.graded, name=EXCLUDED.name, updated_at=NOW()", rows)
        conn.commit()
        if page % 10 == 0 or len(items) < page_size:
            print(f"    page {page}: 累計 {n} 張 (有fmv {fmvn})")
        if len(items) < page_size:
            break
        page += 1
    return n, fmvn


def backfill(conn, platform_id, week=None):
    with conn.cursor() as c:
        c.execute("ALTER TABLE nft_transfers ADD COLUMN IF NOT EXISTS amount_usd NUMERIC(20,6)")
        c.execute("""UPDATE nft_transfers n SET amount_usd = v.fmv
                     FROM card_values v WHERE n.platform_id=%s AND v.platform_id=%s
                     AND n.token_id=v.token_id AND v.fmv IS NOT NULL""", (platform_id, platform_id))
        conn.commit()
        c.execute("""select count(*), count(amount_usd), round(coalesce(sum(amount_usd),0))
                     from nft_transfers where platform_id=%s""", (platform_id,))
        tot, valued, s = c.fetchone()
        print(f"\n[backfill] nft_transfers 全期: {tot:,} 筆，其中有金額 {valued:,} ({valued*100//max(tot,1)}%)，金額合計 ${int(s):,}")
        if week:
            c.execute("""select count(*), count(amount_usd), round(coalesce(sum(amount_usd),0))
                         from nft_transfers where platform_id=%s and block_time>=%s and block_time<%s""",
                      (platform_id, week[0], week[1]))
            t2, v2, s2 = c.fetchone()
            print(f"[backfill] {week[0]}~{week[1]}: {t2:,} 筆，有金額 {v2:,} ({v2*100//max(t2,1)}%)，$ {int(s2):,}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True)
    ap.add_argument("--wallets", nargs="*", help="只掃指定錢包(預設 config official_wallets 全部)")
    ap.add_argument("--backfill-only", action="store_true")
    ap.add_argument("--week", nargs=2, metavar=("FROM", "TO"), default=["2026-05-01", "2026-05-08"])
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
            cookie = _cookie_header(); token = _token(cookie)
            wallets = args.wallets or _collect_wallets(cfg)
            sess = requests.Session()
            print(f"掃 {len(wallets)} 個平台錢包庫存…")
            for w in wallets:
                print(f"  === {w} ===")
                n, fmvn = scan_wallet(sess, cookie, token, w, platform_id, conn)
                print(f"    完成 {n} 張，有 fmv {fmvn}")

        backfill(conn, platform_id, week=tuple(args.week))
    return 0


if __name__ == "__main__":
    sys.exit(main())
