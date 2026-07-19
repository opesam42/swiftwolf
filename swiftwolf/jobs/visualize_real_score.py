"""
jobs/visualize_real_score.py — generates the flagged-vs-normal scatter plot from
your ACTUAL trained model and REAL seeded transaction history, not simulated
data. Read-only: only calls score_one(), never learn_one(), so running this
never changes the model's state.

Usage:
    python jobs/visualize_real_score.py cust_gbenga_demo
"""
import sys
from pathlib import Path

import matplotlib.pyplot as plt
from sqlmodel import select

# Allow running this script directly (python jobs/visualize_real_score.py ...)
# from any working directory — same sys.path fix as jobs/seed_customer_profile.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core import models
from src.core.repository import ZScoreAnomalyDetector
from src.core.services import CustomerProfileService
from src.database import engine


def load_real_scores(customer_id: str, db_session):
    # 1. Load the real, already-trained population-level anomaly model
    model_row = db_session.exec(
        select(models.AnomalyModelState)
    ).first()
    if model_row is None:
        raise RuntimeError("No anomaly_model_state row found — has seeding been run yet?")

    detector = ZScoreAnomalyDetector.from_bytes(
        model_row.model_bytes,
        flag_threshold=model_row.flag_threshold,
    )

    # 2. Load the real customer profile (for category baselines). No
    # redis_client — this script only reads via get_or_create(), which never
    # touches Redis (only save()/get_cached_baseline() do), so the hot-path
    # cache has no role in a read-only, offline visualization script.
    service = CustomerProfileService(db_session)
    profile = service.get_or_create(customer_id)

    # 3. Load this customer's real transaction history, chronologically
    transactions = db_session.exec(
        select(models.Transaction)
        .where(models.Transaction.customer_id == customer_id)
        .order_by(models.Transaction.occurred_at)
    ).all()

    if not transactions:
        raise RuntimeError(f"No transactions found for {customer_id}")

    zscores, hours, scores = [], [], []
    for txn in transactions:
        baseline = profile.get_amount_baseline(txn.transaction_type)
        z = detector._compute_zscore(float(txn.amount), baseline)
        features = {
            "amount_zscore": z,
            "hour": txn.occurred_at.hour,
            "new_beneficiary": 0,  # not stored on the transaction row itself;
                                    # this is a known simplification for this
                                    # visualization script specifically
        }
        # READ-ONLY — score_one() never mutates the model. learn_one() is
        # deliberately never called here, so this script can be re-run safely
        # without affecting the real, already-seeded model.
        score = detector.model.score_one(features)

        zscores.append(z)
        hours.append(txn.occurred_at.hour)
        scores.append(score)

    return zscores, hours, scores, detector.flag_threshold


def plot(zscores, hours, scores, threshold, customer_id):
    flagged = [s >= threshold for s in scores]

    plt.figure(figsize=(9, 5))
    plt.scatter(
        [z for z, f in zip(zscores, flagged) if not f],
        [h for h, f in zip(hours, flagged) if not f],
        c="#4C72B0", alpha=0.5, s=20, label="Normal (learned from during seeding)",
    )
    plt.scatter(
        [z for z, f in zip(zscores, flagged) if f],
        [h for h, f in zip(hours, flagged) if f],
        c="#C44E52", alpha=0.9, s=40, marker="x", label="Flagged (self-filtered)",
    )
    plt.xlabel("Amount z-score (deviation from category baseline)")
    plt.ylabel("Hour of day")
    plt.title(f"Flagged vs normal — real transaction history ({customer_id})")
    plt.legend()
    plt.tight_layout()
    plt.savefig("real_anomaly_scatter.png", dpi=150)
    print(f"Saved real_anomaly_scatter.png — {sum(flagged)} flagged of {len(scores)} total")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python jobs/visualize_real_score.py <customer_id>")
        sys.exit(1)

    customer_id = sys.argv[1]

    from sqlmodel import Session

    # Reuses the app's own engine (sourced from settings.DATABASE_URL in
    # src/config.py) instead of hardcoding a separate connection string here —
    # same fix as jobs/seed_customer_profile.py.
    with Session(engine) as db:
        zscores, hours, scores, threshold = load_real_scores(customer_id, db)
        plot(zscores, hours, scores, threshold, customer_id)