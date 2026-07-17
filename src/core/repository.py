from river import anomaly, compose, preprocessing, stats
from collections import defaultdict
import json
import pickle
from math import atan2, cos, radians, sin, sqrt

class CustomerProfile:
    """One instance per customer. Persisted as JSON, not pickled River objects directly,
    so it survives across process restarts and is human-readable for debugging."""

    def __init__(self):
        # Global fallback ONLY — used when a specific category has too few
        # observations yet (e.g. a customer's first-ever betting transaction).
        # NOT the primary baseline — see get_amount_baseline() below.
        self.global_mean = stats.Mean()
        self.global_var = stats.Var()
        self.hour_counts = {h: 0 for h in range(24)}
        # Composite (account, bank_code) key — a raw account number alone isn't
        # guaranteed globally unique, only unique within a bank, so combining
        # avoids wrongly treating the same account number at two different banks
        # as one beneficiary.
        self.known_beneficiaries = set()
        # Separate bank-level tracking — answers a different question than the
        # composite key above: "has this customer used this bank before at all,"
        # not "have they used this exact account before." A new account at a
        # familiar bank is a smaller signal than a new account at an unfamiliar one.
        self.known_bank_codes = set()
        # "account:bank_code" -> beneficiary_name, latest-seen wins. Not used
        # for any scoring signal (that's still purely the composite key in
        # known_beneficiaries) — this exists so a resolved name (from Praise's
        # account lookup, or the CSV extraction) survives on the profile for
        # future Layer 3 merchant-matching, instead of being discarded.
        self.beneficiary_names: dict[str, str] = {}
        self.location_counts = defaultdict(int)
        self.transaction_count = 0
        # Simpler, single-model stand-in for Part 2C's champion/challenger
        # rebase (not built this pass) — a failed step-up verification is
        # enough evidence to scrutinize this customer's future transactions
        # harder, without needing a second model. Never resets back to
        # "normal" once elevated — a named, stated scope gap, not an oversight.
        self.risk_tier = "normal"

        # THIS is what Layer 1's amount-deviation check actually uses now —
        # per-category mean/variance, not a blended global figure.
        self.category_stats = defaultdict(lambda: {"mean": stats.Mean(), "var": stats.Var()})

    def update(self, amount: float, hour: int, beneficiary_account: str,
               beneficiary_bank_code: str, transaction_type: str, geolocation: dict | None = None,
               beneficiary_name: str | None = None):
        self.global_mean.update(amount)
        self.global_var.update(amount)
        self.hour_counts[hour] += 1
        beneficiary_key = f"{beneficiary_account}:{beneficiary_bank_code}"
        self.known_beneficiaries.add(beneficiary_key)
        self.known_bank_codes.add(beneficiary_bank_code)
        if beneficiary_name:
            self.beneficiary_names[beneficiary_key] = beneficiary_name
        self.transaction_count += 1
        self.category_stats[transaction_type]["mean"].update(amount)
        self.category_stats[transaction_type]["var"].update(amount)

        if geolocation:
            cell = self.location_grid_cell(geolocation["lat"], geolocation["lng"])
            self.location_counts[cell] += 1

    @staticmethod
    def location_grid_cell(lat: float, lng: float, precision: int = 1) -> tuple[float, float]:
        return (round(lat, precision), round(lng, precision))

    def elevate_risk(self) -> None:
        """Called on a failed step-up verification (liveness/security
        question/OTP) at settle time — NOT on 'abandoned', which just means
        the customer walked away, not that an identity claim was rejected."""
        self.risk_tier = "elevated"

    def get_amount_baseline(self, transaction_type: str, min_category_samples: int = 5) -> dict:
        """Layer 1's 'amount deviates from average' check calls THIS, not a
        blended global figure — category-specific baseline when there's enough
        data for it to be meaningful, global average only as a cold-start
        fallback for a category this customer barely has history in yet."""
        cat = self.category_stats.get(transaction_type)
        # check if the no of sample is greater than the min_category samples
        if cat and cat["mean"].n >= min_category_samples:
            avg, var, source = cat["mean"].get(), cat["var"].get(), "category"
        else:
            avg, var, source = self.global_mean.get(), self.global_var.get(), "global_fallback"
        std = 0
        if var > 0:
            std = var ** 0.5
        return {"avg_amount": round(avg, 2), "std_amount": round(std, 2), "source": source}

    def to_baseline_dict(self) -> dict:
        """This is what gets cached in Redis and read by Layer 1."""
        total_hour_txns = sum(self.hour_counts.values()) or 1 # total noumber of transactins the customer has made
        typical_hours = []
        for hour, count in self.hour_counts.items():
            proportion = count / total_hour_txns

            if proportion > 0.15:
                typical_hours.append(hour) # keeps hours covering >15% of this customer's activity
        
        category_baselines = {}
        for transaction_type in self.category_stats:
            category_baselines[transaction_type] = self.get_amount_baseline(transaction_type)

        total_location_txns = sum(self.location_counts.values()) or 1
        known_location_cells = [
            [lat, lng] for (lat, lng), count in self.location_counts.items() if count / total_location_txns > 0.15
        ]
        
        return {
            "category_baselines": category_baselines,
            "typical_hours": typical_hours,
            "known_beneficiaries": list(self.known_beneficiaries),
            "known_bank_codes": list(self.known_bank_codes),
            "known_location_cells": known_location_cells,
            "beneficiary_names": dict(self.beneficiary_names),
            "transaction_count": self.transaction_count,
            "is_cold_start": self.transaction_count < 10,  # Layer 1 should be more lenient if true
            "risk_tier": self.risk_tier,
        }

    def to_json(self) -> str:
        return json.dumps({
            "global_mean": self.global_mean.get(),
            "global_var": self.global_var.get(),
            "global_n": self.global_mean.n,
            "category_stats": {
                t: {"mean": s["mean"].get(), "var": s["var"].get(), "n": s["mean"].n}
                for t, s in self.category_stats.items()
            },
            "hour_counts": self.hour_counts,
            "known_beneficiaries": list(self.known_beneficiaries),
            "known_bank_codes": list(self.known_bank_codes),
            "location_counts": {"{}:{}".format(lat, lng): count for (lat, lng), count in self.location_counts.items()},
            "beneficiary_names": dict(self.beneficiary_names),
            "transaction_count": self.transaction_count,
            "risk_tier": self.risk_tier,
        })

    @classmethod
    def from_json(cls, raw: str) -> "CustomerProfile":
        """Rehydrate from Postgres on load. Note: River's Mean/Var objects don't
        natively support restoring internal state, so we reconstruct manually."""
        data = json.loads(raw)
        profile = cls()
        profile.global_mean._mean = data["global_mean"]
        profile.global_mean._n = data["global_n"]
        for t, s in data["category_stats"].items():
            profile.category_stats[t]["mean"]._mean = s["mean"]
            profile.category_stats[t]["mean"]._n = s["n"]
            profile.category_stats[t]["var"]._mean = s["var"]  # approximate restore, see note below
        profile.hour_counts = {int(k): v for k, v in data["hour_counts"].items()}
        profile.known_beneficiaries = set(data["known_beneficiaries"])
        profile.known_bank_codes = set(data["known_bank_codes"])
        profile.location_counts = defaultdict(int)
        for raw_cell, count in data.get("location_counts", {}).items():
            lat_str, lng_str = raw_cell.split(":")
            profile.location_counts[(float(lat_str), float(lng_str))] = count
        profile.transaction_count = data["transaction_count"]
        profile.risk_tier = data.get("risk_tier", "normal")  # default for profiles seeded before this field existed
        profile.beneficiary_names = dict(data.get("beneficiary_names", {}))  # same default reasoning
        return profile


