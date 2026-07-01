"""
Etherscan V2 multichain client.

Used by ingest pipeline to enumerate the historical txs of any address —
much faster than sweeping eth_getLogs across millions of blocks.

Supported chainids: anything Etherscan V2 supports (Monad mainnet = 143).
See https://api.etherscan.io/v2/chainlist
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterator, Optional

import requests

V2_BASE = "https://api.etherscan.io/v2/api"

# chain name (config.yaml) → Etherscan V2 chainid
CHAIN_IDS = {
    "ethereum": 1,
    "base": 8453,
    "arbitrum": 42161,
    "polygon": 137,
    "optimism": 10,
    "bsc": 56,
    "monad": 143,
    "megaeth": 4326,
}


@dataclass
class TxRecord:
    """Generic tx row (from txlist / tokentx)."""
    block_number: int
    timestamp: int
    tx_hash: str
    from_addr: str
    to_addr: str
    value_raw: int
    token: Optional[str] = None       # token contract for tokentx, None for native
    token_symbol: Optional[str] = None
    token_decimals: Optional[int] = None


class EtherscanV2Client:
    def __init__(self, api_key: str, chain: str, rate_limit_per_sec: float = 5.0):
        self.api_key = api_key
        self.chainid = CHAIN_IDS[chain.lower()]
        self.session = requests.Session()
        self._min_interval = 1.0 / rate_limit_per_sec
        self._last_call = 0.0

    def _call(self, params: dict, max_retries: int = 5) -> dict:
        # simple rate limit
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        params = {**params, "chainid": self.chainid, "apikey": self.api_key}
        # 暫時性網路/伺服器錯誤（ReadTimeout / ConnectionReset / 5xx）指數退避重試
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                r = self.session.get(V2_BASE, params=params, timeout=60)
                self._last_call = time.monotonic()
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                last_exc = e
                self._last_call = time.monotonic()
                if attempt == max_retries - 1:
                    break
                time.sleep(2 ** attempt)  # 1,2,4,8,16s
        raise last_exc

    # --------------------------------------------------------------- #
    # paginated address scans
    # --------------------------------------------------------------- #
    def _paginate(self, action: str, address: Optional[str] = None,
                  start_block: int = 0, end_block: int = 999999999,
                  contractaddress: Optional[str] = None) -> Iterator[dict]:
        """
        Etherscan 帳戶端點分頁。

        Etherscan 對任一查詢硬性限制最多回 10 000 筆（page × offset ≤ 10000），
        所以無法純靠 page 取完整歷史。當一個 window 取滿 10 000 筆時，把
        startblock 推進到「最後一筆的 block」再續抓（邊界 block 會重疊，靠
        content-key 去重），如此可掃完整段區間。

        傳 address（以參與者過濾）或 contractaddress（以 token 合約過濾），或兩者。
        """
        base = {"module": "account", "action": action,
                "offset": 10000, "sort": "asc", "page": 1}
        if address:
            base["address"] = address
        if contractaddress:
            base["contractaddress"] = contractaddress

        seen: set = set()
        cursor = start_block
        while cursor <= end_block:
            data = self._call({**base, "startblock": cursor, "endblock": end_block})
            result = data.get("result")
            if not isinstance(result, list) or not result:
                return
            last_block = cursor
            for row in result:
                last_block = int(row["blockNumber"])
                key = (row.get("hash"), row.get("from"), row.get("to"),
                       row.get("value"), row.get("contractAddress"),
                       row.get("tokenID"), row.get("traceId"))
                if key in seen:
                    continue
                seen.add(key)
                yield row
            if len(result) < 10000:
                return
            # 取滿 10 000 → 推進 window。邊界 block 重疊由 seen 去重；
            # 若單一 block 就 >10000 筆（last==cursor）只能 +1 推進（極罕見）。
            cursor = last_block if last_block > cursor else last_block + 1

    def txlist(self, address: str, start_block: int = 0,
               end_block: int = 999999999) -> Iterator[TxRecord]:
        """Outbound + inbound native txs."""
        for r in self._paginate("txlist", address, start_block, end_block):
            yield TxRecord(
                block_number=int(r["blockNumber"]),
                timestamp=int(r["timeStamp"]),
                tx_hash=r["hash"],
                from_addr=r["from"].lower(),
                to_addr=r["to"].lower() if r.get("to") else "",
                value_raw=int(r["value"]),
            )

    def txlistinternal(self, address: str, start_block: int = 0,
                       end_block: int = 999999999) -> Iterator[TxRecord]:
        """Internal (message-call) native transfers involving address."""
        for r in self._paginate("txlistinternal", address, start_block, end_block):
            yield TxRecord(
                block_number=int(r["blockNumber"]),
                timestamp=int(r["timeStamp"]),
                tx_hash=r["hash"],
                from_addr=r["from"].lower(),
                to_addr=r["to"].lower() if r.get("to") else "",
                value_raw=int(r["value"]),
            )

    def tokentx(self, address: str, start_block: int = 0,
                end_block: int = 999999999) -> Iterator[TxRecord]:
        """ERC-20 transfers involving address."""
        for r in self._paginate("tokentx", address, start_block, end_block):
            yield TxRecord(
                block_number=int(r["blockNumber"]),
                timestamp=int(r["timeStamp"]),
                tx_hash=r["hash"],
                from_addr=r["from"].lower(),
                to_addr=r["to"].lower(),
                value_raw=int(r["value"]),
                token=r["contractAddress"].lower(),
                token_symbol=r.get("tokenSymbol"),
                token_decimals=int(r["tokenDecimal"]) if r.get("tokenDecimal") else None,
            )

    def tokennfttx(self, address: Optional[str] = None, start_block: int = 0,
                   end_block: int = 999999999,
                   contractaddress: Optional[str] = None) -> Iterator[dict]:
        """ERC-721 transfers. 傳 address=參與者過濾；傳 contractaddress=該合約全部轉移。"""
        yield from self._paginate("tokennfttx", address, start_block, end_block,
                                  contractaddress=contractaddress)

    def token1155tx(self, address: Optional[str] = None, start_block: int = 0,
                    end_block: int = 999999999,
                    contractaddress: Optional[str] = None) -> Iterator[dict]:
        """ERC-1155 transfers. 傳 address=參與者過濾；傳 contractaddress=該合約全部轉移。"""
        yield from self._paginate("token1155tx", address, start_block, end_block,
                                  contractaddress=contractaddress)

    # --------------------------------------------------------------- #
    # getLogs (much faster than receipts)
    # --------------------------------------------------------------- #
    def get_logs(
        self,
        address: str,
        topic0: Optional[str] = None,
        from_block: int = 0,
        to_block: int = 999999999,
        topic1: Optional[str] = None,
        topic2: Optional[str] = None,
        topic3: Optional[str] = None,
    ) -> Iterator[dict]:
        """
        Etherscan V2 module=logs&action=getLogs.
        Etherscan caps results at 10 000 per query (10 pages × 1 000).
        When we hit the cap we auto-resume from last_block+1 and repeat,
        so callers always see the full range.
        """
        cursor = from_block
        while cursor <= to_block:
            page = 1
            last_block: Optional[int] = None
            truncated = False
            while True:
                params = {
                    "module": "logs", "action": "getLogs",
                    "address": address,
                    "fromBlock": cursor, "toBlock": to_block,
                    "page": page, "offset": 1000,
                }
                if topic0:
                    params["topic0"] = topic0
                for i, t in enumerate([topic1, topic2, topic3], start=1):
                    if t:
                        params[f"topic{i}"] = t
                        params[f"topic0_{i}_opr"] = "and"
                data = self._call(params)
                result = data.get("result")
                if not isinstance(result, list) or not result:
                    return
                for r in result:
                    yield r
                    last_block = int(r["blockNumber"], 16)
                if len(result) < 1000:
                    return
                page += 1
                if page > 10:           # Etherscan hard cap
                    truncated = True
                    break
            if not truncated or last_block is None:
                return
            # Continue past the cap. Risk: if last_block contains >1000 logs,
            # some might be skipped; trivial for our NFT/USDC scope.
            cursor = last_block + 1

    # --------------------------------------------------------------- #
    # one-shot
    # --------------------------------------------------------------- #
    def get_contract_creation(self, address: str) -> Optional[dict]:
        data = self._call({
            "module": "contract",
            "action": "getcontractcreation",
            "contractaddresses": address,
        })
        result = data.get("result", [])
        return result[0] if isinstance(result, list) and result else None
