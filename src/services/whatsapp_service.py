"""
src/services/whatsapp_service.py
--------------------------------
Adapter Pattern for WhatsApp Business Platform (WABA) onboarding flow delivery.
Provides a deterministic, inspectable MockWhatsAppProvider for Phase 1/2 and
a MetaCloudWhatsAppProvider interface ready for live WABA integration.
"""
from abc import ABC, abstractmethod
from datetime import datetime, timezone
import hashlib
import hmac
import json
from typing import Any, Dict, List
import uuid

from src.core.config import settings
from src.core.logger import get_logger

logger = get_logger()


class WhatsAppProvider(ABC):
    """Abstract interface for WhatsApp Business onboarding flow and loyalty notification providers."""

    @abstractmethod
    def send_onboarding_flow(self, phone_number: str, flow_token: str) -> Dict[str, Any]:
        """
        Sends the onboarding WhatsApp Flow message to the specified normalized phone number.
        Returns a dictionary with delivery metadata (`status`, `message_id`, `flow_token`).
        """
        raise NotImplementedError

    @abstractmethod
    def send_post_purchase_summary(
        self,
        phone_number: str,
        customer_name: str,
        points_balance: int,
        transaction_id: str,
        quick_replies: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Sends the post-purchase WhatsApp summary with updated points balance and up to 3
        Quick Reply buttons for the highest unlocked discount tiers.
        """
        raise NotImplementedError

    @abstractmethod
    def send_redemption_otp(
        self,
        phone_number: str,
        otp_code: str,
        tier_code: str,
        tier_label: str,
    ) -> Dict[str, Any]:
        """
        Sends the generated 6-digit discount redemption OTP code to the customer in WhatsApp chat.
        """
        raise NotImplementedError


class MockWhatsAppProvider(WhatsAppProvider):
    """
    Deterministic Mock WABA provider for development, staging, and automated testing.
    Stores sent messages in an in-memory outbox and logs every dispatch.
    """

    def __init__(self) -> None:
        self.outbox: List[Dict[str, Any]] = []

    def send_onboarding_flow(self, phone_number: str, flow_token: str) -> Dict[str, Any]:
        message_id = f"wamid.mock.{uuid.uuid4()}"
        sent_at = datetime.now(timezone.utc).isoformat()

        outbox_entry = {
            "type": "onboarding_flow",
            "status": "sent",
            "message_id": message_id,
            "phone_number": phone_number,
            "flow_token": flow_token,
            "sent_at": sent_at,
        }
        self.outbox.append(outbox_entry)

        logger.log_execution(
            "whatsapp_service",
            "mock_send_onboarding_flow",
            "ok",
            phone_number=phone_number,
            message_id=message_id,
            flow_token=flow_token,
        )

        return {
            "status": "sent",
            "message_id": message_id,
            "flow_token": flow_token,
        }

    def send_post_purchase_summary(
        self,
        phone_number: str,
        customer_name: str,
        points_balance: int,
        transaction_id: str,
        quick_replies: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        message_id = f"wamid.mock.{uuid.uuid4()}"
        sent_at = datetime.now(timezone.utc).isoformat()

        outbox_entry = {
            "type": "post_purchase_summary",
            "status": "sent",
            "message_id": message_id,
            "phone_number": phone_number,
            "customer_name": customer_name,
            "points_balance": points_balance,
            "transaction_id": transaction_id,
            "quick_replies": list(quick_replies),
            "sent_at": sent_at,
        }
        self.outbox.append(outbox_entry)

        logger.log_execution(
            "whatsapp_service",
            "mock_send_post_purchase_summary",
            "ok",
            phone_number=phone_number,
            message_id=message_id,
            transaction_id=transaction_id,
            points_balance=points_balance,
            quick_replies_count=len(quick_replies),
        )

        return {
            "status": "sent",
            "message_id": message_id,
            "transaction_id": transaction_id,
            "points_balance": points_balance,
            "quick_replies": list(quick_replies),
        }

    def send_redemption_otp(
        self,
        phone_number: str,
        otp_code: str,
        tier_code: str,
        tier_label: str,
    ) -> Dict[str, Any]:
        message_id = f"wamid.mock.{uuid.uuid4()}"
        sent_at = datetime.now(timezone.utc).isoformat()

        outbox_entry = {
            "type": "redemption_otp",
            "status": "sent",
            "message_id": message_id,
            "phone_number": phone_number,
            "otp_code": otp_code,
            "tier_code": tier_code,
            "tier_label": tier_label,
            "sent_at": sent_at,
        }
        self.outbox.append(outbox_entry)

        logger.log_execution(
            "whatsapp_service",
            "mock_send_redemption_otp",
            "ok",
            phone_number=phone_number,
            message_id=message_id,
            tier_code=tier_code,
        )

        return {
            "status": "sent",
            "message_id": message_id,
            "otp_code": otp_code,
            "tier_code": tier_code,
        }

    def simulate_flow_completion(self, flow_token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Builds a signed webhook payload simulating a customer submitting the WhatsApp Flow.
        Computes the X-Hub-Signature-256 HMAC header using WHATSAPP_WEBHOOK_SECRET.
        """
        body_dict = dict(payload)
        body_dict["flow_token"] = flow_token

        # Serialize deterministically (converting date objects if present)
        raw_body = json.dumps(body_dict, default=str, separators=(",", ":")).encode("utf-8")
        digest = hmac.new(
            settings.WHATSAPP_WEBHOOK_SECRET.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        signature = f"sha256={digest}"

        logger.log_execution(
            "whatsapp_service",
            "mock_simulate_flow_completion",
            "ok",
            flow_token=flow_token,
        )

        return {
            "raw_body": raw_body,
            "signature": signature,
            "payload": body_dict,
        }

    def simulate_quick_reply_click(
        self, phone_number: str, discount_rule_code: str
    ) -> Dict[str, Any]:
        """
        Builds a signed webhook payload simulating a customer tapping a discount Quick Reply button.
        Computes the X-Hub-Signature-256 HMAC header using WHATSAPP_WEBHOOK_SECRET.
        """
        body_dict = {
            "event_type": "quick_reply",
            "phone_number": phone_number,
            "discount_rule_code": discount_rule_code,
        }
        raw_body = json.dumps(body_dict, default=str, separators=(",", ":")).encode("utf-8")
        digest = hmac.new(
            settings.WHATSAPP_WEBHOOK_SECRET.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        signature = f"sha256={digest}"

        logger.log_execution(
            "whatsapp_service",
            "mock_simulate_quick_reply_click",
            "ok",
            phone_number=phone_number,
            discount_rule_code=discount_rule_code,
        )

        return {
            "raw_body": raw_body,
            "signature": signature,
            "payload": body_dict,
        }

    def clear_outbox(self) -> None:
        """Clears the in-memory mock outbox (useful in test fixtures)."""
        self.outbox.clear()


class MetaCloudWhatsAppProvider(WhatsAppProvider):
    """
    Production Meta Cloud API WABA provider stub.
    Will be connected to the live Graph API endpoint once WABA credentials are provisioned.
    """

    def send_onboarding_flow(self, phone_number: str, flow_token: str) -> Dict[str, Any]:
        raise NotImplementedError(
            "MetaCloudWhatsAppProvider non è ancora configurato per l'invio live su Meta Graph API."
        )

    def send_post_purchase_summary(
        self,
        phone_number: str,
        customer_name: str,
        points_balance: int,
        transaction_id: str,
        quick_replies: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        raise NotImplementedError(
            "MetaCloudWhatsAppProvider non è ancora configurato per l'invio live su Meta Graph API."
        )

    def send_redemption_otp(
        self,
        phone_number: str,
        otp_code: str,
        tier_code: str,
        tier_label: str,
    ) -> Dict[str, Any]:
        raise NotImplementedError(
            "MetaCloudWhatsAppProvider non è ancora configurato per l'invio live su Meta Graph API."
        )


# Singleton instances so tests and endpoints inspect the same mock outbox
mock_whatsapp_provider = MockWhatsAppProvider()
meta_cloud_whatsapp_provider = MetaCloudWhatsAppProvider()


def get_whatsapp_provider() -> WhatsAppProvider:
    """Returns the configured WhatsAppProvider implementation."""
    provider_name = (settings.WHATSAPP_PROVIDER or "mock").strip().lower()
    if provider_name == "mock":
        return mock_whatsapp_provider
    if provider_name == "meta_cloud":
        return meta_cloud_whatsapp_provider
    raise ValueError(f"Provider WhatsApp non supportato: '{settings.WHATSAPP_PROVIDER}'")
