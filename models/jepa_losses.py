"""
FootballJEPA v5.0 - VICReg Loss Functions
==========================================
毕建模 design: s:v:c:p = 10:1:0.04:25

Components:
  - L_sim (invariance): MSE between context and target encoder outputs
  - L_var (variance): Hinge loss keeping embedding std >= 1.0 per dimension
  - L_cov (covariance): Minimize off-diagonal covariance for independent dims
  - L_pred (prediction): MSE between predictor output and stopgrad target
"""

import torch
import torch.nn as nn
import torch.nn.functional as F

class FootballVICRegLoss(nn.Module):
    """
    Football-adapted VICReg loss with 4 components.
    L = 10.0 * L_sim + 1.0 * L_var + 0.04 * L_cov + 25.0 * L_pred

    s (invariance): MSE between context and target encoder outputs
    v (variance): Hinge loss keeping embedding std >= 1.0 per dimension
    c (covariance): Minimize off-diagonal covariance -> independent dims
    p (prediction): MSE between predictor output and stopgrad target
    """

    def __init__(self, sim_coeff=10.0, var_coeff=1.0, cov_coeff=0.04, pred_coeff=25.0, eps=1e-4):
        super().__init__()
        self.sim_coeff = sim_coeff
        self.var_coeff = var_coeff
        self.cov_coeff = cov_coeff
        self.pred_coeff = pred_coeff
        self.eps = eps

    def forward(self, z_ctx, z_tgt, z_pred=None, z_tgt_sg=None):
        """
        Args:
            z_ctx: (B, D) context encoder output
            z_tgt: (B, D) target encoder output
            z_pred: (B, D) predictor output (optional)
            z_tgt_sg: (B, D) stopgrad target (optional)
        Returns:
            loss: scalar
            components: dict with individual losses for monitoring
        """
        B, D = z_ctx.shape

        # 1. Invariance loss: MSE between context and target embeddings
        sim_loss = F.mse_loss(z_ctx, z_tgt)

        # 2. Variance loss: Hinge(std < 1.0) per dimension
        #    Prevents embedding collapse to single point
        std_ctx = torch.sqrt(z_ctx.var(dim=0) + self.eps)
        var_loss = torch.mean(F.relu(1.0 - std_ctx))

        # 3. Covariance loss: decorrelate embedding dimensions
        z_centered = z_ctx - z_ctx.mean(dim=0, keepdim=True)
        cov = (z_centered.T @ z_centered) / (B - 1)  # (D, D)
        # Zero out diagonal
        diag = torch.eye(D, device=cov.device)
        off_diag = cov * (1 - diag)
        cov_loss = (off_diag ** 2).sum() / D

        # 4. Prediction loss (optional - for EMA dual encoder)
        pred_loss = torch.tensor(0.0, device=z_ctx.device)
        if z_pred is not None and z_tgt_sg is not None:
            pred_loss = F.mse_loss(z_pred, z_tgt_sg)

        total = (self.sim_coeff * sim_loss +
                 self.var_coeff * var_loss +
                 self.cov_coeff * cov_loss +
                 self.pred_coeff * pred_loss)

        return total, {
            'sim': sim_loss.item(),
            'var': var_loss.item(),
            'cov': cov_loss.item(),
            'pred': pred_loss.item(),
            'total': total.item(),
        }

