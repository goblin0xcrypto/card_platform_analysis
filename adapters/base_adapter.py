"""
PlatformAdapter Protocol.

新增平台/新增鏈別：只實作這個 interface，下游 pipeline 與 SQL 不變。
domain 物件（PackOpen / Payment / Transfer / FlowEdge）與鏈無關。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Optional, Protocol


@dataclass
class PackOpen:
    tx_hash: str
    log_index: int
    block_time: datetime
    block_number: int
    opener: str
    pack_id: Optional[str]
    price_raw: int
    price_token: str
    price_usd: Decimal


@dataclass
class Mint:
    tx_hash: str
    log_index: int
    block_time: datetime
    block_number: int
    minter: str
    token_id: str
    contract: str
    pack_open_tx: Optional[str]


@dataclass
class Payment:
    tx_hash: str
    log_index: int
    block_time: datetime
    block_number: int
    from_addr: str
    to_addr: str
    token: str
    amount_raw: int
    amount_usd: Decimal
    direction: str  # pack_pay / royalty / fee / payout / refund


@dataclass
class Transfer:
    tx_hash: str
    log_index: int
    block_time: datetime
    block_number: int
    contract: str
    token_id: str
    from_addr: str
    to_addr: str
    price_usd: Optional[Decimal]
    marketplace: Optional[str]


@dataclass
class FlowEdge:
    """Deployer / 官方錢包資金圖的一條邊。"""
    from_addr: str
    to_addr: str
    token: str
    amount_usd: Decimal
    tx_hash: str
    block_time: datetime
    depth: int  # 距離起點的跳數


class PlatformAdapter(Protocol):
    """所有鏈別適配器實作這個介面。"""

    platform_name: str
    chain: str

    def fetch_pack_opens(
        self, from_block: int, to_block: int
    ) -> Iterable[PackOpen]: ...

    def fetch_mints(
        self, contract: str, from_block: int, to_block: int
    ) -> Iterable[Mint]: ...

    def fetch_payments(
        self, tx_hashes: list[str]
    ) -> Iterable[Payment]:
        """同 tx 的 ERC-20 Transfer + internal tx（原生幣支付）。"""
        ...

    def fetch_nft_transfers(
        self, contract: str, from_block: int, to_block: int
    ) -> Iterable[Transfer]: ...

    def fetch_deployer_flow(
        self, address: str, max_depth: int = 2
    ) -> Iterable[FlowEdge]:
        """以 address 為起點 BFS 走資金圖，回傳所有邊。"""
        ...

    def get_usd_price(self, token: str, block_time: datetime) -> Decimal:
        """寫入時 freeze 當下匯率。"""
        ...
