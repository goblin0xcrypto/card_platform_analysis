"""
MnstrAdapter — Monster Strategy (mnstr.xyz) on MegaETH Mainnet (chainId 4326).

平台特性（與 playkami/Monad 不同處）：
- 開包合約：3 個獨立 OffchainGacha（starter / premium / ultra），同一 deployer，
  皆吃 USDm（18 decimals，非 6）
- 卡片無鏈上 NFT，事件只有 (player, requestId)，沒有 token_id
  → mints / nft_transfers 表預設留空，由 marketplace 補
- 二級市場：CardMarketplace 用 string certNumber 當庫存 key（非 ERC-721）
- 免費玩：admin 可 grantFreePlayCredits，用 credit 開包時 costPaid=0
  → 不能用「沒付錢」直接判定 bot

事件 topic0（OffchainGacha）：
    GachaPlayed(address,uint256,uint256)         — 開包，data 含 costPaid
    NFTRedeemed(address,uint256)                 — 領卡（鏈下 fulfill）
    NFTSoldBack(address,uint256)                 — 賣回
    FreePlayCreditsGranted(address,uint256,uint256)
    FreePlayUsed(address,uint256)

事件 topic0（CardMarketplace）：
    CardListed(string,uint256)
    CardBought(string,address,uint256)
    BidPlaced(string,address,uint256)
    BidAccepted(string,address,uint256)
    SellBackRequested(string,address,uint256)
    RedeemRequested(string,address)
    PackBought(string,address,uint256,uint256)
    PackBidPlaced(string,address,uint256,uint256)
    PackBidAccepted(string,address,uint256,uint256)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Iterator, Optional

from eth_utils import keccak
from web3 import Web3

from adapters.base_adapter import FlowEdge, Mint, PackOpen, Payment, Transfer

# --------------------------------------------------------------------------- #
# Event topic0s
# --------------------------------------------------------------------------- #
def _topic0(sig: str) -> str:
    return "0x" + keccak(text=sig).hex()

# OffchainGacha
GACHA_PLAYED          = _topic0("GachaPlayed(address,uint256,uint256)")
NFT_REDEEMED          = _topic0("NFTRedeemed(address,uint256)")
NFT_SOLD_BACK         = _topic0("NFTSoldBack(address,uint256)")
FREE_PLAY_GRANTED     = _topic0("FreePlayCreditsGranted(address,uint256,uint256)")
FREE_PLAY_USED        = _topic0("FreePlayUsed(address,uint256)")

# CardMarketplace（certNumber/packId 是 non-indexed string → ABI-encoded 在 data）
CARD_LISTED           = _topic0("CardListed(string,uint256)")
CARD_BOUGHT           = _topic0("CardBought(string,address,uint256)")
CARD_DELISTED         = _topic0("CardDelisted(string)")
CARD_PRICE_UPDATED    = _topic0("CardPriceUpdated(string,uint256,uint256)")
BID_PLACED            = _topic0("BidPlaced(string,address,uint256)")
BID_ACCEPTED          = _topic0("BidAccepted(string,address,uint256)")
BID_WITHDRAWN         = _topic0("BidWithdrawn(string,address)")
SELL_BACK_REQUESTED   = _topic0("SellBackRequested(string,address,uint256)")
REDEEM_REQUESTED      = _topic0("RedeemRequested(string,address)")
PACK_BOUGHT           = _topic0("PackBought(string,address,uint256,uint256)")
PACK_BID_PLACED       = _topic0("PackBidPlaced(string,address,uint256,uint256)")
PACK_BID_ACCEPTED     = _topic0("PackBidAccepted(string,address,uint256,uint256)")

# ERC-20 / ERC-721 Transfer 共用同一 topic0
TRANSFER_ERC20_721    = _topic0("Transfer(address,address,uint256)")

USDM_DECIMALS = 18    # USDm/MegaUSD


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _topic_to_addr(topic: str) -> str:
    return "0x" + topic[-40:]


def _addr_to_topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower().removeprefix("0x")


def _log_int(v) -> int:
    if isinstance(v, int):
        return v
    if v in ("0x", "", None):       # MegaETH 對某些零值欄位回 "0x"
        return 0
    return int(v, 16)


def _log_time(log: dict) -> datetime:
    ts = _log_int(log["timeStamp"])
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _data_words(data: str) -> list[bytes]:
    """Split hex data into 32-byte words."""
    raw = bytes.fromhex(data.removeprefix("0x"))
    return [raw[i:i + 32] for i in range(0, len(raw), 32)]


def _decode_string_first_arg(data: str) -> tuple[str, list[bytes]]:
    """
    For events like CardBought(string certNumber, address indexed buyer, uint256 price):
    non-indexed = (string, uint256), data layout:
        word0 = offset to string (=0x40)
        word1 = price
        word2 = string length
        word3+ = string bytes
    Returns (cert_number, [remaining_static_words_after_string_head]).
    The string head is at word 0; static args come at word 1 onwards.
    """
    words = _data_words(data)
    if not words:
        return "", []
    # word0: offset to string (always == 32 * num_non_indexed_args)
    # words[1..] : static non-indexed args followed by string tail
    offset = int.from_bytes(words[0], "big")
    str_start = offset // 32
    if str_start >= len(words):
        return "", words[1:]
    str_len = int.from_bytes(words[str_start], "big")
    str_bytes = b"".join(words[str_start + 1:])[:str_len]
    cert = str_bytes.decode("utf-8", errors="replace")
    statics = words[1:str_start]
    return cert, statics


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
@dataclass
class GachaPlay:
    """OffchainGacha.GachaPlayed — 一次開包紀錄。"""
    tx_hash: str
    log_index: int
    block_time: datetime
    block_number: int
    pack_contract: str          # 哪個 pack（starter/premium/ultra）
    player: str
    request_id: str             # 對應後續 NFTRedeemed/NFTSoldBack 的 key
    cost_paid_raw: int          # USDm wei；0 表示用 FreePlay
    cost_usd: Decimal


@dataclass
class MarketEvent:
    """CardMarketplace 任一動作（list/buy/bid/sellback/redeem）。"""
    tx_hash: str
    log_index: int
    block_time: datetime
    block_number: int
    kind: str                   # 'card_listed' / 'card_bought' / 'bid_placed' ...
    cert_number: str            # 卡片 cert（或 pack_id）
    actor: str                  # buyer / bidder / seller / redeemer / ''
    counterparty: str           # 若可知
    price_raw: int              # USDm wei，無價格事件填 0
    price_usd: Decimal
    quantity: int               # pack 事件才用得到


class MnstrAdapter:
    platform_name: str
    chain: str = "megaeth"

    def __init__(
        self,
        platform_name: str,
        rpc_url: str,
        usdc_address: str,                  # 與 base adapter 簽名一致，這裡實際是 USDm
        chunk_size: int = 1000,
    ):
        self.platform_name = platform_name
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        self.usdm_address = usdc_address.lower()
        self.chunk_size = chunk_size

    # Attributes that ingest.py uses (so it stays chain-agnostic) ---------- #
    pack_event_topics: list[str] = [GACHA_PLAYED]
    transfer_topic0: str = TRANSFER_ERC20_721
    has_onchain_nft: bool = False                 # 卡片無鏈上 NFT，nft_transfers/mints 預設略過
    market_event_topics: list[str] = [
        CARD_LISTED, CARD_BOUGHT, CARD_DELISTED, CARD_PRICE_UPDATED,
        BID_PLACED, BID_ACCEPTED, BID_WITHDRAWN,
        SELL_BACK_REQUESTED, REDEEM_REQUESTED,
        PACK_BOUGHT, PACK_BID_PLACED, PACK_BID_ACCEPTED,
    ]

    @property
    def payment_token(self) -> str:
        return self.usdm_address

    @property
    def payment_decimals(self) -> int:
        return USDM_DECIMALS

    @property
    def payment_token_symbol(self) -> str:
        return "USDm"

    # ------------------------------------------------------------------ #
    # Explorer-driven parsers (raw log dict from Etherscan V2 getLogs)
    # ------------------------------------------------------------------ #
    def parse_gacha_played(self, log: dict) -> GachaPlay:
        # topics: [topic0, player, requestId]
        player = _topic_to_addr(log["topics"][1])
        request_id = str(_log_int(log["topics"][2]))
        cost = _log_int(log["data"])
        return GachaPlay(
            tx_hash=log["transactionHash"],
            log_index=_log_int(log["logIndex"]),
            block_time=_log_time(log),
            block_number=_log_int(log["blockNumber"]),
            pack_contract=log["address"].lower(),
            player=player,
            request_id=request_id,
            cost_paid_raw=cost,
            cost_usd=Decimal(cost) / Decimal(10 ** USDM_DECIMALS),
        )

    def parse_pack_opened(self, log: dict) -> PackOpen:
        """同 base_adapter.PackOpen 形狀，給 ingest.py 共用流程用。"""
        g = self.parse_gacha_played(log)
        return PackOpen(
            tx_hash=g.tx_hash,
            log_index=g.log_index,
            block_time=g.block_time,
            block_number=g.block_number,
            opener=g.player,
            pack_id=g.pack_contract,          # 三個合約即三個 pack tier；用合約地址當 pack_id
            price_raw=g.cost_paid_raw,
            price_token=self.usdm_address,
            price_usd=g.cost_usd,
        )

    def parse_redeem_or_sellback(self, log: dict, kind: str) -> dict:
        """NFTRedeemed / NFTSoldBack — 沒有 token_id，純 (player, requestId)。"""
        return {
            "tx_hash": log["transactionHash"],
            "log_index": _log_int(log["logIndex"]),
            "block_time": _log_time(log),
            "block_number": _log_int(log["blockNumber"]),
            "pack_contract": log["address"].lower(),
            "player": _topic_to_addr(log["topics"][1]),
            "request_id": str(_log_int(log["topics"][2])),
            "kind": kind,                     # 'redeemed' / 'sold_back'
        }

    def parse_usdc_payment(self, log: dict, related_address: str) -> Payment:
        """
        USDm Transfer parser（介面與 MonadAdapter.parse_usdc_payment 對齊以共用 ingest）。
        related_address 可為 pack_opening、marketplace、treasury 任一；單邊比對判 direction。
        """
        related = related_address.lower()
        from_addr = _topic_to_addr(log["topics"][1])
        to_addr   = _topic_to_addr(log["topics"][2])
        amount    = _log_int(log["data"])
        if to_addr == related:
            direction = "pack_pay"
        elif from_addr == related:
            direction = "payout"
        else:
            direction = "side"
        return Payment(
            tx_hash=log["transactionHash"],
            log_index=_log_int(log["logIndex"]),
            block_time=_log_time(log),
            block_number=_log_int(log["blockNumber"]),
            from_addr=from_addr,
            to_addr=to_addr,
            token=self.usdm_address,
            amount_raw=amount,
            amount_usd=Decimal(amount) / Decimal(10 ** USDM_DECIMALS),
            direction=direction,
        )

    # ------------------------------------------------------------------ #
    # CardMarketplace 事件解析
    # ------------------------------------------------------------------ #
    def parse_market_event(self, log: dict) -> Optional[MarketEvent]:
        t0 = log["topics"][0].lower()
        base = dict(
            tx_hash=log["transactionHash"],
            log_index=_log_int(log["logIndex"]),
            block_time=_log_time(log),
            block_number=_log_int(log["blockNumber"]),
            counterparty="",
            quantity=0,
        )

        if t0 == CARD_BOUGHT:
            # CardBought(string certNumber, address indexed buyer, uint256 price)
            cert, statics = _decode_string_first_arg(log["data"])
            price = int.from_bytes(statics[0], "big") if statics else 0
            buyer = _topic_to_addr(log["topics"][1])
            return MarketEvent(**base, kind="card_bought", cert_number=cert,
                               actor=buyer, price_raw=price,
                               price_usd=Decimal(price) / Decimal(10 ** USDM_DECIMALS))

        if t0 == CARD_LISTED:
            # CardListed(string certNumber, uint256 price) — no indexed
            cert, statics = _decode_string_first_arg(log["data"])
            price = int.from_bytes(statics[0], "big") if statics else 0
            return MarketEvent(**base, kind="card_listed", cert_number=cert,
                               actor="", price_raw=price,
                               price_usd=Decimal(price) / Decimal(10 ** USDM_DECIMALS))

        if t0 == CARD_DELISTED:
            cert, _ = _decode_string_first_arg(log["data"])
            return MarketEvent(**base, kind="card_delisted", cert_number=cert,
                               actor="", price_raw=0, price_usd=Decimal(0))

        if t0 == CARD_PRICE_UPDATED:
            # CardPriceUpdated(string, uint256 oldPrice, uint256 newPrice)
            cert, statics = _decode_string_first_arg(log["data"])
            new_price = int.from_bytes(statics[1], "big") if len(statics) > 1 else 0
            return MarketEvent(**base, kind="card_price_updated", cert_number=cert,
                               actor="", price_raw=new_price,
                               price_usd=Decimal(new_price) / Decimal(10 ** USDM_DECIMALS))

        if t0 == BID_PLACED:
            # BidPlaced(string certNumber, address indexed bidder, uint256 amount)
            cert, statics = _decode_string_first_arg(log["data"])
            amount = int.from_bytes(statics[0], "big") if statics else 0
            bidder = _topic_to_addr(log["topics"][1])
            return MarketEvent(**base, kind="bid_placed", cert_number=cert,
                               actor=bidder, price_raw=amount,
                               price_usd=Decimal(amount) / Decimal(10 ** USDM_DECIMALS))

        if t0 == BID_ACCEPTED:
            cert, statics = _decode_string_first_arg(log["data"])
            amount = int.from_bytes(statics[0], "big") if statics else 0
            bidder = _topic_to_addr(log["topics"][1])
            return MarketEvent(**base, kind="bid_accepted", cert_number=cert,
                               actor=bidder, price_raw=amount,
                               price_usd=Decimal(amount) / Decimal(10 ** USDM_DECIMALS))

        if t0 == BID_WITHDRAWN:
            cert, _ = _decode_string_first_arg(log["data"])
            bidder = _topic_to_addr(log["topics"][1])
            return MarketEvent(**base, kind="bid_withdrawn", cert_number=cert,
                               actor=bidder, price_raw=0, price_usd=Decimal(0))

        if t0 == SELL_BACK_REQUESTED:
            # SellBackRequested(string, address indexed seller, uint256 amount)
            cert, statics = _decode_string_first_arg(log["data"])
            amount = int.from_bytes(statics[0], "big") if statics else 0
            seller = _topic_to_addr(log["topics"][1])
            return MarketEvent(**base, kind="sell_back", cert_number=cert,
                               actor=seller, price_raw=amount,
                               price_usd=Decimal(amount) / Decimal(10 ** USDM_DECIMALS))

        if t0 == REDEEM_REQUESTED:
            cert, _ = _decode_string_first_arg(log["data"])
            redeemer = _topic_to_addr(log["topics"][1])
            return MarketEvent(**base, kind="redeem", cert_number=cert,
                               actor=redeemer, price_raw=0, price_usd=Decimal(0))

        if t0 == PACK_BOUGHT:
            # PackBought(string packId, address indexed buyer, uint256 quantity, uint256 totalPrice)
            pack_id, statics = _decode_string_first_arg(log["data"])
            qty = int.from_bytes(statics[0], "big") if statics else 0
            total = int.from_bytes(statics[1], "big") if len(statics) > 1 else 0
            buyer = _topic_to_addr(log["topics"][1])
            ev = MarketEvent(**base, kind="pack_bought", cert_number=pack_id,
                             actor=buyer, price_raw=total,
                             price_usd=Decimal(total) / Decimal(10 ** USDM_DECIMALS))
            ev.quantity = qty
            return ev

        if t0 == PACK_BID_PLACED:
            # PackBidPlaced(string, address indexed bidder, uint256 pricePerPack, uint256 quantity)
            pack_id, statics = _decode_string_first_arg(log["data"])
            per_pack = int.from_bytes(statics[0], "big") if statics else 0
            qty = int.from_bytes(statics[1], "big") if len(statics) > 1 else 0
            bidder = _topic_to_addr(log["topics"][1])
            ev = MarketEvent(**base, kind="pack_bid_placed", cert_number=pack_id,
                             actor=bidder, price_raw=per_pack * qty,
                             price_usd=Decimal(per_pack * qty) / Decimal(10 ** USDM_DECIMALS))
            ev.quantity = qty
            return ev

        if t0 == PACK_BID_ACCEPTED:
            pack_id, statics = _decode_string_first_arg(log["data"])
            qty = int.from_bytes(statics[0], "big") if statics else 0
            total = int.from_bytes(statics[1], "big") if len(statics) > 1 else 0
            bidder = _topic_to_addr(log["topics"][1])
            ev = MarketEvent(**base, kind="pack_bid_accepted", cert_number=pack_id,
                             actor=bidder, price_raw=total,
                             price_usd=Decimal(total) / Decimal(10 ** USDM_DECIMALS))
            ev.quantity = qty
            return ev

        return None

    # ------------------------------------------------------------------ #
    # Stub: 沒有鏈上 NFT，下列方法回空。保留為了和 base_adapter 介面相容。
    # ------------------------------------------------------------------ #
    def parse_nft_transfer(self, log: dict):
        return None, None

    def fetch_mints(self, *_args, **_kw) -> Iterable[Mint]:
        return iter(())

    def fetch_nft_transfers(self, *_args, **_kw) -> Iterable[Transfer]:
        return iter(())

    def fetch_deployer_flow(self, address: str, max_depth: int = 2) -> Iterable[FlowEdge]:
        return iter(())

    # ------------------------------------------------------------------ #
    # 價格：USDm 視為 $1 stablecoin
    # ------------------------------------------------------------------ #
    def get_usd_price(self, token: str, block_time: datetime) -> Decimal:
        if token.lower() == self.usdm_address:
            return Decimal(1)
        return Decimal(0)

    def latest_block(self) -> int:
        return self.w3.eth.block_number
