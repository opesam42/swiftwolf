from datetime import datetime, timezone

from fastapi import BackgroundTasks
from sqlmodel import Session, select

from src.profile.services import CustomerProfileService
from src.settlement.models import Transaction


class SettleService:
    """Orchestrates settlement finalization and triggers asynchronous ML state updates."""

    def __init__(self, db_session: Session, redis_client=None):
        self.db = db_session
        self.redis = redis_client
        self.profile_service = CustomerProfileService(db_session, redis_client)

    def settle(self, payload: dict, background_tasks: BackgroundTasks) -> dict:
        txn_ref = payload["transaction_reference"]

        # 1. Fetch or initialize the transaction
        txn = self.db.exec(
            select(Transaction).where(Transaction.transaction_reference == txn_ref)
        ).first()

        if txn is None:
            txn = Transaction(
                transaction_reference=txn_ref,
                customer_id=payload["customer_id"],
                amount=payload["amount"],
                beneficiary_account=payload["beneficiary_account"],
                beneficiary_bank_code=payload["beneficiary_bank_code"],
                beneficiary_name=payload.get("beneficiary_name"),
                transaction_type=payload["transaction_type"],
                medium=payload["medium"],
                occurred_at=payload.get("settled_at", datetime.now(timezone.utc)),
            )
            self.db.add(txn)

        # 2. Guard against double-settlement (ML State Protection)
        if txn.is_settled:
            return {
                "transaction_reference": txn_ref,
                "status": "ALREADY_SETTLED",
                "is_settled": True,
                "message": "Transaction was previously settled. Background ML updates skipped.",
            }

        # 3. Mark settled in Postgres
        if payload.get("status") == "SUCCESS":
            txn.is_settled = True
            txn.settled_at = payload.get("settled_at", datetime.now(timezone.utc))
            self.db.add(txn)
            self.db.commit()
            self.db.refresh(txn)

            # 4. Schedule async behavioral ML state updates (River ML + Baselines)
            background_tasks.add_task(
                self._run_async_updates,
                customer_id=payload["customer_id"],
                transaction_data=payload,
            )

            return {
                "transaction_reference": txn_ref,
                "status": "SETTLED",
                "is_settled": True,
                "message": "Transaction successfully settled. Background profile & ML updates queued.",
            }

        # Handle failed settlements
        self.db.commit()
        return {
            "transaction_reference": txn_ref,
            "status": "FAILED_SETTLEMENT",
            "is_settled": False,
            "message": "Transaction marked as failed settlement.",
        }

    def _run_async_updates(self, customer_id: str, transaction_data: dict) -> None:
        """Executed asynchronously on a background worker thread."""
        try:
            # Update customer statistical baseline in Postgres/Redis
            self.profile_service.update_from_settled_transaction(customer_id, transaction_data)
        except Exception as e:
            # In production, log to error tracking (e.g., Sentry / Datadog)
            print(f"[Settlement Worker Error] Failed async ML update for {customer_id}: {e}")