"""
ingest_solana_cnft.py — 抓 phygitals 第一代卡片（compressed NFT / cNFT，Bubblegum）→ mints / nft_transfers。

背景：phygitals 2025 期卡片是 cNFT(collection BSG6Dy)，2026-03 遷移到 MPL Core(phygZ，見 ingest_solana_nft.py)。
      payments 從 2025-03 就有平台規模金流,但對應卡片是 cNFT,故需本程式補抓 2025 期卡片面。

資料源（實測 2026-06 定案）：Helius **Enhanced Transactions API**，**以各 merkle tree 位址分頁**
  `GET https://api.helius.xyz/v0/addresses/{tree}/transactions?api-key=&before=&limit=100`(降冪)。
  （collection 位址端點只回得到近期 mint、回溯有限；tree 端點才有完整 mint/transfer/burn 史。）
  回傳已解析的 `events.compressed[]`：type / assetId / oldLeafOwner(from) / newLeafOwner(to) / treeId / leafIndex。
  → cNFT 的 from/to **直接有，不需 SQL LAG**（與 MPL Core 不同）。

寫入(與 MPL Core 同表，用 contract 區分世代；contract = cNFT collection BSG6Dy；token_id = assetId)：
  COMPRESSED_NFT_MINT(_TO_COLLECTION) → mints(minter=newLeafOwner)
  COMPRESSED_NFT_TRANSFER             → nft_transfers(from=oldLeafOwner, to=newLeafOwner)
  COMPRESSED_NFT_BURN                 → nft_transfers(from=oldLeafOwner, to='', marketplace='burn')  # 遷移/贖回實體卡
  (DELEGATE/UPDATE_METADATA 等略過)

floor cursor(ingest_cursors, source=cnft, per-tree)：--days N(預設7)/--days 0(全量,可續)，PK 冪等。

用法：
  .venv/bin/python ingest_solana_cnft.py --platform phygitals --days 7   # 驗證
  .venv/bin/python ingest_solana_cnft.py --platform phygitals --days 0   # 全量(可續)
  .venv/bin/python ingest_solana_cnft.py --platform phygitals --from 2026-06-19 --to 2026-06-21  # 範圍模式：補洞

範圍模式（--from/--to）：同 ingest_solana_nft.py，精準重跑任意窗口、冪等填洞，不動 floor 游標。
  ⚠️ --days 0 會跳過「比 floor 新」的交易當已完成，補不到 floor 之上的缺漏；補洞請用範圍模式。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from ingest import get_conn, insert_mints, insert_nft_transfers, load_config, upsert_platform
from ingest_solana import CURSOR_DDL, _upsert_refs
from ingest_solana_nft import _ts, _parse_when
from tools.bootstrap import load_env_file

ENHANCED = "https://api.helius.xyz/v0/addresses/{addr}/transactions"
MINT_TYPES = ("COMPRESSED_NFT_MINT", "COMPRESSED_NFT_MINT_TO_COLLECTION")
CURSOR_SRC = "cnft"
FLUSH = 1000


def _cnft_entries(cfg: dict):
    """從 config.contracts.cnft 取 [(collection, [trees...])]。"""
    out = []
    for e in (cfg.get("contracts") or {}).get("cnft") or []:
        col = e.get("collection")
        trees = e.get("trees") or []
        if col and trees:
            out.append((col, trees))
    return out


def _enh_page(key, addr, before, retries=6):
    params = {"api-key": key, "limit": 100}
    if before:
        params["before"] = before
    # 回 (ok, list)：ok=True 表「確實成功」(含真的空=到底)；ok=False 表暫時失敗(呼叫端不可當到底)
    for a in range(retries):
        try:
            r = requests.get(ENHANCED.format(addr=addr), params=params, timeout=40)
            if r.status_code == 429:
                time.sleep(min(20, 1.0 * (a + 1))); continue
            if r.status_code >= 500:
                raise RuntimeError(str(r.status_code))
            j = r.json()
            if not isinstance(j, list):
                raise RuntimeError("non-list resp")
            return True, j
        except Exception:
            time.sleep(0.8 * (a + 1))
    return False, []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True)
    ap.add_argument("--days", type=int, default=7, help="抓近 N 天(0=全量)")
    ap.add_argument("--from", dest="from_when", default=None,
                    help="重跑範圍下界(含)：YYYY-MM-DD 或 unix 秒。指定後進入範圍模式，不動 floor 游標")
    ap.add_argument("--to", dest="to_when", default=None,
                    help="重跑範圍上界(含)：YYYY-MM-DD 或 unix 秒。預設到最新")
    ap.add_argument("--reset", action="store_true", help="清 cnft cursor 重抓")
    ap.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    args = ap.parse_args()

    load_env_file(Path(args.env_file).expanduser())
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        print("[cnft] HELIUS_API_KEY 未設定"); return 1

    cfg = load_config(args.platform)
    if (cfg.get("chain") or "").lower() != "solana":
        print("[cnft] 僅處理 solana"); return 1
    entries = _cnft_entries(cfg)
    if not entries:
        print("[cnft] config.contracts.cnft 為空"); return 1

    # 範圍模式：明確 [--from, --to] 重跑任意窗口（補洞用），不讀也不寫 floor 游標。
    range_mode = bool(args.from_when or args.to_when)
    range_top = None
    if range_mode:
        cutoff = _parse_when(args.from_when) if args.from_when else 0
        range_top = _parse_when(args.to_when, end_of_day=True) if args.to_when else None
    else:
        cutoff = 0 if args.days == 0 else int(time.time()) - args.days * 86400
    with get_conn() as conn:
        platform_id = upsert_platform(conn, args.platform, "solana", cfg.get("launch_block", 0), cfg)
        _upsert_refs(conn, platform_id, cfg)
        with conn.cursor() as c:
            c.execute(CURSOR_DDL)
            if args.reset:
                c.execute("DELETE FROM ingest_cursors WHERE platform_id=%s AND source=%s", (platform_id, CURSOR_SRC))
        conn.commit()
        if range_mode:
            print(f"[cnft] platform_id={platform_id} 範圍模式 "
                  f"from={_ts(cutoff).date() if cutoff else 'genesis'} "
                  f"to={_ts(range_top).date() if range_top else 'latest'}（不動 floor 游標）")
        else:
            print(f"[cnft] platform_id={platform_id} cutoff={_ts(cutoff).date() if cutoff else 'genesis'}")

        mint_batch, tr_batch = [], []
        n_mint = n_tr = n_burn = n_seen = 0

        def flush():
            if mint_batch:
                insert_mints(conn, platform_id, mint_batch); mint_batch.clear()
            if tr_batch:
                insert_nft_transfers(conn, platform_id, tr_batch); tr_batch.clear()
            conn.commit()

        for col, trees in entries:
            for tree in trees:
                # per-tree floor cursor（全量才用；位址用 tree 區分）；範圍模式不讀游標
                floor = None
                if args.days == 0 and not range_mode:
                    with conn.cursor() as c:
                        c.execute("SELECT last_block_time FROM ingest_cursors WHERE platform_id=%s AND address=%s AND source=%s",
                                  (platform_id, tree, CURSOR_SRC))
                        r = c.fetchone(); floor = r[0] if r else None
                # 上界跳過門檻：範圍模式用 --to，否則用 resume floor
                skip_above = range_top if range_mode else floor
                print(f"\n[cnft] === collection {col[:10]}… tree {tree[:10]}… === "
                      f"floor={_ts(floor).date() if floor else 'none'}")
                before = None; done = 0; stop = False
                while not stop:
                    ok, page = _enh_page(key, tree, before)
                    if not ok:
                        print(f"[cnft]   ⚠️ {tree[:8]} 連續抓取失敗(非到底)，中止本 tree；可重跑續抓"); break
                    if not page:
                        break   # 確實成功且空 = 真到底
                    before = page[-1]["signature"]
                    bt_low = page[-1].get("timestamp") or 0
                    for t in page:
                        bt = t.get("timestamp") or 0
                        slot = t.get("slot") or 0
                        sig = t.get("signature")
                        if cutoff and bt < cutoff:
                            stop = True; continue
                        if skip_above is not None and bt > skip_above:
                            continue   # 跳過上界以上：resume 已完成段（floor）或 --to 之後（範圍模式）
                        n_seen += 1
                        for e in t.get("events", {}).get("compressed") or []:
                            ty = e.get("type"); asset = e.get("assetId")
                            if not asset:
                                continue
                            li = e.get("leafIndex") or 0
                            if ty in MINT_TYPES:
                                to = e.get("newLeafOwner") or ""
                                mint_batch.append((platform_id, sig, li, _ts(bt), slot, to, asset, col, None))
                                n_mint += 1
                            elif ty == "COMPRESSED_NFT_TRANSFER":
                                tr_batch.append((platform_id, sig, li, _ts(bt), slot, col, asset,
                                                 e.get("oldLeafOwner") or "", e.get("newLeafOwner") or "", None, None))
                                n_tr += 1
                            elif ty == "COMPRESSED_NFT_BURN":
                                tr_batch.append((platform_id, sig, li, _ts(bt), slot, col, asset,
                                                 e.get("oldLeafOwner") or "", "", None, "burn"))
                                n_burn += 1
                        if len(mint_batch) + len(tr_batch) >= FLUSH:
                            flush()
                    flush()
                    if args.days == 0 and not range_mode and bt_low:   # 全量推進 floor(取更小)；範圍模式不動游標
                        with conn.cursor() as c:
                            c.execute("INSERT INTO ingest_cursors(platform_id,address,source,last_block_time) VALUES(%s,%s,%s,%s) "
                                      "ON CONFLICT (platform_id,address,source) DO UPDATE SET "
                                      "last_block_time=LEAST(ingest_cursors.last_block_time,EXCLUDED.last_block_time), updated_at=NOW()",
                                      (platform_id, tree, CURSOR_SRC, bt_low))
                        conn.commit()
                    done += len(page)
                    # 注意：Enhanced API 中途也可能回 <100 的短頁，**短頁不代表到底**（只有 ok 且空才到底）。
                    if done % 2000 < 100 or stop:
                        print(f"[cnft]   {tree[:8]} 累計 {done}  mint={n_mint} tr={n_tr} burn={n_burn}  至 {_ts(bt_low).date()}")

        with conn.cursor() as c:
            for tbl in ("mints", "nft_transfers"):
                c.execute(f"SELECT COUNT(*) FROM {tbl} WHERE platform_id=%s", (platform_id,))
                print(f"[cnft] {tbl} 總計(cNFT+MPL Core): {c.fetchone()[0]}")
            c.execute("SELECT COUNT(*) FROM nft_transfers WHERE platform_id=%s AND marketplace='burn'", (platform_id,))
            print(f"[cnft] 其中 burn 標記: {c.fetchone()[0]}")
    print(f"\n[cnft] done. 掃 {n_seen} 交易  mint={n_mint} transfer={n_tr} burn={n_burn}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
