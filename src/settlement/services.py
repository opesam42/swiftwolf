import logging

from fastapi import BackgroundTasks
from sqlmodel import Session

from src.profile.services import CustomerProfileService
from src.settlement.models import TransactionStatus
from src.settlement.repository import TransactionRepository
from src.core.errors import TransactionNotFoundError, InvalidSettlementData

logger = logging.getLogger(__name__)

class SettleService:
    """Orchestrates settlement finalization and triggers asynchronous ML state updates."""

    def __init__(self, db_session: Session, redis_client=None, repository: TransactionRepository | None = None):
        self.repo = repository or TransactionRepository(db_session)
        self.profile_service = CustomerProfileService(db_session, redis_client)

    def settle(self, payload: dict) -> dict:
        txn_ref = payload["transaction_reference"]

        # Lock the row so a retried/duplicate settle call for the same reference
        # waits here until this one commits, then sees is_settled=True below.
        txn = self.repo.get_for_update(txn_ref)

        if txn is None:
            # This transaction was never scored by us — reject, don't fabricate.
            self.repo.rollback()
            raise TransactionNotFoundError(txn_ref)

        # Guard against double-settlement
        if txn.is_settled:
            self.repo.rollback()  # release the lock
            return {
                "transaction_reference": txn_ref,
                "status": txn.status,
                "is_settled": True,
                "message": "Transaction was previously settled.",
            }

        # Money never moved — record the failure but leave the baseline untouched
        if payload.get("status") == "FAILED":
            txn.status = TransactionStatus.FAILED.value
            txn.settled_at = payload["settled_at"]
            self.repo.save(txn)
            return {
                "transaction_reference": txn_ref,
                "status": txn.status,
                "is_settled": False,
                "message": "Transaction marked as failed settlement.",
            }

        # UPDATE CUSTOMER RISK PROFILE
        txn.is_settled = True
        txn.settled_at = payload["settled_at"]

        try:
            self.profile_service.update_baseline_from_settled_transaction(
                customer_id = txn.customer_id,
                amount = txn.amount,
                destination_key = txn.destination_key,
                transaction_type = txn.transaction_type,
                occurred_at = txn.occurred_at,
                geolocation_lat = txn.geolocation_lat or None,
                geolocation_lng = txn.geolocation_lng or None,
                bank_code = txn.provider or None,
            )
        except InvalidSettlementData as e:
            logger.warning(f"Invalid settlement data for {txn_ref}: {e}")
            self.repo.rollback()
            raise
        except Exception:
            self.repo.rollback()
            raise

        return {
            "transaction_reference": txn_ref,
            "status": txn.status,
            "is_settled": True,
            "message": "Transaction successfully settled."
        }
