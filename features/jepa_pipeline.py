"""
JEPA v5.0 Data Pipeline for FootballAI
=======================================
Builds training samples for the JEPA World Model from real match data.

Output per sample:
  static_72:  (72,)    - static match features (same as v4.1 feature set)
  match_seq:  (10, 32) - last 10 matches per team, 16-dim each → 32-dim per timestep
  odds_drift: (8, 24)  - 8 synthetic time snapshots with drift features
  label:      int      - 0=Home win, 1=Draw, 2=Away win

Data source: D:/AI/footballAI/data/ht_enhanced_training_v6.parquet (312K+ matches)
"""

import pandas as pd
import numpy as np
from pathlib import Path
import glob
import warnings

warnings.filterwarnings("ignore", category=FutureWarning)


# ─── Static 72 feature list (order matters for reproducibility) ───────────────
# These 72 features are selected from the 132 numeric columns in v6 data.
# Grouped logically: Core Odds → Odds Derived → Draw Signals → Team Form →
#   HT-Specific → Drift Dynamics → Advanced Signals → Context
STATIC_72_COLS = [
    # Group 1: Core Odds (18)
    "close_home_odds", "close_draw_odds", "close_away_odds",
    "open_home_odds", "open_draw_odds", "open_away_odds",
    "real_home_odds", "real_draw_odds", "real_away_odds",
    "odds_imp_h", "odds_imp_d", "odds_imp_a",
    "prob_h", "prob_d", "prob_a",
    "imp_h", "imp_d", "imp_a",

    # Group 2: Odds Derived (14)
    "odds_overround", "odds_balance", "odds_confidence",
    "odds_ratio", "odds_spread", "odds_entropy",
    "odds_move_h", "odds_move_d", "odds_move_a",
    "odds_move_magnitude", "odds_fav_move",
    "market_fav_strength", "market_disagreement",
    "odds_model_diverge",

    # Group 3: Draw / Market Signals (2)
    "draw_odds_attract", "draw_with_ht_draw",

    # Group 4: Team Form (8)
    "home_points_avg_10", "home_points_avg_5", "home_win_avg_10",
    "away_points_avg_10",
    "h_team_draw_rate", "a_team_draw_rate",
    "league_draw_rate", "league_avg_goals",

    # Group 5: HT-Specific (8)
    "ht_draw_composite", "ht_draw_prob", "ht_00_prob",
    "ht_goal_pressure", "ht_h_lead_prob", "ht_scoring_diff",
    "exp_ht_goals", "exp_total_goals",

    # Group 6: Drift Dynamics (7)
    "drift_h", "drift_d", "drift_a",
    "drift_h_val", "drift_a_val", "drift_divergence",
    "imp_d_norm",

    # Group 7: Advanced Signals (8)
    "a1", "a5", "a6", "a7", "a8",
    "sigma_trap", "lambda_crush", "epsilon_senti",

    # Group 8: Context (7)
    "rank_diff_factor", "form_momentum", "h2h_factor",
    "rank_factor", "form_factor",
    "is_cold_start", "feat_coverage_ratio",
]

assert len(STATIC_72_COLS) == 72, f"Expected 72 features, got {len(STATIC_72_COLS)}"


