"""
LeCun JEPA Theory — Autonomous Retraining Pipeline
===================================================
基于网格搜索结果:
  - 模型平局概率未校准 → 阈值无法兼顾Acc和Draw-F1
  - 需要在训练中加入校准损失

LeCun理论驱动改进:
  1. Energy-Based Model: 训练时加入temperature-aware loss
  2. VICReg调参: 防嵌入坍缩 + 保持draw表示能力
  3. Draw Calibration Loss: BCE on draw probability alignment
  4. Multi-Temperature Training: 随机温度增强校准鲁棒性
"""
import os, sys, json, math, time
import numpy as np
from pathlib import Path
from collections import defaultdict

import torch
import torch.nn as nn
import torch.nn.functional as F

ROOT = str(Path(__file__).resolve().parent.parent)

from models.jepa import JEPALite, StaticEncoder, OutputHead
from models.jepa_losses import FootballVICRegLoss, DrawPreservingVICReg, EmbeddingCollapseMonitor

# ═══════════════════════════════════════════════════════════════
# Improved Loss: LeCun Calibrated VICReg
# ═══════════════════════════════════════════════════════════════

class LeCunCalibratedLoss(nn.Module):
    """
    LeCun JEPA 校准损失:
    
    1. VICReg (标准): sim=25, var=1, cov=0.04, pred=25
    2. Draw Calibration: BCE(pred_draw, actual_draw) × λ_draw
    3. Energy Regularization: 惩罚过高置信度
    4. Multi-temperature: 随机τ∈[0.5, 3.0]训练
    
    设计原则 (LeCun):
    - 嵌入空间预测 (pred) 比分类更重要
    - 能量校准确保不确定性估计准确
    - Draw calibration通过BCE直接对齐
    """
    
    def __init__(self, sim_coeff=25.0, var_coeff=1.0, cov_coeff=0.04, 
                 pred_coeff=25.0, ce_coeff=0.5, draw_cal_coeff=2.0,
                 energy_reg_coeff=0.1, multi_temp=True):
        super().__init__()
        self.vicreg = FootballVICRegLoss(
            sim_coeff=sim_coeff, var_coeff=var_coeff, 
            cov_coeff=cov_coeff, pred_coeff=pred_coeff
        )
        self.draw_preserving = DrawPreservingVICReg()
        self.ce_coeff = ce_coeff
        self.draw_cal_coeff = draw_cal_coeff
        self.energy_reg_coeff = energy_reg_coeff
        self.multi_temp = multi_temp
        self.collapse_monitor = EmbeddingCollapseMonitor()
    
    def forward(self, z_ctx, z_tgt, logits, labels, z_pred=None, z_tgt_sg=None):
        """
        z_ctx: 上下文嵌入 (B, D)
        z_tgt: 目标嵌入 (B, D) 
        logits: 分类logits (B, 3)
        labels: 真实标签 (B,)  0=H, 1=D, 2=A
        """
        B = logits.shape[0]
        
        # 1. VICReg loss (embedding quality)
        vicreg_loss, vicreg_comps = self.vicreg(z_ctx, z_tgt, z_pred, z_tgt_sg)
        
        # 2. Draw-preserving VICReg
        draw_vic_loss, draw_comps = self.draw_preserving(z_ctx, z_tgt, logits, labels, z_pred, z_tgt_sg)
        
        # 3. Classification CE with temperature augmentation
        if self.multi_temp:
            tau = torch.empty(1).uniform_(0.5, 3.0).item()
            scaled_logits = logits / tau
        else:
            scaled_logits = logits / 1.0
        
        ce_loss = F.cross_entropy(scaled_logits, labels)
        
        # 4. Draw Calibration Loss (LeCun EBM principle)
        # BCE: does the model's draw probability match reality?
        probs = torch.softmax(scaled_logits, dim=-1)
        pred_draw = probs[:, 1]  # draw probability
        is_draw = (labels == 1).float()
        
        # BCE on draw probability
        draw_cal_loss = F.binary_cross_entropy(pred_draw, is_draw)
        
        # 5. Energy Regularization (prevent overconfidence)
        # E = -log(sum(exp(logits/tau))) — penalize very low energy (high confidence)
        energy = -torch.logsumexp(scaled_logits, dim=-1).mean()
        # Target: energy ≈ 0.5-1.0 (moderate confidence)
        energy_target = 0.8
        energy_reg = (energy - energy_target) ** 2
        
        # Total loss
        total = (vicreg_loss * 0.3 +           # Embedding quality (reduced weight)
                 draw_vic_loss * 0.5 +          # Draw-preserving (increased weight)  
                 ce_loss * self.ce_coeff +      # Classification
                 draw_cal_loss * self.draw_cal_coeff +  # Draw calibration (NEW)
                 energy_reg * self.energy_reg_coeff)    # Energy regularization (NEW)
        
        comps = {
            **{f'vic_{k}': v for k, v in vicreg_comps.items()},
            **{f'draw_{k}': v for k, v in draw_comps.items()},
            'ce': ce_loss.item(),
            'draw_cal': draw_cal_loss.item(),
            'energy_reg': energy_reg.item(),
            'energy': energy.item(),
            'total': total.item(),
        }
        
        return total, comps

