# src/settlement/repository.py

from sqlmodel import Session, select, func
from src.settlement.models import Transaction

class TransactionRepository:
    """Owns all direct database access for Transaction rows. Services never
    touch SQLModel/SQLAlchemy directly — they only call methods here."""

    def __init__(self, db_session: Session):
        self.db = db_session

    def get(self, transaction_reference: str) -> Transaction | None:
        """Plain read, no lock. Returns None if the transaction was never recorded."""
        return self.db.exec(
            select(Transaction).where(Transaction.transaction_reference == transaction_reference)
        ).first()

    def get_for_update(self, transaction_reference: str) -> Transaction | None:
        """Fetches a transaction row and locks it for the duration of the
        current transaction, so two concurrent settle calls for the same
        reference can't both see is_settled=False."""
        return self.db.exec(
            select(Transaction)
            .where(Transaction.transaction_reference == transaction_reference)
            .with_for_update()
        ).first()

    def save(self, txn: Transaction) -> Transaction:
        """Commits the current transaction and refreshes the object
        from the database."""
        self.db.add(txn)
        self.db.commit()
        self.db.refresh(txn)
        return txn

    def rollback(self) -> None:
        """Discards all uncommitted changes and releases any row locks."""
        self.db.rollback()

    def get_count_for_customer_transaction(self, customer_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.is_settled == True)
            .where(Transaction.customer_id == customer_id)
        )
        count = self.db.exec(stmt).one()
        return count