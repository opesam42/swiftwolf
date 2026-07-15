import tempfile
import unittest
from pathlib import Path

from sqlmodel import SQLModel, create_engine

from src.core.models import (
    AnomalyModelState,
    Base,
    Customer,
    RiskEvent,
    Transaction,
)
from src.database import build_engine


class ModelSchemaTests(unittest.TestCase):
    def test_model_metadata(self):
        self.assertEqual(Customer.__tablename__, "customers")
        self.assertEqual(Transaction.__tablename__, "transactions")
        self.assertEqual(RiskEvent.__tablename__, "risk_events")
        self.assertEqual(AnomalyModelState.__tablename__, "anomaly_model_state")

    def test_expected_columns_exist(self):
        self.assertIn("customer_id", Customer.__table__.columns.keys())
        self.assertIn("profile_json", Customer.__table__.columns.keys())
        self.assertIn("transaction_reference", Transaction.__table__.columns.keys())
        self.assertIn("reasons", RiskEvent.__table__.columns.keys())
        self.assertIn("flag_threshold", AnomalyModelState.__table__.columns.keys())

    def test_base_is_declared(self):
        self.assertTrue(hasattr(Base, "metadata"))

    def test_sqlite_metadata_create_all(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "swiftwolf.sqlite"
            engine = build_engine(f"sqlite:///{db_path}")
            SQLModel.metadata.create_all(engine)
            self.assertTrue(db_path.exists())


if __name__ == "__main__":
    unittest.main()
