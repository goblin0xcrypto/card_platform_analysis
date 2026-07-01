from adapters.base_adapter import (
    FlowEdge,
    Mint,
    Payment,
    PackOpen,
    PlatformAdapter,
    Transfer,
)

__all__ = ["FlowEdge", "Mint", "Payment", "PackOpen", "PlatformAdapter", "Transfer"]


def get_adapter(chain: str, **kwargs):
    """Factory: pick adapter by chain name from config.yaml."""
    chain = chain.lower()
    if chain == "monad":
        from adapters.monad_adapter import MonadAdapter
        return MonadAdapter(**kwargs)
    if chain == "megaeth":
        from adapters.mnstr_adapter import MnstrAdapter
        return MnstrAdapter(**kwargs)
    raise ValueError(f"unsupported chain: {chain}")
