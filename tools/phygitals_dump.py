"""
[探索輔助工具，非資料管線] — 登入後 dump 平台已知端點、找收款/設定地址，一次性偵察用。
  本檔產物：確認收款錢包 vmNftOwner/vmBuyback 皆 62Q9、solanaFeePayer=mGrEw（全站單一、不分卡包）。

phygitals_dump.py — 用「已登入的瀏覽器 session」直接抓 phygitals 後端關鍵端點，找收款地址。

比 passive 側錄可靠：Playwright 同步模式在 time.sleep 迴圈不派發事件，會漏抓；這裡改用
context.request 主動呼叫已知端點(帶登入 cookie)，deterministic、不需點卡包、唯讀不花錢。

已知端點(從實際流量抓到)：
  GET  /api/vm/available?includeRepacks=true&platform=mainnet   卡包清單(各卡包設定/是否含 recipient)
  GET  /api/vm/repacks?limit=100&offset=0                        repack 清單
  GET  /api/vm/chase/mythic-pack                                 chase pack
  POST /api/orpc/config/signers/pubkeys                          平台收款/簽名錢包 pubkeys ★關鍵

流程：開系統 Chrome(persistent profile，沿用你上次登入)。若未登入會等你在視窗登入。
      然後主動抓上述端點，dump 全文到 captures/、抽出所有 base58 地址並標記已知平台錢包。

用法：python3 tools/phygitals_dump.py
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parent.parent
PROFILE = REPO / ".phygitals_profile"
OUTDIR = REPO / "captures"; OUTDIR.mkdir(exist_ok=True)
API = "https://api.phygitals.com"

B58 = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
SKIP = ("1111111", "Vote111", "Sysvar", "Compute", "ATokenGP", "Stake111", "TokenkegQ", "TokenzQ")
KNOWN = {
    "62Q9eeDY3eM8A5CnprBGYMPShdBjAzdpBdr71QHsS8dS": "★62Q9 主收付款",
    "Fi5hNT89iLtifKKyoNa6gDhQU5Az8zZ6bM65fSo82Ar1": "★Fi5hNT8 平台",
    "8d56BJgENF7v9A6YJnMinXqrQb7KKLxMhe3WmUf1Pa4N": "★8d56 平台",
    "FuhchUraFkdRfdysVLi5d8vB9YgRWAbnkyGLYAMVhe3t": "★Fuhch 平台",
    "5SWGkfFHWitcLSV23EL5eyw4YSNnTYceugJz4TScmuam": "★5SWGkf 平台",
    "2AyUvtafCjU9HcoQogZqu7gndXaKSd4ivSRQeLHbX4TC": "★2AyUvta 平台",
    "Gg8uoC2FpjLGBSeA6r8onLFDdc5ia81PSbB8LRjYR1xN": "★Gg8uoC2 平台",
    "6gn64NNTDS4sNDfAq6Qk6cdgPeCWjupPbUVLfNSHeY4S": "★6gn64 authority",
    "mGrEwYbMo98b6koTfy9pSfFJA1FZoxFxY7kjcsKf9C5": "★mGrEw fee_payer",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC mint",
    "HcrWkcgVMuS9D6vS6WfjynKuH7ud4ZykB7ASq2dyN4ks": "PIKA 代幣(非收款)",
    "So11111111111111111111111111111111111111112": "wSOL",
    "HM25LvxMaS61voYRqdPAiidhYhme3X3YtcoikRv3Y94z": "(你的錢包)",
}

ENDPOINTS = [
    ("GET",  "/api/vm/available?includeRepacks=true&platform=mainnet", None),
    ("GET",  "/api/vm/repacks?limit=100&offset=0", None),
    ("GET",  "/api/vm/chase/mythic-pack", None),
    ("POST", "/api/orpc/config/signers/pubkeys", {}),
]


def addrs_in(blob: str):
    out = {}
    for a in set(B58.findall(blob or "")):
        if any(a.startswith(p) for p in SKIP):
            continue
        out[a] = KNOWN.get(a, "")
    return out


def logged_in(ctx) -> bool:
    try:
        r = ctx.request.get(f"{API}/api/users/conversations", timeout=15000)
        return r.status == 200
    except Exception:
        return False


def main():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            viewport={"width": 1200, "height": 850},
            args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("https://www.phygitals.com/claw", wait_until="domcontentloaded")
        except Exception:
            pass

        print("等待登入狀態…（若視窗未登入，請在視窗內用 Google 登入；已登入會自動繼續）")
        for i in range(120):
            if logged_in(ctx):
                print("✓ 已登入，開始抓端點\n"); break
            time.sleep(2)
        else:
            print("⚠️ 120 秒內未偵測到登入，仍嘗試抓取（可能失敗）\n")

        results = {}
        allhits = {}
        for method, path, body in ENDPOINTS:
            url = API + path
            try:
                if method == "GET":
                    r = ctx.request.get(url, timeout=20000)
                else:
                    r = ctx.request.post(url, data=json.dumps(body),
                                         headers={"content-type": "application/json"}, timeout=20000)
                txt = r.text()
                results[path] = {"status": r.status, "body": txt[:60000]}
                hits = addrs_in(txt)
                allhits.update(hits)
                print(f"── {method} {path}  [{r.status}]  {len(txt)}B")
                if hits:
                    for a, tag in sorted(hits.items(), key=lambda kv: kv[1] == ""):
                        flag = "" if tag else "   ← 未知地址(可能新收款錢包!)"
                        print(f"     {a}  {tag}{flag}")
                else:
                    print("     (無 base58 地址)")
            except Exception as e:
                results[path] = {"error": str(e)[:200]}
                print(f"── {method} {path}  ERROR {str(e)[:120]}")

        out = OUTDIR / f"phygitals_dump_{int(time.time())}.json"
        out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n全文存 → {out}")
        unknown = [a for a, t in allhits.items() if not t]
        print(f"\n=== 彙總 ===")
        print(f"出現的『未知地址』(需判斷是否為新收款錢包): {len(unknown)}")
        for a in unknown: print(f"  {a}")
        print("\n若收款/簽名地址只出現已知★錢包 → 沒有新地址；有未知地址 → 有分流/新錢包，我再幫你上鏈確認。")
        input("\n按 Enter 關閉瀏覽器…")
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