class JEPADataPipeline:
    """
    JEPA World Model training data pipeline.

    Uses actual match data from ht_enhanced_training_v6.parquet with
    robust column-name detection and graceful fallback for any missing fields.
    """

    def __init__(self, data_dir="D:/AI/footballAI/data", preferred_file=None):
        self.data_dir = Path(data_dir)
        self.matches = self._load_matches(preferred_file)
        self._date_col = self._detect_date_column()
        self._home_team_col = self._detect_column(["home_team", "team_home"])
        self._away_team_col = self._detect_column(["away_team", "team_away"])
        self._home_score_col = self._detect_column(["home_score", "goals_home", "home_goals"])
        self._away_score_col = self._detect_column(["away_score", "goals_away", "away_goals"])
        self._result_col = self._detect_column(["final_result", "result", "result_text"], required=False)

        # Ensure date is sortable as string (YYYY-MM-DD)
        self._ensure_sortable_date()

        # Pre-compute match result vector cache for fast lookup
        self._result_cache = {}

        print(f"[JEPADataPipeline] Loaded {len(self.matches):,} matches")
        print(f"  Date column: '{self._date_col}', range: {self.matches[self._date_col].min()} → {self.matches[self._date_col].max()}")
        print(f"  Team columns: '{self._home_team_col}' / '{self._away_team_col}'")
        print(f"  Score columns: '{self._home_score_col}' / '{self._away_score_col}'")
        print(f"  Result column: '{self._result_col}'")

    # ─── Data Loading ────────────────────────────────────────────────────────

    def _load_matches(self, preferred_file):
        """Load parquet files, preferring specified file if given."""
        if preferred_file:
            path = Path(preferred_file)
            if path.exists():
                print(f"[JEPADataPipeline] Loading preferred file: {path}")
                return pd.read_parquet(str(path))

        # Search for latest v6 file
        candidates = []
        if self.data_dir.exists():
            candidates = sorted(glob.glob(str(self.data_dir / "ht_enhanced_training_v6*.parquet")))

        if not candidates:
            # Fall back to any parquet
            candidates = sorted(glob.glob(str(self.data_dir / "*.parquet")))

        if not candidates:
            raise FileNotFoundError(
                f"No parquet files found in {self.data_dir}. "
                f"Place ht_enhanced_training_v6.parquet there."
            )

        print(f"[JEPADataPipeline] Loading: {Path(candidates[-1]).name}")
        return pd.read_parquet(candidates[-1])

    def _detect_column(self, candidates, required=True):
        """Find first matching column name from candidates list."""
        for col in candidates:
            if col in self.matches.columns:
                return col
        if required:
            available = list(self.matches.columns[:10])
            raise KeyError(
                f"Required column not found. Tried: {candidates}. "
                f"First 10 columns available: {available}"
            )
        return None

    def _detect_date_column(self):
        """Detect the date column name."""
        for col in ["match_date", "date", "game_date", "match_datetime"]:
            if col in self.matches.columns:
                return col
        raise KeyError(f"No date column found. Available: {list(self.matches.columns[:20])}")

    def _ensure_sortable_date(self):
        """Ensure date column is YYYY-MM-DD string for reliable string comparison."""
        date_series = self.matches[self._date_col]
        if pd.api.types.is_datetime64_any_dtype(date_series):
            self.matches[self._date_col] = date_series.dt.strftime("%Y-%m-%d")
        else:
            # Already string — ensure clean format
            self.matches[self._date_col] = date_series.astype(str).str.strip()
            # Handle possible datetime-like strings: extract YYYY-MM-DD portion
            self.matches[self._date_col] = self.matches[self._date_col].str[:10]

    # ─── Static 72 Builder ──────────────────────────────────────────────────

    def _build_static_72(self, row):
        """
        Build 72-dim static feature vector from a single match row.

        Uses the actual feature columns from v6 data. Missing features
        default to 0.0. Values are clipped/normalized for model consumption.
        """
        v = np.zeros(72, dtype=np.float32)
        for i, col in enumerate(STATIC_72_COLS):
            if col in row.index:
                val = row[col]
                if pd.notna(val) and np.isfinite(val):
                    v[i] = float(val)
        return v

    # ─── Match Sequence Builder ─────────────────────────────────────────────

    def _get_team_history(self, team, before_date, n=10):
        """
        Get last n matches for a team before a given date.

        Uses string comparison on YYYY-MM-DD dates for correctness.
        Returns empty DataFrame if team not found or no history.
        """
        if not team or pd.isna(team) or pd.isna(before_date):
            return pd.DataFrame()

        df = self.matches
        date = self._date_col
        home = self._home_team_col
        away = self._away_team_col

        # Match where team is either home or away
        mask = (df[home] == team) | (df[away] == team)
        mask &= df[date].astype(str) < str(before_date)

        hist = df[mask].sort_values(date).tail(n)
        return hist

    def _encode_match(self, match_row):
        """
        Encode a single historical match into a 16-dim vector.

        [0:2]   scores normalized (goals_for/5, goals_against/5)
        [2:5]   result one-hot (win, draw, loss)
        [5:8]   implied odds probs
        [8]     possession proxy (0.5 constant)
        [9]     total goals normalized
        [10:16] padding / reserved
        """
        v = np.zeros(16, dtype=np.float32)

        # Goals for/against (from the team's perspective)
        home = self._home_team_col
        away = self._away_team_col
        home_score = self._home_score_col
        away_score = self._away_score_col

        gf = match_row.get(home_score, 0)
        ga = match_row.get(away_score, 0)

        v[0] = min(float(gf) / 5.0, 1.0)
        v[1] = min(float(ga) / 5.0, 1.0)

        # Result one-hot
        if gf > ga:
            v[2] = 1.0  # win
        elif gf == ga:
            v[3] = 1.0  # draw
        else:
            v[4] = 1.0  # loss

        # Implied odds probabilities
        ho = match_row.get("close_home_odds", match_row.get("open_home_odds", 2.0))
        do = match_row.get("close_draw_odds", match_row.get("open_draw_odds", 3.5))
        ao = match_row.get("close_away_odds", match_row.get("open_away_odds", 3.5))
        ho = max(float(ho), 1.01)
        do = max(float(do), 1.03)
        ao = max(float(ao), 1.01)
        inv_sum = 1.0 / ho + 1.0 / do + 1.0 / ao
        if inv_sum > 0:
            v[5] = (1.0 / ho) / inv_sum
            v[6] = (1.0 / do) / inv_sum
            v[7] = (1.0 / ao) / inv_sum

        v[8] = 0.5  # possession placeholder
        v[9] = min((float(gf) + float(ga)) / 8.0, 1.0)

        return v

    def build_match_sequence(self, match_idx):
        """
        Build (10, 32) match sequence for a given match index.

        The first 16 dims are the home team's last 10 matches (most recent last).
        The last 16 dims are the away team's last 10 matches.

        For home team matches:
          goals_for = home_score if team was home, else away_score
          goals_against = away_score if team was home, else home_score
        """
        row = self.matches.iloc[match_idx]
        home_team = row[self._home_team_col]
        away_team = row[self._away_team_col]
        match_date = row[self._date_col]

        home_prev = self._get_team_history(home_team, match_date, n=10)
        away_prev = self._get_team_history(away_team, match_date, n=10)

        seq = np.zeros((10, 32), dtype=np.float32)

        for i in range(10):
            if i < len(home_prev):
                seq[i, :16] = self._encode_match_for_team(
                    home_prev.iloc[i], team=home_team
                )
            if i < len(away_prev):
                seq[i, 16:] = self._encode_match_for_team(
                    away_prev.iloc[i], team=away_team
                )

        return seq

    def _encode_match_for_team(self, match_row, team):
        """
        Encode match from a specific team's perspective.

        If team was home: goals_for = home_score, goals_against = away_score
        If team was away: goals_for = away_score, goals_against = home_score
        """
        v = np.zeros(16, dtype=np.float32)

        home_col = self._home_team_col
        home_sc = self._home_score_col
        away_sc = self._away_score_col

        is_home = str(match_row[home_col]) == str(team)

        gf = match_row[home_sc] if is_home else match_row[away_sc]
        ga = match_row[away_sc] if is_home else match_row[home_sc]

        gf = float(gf) if pd.notna(gf) else 0.0
        ga = float(ga) if pd.notna(ga) else 0.0

        v[0] = min(gf / 5.0, 1.0)
        v[1] = min(ga / 5.0, 1.0)

        if gf > ga:
            v[2] = 1.0
        elif gf == ga:
            v[3] = 1.0
        else:
            v[4] = 1.0

        # Implied odds probs from close odds (with fallback)
        ho = self._safe_float(match_row.get("close_home_odds", None),
                              match_row.get("open_home_odds", 2.0))
        do = self._safe_float(match_row.get("close_draw_odds", None),
                              match_row.get("open_draw_odds", 3.5))
        ao = self._safe_float(match_row.get("close_away_odds", None),
                              match_row.get("open_away_odds", 3.5))
        inv_sum = 1.0 / ho + 1.0 / do + 1.0 / ao
        if inv_sum > 0:
            v[5] = (1.0 / ho) / inv_sum
            v[6] = (1.0 / do) / inv_sum
            v[7] = (1.0 / ao) / inv_sum

        v[8] = 0.5
        v[9] = min((gf + ga) / 8.0, 1.0)

        return v

    # ─── Odds Drift Builder ─────────────────────────────────────────────────

    def build_odds_drift(self, match_idx):
        """
        Build (8, 24) synthetic odds drift features.

        Uses open→close odds transition to simulate 8 temporal snapshots.
        Each snapshot:
          [0:3]   normalized odds (odds/10)
          [3:6]   implied probabilities
          [6]     time position (0→1)
          [7]     uncertainty (std estimate)
          [8:11]  1st derivatives (prob changes)
          [11:14] 2nd derivatives
          [14]    inflection score
          [15:18] convergence to final (residual)
          [18]    confidence
          [19:24] reserved
        """
        row = self.matches.iloc[match_idx]

        # Get open and close odds (use real odds as final truth)
        h_open = self._safe_float(row.get("open_home_odds", 2.0))
        d_open = self._safe_float(row.get("open_draw_odds", 3.5))
        a_open = self._safe_float(row.get("open_away_odds", 3.0))

        h_final = self._safe_float(row.get("close_home_odds", h_open),
                                   row.get("real_home_odds", h_open))
        d_final = self._safe_float(row.get("close_draw_odds", d_open),
                                   row.get("real_draw_odds", d_open))
        a_final = self._safe_float(row.get("close_away_odds", a_open),
                                   row.get("real_away_odds", a_open))

        # Seed deterministic per match for reproducibility
        seed = hash(str(match_idx) + str(row[self._home_team_col]) + str(row[self._away_team_col])) % (2**31)
        rng = np.random.RandomState(seed)

        drift = np.zeros((8, 24), dtype=np.float32)

        for t in range(8):
            alpha = t / 7.0  # linear interpolation: 0=open, 1=close
            noise_base = 0.06 * (1.0 - alpha)  # decreasing noise

            # Interpolated odds with noise
            h_t = h_open + (h_final - h_open) * alpha + rng.normal(0, noise_base)
            d_t = d_open + (d_final - d_open) * alpha + rng.normal(0, noise_base * 1.2)
            a_t = a_open + (a_final - a_open) * alpha + rng.normal(0, noise_base)

            # Clamp to valid odds range
            h_t = max(1.05, h_t)
            d_t = max(1.10, d_t)
            a_t = max(1.05, a_t)

            # Implied probabilities
            inv_sum = 1.0 / h_t + 1.0 / d_t + 1.0 / a_t
            ph = (1.0 / h_t) / inv_sum if inv_sum > 0 else 1.0 / 3.0
            pd = (1.0 / d_t) / inv_sum if inv_sum > 0 else 1.0 / 3.0
            pa = (1.0 / a_t) / inv_sum if inv_sum > 0 else 1.0 / 3.0

            drift[t, 0] = h_t / 10.0
            drift[t, 1] = d_t / 10.0
            drift[t, 2] = a_t / 10.0
            drift[t, 3] = ph
            drift[t, 4] = pd
            drift[t, 5] = pa
            drift[t, 6] = alpha
            drift[t, 7] = noise_base * 10.0
            drift[t, 18] = 1.0 - noise_base * 10.0

        # Compute 1st derivatives (prob changes)
        for t in range(1, 8):
            drift[t, 8:11] = drift[t, 3:6] - drift[t - 1, 3:6]

        # Compute 2nd derivatives
        for t in range(2, 8):
            drift[t, 11:14] = drift[t, 8:11] - drift[t - 1, 8:11]

        # Inflection: zero-crossing in 1st derivative
        for t in range(1, 8):
            prev_sign = np.sign(drift[t - 1, 8:11])
            curr_sign = np.sign(drift[t, 8:11])
            sign_change = (prev_sign != curr_sign) & (np.abs(drift[t, 8:11]) > 0.001)
            drift[t, 14] = float(np.any(sign_change) or (abs(drift[t, 11:14]).sum() > 0.05))

        # Convergence: residual to final snapshot
        for t in range(8):
            drift[t, 15:18] = drift[t, 3:6] - drift[7, 3:6]

        return drift

    # ─── Utility ─────────────────────────────────────────────────────────────

    @staticmethod
    def _safe_float(*values):
        """Return first non-NaN, finite float from values or fallback to 2.0."""
        for v in values:
            if v is not None and pd.notna(v) and np.isfinite(float(v)):
                return max(float(v), 1.01)
        return 2.0

    # ─── Split Logic ────────────────────────────────────────────────────────

    def _get_split_indices(self, split):
        """Get match indices for a given split using string date comparison."""
        date_series = self.matches[self._date_col].astype(str)

        if split == "train":
            mask = date_series < "2023-01-01"
        elif split == "val":
            mask = (date_series >= "2023-01-01") & (date_series < "2024-01-01")
        elif split == "test":
            mask = date_series >= "2024-01-01"
        else:
            raise ValueError(f"Unknown split: '{split}'. Use 'train', 'val', or 'test'.")

        return self.matches[mask].index.tolist()

    # ─── Dataset Builder ────────────────────────────────────────────────────

    def build_dataset(self, split="train", max_samples=None):
        """
        Build complete dataset for a given split.

        Args:
            split: 'train', 'val', or 'test'
            max_samples: Optional limit for quick testing (None = full)

        Returns:
            dict with keys: static, sequence, drift, labels
        """
        indices = self._get_split_indices(split)
        if max_samples is not None:
            indices = indices[:max_samples]

        n_total = len(indices)
        static_list = []
        seq_list = []
        drift_list = []
        labels = []

        n_skipped = 0
        for i, idx in enumerate(indices):
            if (i + 1) % 50000 == 0:
                print(f"  [{split}] Progress: {i+1:,}/{n_total:,}")

            try:
                row = self.matches.loc[idx]

                static = self._build_static_72(row)
                seq = self.build_match_sequence(idx)
                drift = self.build_odds_drift(idx)

                # Label: 0=H, 1=D, 2=A
                label = self._extract_label(row)

                static_list.append(static)
                seq_list.append(seq)
                drift_list.append(drift)
                labels.append(label)

            except Exception as e:
                n_skipped += 1
                if n_skipped <= 5:
                    print(f"  [{split}] Skipping idx={idx}: {e}")
                continue

        if n_skipped > 0:
            print(f"  [{split}] Skipped {n_skipped}/{n_total} samples ({n_skipped/n_total*100:.1f}%)")

        result = {
            "static": np.array(static_list, dtype=np.float32),
            "sequence": np.array(seq_list, dtype=np.float32),
            "drift": np.array(drift_list, dtype=np.float32),
            "labels": np.array(labels, dtype=np.int64),
        }

        return result

    def _extract_label(self, row):
        """Extract label from a match row. Returns 0=H, 1=D, 2=A."""
        # Try explicit result column first
        if self._result_col and self._result_col in row.index:
            val = str(row[self._result_col]).strip().upper()
            if val == "H":
                return 0
            elif val == "D":
                return 1
            elif val == "A":
                return 2

        # Fall back to score comparison
        hg = row[self._home_score_col]
        ag = row[self._away_score_col]
        hg = int(hg) if pd.notna(hg) else 0
        ag = int(ag) if pd.notna(ag) else 0

        if hg > ag:
            return 0
        elif hg == ag:
            return 1
        else:
            return 2


