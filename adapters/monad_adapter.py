"""
MonadAdapter — implements PlatformAdapter for Monad EVM L1.

Monad-specific notes:
- RPC: https://rpc3.monad.xyz (default, no archive state)
- eth_getLogs has a tight block range limit (~100 blocks per call)
- Block time ≈ 0.4s, so ranges go BIG quickly
- USDC on Monad: 0x754704bc059f8c67012fed69bc8a327a5aafb603 (decimals=6)
- pack_opening contracts on Monad use EIP-1967 proxy pattern (verifier code confirms)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Iterator, Optional

from eth_utils import keccak
from web3 import Web3

from adapters.base_adapter import FlowEdge, Mint, PackOpen, Payment, Transfer

# Event topic0s (precomputed)
PACK_OPENED_NEW = "0x" + keccak(
    text="PackOpened(address,uint256,bytes32,bytes32,bytes32,uint256,uint256[],bytes32)"
).hex()
PACK_OPENED_OLD = "0x" + keccak(
    text="PackOpened(address,uint256,bytes32,bytes32,bytes32,uint256,uint256[])"
).hex()
TRANSFER_ERC20_721 = "0x" + keccak(text="Transfer(address,address,uint256)").hex()


def _addr_to_topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower().removeprefix("0x")


def _topic_to_addr(topic: str) -> str:
    return "0x" + topic[-40:]


class MonadAdapter:
    platform_name: str
    chain: str = "monad"

    def __init__(
        self,
        platform_name: str,
        rpc_url: str,
        usdc_address: str,
        chunk_size: int = 100,
    ):
        self.platform_name = platform_name
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        self.usdc_address = usdc_address.lower()
        self.chunk_size = chunk_size

    # Attributes that ingest.py uses (so it stays chain-agnostic) ---------- #
    pack_event_topics: list[str] = [PACK_OPENED_NEW, PACK_OPENED_OLD]
    transfer_topic0: str = TRANSFER_ERC20_721
    has_onchain_nft: bool = True
    market_event_topics: list[str] = []          # 沒有 marketplace 事件 ingest

    @property
    def payment_token(self) -> str:
        return self.usdc_address

    @property
    def payment_decimals(self) -> int:
        return 6

    @property
    def payment_token_symbol(self) -> str:
        return "USDC"

    # ------------------------------------------------------------------ #
    # internal helpers
    # ------------------------------------------------------------------ #
    def _iter_logs(
        self,
        from_block: int,
        to_block: int,
        address: Optional[str] = None,
        topics: Optional[list] = None,
    ) -> Iterator[dict]:
        """Walk getLogs in chunks of self.chunk_size."""
        for start in range(from_block, to_block + 1, self.chunk_size):
            end = min(start + self.chunk_size - 1, to_block)
            params: dict = {"fromBlock": start, "toBlock": end}
            if address:
                params["address"] = Web3.to_checksum_address(address)
            if topics:
                params["topics"] = topics
            try:
                for log in self.w3.eth.get_logs(params):
                    yield log
            except Exception as e:
                # silently skip bad windows but keep going
                print(f"[monad] getLogs err in {start}-{end}: {e}")
                continue

    def _block_time(self, block_number: int) -> datetime:
        block = self.w3.eth.get_block(block_number)
        return datetime.fromtimestamp(block.timestamp, tz=timezone.utc)

    # ------------------------------------------------------------------ #
    # PackOpened events
    # ------------------------------------------------------------------ #
    def fetch_pack_opens(
        self,
        from_block: int,
        to_block: int,
        pack_opening_address: Optional[str] = None,
    ) -> Iterable[PackOpen]:
        if not pack_opening_address:
            raise ValueError("pack_opening_address required")

        # Try new event first; if any platform has only old event, caller can re-fetch
        for topic in (PACK_OPENED_NEW, PACK_OPENED_OLD):
            for log in self._iter_logs(
                from_block, to_block, address=pack_opening_address, topics=[topic]
            ):
                yield self._parse_pack_opened(log, topic == PACK_OPENED_NEW)

    def _parse_pack_opened(self, log: dict, is_new: bool) -> PackOpen:
        # Indexed: user, packId, clientSeed (topics[1..3])
        # New non-indexed data: serverSeed, finalRandomness, blockTimestamp, cardIds_offset, snapshotHash
        # Old non-indexed data: serverSeed, finalRandomness, blockTimestamp, cardIds (no snapshot)
        opener = _topic_to_addr(log["topics"][1].hex())
        pack_id = int(log["topics"][2].hex(), 16)

        bt = self._block_time(log["blockNumber"])

        # PRICE: PackOpened event itself doesn't carry price; we resolve it later
        # in fetch_payments by reading USDC Transfer to pack_opening in the same tx.
        # For now store 0 — ingest.py will join payments after.
        return PackOpen(
            tx_hash="0x" + log["transactionHash"].hex(),
            log_index=log["logIndex"],
            block_time=bt,
            block_number=log["blockNumber"],
            opener=opener,
            pack_id=str(pack_id),
            price_raw=0,
            price_token=self.usdc_address,
            price_usd=Decimal(0),
        )

    # ------------------------------------------------------------------ #
    # NFT Mint events  (Transfer with from = 0x0)
    # ------------------------------------------------------------------ #
    def fetch_mints(
        self, contract: str, from_block: int, to_block: int
    ) -> Iterable[Mint]:
        zero_topic = "0x" + "0" * 64
        for log in self._iter_logs(
            from_block,
            to_block,
            address=contract,
            topics=[TRANSFER_ERC20_721, zero_topic],
        ):
            # ERC-721 Transfer has 3 indexed args (from, to, tokenId)
            # ERC-20 Transfer has 2 indexed args (from, to). Distinguish by topics length.
            topics = log["topics"]
            if len(topics) < 4:
                continue  # ERC-20-style; not an NFT mint we care about
            to_addr = _topic_to_addr(topics[2].hex())
            token_id = str(int(topics[3].hex(), 16))
            yield Mint(
                tx_hash="0x" + log["transactionHash"].hex(),
                log_index=log["logIndex"],
                block_time=self._block_time(log["blockNumber"]),
                block_number=log["blockNumber"],
                minter=to_addr,
                token_id=token_id,
                contract=contract.lower(),
                pack_open_tx="0x" + log["transactionHash"].hex(),
            )

    # ------------------------------------------------------------------ #
    # NFT all transfers (for nft_transfers / endstate)
    # ------------------------------------------------------------------ #
    def fetch_nft_transfers(
        self, contract: str, from_block: int, to_block: int
    ) -> Iterable[Transfer]:
        for log in self._iter_logs(
            from_block, to_block, address=contract, topics=[TRANSFER_ERC20_721]
        ):
            topics = log["topics"]
            if len(topics) < 4:
                continue
            from_addr = _topic_to_addr(topics[1].hex())
            to_addr = _topic_to_addr(topics[2].hex())
            token_id = str(int(topics[3].hex(), 16))
            yield Transfer(
                tx_hash="0x" + log["transactionHash"].hex(),
                log_index=log["logIndex"],
                block_time=self._block_time(log["blockNumber"]),
                block_number=log["blockNumber"],
                contract=contract.lower(),
                token_id=token_id,
                from_addr=from_addr,
                to_addr=to_addr,
                price_usd=None,
                marketplace=None,
            )

    # ------------------------------------------------------------------ #
    # USDC Transfers as payments
    # ------------------------------------------------------------------ #
    def fetch_payments(
        self,
        from_block: int,
        to_block: int,
        related_addresses: list[str],
    ) -> Iterable[Payment]:
        """
        Scan USDC Transfer events where either `from` or `to` is in
        `related_addresses` (pack_opening, treasury, deployer, etc.).
        """
        related_topics = [_addr_to_topic(a) for a in related_addresses]

        # Two passes: USDC transferred TO related, then FROM related.
        # eth_getLogs supports OR within a single topic position; pass list.
        for log in self._iter_logs(
            from_block,
            to_block,
            address=self.usdc_address,
            topics=[TRANSFER_ERC20_721, None, related_topics],
        ):
            yield self._parse_payment(log, direction_hint="pack_pay")

        for log in self._iter_logs(
            from_block,
            to_block,
            address=self.usdc_address,
            topics=[TRANSFER_ERC20_721, related_topics],
        ):
            yield self._parse_payment(log, direction_hint="payout")

    def _parse_payment(self, log: dict, direction_hint: str) -> Payment:
        from_addr = _topic_to_addr(log["topics"][1].hex())
        to_addr = _topic_to_addr(log["topics"][2].hex())
        amount_raw = int(log["data"], 16) if isinstance(log["data"], str) else int.from_bytes(log["data"], "big")
        # USDC has 6 decimals; treat 1 USDC = 1 USD
        amount_usd = Decimal(amount_raw) / Decimal(10**6)
        return Payment(
            tx_hash="0x" + log["transactionHash"].hex(),
            log_index=log["logIndex"],
            block_time=self._block_time(log["blockNumber"]),
            block_number=log["blockNumber"],
            from_addr=from_addr,
            to_addr=to_addr,
            token=self.usdc_address,
            amount_raw=amount_raw,
            amount_usd=amount_usd,
            direction=direction_hint,
        )

    # ------------------------------------------------------------------ #
    # Deployer flow — out of scope for v1, returns empty
    # ------------------------------------------------------------------ #
    def fetch_deployer_flow(
        self, address: str, max_depth: int = 2
    ) -> Iterable[FlowEdge]:
        # TODO: implement via trace_transaction / eth_getLogs union BFS
        return iter(())

    # ------------------------------------------------------------------ #
    # USDC price = 1 (stablecoin assumption)
    # ------------------------------------------------------------------ #
    def get_usd_price(self, token: str, block_time: datetime) -> Decimal:
        if token.lower() == self.usdc_address:
            return Decimal(1)
        # Other tokens: out of scope for v1, return 0 so caller can detect
        return Decimal(0)

    # ------------------------------------------------------------------ #
    # Parse a raw log dict (from Etherscan getLogs) — no RPC needed.
    # All log fields are hex strings.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _log_int(v) -> int:
        if isinstance(v, int):
            return v
        return int(v, 16)

    @staticmethod
    def _log_time(log: dict) -> datetime:
        ts = MonadAdapter._log_int(log["timeStamp"])
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    def parse_pack_opened(self, log: dict) -> PackOpen:
        return PackOpen(
            tx_hash=log["transactionHash"],
            log_index=self._log_int(log["logIndex"]),
            block_time=self._log_time(log),
            block_number=self._log_int(log["blockNumber"]),
            opener=_topic_to_addr(log["topics"][1]),
            pack_id=str(self._log_int(log["topics"][2])),
            price_raw=0,                    # filled later by JOIN payments
            price_token=self.usdc_address,
            price_usd=Decimal(0),
        )

    def parse_nft_transfer(self, log: dict) -> tuple[Transfer, Optional[Mint]]:
        topics = log["topics"]
        from_addr = _topic_to_addr(topics[1])
        to_addr = _topic_to_addr(topics[2])
        token_id = str(self._log_int(topics[3]))
        bn = self._log_int(log["blockNumber"])
        bt = self._log_time(log)
        contract = log["address"].lower()
        tr = Transfer(
            tx_hash=log["transactionHash"],
            log_index=self._log_int(log["logIndex"]),
            block_time=bt, block_number=bn,
            contract=contract, token_id=token_id,
            from_addr=from_addr, to_addr=to_addr,
            price_usd=None, marketplace=None,
        )
        mint = None
        if from_addr == "0x0000000000000000000000000000000000000000":
            mint = Mint(
                tx_hash=tr.tx_hash, log_index=tr.log_index,
                block_time=bt, block_number=bn,
                minter=to_addr, token_id=token_id,
                contract=contract, pack_open_tx=tr.tx_hash,
            )
        return tr, mint

    def parse_usdc_payment(self, log: dict, pack_opening_address: str) -> Payment:
        pack_opening_address = pack_opening_address.lower()
        from_addr = _topic_to_addr(log["topics"][1])
        to_addr = _topic_to_addr(log["topics"][2])
        data = log["data"]
        amount_raw = self._log_int(data)
        if to_addr == pack_opening_address:
            direction = "pack_pay"
        elif from_addr == pack_opening_address:
            direction = "payout"
        else:
            direction = "side"
        return Payment(
            tx_hash=log["transactionHash"],
            log_index=self._log_int(log["logIndex"]),
            block_time=self._log_time(log),
            block_number=self._log_int(log["blockNumber"]),
            from_addr=from_addr, to_addr=to_addr,
            token=self.usdc_address,
            amount_raw=amount_raw,
            amount_usd=Decimal(amount_raw) / Decimal(10**6),
            direction=direction,
        )

    # ------------------------------------------------------------------ #
    # Explorer-driven path: process a single tx hash, return all the
    # domain rows extracted from its logs. Much faster than scanning.
    # ------------------------------------------------------------------ #
    def process_tx(
        self,
        tx_hash: str,
        pack_opening_address: str,
        nft_contracts: list[str],
    ) -> tuple[list[PackOpen], list[Mint], list[Transfer], list[Payment]]:
        """Pull tx receipt, classify every log."""
        pack_opening_address = pack_opening_address.lower()
        nft_set = {a.lower() for a in nft_contracts}

        receipt = self.w3.eth.get_transaction_receipt(tx_hash)
        bn = receipt["blockNumber"]
        bt = self._block_time(bn)

        pack_opens: list[PackOpen] = []
        mints: list[Mint] = []
        transfers: list[Transfer] = []
        payments: list[Payment] = []

        for log in receipt["logs"]:
            addr = log["address"].lower()
            if not log["topics"]:
                continue
            t0 = "0x" + log["topics"][0].hex()

            # PackOpened event on pack_opening
            if addr == pack_opening_address and t0 in (PACK_OPENED_NEW, PACK_OPENED_OLD):
                pack_opens.append(PackOpen(
                    tx_hash="0x" + receipt["transactionHash"].hex(),
                    log_index=log["logIndex"],
                    block_time=bt,
                    block_number=bn,
                    opener=_topic_to_addr(log["topics"][1].hex()),
                    pack_id=str(int(log["topics"][2].hex(), 16)),
                    price_raw=0,
                    price_token=self.usdc_address,
                    price_usd=Decimal(0),
                ))
                continue

            # ERC-721 Transfer (3 indexed → 4 topics)
            if t0 == TRANSFER_ERC20_721 and len(log["topics"]) == 4:
                from_addr = _topic_to_addr(log["topics"][1].hex())
                to_addr = _topic_to_addr(log["topics"][2].hex())
                token_id = str(int(log["topics"][3].hex(), 16))
                tr = Transfer(
                    tx_hash="0x" + receipt["transactionHash"].hex(),
                    log_index=log["logIndex"],
                    block_time=bt,
                    block_number=bn,
                    contract=addr,
                    token_id=token_id,
                    from_addr=from_addr,
                    to_addr=to_addr,
                    price_usd=None,
                    marketplace=None,
                )
                if addr in nft_set:
                    transfers.append(tr)
                    if from_addr == "0x0000000000000000000000000000000000000000":
                        mints.append(Mint(
                            tx_hash=tr.tx_hash,
                            log_index=tr.log_index,
                            block_time=bt,
                            block_number=bn,
                            minter=to_addr,
                            token_id=token_id,
                            contract=addr,
                            pack_open_tx=tr.tx_hash,
                        ))
                continue

            # ERC-20 Transfer (2 indexed → 3 topics) — match our USDC only
            if t0 == TRANSFER_ERC20_721 and len(log["topics"]) == 3 and addr == self.usdc_address:
                from_addr = _topic_to_addr(log["topics"][1].hex())
                to_addr = _topic_to_addr(log["topics"][2].hex())
                amount_raw = int.from_bytes(log["data"], "big") if isinstance(log["data"], bytes) else int(log["data"], 16)
                # direction: pack_pay = USDC → pack_opening ; payout = pack_opening → x
                if to_addr == pack_opening_address:
                    direction = "pack_pay"
                elif from_addr == pack_opening_address:
                    direction = "payout"
                else:
                    direction = "side"
                payments.append(Payment(
                    tx_hash="0x" + receipt["transactionHash"].hex(),
                    log_index=log["logIndex"],
                    block_time=bt,
                    block_number=bn,
                    from_addr=from_addr,
                    to_addr=to_addr,
                    token=self.usdc_address,
                    amount_raw=amount_raw,
                    amount_usd=Decimal(amount_raw) / Decimal(10**6),
                    direction=direction,
                ))

        return pack_opens, mints, transfers, payments

    # ------------------------------------------------------------------ #
    # Misc helpers
    # ------------------------------------------------------------------ #
    def latest_block(self) -> int:
        return self.w3.eth.block_number
