"""
phygitals_capture.py — 被動側錄 phygitals 前端對後端 API 的請求/回應，抽出「收款地址」。

用途：確認 /claw 各種卡包買/開時的 USDC 收款地址是否分流(不同卡包→不同錢包)，
      以及賣回/buyback 的付款地址。**唯讀**——只側錄流量，不自動點擊、不簽名、不送交易、不花錢。

設計(human-in-the-loop，最安全)：
  - 用系統 Chrome + persistent profile 開一個有頭視窗(能過 Vercel/Cloudflare 挑戰、登入狀態持久)。
  - **你手動**：用 Google 登入 → 進 /claw → 點各種卡包到「準備購買/確認」那步(先別真的簽名送出)。
  - 腳本在旁被動攔截所有 api.phygitals.com 的 request/response，存全文到 captures/ 並即時印出:
      * 回應/請求 body 裡出現的 base58 地址(32-44 字)，標出是否為已知平台錢包(62Q9 等)。
      * URL 含 buy/open/purchase/prepare/order/claw/transaction 的請求(可能含收款地址)。
  - 你每種卡包各操作一次，腳本就會把各自的收款地址記下來 → 比對即知有沒有分流。

不碰你的 Google 密碼(OAuth 由你在視窗內手動完成)。token 不外傳。

用法：
  .venv/bin/python tools/phygitals_capture.py
  （視窗開啟後自行登入操作；每筆抓到的 api 呼叫會即時印出。Ctrl-C 結束，全文存 captures/）
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parent.parent
PROFILE = REPO / ".phygitals_profile"     # persistent 登入狀態(勿 commit)
OUTDIR = REPO / "captures"
OUTDIR.mkdir(exist_ok=True)

B58 = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
INTEREST = re.compile(r"(buy|open|purchase|prepare|order|claw|transaction|tx|checkout|pay|deposit|withdraw|sell|buyback|quote|mint)", re.I)

# 已知平台/標準地址標記(來自先前分析)
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
    "So11111111111111111111111111111111111111112": "wSOL",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "SPL Token prog",
    "HM25LvxMaS61voYRqdPAiidhYhme3X3YtcoikRv3Y94z": "(你的錢包)",
}

SKIP_PREFIX = ("1111111", "Vote111", "Sysvar", "Compute", "ATokenGP", "Stake111")
seen_addr = set()
records = []


def note_addrs(where: str, blob: str):
    for a in set(B58.findall(blob or "")):
        if any(a.startswith(p) for p in SKIP_PREFIX):
            continue
        tag = KNOWN.get(a, "")
        key = (where, a)
        if key in seen_addr:
            continue
        seen_addr.add(key)
        flag = "" if tag else "  ← 未知地址(可能是新收款地址!)"
        print(f"    addr {a}  {tag}{flag}")


def main():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            channel="chrome",           # 用系統 Chrome
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_response(resp):
            url = resp.url
            if "phygitals.com" not in url or "/_next/" in url or url.endswith((".js", ".css", ".png", ".jpg", ".svg", ".woff2")):
                return
            if "api.phygitals.com" not in url and not INTEREST.search(url):
                return
            try:
                body = resp.text()
            except Exception:
                body = ""
            is_api = "api.phygitals.com" in url
            interesting = is_api or INTEREST.search(url)
            has_addr = bool(B58.search(body or ""))
            try:
                rb = resp.request.post_data or ""
            except Exception:
                rb = ""
            # 所有 api.phygitals.com 呼叫都印一行(方便看到 buy/prepare 端點)
            if is_api:
                print(f"  · api {resp.request.method} {resp.status} {url.split('api.phygitals.com')[1][:90]}")
            # 記錄條件：所有 api.phygitals.com 回應全存(修正:之前漏存無地址的 GET)；或其它含地址/非-GET
            if not (is_api or (interesting and (has_addr or resp.request.method != "GET"))):
                return
            ts = time.strftime("%H:%M:%S")
            if has_addr or rb:
                print(f"\n[{ts}] {resp.request.method} {resp.status} {url[:110]}")
                if rb:
                    note_addrs("req", rb)
                note_addrs("resp", body)
            records.append({"ts": ts, "method": resp.request.method, "status": resp.status,
                            "url": url, "req_body": rb[:4000], "resp_body": (body or "")[:40000]})

        page.on("response", on_response)

        print("=" * 70)
        print("Chrome 視窗已開。請在視窗內：")
        print(" 1) 用 Google 登入 phygitals（OAuth 由你手動完成，我不碰你密碼）")
        print(" 2) 進 /claw，點每一種卡包到「準備購買/確認」那步（先別簽名送出）")
        print(" 3) 想查賣回就點賣回到準備那步")
        print("每抓到一個 api 呼叫會即時印出地址；★=已知平台錢包，『未知地址』=可能的新收款地址")
        print("完成後回終端機按 Ctrl-C，全文會存到 captures/")
        print("=" * 70)
        try:
            page.goto("https://www.phygitals.com/claw", wait_until="domcontentloaded")
        except Exception:
            pass
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            out = OUTDIR / f"phygitals_api_{int(time.time())}.json"
            out.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"\n側錄 {len(records)} 筆 api 呼叫 → {out}")
            # 收款地址彙總
            unknown = sorted({a for (w, a) in seen_addr if a not in KNOWN and not any(a.startswith(p) for p in SKIP_PREFIX)})
            print(f"出現的『未知地址』(需人工判斷是否為新收款錢包): {len(unknown)}")
            for a in unknown[:20]:
                print(f"  {a}")
            ctx.close()


if __name__ == "__main__":
    sys.exit(main())
