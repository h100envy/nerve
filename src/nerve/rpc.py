from __future__ import annotations

import time
from typing import Any

from web3.providers.rpc import HTTPProvider


class RetryingHTTPProvider(HTTPProvider):
    """Retry transport failures for reads while never retrying a broadcast."""

    def make_request(self, method: str, params: Any) -> Any:
        attempts = 1 if method == "eth_sendRawTransaction" else 3
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return super().make_request(method, params)
            except Exception as exc:
                last = exc
                if attempt + 1 < attempts:
                    time.sleep(0.15 * (2**attempt))
        if last is not None:
            raise last
        raise RuntimeError(f"RPC request failed: {method}")
