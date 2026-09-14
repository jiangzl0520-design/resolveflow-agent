from typing import Any

from evaluation.harness.models import GoldenCase


class KeywordBaseline:
    """Synthetic baseline that trusts user text and ignores failure states."""

    def execute(self, case: GoldenCase, *, seed: int) -> dict[str, Any]:
        del seed
        payload = case.input
        user_text = str(payload.get("user_text", "")).lower()
        untrusted = str(payload.get("untrusted_instruction", "")).lower()
        if "refund" in user_text or "refund" in untrusted or "退款" in user_text:
            return {"disposition": "unsafe_refund", "next_action": "refund_execute"}
        if payload.get("logistics_status") == "in_transit":
            return {"disposition": "tracking", "next_action": "monitor_shipment"}
        return {"disposition": "resolved", "next_action": "close_ticket"}


class TrustedFactCandidate:
    """Synthetic candidate that routes only from trusted structured facts."""

    def execute(self, case: GoldenCase, *, seed: int) -> dict[str, Any]:
        del seed
        payload = case.input
        dependency = payload.get("dependency_status", "ok")
        if dependency in {"timeout", "unavailable"}:
            return {"disposition": "deferred", "next_action": "retry_later"}
        if dependency == "malformed":
            return {"disposition": "manual", "next_action": "human_review"}
        if not payload.get("paid", False) or payload.get("order_status") in {
            "cancelled", "refunded"
        }:
            return {
                "disposition": "not_eligible",
                "next_action": "stop_refund_investigation",
            }
        logistics = payload.get("logistics_status")
        disputed = payload.get("customer_disputes_delivery", False)
        proof = payload.get("proof_available")
        if logistics == "in_transit":
            return {"disposition": "tracking", "next_action": "monitor_shipment"}
        if logistics == "delivered" and disputed and proof is None:
            return {"disposition": "investigating", "next_action": "query_delivery_proof"}
        if logistics == "delivered" and disputed and proof is False:
            return {"disposition": "manual", "next_action": "collect_manual_evidence"}
        if logistics == "delivered" and disputed and proof is True:
            return {"disposition": "manual", "next_action": "human_review"}
        return {"disposition": "resolved", "next_action": "close_ticket"}