# ─── CLI Entry Point ─────────────────────────────────────────────────────────

def build_all_splits(output_dir="D:/Architecture v4.0/data",
                     data_dir="D:/AI/footballAI/data",
                     max_samples=None):
    """
    Build and save all 3 dataset splits as .npz files.

    Args:
        output_dir: Directory for output .npz files
        data_dir: Directory containing parquet training data
        max_samples: Optional per-split cap (None = full, useful for dev)
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    pipeline = JEPADataPipeline(data_dir=data_dir)

    for split in ["train", "val", "test"]:
        print(f"\n{'='*60}")
        print(f"Building {split.upper()} split...")

        data = pipeline.build_dataset(split=split, max_samples=max_samples)

        n = len(data["labels"])
        h_count = int((data["labels"] == 0).sum())
        d_count = int((data["labels"] == 1).sum())
        a_count = int((data["labels"] == 2).sum())

        out_path = output_dir / f"jepa_{split}.npz"
        np.savez_compressed(out_path, **data)

        size_mb = out_path.stat().st_size / 1024 / 1024

        print(f"  Saved {n:,} samples to {out_path}")
        print(f"  File size: {size_mb:.1f} MB")
        print(f"  Label distribution: H={h_count:,} ({h_count/n*100:.1f}%), "
              f"D={d_count:,} ({d_count/n*100:.1f}%), "
              f"A={a_count:,} ({a_count/n*100:.1f}%)")


if __name__ == "__main__":
    build_all_splits()
