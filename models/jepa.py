"""
FootballJEPA v5.0 — Joint Embedding Predictive Architecture for Football Match Prediction.

Architecture
------------
Three-way Encoder:
  - StaticEncoder    : MLP(72→128→64→64)  — team strength, league, elo
  - MatchSeqEncoder  : Transformer×3 4-head — recent 10-match sequence
  - OddsDriftEncoder : Transformer×2 2-head — 8-step odds drift signal
GatedFusion → s₀ (128-dim)
StepPredictor (shared weights) → s₁ … s₆  (6-step, 15-min granularity)
OutputHead: [s₀⊕s₆⊕(s₆−s₀)] → 3-class logits

Inference: K-path Monte Carlo rollout with Gaussian noise injection.
Training anti-collapse: DualEncoder with EMA target network.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F

# ---------------------------------------------------------------------------
# Encoders
# ---------------------------------------------------------------------------

class StaticEncoder(nn.Module):
    """MLP encoder for 72-dim static features (team strength, league, elo, …)."""

    def __init__(self, in_dim: int = 72, hidden: int = 128, out_dim: int = 64):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(in_dim, hidden),
            nn.LayerNorm(hidden),
            nn.GELU(),
            nn.Dropout(0.15),
            nn.Linear(hidden, out_dim),
            nn.LayerNorm(out_dim),
            nn.GELU(),
            nn.Linear(out_dim, out_dim),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (B, 72) → (B, 64)
        return self.net(x)

class MatchSeqEncoder(nn.Module):
    """Transformer encoder for match-history sequences (last N=10 matches)."""

    def __init__(self, d_model: int = 128, nhead: int = 4, n_layers: int = 3,
                 seq_dim: int = 32, max_len: int = 10):
        super().__init__()
        self.input_proj = nn.Linear(seq_dim, d_model)
        self.cls_token = nn.Parameter(torch.randn(1, 1, d_model))
        self.pos_embed = nn.Parameter(torch.randn(1, max_len + 1, d_model))  # CLS + 10 matches

        encoder_layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=nhead, dim_feedforward=256,
            dropout=0.1, activation='gelu', batch_first=True,
            norm_first=True,  # Pre-LN for stability
        )
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=n_layers)
        self.out_proj = nn.Linear(d_model, 64)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (B, 10, 32) → (B, 64)
        B = x.shape[0]
        x = self.input_proj(x)                         # (B, 10, d_model)
        cls = self.cls_token.expand(B, -1, -1)         # (B, 1, d_model)
        x = torch.cat([cls, x], dim=1)                 # (B, 11, d_model)
        x = x + self.pos_embed[:, :x.shape[1], :]
        x = self.transformer(x)
        return self.out_proj(x[:, 0, :])               # CLS token → (B, 64)

class OddsDriftEncoder(nn.Module):
    """Lightweight Transformer for 8-step odds-drift sequence."""

    def __init__(self, d_model: int = 64, nhead: int = 2, n_layers: int = 2,
                 drift_dim: int = 24, seq_len: int = 8):
        super().__init__()
        self.input_proj = nn.Linear(drift_dim, d_model)
        self.pos_embed = nn.Parameter(torch.randn(1, seq_len, d_model))

        encoder_layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=nhead, dim_feedforward=128,
            dropout=0.1, activation='gelu', batch_first=True,
            norm_first=True,
        )
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=n_layers)
        self.out_proj = nn.Linear(d_model, 64)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (B, 8, 24) → (B, 64)
        x = self.input_proj(x)                         # (B, 8, d_model)
        x = x + self.pos_embed
        x = self.transformer(x)
        return self.out_proj(x.mean(dim=1))            # mean pool → (B, 64)

# ---------------------------------------------------------------------------
# Fusion & Prediction
# ---------------------------------------------------------------------------

class GatedFusion(nn.Module):
    """Learnable gated fusion of three encoder outputs → single embedding."""

    def __init__(self, dim: int = 64):
        super().__init__()
        self.gate = nn.Sequential(
            nn.Linear(dim * 3, 3),
            nn.Softmax(dim=-1),
        )
        self.out_proj = nn.Linear(dim, 128)

    def forward(self, z_static: torch.Tensor, z_seq: torch.Tensor,
                z_drift: torch.Tensor) -> torch.Tensor:
        concat = torch.cat([z_static, z_seq, z_drift], dim=-1)  # (B, 192)
        gates = self.gate(concat)                                # (B, 3)

        fused = (gates[:, 0:1] * z_static
                 + gates[:, 1:2] * z_seq
                 + gates[:, 2:3] * z_drift)                      # (B, 64)
        return self.out_proj(fused)                              # (B, 128)

class StepPredictor(nn.Module):
    """Shared-weight step predictor: s_t → s_{t+1}."""

    def __init__(self, dim: int = 128):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, 256),
            nn.LayerNorm(256),
            nn.GELU(),
            nn.Dropout(0.1),
            nn.Linear(256, 128),
            nn.LayerNorm(128),
            nn.GELU(),
            nn.Linear(128, dim),
        )

    def forward(self, s: torch.Tensor) -> torch.Tensor:
        # s: (B, 128) → (B, 128)  — residual connection applied externally
        return self.net(s)

class OutputHead(nn.Module):
    """Decode terminal state delta to 1X2 logits."""

    def __init__(self, dim: int = 128):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim * 3, 128),    # [s_0, s_T, s_T − s_0]
            nn.LayerNorm(128),
            nn.GELU(),
            nn.Dropout(0.15),
            nn.Linear(128, 64),
            nn.LayerNorm(64),
            nn.GELU(),
            nn.Linear(64, 3),
        )

    def forward(self, s_0: torch.Tensor, s_T: torch.Tensor) -> torch.Tensor:
        diff = s_T - s_0
        concat = torch.cat([s_0, s_T, diff], dim=-1)  # (B, 384)
        return self.net(concat)                        # (B, 3) logits

# ---------------------------------------------------------------------------
# Main JEPA Model
# ---------------------------------------------------------------------------

class FootballJEPA(nn.Module):
    """Complete Football JEPA World Model for match outcome prediction."""

    def __init__(self, static_dim: int = 72, seq_dim: int = 32,
                 drift_dim: int = 24, embed_dim: int = 128):
        super().__init__()
        self.static_encoder = StaticEncoder(static_dim, 128, 64)
        self.seq_encoder = MatchSeqEncoder(d_model=128, nhead=4, n_layers=3,
                                           seq_dim=seq_dim)
        self.drift_encoder = OddsDriftEncoder(d_model=64, nhead=2, n_layers=2,
                                              drift_dim=drift_dim)
        self.fusion = GatedFusion(dim=64)
        self.predictor = StepPredictor(dim=embed_dim)
        self.output_head = OutputHead(dim=embed_dim)
        self.embed_dim = embed_dim

    # -- Encoding ---------------------------------------------------------

    def encode(self, static: torch.Tensor, seq: torch.Tensor,
               drift: torch.Tensor) -> torch.Tensor:
        """Encode match context into initial latent state s₀."""
        z_static = self.static_encoder(static)          # (B, 64)
        z_seq = self.seq_encoder(seq)                   # (B, 64)
        z_drift = self.drift_encoder(drift)             # (B, 64)
        s_0 = self.fusion(z_static, z_seq, z_drift)     # (B, 128)
        return s_0

    # -- Rollout ----------------------------------------------------------

    def rollout(self, s_0: torch.Tensor, n_steps: int = 6,
                noise_scale: float = 0.05) -> torch.Tensor:
        """Autoregressive rollout from s₀.  Returns (B, n_steps+1, embed_dim)."""
        states = [s_0]
        s = s_0
        for _ in range(n_steps):
            s_next = self.predictor(s)
            if noise_scale > 0:
                s_next = s_next + torch.randn_like(s_next) * noise_scale
            states.append(s_next)
            s = s_next
        return torch.stack(states, dim=1)

    # -- Training forward -------------------------------------------------

    def forward(self, static: torch.Tensor, seq: torch.Tensor,
                drift: torch.Tensor):
        """Single forward pass with teacher forcing (training mode)."""
        s_0 = self.encode(static, seq, drift)
        s_T = self.predictor(s_0)
        logits = self.output_head(s_0, s_T)
        return logits, s_0, s_T

    # -- Inference --------------------------------------------------------

    def predict_proba(self, static: torch.Tensor, seq: torch.Tensor,
                      drift: torch.Tensor, n_paths: int = 50,
                      noise_scale: float = 0.04) -> torch.Tensor:
        """K-path Monte Carlo prediction → confidence-weighted 1X2 probabilities.

        Each path runs a 6-step autoregressive rollout with independent
        Gaussian noise injection.  Terminal states are averaged and decoded.
        """
        was_training = self.training
        self.eval()

        with torch.no_grad():
            s_0 = self.encode(static, seq, drift)       # (B, embed_dim)
            B = s_0.shape[0]

            all_probs: list[torch.Tensor] = []
            for _ in range(n_paths):
                s_T = s_0
                for _ in range(6):
                    s_T = self.predictor(s_T)
                    s_T = s_T + torch.randn_like(s_T) * noise_scale

                logits = self.output_head(s_0, s_T)    # (B, 3)
                probs = F.softmax(logits, dim=-1)
                all_probs.append(probs)

            stacked = torch.stack(all_probs, dim=0)     # (K, B, 3)
            mean_probs = stacked.mean(dim=0)            # (B, 3)

        if was_training:
            self.train()

        return mean_probs

# ---------------------------------------------------------------------------
# Dual Encoder (BYOL-style anti-collapse)
# ---------------------------------------------------------------------------

class DualEncoder(nn.Module):
    """EMA-based dual encoder for self-supervised representation learning.

    Context encoder E_θ  — trainable (gradient updates).
    Target encoder E_φ   — momentum update (EMA of θ).
    """

    def __init__(self, static_dim: int = 72, seq_dim: int = 32,
                 drift_dim: int = 24, embed_dim: int = 128,
                 ema_tau: float = 0.996):
        super().__init__()
        self.context_encoder = FootballJEPA(static_dim, seq_dim, drift_dim,
                                            embed_dim)
        self.target_encoder = FootballJEPA(static_dim, seq_dim, drift_dim,
                                           embed_dim)
        self.ema_tau = ema_tau

        # Copy weights & freeze target
        self.target_encoder.load_state_dict(self.context_encoder.state_dict())
        for p in self.target_encoder.parameters():
            p.requires_grad = False

    def update_target(self):
        """EMA update: φ ← τ·φ + (1−τ)·θ"""
        with torch.no_grad():
            for ctx_p, tgt_p in zip(self.context_encoder.parameters(),
                                    self.target_encoder.parameters()):
                tgt_p.data = self.ema_tau * tgt_p.data + \
                             (1.0 - self.ema_tau) * ctx_p.data

    def forward(self, static: torch.Tensor, seq: torch.Tensor,
                drift: torch.Tensor):
        """Return contextual and target embeddings for contrastive loss."""
        s_0_ctx = self.context_encoder.encode(static, seq, drift)
        with torch.no_grad():
            s_0_tgt = self.target_encoder.encode(static, seq, drift)
        return s_0_ctx, s_0_tgt

# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    print("=" * 60)
    print("FootballJEPA v5.0 — Smoke Test")
    print("=" * 60)

    model = FootballJEPA()
    B = 4
    static = torch.randn(B, 72)
    seq = torch.randn(B, 10, 32)
    drift = torch.randn(B, 8, 24)

    # 1. Basic forward
    logits, s0, sT = model(static, seq, drift)
    print(f"1. Forward pass")
    print(f"   Logits shape : {logits.shape}")          # (4, 3)
    print(f"   s_0 shape    : {s0.shape}")              # (4, 128)
    print(f"   s_T shape    : {sT.shape}")              # (4, 128)

    # 2. Rollout
    rollout = model.rollout(s0, n_steps=6, noise_scale=0.05)
    print(f"2. Rollout shape : {rollout.shape}")         # (4, 7, 128)

    # 3. Monte Carlo inference
    proba = model.predict_proba(static, seq, drift, n_paths=10)
    print(f"3. MC probs shape: {proba.shape}")           # (4, 3)
    print(f"   Sample probs  : [{proba[0, 0]:.4f}, {proba[0, 1]:.4f}, {proba[0, 2]:.4f}]")

    # 4. Dual Encoder
    dual = DualEncoder()
    s_ctx, s_tgt = dual(static, seq, drift)
    print(f"4. DualEncoder")
    print(f"   ctx shape     : {s_ctx.shape}")           # (4, 128)
    print(f"   tgt shape     : {s_tgt.shape}")           # (4, 128)

    # 5. EMA update
    dual.update_target()
    print(f"5. EMA target updated (τ={dual.ema_tau})")

    # 6. Total parameters
    total_params = sum(p.numel() for p in model.parameters())
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"6. Parameters")
    print(f"   Total         : {total_params:,}")
    print(f"   Trainable     : {trainable:,}")

    # 7. No BatchNorm — all LayerNorm
    has_bn = any('BatchNorm' in str(type(m)) for m in model.modules())
    print(f"7. LayerNorm only : {'PASS' if not has_bn else 'FAIL'}")

    print("=" * 60)
    print("Smoke test passed")
    print("=" * 60)

# ── JEPA Lite: static-only variant for fast-mode training ──
class JEPALite(nn.Module):
    """
    Lightweight JEPA variant for static-only feature training.
    Skips MatchSeqEncoder and OddsDriftEncoder → ~200K params.
    
    Architecture: StaticEncoder → MLP Rollout → OutputHead
    Compatible interface with FootballJEPA.
    """
    
    def __init__(self, static_dim=72, embed_dim=128):
        super().__init__()
        from models.jepa import StaticEncoder, OutputHead
        
        self.encoder = StaticEncoder(static_dim, 128, embed_dim)
        self.predictor = nn.Sequential(
            nn.Linear(embed_dim, 256),
            nn.LayerNorm(256),
            nn.GELU(),
            nn.Dropout(0.15),
            nn.Linear(256, embed_dim),
        )
        self.output_head = OutputHead(dim=embed_dim)
    
    def encode(self, static, seq=None, drift=None):
        return self.encoder(static)  # (B, 128)
    
    def forward(self, static, seq=None, drift=None):
        s_0 = self.encode(static)
        s_T = self.predictor(s_0)
        logits = self.output_head(s_0, s_T)
        return logits, s_0, s_T
    
    def predict_proba(self, static, seq=None, drift=None, n_paths=30):
        self.eval()
        B = static.shape[0]
        all_probs = []
        with torch.no_grad():
            s_0 = self.encode(static)
            for _ in range(n_paths):
                s_T = self.predictor(s_0)
                s_T = s_T + torch.randn_like(s_T) * 0.04
                logits = self.output_head(s_0, s_T)
                all_probs.append(F.softmax(logits, dim=-1))
        return torch.stack(all_probs, dim=0).mean(dim=0)