# ═══════════════════════════════════════════════════════════════
# Training Pipeline
# ═══════════════════════════════════════════════════════════════

def load_data():
    """Load JEPA pre-built datasets"""
    data_dir = os.path.join(ROOT, 'data')
    train = np.load(os.path.join(data_dir, 'jepa_train.npz'))
    val = np.load(os.path.join(data_dir, 'jepa_val.npz'))
    test = np.load(os.path.join(data_dir, 'jepa_test.npz'))
    
    return (
        (train['static'], train['sequence'], train['drift'], train['labels']),
        (val['static'], val['sequence'], val['drift'], val['labels']),
        (test['static'], test['sequence'], test['drift'], test['labels']),
    )

def create_loaders(train_data, val_data, batch_size=512):
    """Create DataLoaders"""
    tr_s, tr_seq, tr_dr, tr_l = train_data
    vl_s, vl_seq, vl_dr, vl_l = val_data
    
    train_ds = torch.utils.data.TensorDataset(
        torch.tensor(tr_s), torch.tensor(tr_seq), 
        torch.tensor(tr_dr), torch.tensor(tr_l)
    )
    val_ds = torch.utils.data.TensorDataset(
        torch.tensor(vl_s), torch.tensor(vl_seq), 
        torch.tensor(vl_dr), torch.tensor(vl_l)
    )
    
    train_loader = torch.utils.data.DataLoader(train_ds, batch_size=batch_size, shuffle=True)
    val_loader = torch.utils.data.DataLoader(val_ds, batch_size=batch_size, shuffle=False)
    
    return train_loader, val_loader

def train_epoch(model, loader, optimizer, scheduler, loss_fn, device, epoch):
    model.train()
    total_loss = 0
    comps_sum = defaultdict(float)
    n_batches = 0
    
    for batch in loader:
        static, seq, drift, labels = [b.to(device) for b in batch]
        
        optimizer.zero_grad()
        
        # Forward
        logits, s_0, s_T = model(static, seq, drift)
        
        # Loss
        loss, comps = loss_fn(s_0, s_0.detach(), logits, labels, s_T, s_0.detach())
        
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        
        total_loss += loss.item()
        for k, v in comps.items():
            comps_sum[k] += v
        n_batches += 1
    
    if scheduler:
        scheduler.step()
    
    avg_loss = total_loss / n_batches
    avg_comps = {k: v / n_batches for k, v in comps_sum.items()}
    
    return avg_loss, avg_comps

@torch.no_grad()
def validate(model, loader, device, loss_fn=None):
    model.eval()
    all_probs, all_labels = [], []
    all_s0 = []
    
    for batch in loader:
        static, seq, drift, labels = [b.to(device) for b in batch]
        
        # Encode
        s_0 = model.encode(static)
        
        # Predict with 30-path rollout
        probs = model.predict_proba(static, seq, drift, n_paths=30)
        
        all_probs.append(probs.cpu().numpy())
        all_labels.append(labels.cpu().numpy())
        all_s0.append(s_0.cpu().numpy())
    
    probs = np.concatenate(all_probs)
    labels = np.concatenate(all_labels)
    embeddings = np.concatenate(all_s0)
    
    # Metrics
    pred_class = probs.argmax(axis=1)
    acc = (pred_class == labels).mean()
    
    # Per-class F1
    from sklearn.metrics import f1_score, classification_report
    f1_per_class = f1_score(labels, pred_class, average=None, labels=[0, 1, 2])
    
    # Draw metrics
    draw_mask = (labels == 1)
    draw_pred_mask = (pred_class == 1)
    draw_acc = (pred_class[draw_mask] == 1).mean() if draw_mask.any() else 0
    
    # Collapse check
    monitor = EmbeddingCollapseMonitor()
    collapse = monitor.check_collapse(
        torch.tensor(embeddings), torch.tensor(labels)
    )
    
    # Calibration: average draw probability vs actual draw rate
    avg_draw_prob = probs[:, 1].mean()
    actual_draw_rate = (labels == 1).mean()
    draw_cal_error = abs(avg_draw_prob - actual_draw_rate)
    
    return {
        'acc': acc,
        'f1_H': f1_per_class[0], 'f1_D': f1_per_class[1], 'f1_A': f1_per_class[2],
        'macro_f1': f1_per_class.mean(),
        'draw_acc': draw_acc,
        'draw_cal_error': draw_cal_error,
        'avg_draw_prob': avg_draw_prob,
        'actual_draw_rate': actual_draw_rate,
        'collapse': collapse,
    }

