"""
[探索輔助工具，非資料管線] — 用來「找出某頁面背後的 API 端點」，一次性偵察用。
  正式抓取請用 ingest_card_charts.py（逐筆金額）等管線腳本。本檔的產物：發現了
  /api/marketplace/single-nft/chart 這個公開價格歷史端點（已被 ingest_card_charts.py 採用）。

phygitals_card_capture.py — 攔卡片詳情頁載入時打的 API，找「價格歷史」端點。

用 networkidle 等待(事件會正確派發,不用 sleep 迴圈)。開卡片頁 → 記錄所有 api.phygitals.com
請求(method+url+回應前段) → dump captures/，並標出含 price/history/chart 的端點。

用法：python3 tools/phygitals_card_capture.py [card_url]
預設 card: 2023-pokemon-sword-shield-crow-fdi3lz
"""
import json, sys, time, re
from pathlib import Path
from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parent.parent
PROFILE = REPO / ".phygitals_profile"
OUTDIR = REPO / "captures"; OUTDIR.mkdir(exist_ok=True)
URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.phygitals.com/card/2023-pokemon-sword-shield-crow-fdi3lz"

recs = []


def main():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            viewport={"width": 1200, "height": 850})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_resp(r):
            u = r.url
            if "api.phygitals.com" not in u:
                return
            try:
                body = r.text()
            except Exception:
                body = ""
            path = u.split("api.phygitals.com")[1]
            recs.append({"method": r.request.method, "status": r.status, "path": path,
                         "body": (body or "")[:8000]})
            hot = re.search(r"price|history|chart|fmv|value|sales|comp", path, re.I)
            print(f"{'★' if hot else ' '} {r.request.method} {r.status} {path[:100]}")

        page.on("response", on_resp)
        print(f"開卡片頁: {URL}\n(等 networkidle；若被 Vercel 擋會顯示 checkpoint，稍等它過)\n")
        try:
            page.goto(URL, wait_until="networkidle", timeout=60000)
        except Exception as e:
            print("goto note:", str(e)[:80])
        # 再多等網路穩定 & 讓圖表載入(用 wait_for_timeout, 會派發事件)
        try:
            page.wait_for_timeout(8000)
        except Exception:
            pass

        out = OUTDIR / f"card_api_{int(time.time())}.json"
        out.write_text(json.dumps(recs, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n共 {len(recs)} 個 api 呼叫 → {out}")
        print("★ 標記者=疑似價格/歷史端點；把該 path 貼給我即可")
        input("按 Enter 關閉…")
        ctx.close()


if __name__ == "__main__":
    main()