class ZScoreAnomalyDetector:
    """Layer 2's population-level anomaly detector (Part 2D, Approach 2 — pre-normalized
    deviation score). Feeds the model a z-score computed from CustomerProfile.get_amount_baseline()
    instead of a raw amount, so the amount-vs-category relationship is computed upstream by
    CustomerProfile rather than re-learned by the model's own tree splits."""

    def __init__(self, flag_threshold: float = 0.6, window_size: int = 250):
        self.model = compose.Pipeline(
            preprocessing.MinMaxScaler(),
            anomaly.HalfSpaceTrees(n_trees=25, height=8, window_size=window_size, seed=42),
        )
        self.flag_threshold = flag_threshold
        self.learned_count = 0
        self.skipped_count = 0

    def process(self, transaction: dict, profile: CustomerProfile) -> dict:
        # CRITICAL ORDERING: baseline must be read BEFORE this transaction is fed
        # into profile.update(). Getting this backwards lets the transaction dilute
        # its own baseline, understating how anomalous it actually was — always call
        # this BEFORE profile.update() for the same transaction, never after.
        features, baseline = self._build_features(transaction, profile)
        score = self.model.score_one(features)

        # Self-filtering guard (Part 2D): skip learn_one() for anything already
        # scoring as anomalous, so a single outlier can't poison the model's own
        # sense of "normal" or stretch the scaler's range. This guard protects
        # against LIVE fraud slipping in disguised as normal — it has no role
        # during seed_one() below, where the source data is already trusted.
        if score < self.flag_threshold:
            self.model.learn_one(features)
            self.learned_count += 1
            learned = True
        else:
            self.skipped_count += 1
            learned = False

        return {
            "score": score,
            "flagged": score >= self.flag_threshold,
            "learned_from": learned,
            "zscore": round(features["amount_zscore"], 3),
            "baseline_source": baseline["source"],  # "category" or "global_fallback"
        }

    def seed_one(self, transaction: dict, profile: CustomerProfile) -> float:
        """Used only for trusted historical seeding — NOT live scoring. Learns
        from every row unconditionally, skipping process()'s self-filtering
        guard entirely: that guard exists to stop LIVE fraud from disguising
        itself as normal, which doesn't apply here since seed data is already
        presumed legitimate. Same CRITICAL ORDERING requirement as process():
        call this before profile.update() for the same transaction.

        Returns the raw score so callers can collect a distribution across the
        whole seed run and calibrate flag_threshold from it afterward, via
        calibrate_from_scores() — seeding has no threshold to self-filter
        against yet in the first place."""
        features, _ = self._build_features(transaction, profile)
        score = self.model.score_one(features)
        self.model.learn_one(features)
        self.learned_count += 1
        return score

    def calibrate_from_scores(self, scores: list[float], percentile: float = 95) -> float:
        """Sets flag_threshold from a distribution of scores this SAME detector
        just produced (via seed_one() over real historical data), then returns
        the computed value. From this point on, process() applies the
        self-filtering guard using this threshold against genuinely new
        incoming transactions — seeding never needed one."""
        if scores:
            sorted_scores = sorted(scores)
            index = int(len(sorted_scores) * percentile / 100)
            self.flag_threshold = sorted_scores[min(index, len(sorted_scores) - 1)]
        return self.flag_threshold

    def _build_features(self, transaction: dict, profile: CustomerProfile) -> tuple[dict, dict]:
        """Shared by process() and seed_one() so both compute the exact same
        feature shape from the exact same baseline read."""
        baseline = profile.get_amount_baseline(transaction["transaction_type"])
        z_score = self._compute_zscore(transaction["amount"], baseline)
        features = {
            "amount_zscore": z_score,
            "hour": transaction["timestamp"].hour,
            "new_beneficiary": 1 if transaction.get("new_beneficiary") else 0,
        }
        return features, baseline

    def _compute_zscore(self, amount: float, baseline: dict) -> float:
        std = baseline["std_amount"]
        if std == 0:
            return 0.0  # cold-start category with no variance established yet
        return (amount - baseline["avg_amount"]) / std

    def to_bytes(self) -> bytes:
        """Pickled for AnomalyModelState.model_bytes. Unlike CustomerProfile (JSON,
        manually reconstructed), this is a single population-level model whose tree
        structure is too complex to rebuild by hand, so pickling is the right tradeoff."""
        return pickle.dumps(self.model)

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        flag_threshold: float = 0.6,
        window_size: int = 250,
        learned_count: int = 0,
        skipped_count: int = 0,
    ) -> "ZScoreAnomalyDetector":
        """Rehydrate from AnomalyModelState. flag_threshold/learned_count/skipped_count
        are stored as their own columns rather than inside the pickle blob."""
        instance = cls(flag_threshold=flag_threshold, window_size=window_size)
        instance.model = pickle.loads(data)
        instance.learned_count = learned_count
        instance.skipped_count = skipped_count
        return instance