def train_model(
    model, train_loader, val_loader, device, 
    epochs=30, lr=1e-3, wd=1e-5,
    output_dir=None
):
    """Full training loop with early stopping"""
    if output_dir is None:
        output_dir = os.path.join(ROOT, 'models/jepa/checkpoints')
    os.makedirs(output_dir, exist_ok=True)
    
    loss_fn = LeCunCalibratedLoss(
        sim_coeff=25.0, var_coeff=1.5, cov_coeff=0.05,
        pred_coeff=25.0, ce_coeff=0.3,           # Lower CE, let VICReg dominate
        draw_cal_coeff=1.0,                       # v2 fix: 3.0→1.0, less aggressive
        energy_reg_coeff=0.5,                     # v2 fix: 0.2→0.5, stop overconfidence
        multi_temp=False                          # v2 fix: train at τ=1.0, calibrate after
    )
    
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=wd)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
        optimizer, T_0=10, T_mult=2, eta_min=1e-6
    )
    
    best_metric = 0  # macro_f1 + acc
    best_epoch = 0
    patience = 12
    patience_counter = 0
    history = []
    
    print(f"\n{'='*60}")
    print(f"  LeCun Calibrated JEPA Training")
    print(f"  Model: JEPALite (167K params)")
    print(f"  Loss: LeCunCalibratedLoss (VICReg+DrawCal+EnergyReg)")
    print(f"  Train: {len(train_loader.dataset)} samples")
    print(f"  Val:   {len(val_loader.dataset)} samples")
    print(f"  Epochs: {epochs} | LR: {lr} | WD: {wd}")
    print(f"{'='*60}\n")
    
    for epoch in range(1, epochs + 1):
        t0 = time.time()
        
        train_loss, train_comps = train_epoch(
            model, train_loader, optimizer, scheduler, loss_fn, device, epoch
        )
        val_metrics = validate(model, val_loader, device)
        
        elapsed = time.time() - t0
        
        # Combined metric
        combined = val_metrics['macro_f1'] * 0.5 + val_metrics['acc'] * 0.5
        
        history.append({
            'epoch': epoch,
            'train_loss': train_loss,
            **{f'train_{k}': v for k, v in train_comps.items()},
            **val_metrics,
            'combined': combined,
            'time': elapsed,
        })
        
        # Log
        c = val_metrics['collapse']
        print(f"E{epoch:2d} | Loss:{train_loss:.4f} | "
              f"Acc:{val_metrics['acc']:.3f} F1_D:{val_metrics['f1_D']:.3f} "
              f"MacroF1:{val_metrics['macro_f1']:.3f} | "
              f"CalErr:{val_metrics['draw_cal_error']:.3f} "
              f"p(D)={val_metrics['avg_draw_prob']:.2f} vs {val_metrics['actual_draw_rate']:.2f} | "
              f"{'⚠️COLLAPSE' if c['collapsed'] else 'OK'} | {elapsed:.0f}s")
        
        # Early stopping check
        if combined > best_metric + 0.001:
            best_metric = combined
            best_epoch = epoch
            patience_counter = 0
            
            # Save best model
            torch.save({
                'model': model.state_dict(),
                'optimizer': optimizer.state_dict(),
                'epoch': epoch,
                'val_acc': val_metrics['acc'],
                'val_f1_D': val_metrics['f1_D'],
                'val_macro_f1': val_metrics['macro_f1'],
                'acc': val_metrics['acc'],
            }, os.path.join(output_dir, 'best_model_lite.pt'))
            print(f"  ✓ Best model saved (combined={combined:.4f})")
        else:
            patience_counter += 1
        
        if patience_counter >= patience:
            print(f"\n  Early stopping at epoch {epoch} (best: epoch {best_epoch}, combined={best_metric:.4f})")
            break
    
    # Save history
    def _clean(v):
        if isinstance(v, (np.floating, np.integer)):
            return float(v)
        if isinstance(v, dict):
            return {k: _clean(v2) for k, v2 in v.items()}
        return v
    
    clean_history = [{k: _clean(v) for k, v in h.items() if k not in ('collapse',)} for h in history]
    
    hist_path = os.path.join(output_dir, 'training_history.json')
    with open(hist_path, 'w') as f:
        json.dump(clean_history, f, indent=2)
    
    # Print summary
    print(f"\n{'='*60}")
    print(f"  TRAINING COMPLETE")
    print(f"  Best epoch: {best_epoch} | Combined: {best_metric:.4f}")
    print(f"  History saved: {hist_path}")
    print(f"{'='*60}")
    
    return history

