"""
jobs/visualize_hourly_histogram.py — plots a customer's real hour_counts
against the 15% typical_hours threshold, straight from CustomerProfile.
Read-only: only calls get_or_create(), never .update(), so running this
never changes the profile's state.

Usage:
    python jobs/visualize_hourly_histogram.py cust_demo
"""
import sys
from pathlib import Path

import matplotlib.pyplot as plt

# Allow running this script directly (python jobs/visualize_hourly_histogram.py ...)
# from any working directory — same sys.path fix as the other jobs/ scripts.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.services import CustomerProfileService
from src.database import engine


def load_hourly_data(customer_id: str, db_session):
    # No redis_client — this script only reads via get_or_create(), which
    # never touches Redis (only save()/get_cached_baseline() do), same
    # reasoning as jobs/visualize_real_score.py.
    service = CustomerProfileService(db_session)
    profile = service.get_or_create(customer_id)

    hour_counts = profile.hour_counts
    total = sum(hour_counts.values())

    # Same 0.15 proportion CustomerProfile.to_baseline_dict() itself uses for
    # typical_hours — computed here, not hardcoded, so this always matches
    # whatever the real scoring logic actually uses.
    threshold_count = total * 0.15
    typical_hours = [hour for hour, count in hour_counts.items() if total and count / total > 0.15]

    return hour_counts, threshold_count, typical_hours


def plot(hour_counts: dict, threshold_count: float, typical_hours: list, customer_id: str):
    hours = list(range(24))
    counts = [hour_counts.get(h, 0) for h in hours]
    colors = ["#C44E52" if c >= threshold_count and threshold_count > 0 else "#4C72B0" for c in counts]

    peak_hour = max(hours, key=lambda h: hour_counts.get(h, 0))
    peak_count = hour_counts.get(peak_hour, 0)

    if typical_hours:
        status = f"{len(typical_hours)} hour(s) qualify as typical"
    else:
        status = "no hour crosses the typical-hour threshold"

    fig, ax = plt.subplots(figsize=(10, 5.5))
    ax.bar(hours, counts, color=colors, width=0.7, zorder=3)
    ax.axhline(
        threshold_count, linestyle="--", color="#898781", linewidth=1,
        label=f"15% threshold ({threshold_count:.0f} transactions)",
    )
    ax.set_xlabel("Hour of day")
    ax.set_ylabel("Transaction count")
    ax.set_xticks(hours)
    ax.set_title(f"Hourly activity — {customer_id} ({status})", fontsize=12)
    ax.legend(loc="upper right", fontsize=9, frameon=False)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", color="#e1e0d9", linewidth=0.8, zorder=0)

    plt.tight_layout()
    plt.savefig("hourly_histogram.png", dpi=150)
    print(
        f"Saved hourly_histogram.png — peak hour is {peak_hour}:00 with {peak_count} transactions, "
        f"threshold is {threshold_count:.0f}, typical_hours: {typical_hours}"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python jobs/visualize_hourly_histogram.py <customer_id>")
        sys.exit(1)

    customer_id = sys.argv[1]

    from sqlmodel import Session

    # Reuses the app's own engine (sourced from settings.DATABASE_URL in
    # src/config.py) instead of hardcoding a separate connection string here —
    # same fix as the other jobs/ scripts.
    with Session(engine) as db:
        hour_counts, threshold_count, typical_hours = load_hourly_data(customer_id, db)
        plot(hour_counts, threshold_count, typical_hours, customer_id)