class DrawPreservingVICReg(nn.Module):
    """
    Enhanced VICReg with:
    - Home-Bias Hinge: penalty when model over-predicts home wins
    - Triplet constraint (lambda=2.0): ensure H/D/A all have distinguishable embeddings
    - DALS (Distribution-Aware Label Smoothing): KL(pred || empirical_prior)
    """

    def __init__(self, base_loss=None, triplet_coeff=2.0, home_bias_thresh=0.05,
                 prior_H=0.45, prior_D=0.25, prior_A=0.30):
        super().__init__()
        self.base_loss = base_loss or FootballVICRegLoss()
        self.triplet_coeff = triplet_coeff
        self.home_bias_thresh = home_bias_thresh
        self.register_buffer('prior', torch.tensor([prior_H, prior_D, prior_A]))

    def triplet_loss(self, embeddings, labels):
        """
        Ensure H/D/A embeddings are distinguishable.
        For each batch, compute:
          loss = max(0, margin - ||z_H - z_D||) + max(0, margin - ||z_A - z_D||)
        This PUSHES draw embeddings away from H and A clusters.
        """
        B = embeddings.shape[0]
        if B < 3:
            return torch.tensor(0.0, device=embeddings.device)

        # Separate by label
        h_mask = (labels == 0)
        d_mask = (labels == 1)
        a_mask = (labels == 2)

        loss = torch.tensor(0.0, device=embeddings.device)
        count = 0

        if h_mask.sum() > 0 and d_mask.sum() > 0:
            z_h = embeddings[h_mask].mean(dim=0)
            z_d = embeddings[d_mask].mean(dim=0)
            dist_hd = torch.norm(z_h - z_d)
            loss += F.relu(0.5 - dist_hd)
            count += 1

        if a_mask.sum() > 0 and d_mask.sum() > 0:
            z_a = embeddings[a_mask].mean(dim=0)
            z_d = embeddings[d_mask].mean(dim=0)
            dist_ad = torch.norm(z_a - z_d)
            loss += F.relu(0.5 - dist_ad)
            count += 1

        return loss / max(count, 1)

    def home_bias_hinge(self, logits):
        """Penalize extreme home-win bias"""
        probs = F.softmax(logits, dim=-1)  # (B, 3)
        batch_p = probs.mean(dim=0)  # (3,) batch distribution

        # Only penalize if home prob exceeds prior by > threshold
        excess = batch_p[0] - self.prior[0] - self.home_bias_thresh
        return F.relu(excess)

    def dals_loss(self, logits):
        """Distribution-aware smoothing: KL(batch_pred || empirical_prior)"""
        probs = F.softmax(logits, dim=-1)
        batch_p = probs.mean(dim=0)
        # Symmetric KL
        kl = (batch_p * (torch.log(batch_p + 1e-8) - torch.log(self.prior + 1e-8))).sum()
        kl += (self.prior * (torch.log(self.prior + 1e-8) - torch.log(batch_p + 1e-8))).sum()
        return 0.5 * kl

    def forward(self, z_ctx, z_tgt, logits, labels, z_pred=None, z_tgt_sg=None):
        base_loss, components = self.base_loss(z_ctx, z_tgt, z_pred, z_tgt_sg)

        trip = self.triplet_loss(z_ctx, labels)
        bias = self.home_bias_hinge(logits)
        dals = self.dals_loss(logits)

        total = base_loss + self.triplet_coeff * trip + 2.0 * bias + 0.5 * dals

        components['triplet'] = trip.item()
        components['home_bias'] = bias.item()
        components['dals'] = dals.item()
        components['total'] = total.item()

        return total, components

class EmbeddingCollapseMonitor:
    """Monitor embedding collapse during training"""

    def __init__(self, threshold=0.55):
        self.threshold = threshold

    def check_collapse(self, embeddings, labels):
        """Check if draw embeddings are separable via linear probe"""
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import cross_val_score

        Z = embeddings.detach().cpu().numpy()
        y = (labels.detach().cpu().numpy() == 1).astype(int)  # 1=Draw

        if y.sum() < 5:
            return {'collapsed': None, 'probe_acc': None, 'msg': 'Insufficient draw samples'}

        probe = LogisticRegression(max_iter=1000, class_weight='balanced')
        scores = cross_val_score(probe, Z, y, cv=min(5, y.sum()))
        acc = scores.mean()

        return {
            'collapsed': acc < self.threshold,
            'probe_acc': acc,
            'msg': f'Linear probe accuracy: {acc:.3f} (threshold={self.threshold})'
        }
