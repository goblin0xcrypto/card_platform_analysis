"""
bootstrap.py — 給平台 URL，自動填 config.yaml 與 platform_profile.md。

流程：
1. WebFetch 平台首頁 / docs / whitepaper → 蒐集所有合約地址候選
2. 從 page meta、CSP、外連 RPC 推測鏈別
3. 對每個候選地址打 chain explorer API：
   - 確認合約是否存在、是否 verified
   - 取得 deployer、deploy block、deploy tx
   - 讀 ABI 判定類型（pack_opening / nft / marketplace / token）
4. 從 NFT 合約 Transfer event 反查實際的 minter（pack_opening 合約）
5. 從 pack_opening 合約最近 N 筆 tx 解 ERC-20 Transfer → 推付款幣種
6. 寫入 platforms/<name>/config.yaml + platform_profile.md

用法：
    python tools/bootstrap.py --url https://playkami.example --name playkami
    python tools/bootstrap.py --url https://playkami.example --name playkami --chain ethereum  # 跳過自動偵測
    python tools/bootstrap.py --url ... --name ... --env-file .env.local                       # 指定 .env 路徑

API key 讀取優先順序：
    1. 命令列環境變數（export ETHERSCAN_API_KEY=...）
    2. --env-file 指定的檔案
    3. 專案根目錄的 .env

需要的環境變數：
    ETHERSCAN_API_KEY   # 或對應鏈的 explorer key
    HELIUS_API_KEY      # Solana 用
    ANTHROPIC_API_KEY   # 用 Claude 解析 docs HTML 找合約段落（可選，沒有就用 regex 兜底）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PLATFORMS_DIR = REPO_ROOT / "platforms"
TEMPLATE_DIR = PLATFORMS_DIR / "_template"


def load_env_file(path: Path) -> int:
    """
    讀取 .env 檔案到 os.environ。
    - 已存在的環境變數不會被覆寫（cli > .env）
    - 支援 KEY=VALUE、KEY="VALUE"、KEY='VALUE'、# 註解、空行
    回傳新載入的變數數量。
    """
    if not path.exists():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val
            loaded += 1
    return loaded

EVM_ADDR_RE = re.compile(r"0x[a-fA-F0-9]{40}")
SOL_ADDR_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

CHAIN_HINTS = {
    "ethereum": ["etherscan.io", "ethereum", "mainnet.infura", "cloudflare-eth"],
    "base":     ["basescan.org", "base-mainnet", "base.org"],
    "arbitrum": ["arbiscan.io", "arb1.arbitrum"],
    "polygon":  ["polygonscan.com", "polygon-rpc"],
    "optimism": ["optimistic.etherscan.io", "mainnet.optimism.io"],
    "bsc":      ["bscscan.com", "bsc-dataseed"],
    "solana":   ["solscan.io", "solana.com", "helius"],
    "ton":      ["tonscan.org", "tonapi.io"],
    "sui":      ["suiscan.xyz", "fullnode.mainnet.sui"],
}

EXPLORER_API = {
    "ethereum": "https://api.etherscan.io/api",
    "base":     "https://api.basescan.org/api",
    "arbitrum": "https://api.arbiscan.io/api",
    "polygon":  "https://api.polygonscan.com/api",
    "optimism": "https://api-optimistic.etherscan.io/api",
    "bsc":      "https://api.bscscan.com/api",
}


@dataclass
class ContractInfo:
    address: str
    type: Optional[str] = None        # pack_opening / nft / marketplace / token / unknown
    name: Optional[str] = None
    deployer: Optional[str] = None
    deploy_block: Optional[int] = None
    deploy_tx: Optional[str] = None
    verified: bool = False
    is_proxy: bool = False
    implementation: Optional[str] = None


@dataclass
class BootstrapResult:
    platform: str
    chain: Optional[str] = None
    launch_block: Optional[int] = None
    contracts: list[ContractInfo] = field(default_factory=list)
    payment_tokens: list[str] = field(default_factory=list)
    official_links: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Step 1: scrape platform site
# --------------------------------------------------------------------------- #
def fetch_pages(start_url: str, max_pages: int = 20) -> list[tuple[str, str]]:
    """抓首頁 + docs 子路徑。回傳 [(url, html), ...]。"""
    pages: list[tuple[str, str]] = []
    seen: set[str] = set()
    queue: list[str] = [start_url]
    candidates = ["/docs", "/whitepaper", "/litepaper", "/contracts", "/about", "/faq"]
    for path in candidates:
        queue.append(start_url.rstrip("/") + path)

    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "card-analysis-bootstrap/1.0"})
            if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
                pages.append((url, r.text))
        except requests.RequestException:
            continue
    return pages


# --------------------------------------------------------------------------- #
# Step 2: detect chain
# --------------------------------------------------------------------------- #
def detect_chain(pages: list[tuple[str, str]]) -> Optional[str]:
    blob = " ".join(html.lower() for _, html in pages)
    scores: dict[str, int] = {}
    for chain, hints in CHAIN_HINTS.items():
        scores[chain] = sum(blob.count(h) for h in hints)
    best = max(scores.items(), key=lambda kv: kv[1])
    return best[0] if best[1] > 0 else None


# --------------------------------------------------------------------------- #
# Step 3: extract candidate addresses
# --------------------------------------------------------------------------- #
def extract_addresses(pages: list[tuple[str, str]], chain: Optional[str]) -> list[str]:
    blob = "\n".join(html for _, html in pages)
    if chain == "solana":
        candidates = set(SOL_ADDR_RE.findall(blob))
    else:
        candidates = {a.lower() for a in EVM_ADDR_RE.findall(blob)}
    # 排除全 0 / 常見假地址
    candidates.discard("0x0000000000000000000000000000000000000000")
    candidates.discard("0x000000000000000000000000000000000000dead")
    return sorted(candidates)


# --------------------------------------------------------------------------- #
# Step 4: enrich via chain explorer
# --------------------------------------------------------------------------- #
def enrich_evm_contract(chain: str, address: str, api_key: str) -> ContractInfo:
    api = EXPLORER_API[chain]
    info = ContractInfo(address=address)

    # 是否為合約 + 取 ABI / 名稱
    r = requests.get(api, params={
        "module": "contract", "action": "getsourcecode",
        "address": address, "apikey": api_key,
    }, timeout=10).json()
    if r.get("status") == "1" and r["result"]:
        src = r["result"][0]
        info.name = src.get("ContractName") or None
        info.verified = bool(src.get("SourceCode"))
        if src.get("Proxy") == "1":
            info.is_proxy = True
            info.implementation = src.get("Implementation") or None

    # Deployer + deploy tx
    r2 = requests.get(api, params={
        "module": "contract", "action": "getcontractcreation",
        "contractaddresses": address, "apikey": api_key,
    }, timeout=10).json()
    if r2.get("status") == "1" and r2["result"]:
        info.deployer = r2["result"][0]["contractCreator"]
        info.deploy_tx = r2["result"][0]["txHash"]

    # 從 ABI / 名稱猜類型
    info.type = guess_contract_type(info)
    return info


def guess_contract_type(info: ContractInfo) -> str:
    name = (info.name or "").lower()
    if any(k in name for k in ("pack", "mint", "box", "gacha", "loot", "opener")):
        return "pack_opening"
    if any(k in name for k in ("market", "exchange", "trade", "auction")):
        return "marketplace"
    if any(k in name for k in ("erc721", "erc1155", "nft", "card", "collection")):
        return "nft"
    if any(k in name for k in ("token", "erc20", "coin")):
        return "token"
    if "stak" in name:
        return "staking"
    return "unknown"


# --------------------------------------------------------------------------- #
# Step 5: detect payment token by reading recent txs
# --------------------------------------------------------------------------- #
def detect_payment_token(chain: str, pack_opening_addr: str, api_key: str) -> list[str]:
    api = EXPLORER_API[chain]
    r = requests.get(api, params={
        "module": "account", "action": "tokentx",
        "address": pack_opening_addr,
        "page": 1, "offset": 100, "sort": "desc",
        "apikey": api_key,
    }, timeout=10).json()
    if r.get("status") != "1":
        return []
    tokens: dict[str, int] = {}
    for tx in r["result"]:
        sym = tx.get("tokenSymbol", "?")
        tokens[sym] = tokens.get(sym, 0) + 1
    return [t for t, _ in sorted(tokens.items(), key=lambda kv: -kv[1])][:3]


# --------------------------------------------------------------------------- #
# Step 6: render config.yaml + platform_profile.md
# --------------------------------------------------------------------------- #
def render_config(result: BootstrapResult, out_dir: Path) -> None:
    template = (TEMPLATE_DIR / "config.yaml").read_text(encoding="utf-8")
    by_type: dict[str, list[str]] = {}
    for c in result.contracts:
        by_type.setdefault(c.type or "unknown", []).append(c.address)

    cfg = yaml.safe_load(template)
    cfg["platform"] = result.platform
    cfg["chain"] = result.chain or "REPLACE_ME"
    cfg["launch_block"] = result.launch_block or 0
    cfg["contracts"]["pack_opening"] = (by_type.get("pack_opening") or [""])[0]
    cfg["contracts"]["marketplace"]  = (by_type.get("marketplace")  or [""])[0]
    cfg["contracts"]["staking"]      = (by_type.get("staking")      or [""])[0]
    cfg["contracts"]["token"]        = (by_type.get("token")        or [""])[0]
    cfg["contracts"]["nft"]          = by_type.get("nft", [])
    cfg["deployers"] = sorted({c.deployer for c in result.contracts if c.deployer})

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")


def render_profile(result: BootstrapResult, out_dir: Path) -> None:
    lines: list[str] = []
    lines.append(f"# Platform Profile — {result.platform}\n")
    lines.append("> 由 tools/bootstrap.py 自動產生。**請人工複核**所有欄位。\n")
    lines.append("## 基本資訊\n")
    lines.append("| 項目 | 內容 |\n|------|------|")
    lines.append(f"| 平台名稱 | {result.platform} |")
    lines.append(f"| 鏈別 | {result.chain or '⚠️ 未偵測到，請人工確認'} |")
    lines.append(f"| 啟動區塊 | {result.launch_block or 'TBD'} |")
    lines.append(f"| 官方連結 | {', '.join(result.official_links) or 'N/A'} |")
    lines.append("")

    lines.append("## 合約清單\n")
    lines.append("| 類型 | 地址 | Deployer | Deploy Block | Verified | Proxy | 備註 |")
    lines.append("|------|------|----------|--------------|----------|-------|------|")
    for c in result.contracts:
        lines.append(
            f"| {c.type or 'unknown'} | `{c.address}` | "
            f"`{c.deployer or '?'}` | {c.deploy_block or '?'} | "
            f"{'✅' if c.verified else '⚠️'} | "
            f"{'是 → ' + (c.implementation or '?') if c.is_proxy else '否'} | "
            f"{c.name or ''} |"
        )
    lines.append("")

    lines.append("## 付款幣種（由近 100 筆 tokentx 推測）\n")
    if result.payment_tokens:
        for t in result.payment_tokens:
            lines.append(f"- {t}")
    else:
        lines.append("- ⚠️ 無法自動偵測，請人工確認")
    lines.append("")

    lines.append("## 待人工確認事項\n")
    lines.append("- [ ] 合約類型分類是否正確（自動推測可能誤判）")
    lines.append("- [ ] 是否有未列出的官方錢包（treasury / 多簽）")
    lines.append("- [ ] Deployer 資金來源 trace（≤ 3 跳）")
    lines.append("- [ ] Deployer 過去部署過哪些合約")
    lines.append("- [ ] 比對 `shared/known_addresses.csv` 是否命中已知地址")
    lines.append("")

    if result.notes:
        lines.append("## Bootstrap 過程備註\n")
        for n in result.notes:
            lines.append(f"- {n}")

    (out_dir / "platform_profile.md").write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--name", required=True, help="kebab-case platform name")
    ap.add_argument("--chain", help="覆寫自動偵測")
    ap.add_argument("--env-file", default=str(REPO_ROOT / ".env"),
                    help="dotenv 檔案路徑（預設專案根目錄 .env）")
    args = ap.parse_args()

    env_path = Path(args.env_file).expanduser().resolve()
    loaded = load_env_file(env_path)
    if loaded:
        print(f"[env] loaded {loaded} vars from {env_path}")
    elif env_path.exists():
        print(f"[env] {env_path} exists but all keys already set in shell")
    else:
        print(f"[env] {env_path} not found, using shell env only")

    result = BootstrapResult(platform=args.name, official_links=[args.url])

    print(f"[1/6] Fetching pages from {args.url} ...")
    pages = fetch_pages(args.url)
    if not pages:
        print("  ⚠️ 無法抓到任何頁面，是否需要 JS render？fallback: 人工貼 HTML")
        return 1
    result.notes.append(f"抓取頁面數：{len(pages)}")

    print("[2/6] Detecting chain ...")
    result.chain = args.chain or detect_chain(pages)
    print(f"  → chain = {result.chain}")
    if not result.chain:
        result.notes.append("⚠️ 鏈別自動偵測失敗")

    print("[3/6] Extracting candidate addresses ...")
    candidates = extract_addresses(pages, result.chain)
    print(f"  → {len(candidates)} candidates")

    if result.chain in EXPLORER_API:
        api_key = os.environ.get("ETHERSCAN_API_KEY")
        if not api_key:
            print("  ⚠️ ETHERSCAN_API_KEY 未設定，跳過 enrich")
        else:
            print("[4/6] Enriching via explorer ...")
            for addr in candidates[:50]:
                try:
                    info = enrich_evm_contract(result.chain, addr, api_key)
                    if info.deployer:
                        result.contracts.append(info)
                except Exception as e:
                    result.notes.append(f"enrich 失敗 {addr}: {e}")

            print("[5/6] Detecting payment token ...")
            pack = next((c for c in result.contracts if c.type == "pack_opening"), None)
            if pack:
                result.payment_tokens = detect_payment_token(result.chain, pack.address, api_key)
            else:
                result.notes.append("⚠️ 未找到 pack_opening 合約，無法偵測付款幣")
    else:
        result.notes.append(f"⚠️ 鏈別 {result.chain} 尚未實作 explorer adapter")

    print("[6/6] Writing config.yaml + platform_profile.md ...")
    out_dir = PLATFORMS_DIR / args.name
    render_config(result, out_dir)
    render_profile(result, out_dir)
    print(f"  → {out_dir}/config.yaml")
    print(f"  → {out_dir}/platform_profile.md")
    print("Done. 請人工複核標 ⚠️ 的欄位。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
