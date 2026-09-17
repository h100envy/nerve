from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from ..models import AnalystVerdict, Impulse, NodeType, Verdict
from ..protocol import NerveNode


class AnalystNode(NerveNode):
    node_type = NodeType.ANALYST
    owns = "evaluates normalized pool metrics and returns a typed thesis"
    boundary = "never sees private keys, tools, gas limits or position size"

    SYSTEM = (
        "You are ANALYST in NERVE, a skeptical memecoin desk. "
        "Use only supplied metrics. Reject missing, stale or contradictory facts. "
        "Return PASS only when the setup has a plausible catalyst and survivable liquidity."
    )

    def __init__(self, api_key: str = "", model: str = "gpt-6-astra", brain: Any = None) -> None:
        self.model = model
        self._brain = brain
        self._client: Any = None
        if api_key and brain is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=api_key)

    def process(self, impulse: Impulse) -> Impulse:
        if impulse.verdict is Verdict.REJECT:
            return impulse
        if self._client is None and self._brain is None:
            verdict = AnalystVerdict(verdict="PASS", reason="paper rule", thesis="paper candidate", confidence=Decimal("0.72"))
        else:
            verdict = self._call_model(impulse)
        impulse.thesis = verdict.thesis[:600]
        impulse.confidence = verdict.confidence
        # SENTINEL's bytecode flags are facts; the model may add flags, never erase them.
        impulse.risk_flags = list(dict.fromkeys([*impulse.risk_flags, *verdict.risk_flags]))
        impulse.score = max(0, min(100, impulse.score + int(verdict.confidence * 15)))
        if verdict.verdict.upper() != "PASS":
            return impulse.advance(NodeType.ANALYST, Verdict.REJECT, verdict.reason)
        return impulse.advance(NodeType.ANALYST, Verdict.PASS, verdict.thesis)

    def _call_model(self, impulse: Impulse) -> AnalystVerdict:
        facts = {"chain": str(impulse.chain), "token": impulse.token, "pool": impulse.pool,
                 "liquidity_usd": str(impulse.liquidity_usd), "volume_1h_usd": str(impulse.volume_1h_usd),
                 "volume_24h_usd": str(impulse.volume_24h_usd), "top10_pct": str(impulse.top10_pct),
                 "slippage_bps": impulse.slippage_bps, "pool_age_hours": str(impulse.pool_age_hours),
                 "mint_renounced": impulse.mint_renounced, "lp_locked": impulse.lp_locked,
                 "buy_tax_pct": None if impulse.buy_tax_pct is None else str(impulse.buy_tax_pct),
                 "sell_tax_pct": None if impulse.sell_tax_pct is None else str(impulse.sell_tax_pct),
                 "buy_route": impulse.buy_route, "sell_route": impulse.sell_route,
                 "risk_flags": list(impulse.risk_flags)}
        if self._brain is not None:
            result = self._brain(facts)
            return AnalystVerdict.model_validate(result)
        schema = {"type": "object", "additionalProperties": False, "properties": {
            "verdict": {"type": "string", "enum": ["PASS", "REJECT"]},
            "reason": {"type": "string"}, "thesis": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "risk_flags": {"type": "array", "items": {"type": "string"}},
        }, "required": ["verdict", "reason", "thesis", "confidence", "risk_flags"]}
        response = self._client.responses.create(
            model=self.model, instructions=self.SYSTEM, input=json.dumps(facts, sort_keys=True),
            reasoning={"effort": "low"}, max_output_tokens=700, store=False,
            text={"format": {"type": "json_schema", "name": "nerve_analyst", "strict": True, "schema": schema}},
        )
        return AnalystVerdict.model_validate_json(response.output_text)
