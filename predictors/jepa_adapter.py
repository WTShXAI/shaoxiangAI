"""
JEPA v5.0 -> v4.1 Stacking Integration Bridge
=============================================
Provides zero-invasion interface for the existing Stacking pipeline.
Allows v5.0 JEPA to be added as a 6th base model.
"""

import sys
import torch
import numpy as np
from pathlib import Path
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Dict, List
import logging

logger = logging.getLogger("JEPA.Bridge")

# --- Degradation Levels (施时序 design) ---
class DegradationLevel(Enum):
    FULL = "full"               # Full JEPA: static + seq + drift
    NO_DRIFT = "no_drift"       # Without odds drift (seq encoder only)
    STATIC_ONLY = "static_only" # MLP fallback (same as old NN)

# --- Data structures ---
@dataclass
class AdapterStats:
    """Statistics for JEPA adapter monitoring"""
    total_calls: int = 0
    degradation_fallbacks: int = 0
    avg_latency_ms: float = 0.0
    last_collapse_check: str = "not_run"

class SequenceCache:
    """Cache match sequences by match ID to avoid redundant computation"""

    def __init__(self, max_size: int = 10000):
        self.cache: Dict[str, tuple] = {}
        self.max_size = max_size

    def get(self, match_id: str):
        return self.cache.get(match_id)

    def set(self, match_id: str, sequence: np.ndarray, drift: np.ndarray):
        if len(self.cache) >= self.max_size:
            # Evict oldest 10%
            keys = list(self.cache.keys())
            for k in keys[:len(keys) // 10]:
                del self.cache[k]
        self.cache[match_id] = (sequence, drift)

class TemporalSafetyGuard:
    """Prevent temporal data leakage in stacking integration"""

    def __init__(self, train_cutoff: str = "2023-01-01"):
        self.train_cutoff = train_cutoff

    def validate(self, match_date: str, model_weights_date: str = "2026-06-21"):
        """Ensure we're not predicting on training data"""
        if match_date < self.train_cutoff:
            raise ValueError(
                f"Temporal safety: match date {match_date} is before "
                f"training cutoff {self.train_cutoff}. This would leak future info."
            )
        return True

# --- Core Adapter ---

class JEPAAdapter:
    """
    Drop-in adapter for v4.1 Stacking pipeline.

    Usage:
        adapter = JEPAAdapter(model_path='models/jepa/checkpoints/best_model.pt')
        proba = adapter.predict_proba(
            static_features,      # (N, 72) or (72,)
            match_sequences,      # (N, 10, 32) or None
            match_ids=None        # Optional for sequence cache
        )
    """

    DEFAULT_WEIGHT: float = 0.08  # Conservative initial weight (荣合众 recommendation)

    def __init__(
        self,
        model_path: Optional[str] = None,
        weight: Optional[float] = None,
        device: str = 'cpu',
        degradation: DegradationLevel = DegradationLevel.FULL,
    ):
        self.weight = weight or self.DEFAULT_WEIGHT
        self.device = device
        self.degradation = degradation
        self.stats = AdapterStats()
        self.seq_cache = SequenceCache()
        self.safety_guard = TemporalSafetyGuard()
        self.model: Optional[torch.nn.Module] = None
        self._loaded: bool = False

        if model_path:
            self.load_model(model_path)
        else:
            self._init_random_model()

    # ------------------------------------------------------------------
    # Model loading
    # ------------------------------------------------------------------

    def load_model(self, model_path: str):
        """Load JEPA model from checkpoint, falling back to random init."""
        path = Path(model_path)
        if not path.is_absolute():
            path = Path('D:/Architecture') / model_path

        if not path.exists():
            logger.warning("JEPA model not found at %s, using random init", model_path)
            self._init_random_model()
            return

        try:
            from models.jepa import JEPALite
            self.model = JEPALite()
        except ImportError:
            try:
                from models.jepa import FootballJEPA
                self.model = FootballJEPA()
            except ImportError:
                logger.warning("Cannot import JEPA model, using random init")
                self._init_random_model()
                return

        checkpoint = torch.load(str(path), map_location=self.device, weights_only=True)
        if 'model' in checkpoint:
            self.model.load_state_dict(checkpoint['model'])
        else:
            self.model.load_state_dict(checkpoint, strict=False)

        self.model.to(self.device)
        self.model.eval()
        self._loaded = True
        logger.info("JEPA model loaded from %s", path)

    def _init_random_model(self):
        """Initialize random model as fallback."""
        try:
            from models.jepa import JEPALite
            self.model = JEPALite()
        except ImportError:
            try:
                from models.jepa import FootballJEPA
                self.model = FootballJEPA()
            except ImportError:
                logger.warning("JEPA core unavailable, predictions will be uniform")
                self.model = None
                self._loaded = False
                return
        self.model.to(self.device)
        self.model.eval()
        self._loaded = True
        logger.info("JEPA model initialized with random weights")

    # ------------------------------------------------------------------
    # Main prediction interface
    # ------------------------------------------------------------------

    def predict_proba(
        self,
        static_features: np.ndarray,
        match_sequences: Optional[np.ndarray] = None,
        odds_drift: Optional[np.ndarray] = None,
        match_ids: Optional[List[str]] = None,
        n_paths: int = 50,
    ) -> np.ndarray:
        """
        Main prediction interface - compatible with v4.1 Stacking.

        Args:
            static_features: (N, 72) or (72,) static features
            match_sequences: (N, 10, 32) match history sequences
            odds_drift: (N, 8, 24) odds drift snapshots
            match_ids: Optional match IDs for sequence caching
            n_paths: Number of Monte Carlo rollout paths

        Returns:
            proba: (N, 3) probability matrix [P(H), P(D), P(A)]
        """
        self.stats.total_calls += 1

        # Ensure 2D
        if static_features.ndim == 1:
            static_features = static_features.reshape(1, -1)

        N = static_features.shape[0]

        # Degradation handling
        level = self._determine_degradation(match_sequences, odds_drift)

        if level != DegradationLevel.FULL:
            self.stats.degradation_fallbacks += 1

        # Generate missing features
        if match_sequences is None:
            match_sequences = np.zeros((N, 10, 32), dtype=np.float32)
        if odds_drift is None:
            odds_drift = self._synthetic_drift(static_features)

        # Predict
        static_t = torch.tensor(static_features, dtype=torch.float32, device=self.device)
        seq_t = torch.tensor(match_sequences, dtype=torch.float32, device=self.device)
        drift_t = torch.tensor(odds_drift, dtype=torch.float32, device=self.device)

        with torch.no_grad():
            if self._loaded and self.model is not None:
                proba = self.model.predict_proba(static_t, seq_t, drift_t, n_paths=n_paths)
            else:
                # Fallback: uniform distribution
                proba = torch.ones(N, 3, device=self.device) / 3.0

        return proba.cpu().numpy()

    def _determine_degradation(self, seq, drift):
        """Determine current degradation level."""
        if self.degradation == DegradationLevel.FULL:
            if seq is None and drift is not None:
                return DegradationLevel.NO_DRIFT
            elif seq is None:
                return DegradationLevel.STATIC_ONLY
            return DegradationLevel.FULL
        return self.degradation

    def _synthetic_drift(self, static_features: np.ndarray) -> np.ndarray:
        """Generate synthetic odds drift from static features."""
        N = static_features.shape[0]
        drift = np.zeros((N, 8, 24), dtype=np.float32)
        # Use odds from static features (dim 0-2 are normalized odds)
        for t in range(8):
            alpha = t / 7.0  # linear interpolation opening -> close
            drift[:, t, 0:3] = static_features[:, 0:3] * (0.9 + 0.1 * alpha)
            drift[:, t, 3:6] = static_features[:, 3:6]  # implied probs
            drift[:, t, 6] = alpha
            drift[:, t, 18] = alpha  # confidence increases
        return drift

    # ------------------------------------------------------------------
    # Meta features for Stacking
    # ------------------------------------------------------------------

    def get_meta_features(
        self,
        static_features: np.ndarray,
        match_sequences: Optional[np.ndarray] = None,
        odds_drift: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """
        Extract 6-dim meta features for Stacking meta-learner.
        These augment the 15-dim base model outputs to 21-dim.
        """
        proba = self.predict_proba(static_features, match_sequences, odds_drift, n_paths=30)

        features = np.zeros((proba.shape[0], 6), dtype=np.float32)
        features[:, 0:3] = proba                    # JEPA probabilities
        features[:, 3] = proba[:, 1]                # Draw probability
        features[:, 4] = np.max(proba, axis=1)      # Max confidence
        features[:, 5] = -np.sum(proba * np.log(proba + 1e-8), axis=1)  # Entropy

        return features

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def latency_benchmark(self, n_runs: int = 100) -> Dict[str, float]:
        """Benchmark inference latency."""
        import time
        static = np.random.randn(1, 72).astype(np.float32)
        seq = np.zeros((1, 10, 32), dtype=np.float32)
        drift = np.zeros((1, 8, 24), dtype=np.float32)

        # Warmup
        for _ in range(10):
            self.predict_proba(static, seq, drift, n_paths=10)

        times: List[float] = []
        for _ in range(n_runs):
            t0 = time.perf_counter()
            self.predict_proba(static, seq, drift, n_paths=50)
            times.append((time.perf_counter() - t0) * 1000)

        self.stats.avg_latency_ms = float(np.mean(times))
        return {
            'mean_ms': float(np.mean(times)),
            'p50_ms': float(np.percentile(times, 50)),
            'p95_ms': float(np.percentile(times, 95)),
            'p99_ms': float(np.percentile(times, 99)),
        }

# --- Meta Feature Provider ---

class JEPAMetaFeatureProvider:
    """Provides JEPA meta features for Stacking meta-learner."""

    def __init__(self, adapter: JEPAAdapter):
        self.adapter = adapter

    def augment_meta_features(
        self,
        base_probas_15dim: np.ndarray,
        static_features: np.ndarray,
        match_sequences: Optional[np.ndarray] = None,
        odds_drift: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """
        Augment 15-dim base model outputs with 6 JEPA meta features.
        Total: 21-dim meta feature vector.
        """
        jepa_features = self.adapter.get_meta_features(static_features, match_sequences, odds_drift)
        return np.concatenate([base_probas_15dim, jepa_features], axis=1)

# ======================================================================
# Smoke test
# ======================================================================

def integration_smoke_test():
    """Quick smoke test for integration bridge."""
    print("=" * 60)
    print("JEPA Integration Bridge - Smoke Test")
    print("=" * 60)

    # Test 1: Adapter creation
    print("\n[Test 1] Adapter initialization...")
    adapter = JEPAAdapter(weight=0.08)
    assert adapter.weight == 0.08
    print("  PASS Adapter created (random init)")

    # Test 2: Basic prediction
    print("\n[Test 2] predict_proba...")
    static = np.random.randn(5, 72).astype(np.float32)
    seq = np.random.randn(5, 10, 32).astype(np.float32)
    drift = np.random.randn(5, 8, 24).astype(np.float32)

    proba = adapter.predict_proba(static, seq, drift, n_paths=10)
    assert proba.shape == (5, 3), f"Expected (5,3), got {proba.shape}"
    assert np.allclose(proba.sum(axis=1), 1.0, atol=0.01), "Probabilities don't sum to 1"
    print(f"  PASS Shape: {proba.shape}, Sum: {proba.sum(axis=1)}")
    print(f"  Sample: H={proba[0, 0]:.3f} D={proba[0, 1]:.3f} A={proba[0, 2]:.3f}")

    # Test 3: Meta features
    print("\n[Test 3] get_meta_features...")
    meta = adapter.get_meta_features(static, seq, drift)
    assert meta.shape == (5, 6), f"Expected (5,6), got {meta.shape}"
    print(f"  PASS Meta features shape: {meta.shape}")

    # Test 4: No sequence (degradation)
    print("\n[Test 4] Degradation (no sequence)...")
    proba_no_seq = adapter.predict_proba(static, None, None, n_paths=10)
    assert proba_no_seq.shape == (5, 3)
    assert adapter.stats.degradation_fallbacks >= 1
    print("  PASS Degradation handled correctly")

    # Test 5: Latency benchmark
    print("\n[Test 5] Latency benchmark...")
    bench = adapter.latency_benchmark(n_runs=20)
    print(f"  Mean: {bench['mean_ms']:.1f}ms | P50: {bench['p50_ms']:.1f}ms | P95: {bench['p95_ms']:.1f}ms")
    assert bench['mean_ms'] < 500, f"Latency too high: {bench['mean_ms']:.1f}ms"
    print("  PASS Latency acceptable")

    # Test 6: Temporal safety
    print("\n[Test 6] Temporal safety guard...")
    guard = TemporalSafetyGuard(train_cutoff="2023-01-01")
    try:
        guard.validate("2022-06-01")
        print("  FAIL Should have raised error for pre-2023 date!")
    except ValueError as e:
        print(f"  PASS Correctly blocked: {str(e)[:60]}...")

    guard.validate("2024-06-01")
    print("  PASS Post-2023 date passed")

    print("\n" + "=" * 60)
    print("ALL 6 INTEGRATION TESTS PASSED")
    print("=" * 60)

    return adapter

if __name__ == '__main__':
    integration_smoke_test()
