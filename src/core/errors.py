# src/settlement/exceptions.py

class TransactionNotFoundError(Exception):
    """Raised when a settlement notification references a transaction
    that was never scored by SwiftWolf."""
    def __init__(self, transaction_reference: str):
        self.transaction_reference = transaction_reference
        super().__init__(f"No scored transaction found for reference: {transaction_reference}")


class SettlementError(Exception): 
    """Base domain exception for settlement operations.""" 
    pass

class InvalidSettlementData(SettlementError, ValueError): 
    """Raised when settlement payload violates domain invariants (e.g., missing bank\_code for transfer).""" 
    def __init__(self, customer_id: str, message: str): 
        self.customer_id = customer_id 
        self.message = message 
        super().__init__(f"Customer {customer_id}: {message}")