# ═══════════════════════════════════════════════════════════════
# World Cup Evaluation
# ═══════════════════════════════════════════════════════════════

def evaluate_worldcup(model, device):
    """Evaluate on World Cup 2026 matches"""
    from validation.validate_full_features import (
        load_stats, build_features, STATIC_72_COLS, COL_IDX
    )
    from validation.validate_full_features import MANUAL_ODDS, RESULTS_JSON as WC_JSON
    
    mean, std = load_stats()
    
    with open(os.path.join(ROOT, 'validation/wc2026_results.json'), 'r', encoding='utf-8') as f:
        matches = json.load(f)["matches"]
    
    def mk(home, away):
        return f"{home.replace(' ','').replace('-','')}_{away.replace(' ','').replace('-','')}"
    
    # Build features
    from validation.validate_full_features import build_features as bf
    features_list = []
    actuals = []
    match_info = []
    
    for m in matches:
        key = mk(m['home'], m['away'])
        if key not in MANUAL_ODDS:
            continue
        ho, do, oa = MANUAL_ODDS[key]
        feats = bf(ho, do, oa, mean, std)
        features_list.append(feats)
        actuals.append(m['result'])
        match_info.append((m['home'], m['away'], f"{m['home_score']}-{m['away_score']}"))
    
    # Predict
    model.eval()
    results = []
    
    with torch.no_grad():
        for i, feats in enumerate(features_list):
            x = torch.from_numpy(feats).unsqueeze(0).float().to(device)
            probs = model.predict_proba(x, n_paths=30).cpu().numpy()[0]
            
            # Use default threshold for now
            labels = ["H", "D", "A"]
            pred = labels[int(np.argmax(probs))]
            actual = actuals[i]
            
            results.append({
                'match': f"{match_info[i][0]} vs {match_info[i][1]}",
                'pred': pred, 'actual': actual,
                'correct': pred == actual,
                'probs': probs.tolist(),
                'score': match_info[i][2],
            })
    
    n = len(results)
    correct = sum(1 for r in results if r['correct'])
    acc = correct / n
    
    # Draw F1
    tp = sum(1 for r in results if r['pred'] == 'D' and r['actual'] == 'D')
    fp = sum(1 for r in results if r['pred'] == 'D' and r['actual'] != 'D')
    fn = sum(1 for r in results if r['pred'] != 'D' and r['actual'] == 'D')
    dp = tp / (tp + fp) if (tp + fp) > 0 else 0
    dr = tp / (tp + fn) if (tp + fn) > 0 else 0
    draw_f1 = 2 * dp * dr / (dp + dr) if (dp + dr) > 0 else 0
    
    print(f"\n  World Cup Eval:")
    print(f"  Acc={acc:.1%} ({correct}/{n}) DrawF1={draw_f1:.4f} (P={dp:.2f} R={dr:.2f})")
    
    return acc, draw_f1, results

# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    device = torch.device('cpu')
    print(f"Device: {device}")
    
    # 1. Load data
    print("\n[1/3] Loading data...")
    train_data, val_data, test_data = load_data()
    train_loader, val_loader = create_loaders(train_data, val_data, batch_size=256)
    
    # 2. Create model
    print("\n[2/3] Creating JEPALite...")
    model = JEPALite(static_dim=72, embed_dim=128)
    model.to(device)
    print(f"  Params: {sum(p.numel() for p in model.parameters()):,}")
    
    # 3. Train
    print("\n[3/3] Training...")
    history = train_model(
        model, train_loader, val_loader, device,
        epochs=30, lr=2e-3, wd=1e-5,
    )
    
    # 4. Quick World Cup evaluation
    print("\n=== WORLD CUP QUICK EVAL ===")
    wc_acc, wc_df1, wc_results = evaluate_worldcup(model, device)
    
    print("\n  Per-match:")
    for r in wc_results:
        mark = "O" if r['correct'] else "X"
        probs = r['probs']
        print(f"  {mark} {r['match']:<35} pred={r['pred']} act={r['actual']} ({r['score']}) "
              f"H={probs[0]:.1%} D={probs[1]:.1%} A={probs[2]:.1%}")

if __name__ == '__main__':
    main()
