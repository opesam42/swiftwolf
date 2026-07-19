"""
jobs/generate_baseline_chart.py — server-side chart generation, no Praise
dependency at all. Run this yourself, show the PNG during narration.

Usage:
    python jobs/generate_baseline_chart.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib.pyplot as plt

# Allow running this script directly (python jobs/generate_baseline_chart.py)
# from any working directory — same sys.path fix as the other jobs/ scripts.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def plot_customer_baseline(customer_id: str, category: str, baseline: dict, points: list[dict], output_path: str = "baseline_chart.png"):
    """
    baseline: {"avg_amount": float, "std_amount": float, "source": str}
    points: [{"days_ago": int, "amount": float}, ...]  -- real transaction history

    Flagging logic matches Layer 1's own amount_deviation rule (>2 std devs)
    so the chart visually explains the same threshold the scoring engine
    actually uses -- not an arbitrary, disconnected visual choice.
    """
    avg = baseline["avg_amount"]
    std = baseline["std_amount"]
    threshold = avg + 2 * std

    normal = [p for p in points if p["amount"] <= threshold]
    flagged = [p for p in points if p["amount"] > threshold]

    fig, ax = plt.subplots(figsize=(10, 5.5))

    # Baseline average line -- dashed, muted, matches the "recessive axis"
    # design principle: it's context, not the star of the chart
    x_range = [-max(p["days_ago"] for p in points) - 3, 3]
    ax.plot(x_range, [avg, avg], linestyle="--", color="#898781", linewidth=1, label=f"Baseline average (NGN{avg:,.0f})")

    if normal:
        ax.scatter([-p["days_ago"] for p in normal], [p["amount"] for p in normal],
                    color="#2a78d6", s=60, label="Normal transfer", zorder=3)
    if flagged:
        ax.scatter([-p["days_ago"] for p in flagged], [p["amount"] for p in flagged],
                    color="#e34948", s=140, marker="^", label="Flagged transfer", zorder=4)

    ax.set_xlabel("Days ago")
    ax.set_ylabel(f"Amount (NGN) -- {category}")
    ax.set_title(f"{customer_id} -- {category} transfer history vs. baseline", fontsize=12)
    ax.legend(loc="upper left", fontsize=9, frameon=False)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", color="#e1e0d9", linewidth=0.8, zorder=0)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    print(f"Saved {output_path} -- {len(normal)} normal, {len(flagged)} flagged")
    return output_path


if __name__ == "__main__":
    from sqlmodel import Session, select

    from src.core.models import Transaction
    from src.core.services import CustomerProfileService
    from src.database import engine

    customer_id = "cust_demo"
    category = "transfer"

    with Session(engine) as db:
        profile = CustomerProfileService(db).get_or_create(customer_id)
        baseline = profile.get_amount_baseline(category)
        print(f"Baseline: {baseline}")

        now = datetime.now(timezone.utc)
        txns = db.exec(
            select(Transaction).where(
                Transaction.customer_id == customer_id,
                Transaction.transaction_type == category,
                Transaction.direction == "debit",
            )
        ).all()
        points = [
            {"days_ago": (now - t.occurred_at).days, "amount": float(t.amount)}
            for t in txns
        ]

    plot_customer_baseline(customer_id, category, baseline, points)
