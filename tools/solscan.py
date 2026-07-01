"""
Solscan Pro API v2 client — Solana 的「索引資料」來源，對應 EVM 的 EtherscanV2Client。

為什麼用索引 API 而非 RPC：
  Solana RPC 沒有「給我這地址所有轉帳」的端點，只能 getSignaturesForAddress 逐筆翻
  再對每個簽名打 getTransaction 自己解析（N+1，活躍錢包要數小時且會被公開 RPC 限流）。
  Solscan 已預先索引並解析，/account/transfer 直接回「已解析的轉帳列」，分頁即可掃完。

認證：HTTP header `token: <SOLANA_API_KEY>`（從 solscan.io profile 取得；需付費方案，
      免費 key 對 v2 端點全回 401「upgrade your api key level」）。base = pro-api.solscan.io/v2.0。

規格坑（實測 2026-06）：
  - page_size 只能用允許值；帳戶/代幣類 10/20/30/40/60/100，NFT 類 12/24/36。
  - 路徑 balance_change 是底線，不是文件寫的連字號。
  - sort_by=block_time & sort_order=asc 可得穩定排序 → 支援以 block_time resume。
  - 支援 token= 過濾與 block_time[]=[from,to] 時間範圍過濾。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterator, Optional

import requests

PRO_BASE = "https://pro-api.solscan.io/v2.0"

# Solscan 用全 1 的 mint 代表原生 SOL（注意：wSOL 是結尾 ...2）
NATIVE_SOL = "So11111111111111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"

ACCOUNT_PAGE_SIZES = (10, 20, 30, 40, 60, 100)
NFT_PAGE_SIZES = (12, 24, 36)


@dataclass
class SolTransfer:
    """/account/transfer 一列（已解析的 SOL / SPL / NFT 轉帳）。"""
    slot: int
    block_time: int
    signature: str
    activity_type: str          # ACTIVITY_SPL_TRANSFER / ACTIVITY_SPL_CREATE_ACCOUNT / ...
    from_addr: str
    to_addr: str
    token: Optional[str]        # token mint；原生 SOL = NATIVE_SOL
    token_decimals: Optional[int]
    amount_raw: int             # 最小單位整數
    flow: str                   # in / out（相對被查詢的 address）
    value_usd: Optional[float]  # Solscan 估的當下 USD 值（可能為 0/None）


class SolscanClient:
    def __init__(self, api_key: str, base: str = PRO_BASE, rate_limit_per_sec: float = 5.0):
        if not api_key:
            raise ValueError("SolscanClient 需要 SOLANA_API_KEY")
        self.base = base.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"token": api_key, "accept": "application/json"})
        self._min_interval = 1.0 / rate_limit_per_sec
        self._last_call = 0.0

    # ------------------------------------------------------------------ #
    def _call(self, path: str, params: dict, max_retries: int = 10) -> dict:
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        url = f"{self.base}{path}"
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                r = self.session.get(url, params=params, timeout=60)
                self._last_call = time.monotonic()
                if r.status_code == 429:
                    # 滾動配額：耐心等窗口重置(別把長 run 弄死)；漸進但封頂 30s
                    raise requests.HTTPError("429", response=r)
                if r.status_code >= 500:
                    raise requests.HTTPError(f"{r.status_code}", response=r)
                j = r.json()
                if not j.get("success", False):
                    msg = j.get("errors", {}).get("message", j)
                    raise RuntimeError(f"Solscan {path} 失敗: {msg}")
                return j
            except (requests.RequestException, RuntimeError) as e:
                last_exc = e
                self._last_call = time.monotonic()
                if isinstance(e, RuntimeError) and "api key level" in str(e):
                    raise  # 認證/方案類不重試
                if attempt == max_retries - 1:
                    break
                is429 = isinstance(e, requests.HTTPError) and "429" in str(e)
                time.sleep(min(30, (5 if is429 else 1) * (attempt + 1)))  # 429 等久一點
        raise last_exc

    def _paginate(self, path: str, base_params: dict,
                  page_size: int = 100, max_pages_per_window: int = 1000) -> Iterator[dict]:
        """
        page 分頁 + block_time 時間窗 fallback。

        多數查詢 page 翻到底即可（`len(data) < page_size` 即結束）；只有當方案對 page 數設上限、
        達 max_pages_per_window 仍滿頁時，才把 from_time 推進到「最後一列的 block_time」再從
        page=1 續抓（重疊由 seen 去重）。窗口設大可減少重啟→大幅降低重複抓取(實測 50 頁窗口
        會造成 ~3.8x 重抓)。需 sort_order=asc 才能穩定推進。
        """
        params = {**base_params, "page_size": page_size,
                  "sort_by": "block_time", "sort_order": "asc"}
        seen: set = set()
        from_time = base_params.get("_from_time", 0)
        to_time = base_params.get("_to_time")
        params.pop("_from_time", None); params.pop("_to_time", None)
        while True:
            page = 1
            last_bt = from_time
            got_full_window = False
            while page <= max_pages_per_window:
                q = {**params, "page": page}
                bt = [from_time, to_time] if to_time else None
                if from_time:
                    q["block_time[]"] = bt if bt else from_time
                data = self._call(path, q).get("data") or []
                if not data:
                    return
                for row in data:
                    key = (row.get("trans_id"), row.get("token_address"),
                           row.get("from_address"), row.get("to_address"),
                           row.get("amount"), row.get("flow"))
                    if key in seen:
                        continue
                    seen.add(key)
                    last_bt = row.get("block_time", last_bt)
                    yield row
                if len(data) < page_size:
                    return
                page += 1
            else:
                got_full_window = True
            if not got_full_window:
                return
            # 達 page 上限仍滿 → 用時間窗續抓
            from_time = last_bt if last_bt > from_time else last_bt + 1

    # ------------------------------------------------------------------ #
    # account
    # ------------------------------------------------------------------ #
    def account_transfers(self, address: str, from_time: int = 0,
                          to_time: Optional[int] = None, token: Optional[str] = None,
                          page_size: int = 100) -> Iterator[SolTransfer]:
        """某地址的所有 SOL/SPL/NFT 轉帳（已解析）。可選 token 過濾、時間範圍。"""
        base = {"address": address, "_from_time": from_time}
        if to_time:
            base["_to_time"] = to_time
        if token:
            base["token"] = token
        for r in self._paginate("/account/transfer", base, page_size=page_size):
            amt = r.get("amount")
            yield SolTransfer(
                slot=r.get("block_id") or 0,
                block_time=r.get("block_time") or 0,
                signature=r.get("trans_id") or "",
                activity_type=r.get("activity_type") or "",
                from_addr=r.get("from_address") or "",
                to_addr=r.get("to_address") or "",
                token=r.get("token_address"),
                token_decimals=r.get("token_decimals"),
                amount_raw=int(amt) if amt is not None else 0,
                flow=r.get("flow") or "",
                value_usd=r.get("value"),
            )

    def account_detail(self, address: str) -> dict:
        return self._call("/account/detail", {"address": address}).get("data") or {}

    def account_transactions(self, address: str, limit: int = 40) -> Iterator[dict]:
        """某地址的交易清單（含 parsed_instructions，但不含每指令 accounts）。
        以 `before`=最後一筆 tx_hash 游標分頁(降冪)。用於列舉 mpl_core 交易。"""
        before = None
        while True:
            params = {"address": address, "limit": limit}
            if before:
                params["before"] = before
            data = self._call("/account/transactions", params).get("data") or []
            if not data:
                return
            for t in data:
                yield t
            if len(data) < limit:
                return
            before = data[-1].get("tx_hash")
            if not before:
                return

    def transaction_detail(self, tx: str) -> dict:
        """單筆交易詳情（parsed_instructions 內含每指令的 accounts 陣列）。"""
        return self._call("/transaction/detail", {"tx": tx}).get("data") or {}

    # ------------------------------------------------------------------ #
    # token
    # ------------------------------------------------------------------ #
    def token_meta(self, address: str) -> dict:
        """token mint 的 metadata（symbol / decimals / name ...）。"""
        return self._call("/token/meta", {"address": address}).get("data") or {}

    # ------------------------------------------------------------------ #
    # usage
    # ------------------------------------------------------------------ #
    def usage(self) -> dict:
        """方案/CU 用量（/monitor/usage）。"""
        return self._call("/monitor/usage", {}).get("data") or {}
