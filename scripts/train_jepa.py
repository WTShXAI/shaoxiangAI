"""
FootballJEPA v5.0 - Training Pipeline
======================================
JEPATrainer with VICReg loss integration, collapse monitoring, and EMA.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset
from pathlib import Path
import numpy as np
from tqdm import tqdm
import json

from models.jepa_losses import FootballVICRegLoss, DrawPreservingVICReg, EmbeddingCollapseMonitor

class JEPATrainer:
    """Training pipeline for FootballJEPA"""

    def __init__(self, model, dual_encoder, loss_fn, device='cpu',
                 lr=1e-3, weight_decay=1e-5, output_dir='D:/Architecture/models/jepa/checkpoints'):
        self.model = model
        self.dual_encoder = dual_encoder
        self.loss_fn = loss_fn
        self.device = device
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.optimizer = optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
        self.scheduler = optim.lr_scheduler.CosineAnnealingWarmRestarts(self.optimizer, T_0=10, T_mult=2)

        self.collapse_monitor = EmbeddingCollapseMonitor()

        # Move to device
        self.model.to(device)
        if dual_encoder:
            self.dual_encoder.to(device)

    def train_epoch(self, loader, epoch):
        self.model.train()
        total_loss = 0
        components_sum = {}

        pbar = tqdm(loader, desc=f'Epoch {epoch}')
        for batch in pbar:
            static, seq, drift, labels = [b.to(self.device) for b in batch]

            self.optimizer.zero_grad()

            # Forward
            logits, s_0, s_T = self.model(static, seq, drift)

            # VICReg loss
            if self.dual_encoder:
                s_0_ctx, s_0_tgt = self.dual_encoder(static, seq, drift)
                loss, comps = self.loss_fn(s_0_ctx, s_0_tgt, logits, labels, z_pred=s_T, z_tgt_sg=s_0_tgt.detach())
            else:
                loss, comps = self.loss_fn(s_0, s_0.detach(), logits, labels)

            # Classification CE as auxiliary
            ce_loss = F.cross_entropy(logits, labels)
            loss = loss + 0.3 * ce_loss
            comps['ce'] = ce_loss.item()

            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)
            self.optimizer.step()

            total_loss += loss.item()
            for k, v in comps.items():
                components_sum[k] = components_sum.get(k, 0) + v

            pbar.set_postfix(loss=f'{loss.item():.4f}')

        # Update EMA
        if self.dual_encoder:
            self.dual_encoder.update_target()

        self.scheduler.step()

        # Average components
        n = len(loader)
        avg_loss = total_loss / n
        avg_comps = {k: v / n for k, v in components_sum.items()}

        return avg_loss, avg_comps

    @torch.no_grad()
    def validate(self, loader):
        self.model.eval()
        all_preds, all_labels = [], []
        all_embeddings = []

        for batch in loader:
            static, seq, drift, labels = [b.to(self.device) for b in batch]
            s_0 = self.model.encode(static, seq, drift)
            proba = self.model.predict_proba(static, seq, drift, n_paths=30)

            all_preds.append(proba.cpu().numpy())
            all_labels.append(labels.cpu().numpy())
            all_embeddings.append(s_0.cpu().numpy())

        preds = np.concatenate(all_preds)
        labels = np.concatenate(all_labels)
        embeddings = np.concatenate(all_embeddings)

        # Metrics
        pred_class = preds.argmax(axis=1)
        acc = (pred_class == labels).mean()

        # Per-class F1
        from sklearn.metrics import f1_score
        f1_per_class = f1_score(labels, pred_class, average=None, labels=[0, 1, 2])

        # Collapse check
        collapse = self.collapse_monitor.check_collapse(
            torch.tensor(embeddings), torch.tensor(labels)
        )

        return {
            'acc': acc,
            'f1_H': f1_per_class[0],
            'f1_D': f1_per_class[1],
            'f1_A': f1_per_class[2],
            'collapse': collapse,
        }

    def train(self, train_loader, val_loader, epochs=50, early_stop_patience=10):
        best_acc = 0
        patience_counter = 0
        history = []

        for epoch in range(1, epochs + 1):
            train_loss, comps = self.train_epoch(train_loader, epoch)
            val_metrics = self.validate(val_loader)

            history.append({
                'epoch': epoch,
                'train_loss': train_loss,
                'val_acc': val_metrics['acc'],
                'val_f1_D': val_metrics['f1_D'],
                'collapse_warning': val_metrics['collapse']['collapsed'],
            })

            print(f"Epoch {epoch:3d} | Train Loss: {train_loss:.4f} | "
                  f"Val Acc: {val_metrics['acc']:.3f} | "
                  f"F1_D: {val_metrics['f1_D']:.3f} | "
                  f"Collapse: {val_metrics['collapse']['msg']}")

            if val_metrics['collapse']['collapsed']:
                print(f"  WARNING: EMBEDDING COLLAPSE DETECTED - consider increasing VICReg var_coeff")

            if val_metrics['acc'] > best_acc:
                best_acc = val_metrics['acc']
                patience_counter = 0
                torch.save({
                    'model': self.model.state_dict(),
                    'optimizer': self.optimizer.state_dict(),
                    'epoch': epoch,
                    'val_acc': val_metrics['acc'],
                }, self.output_dir / 'best_model.pt')
            else:
                patience_counter += 1

            if patience_counter >= early_stop_patience:
                print(f"Early stopping at epoch {epoch}")
                break

        # Save history (convert numpy types to native Python)
        def _to_native(v):
            if hasattr(v, 'item'): return v.item()
            return v
        clean_history = [{k: _to_native(v) for k, v in h.items()} for h in history]
        with open(self.output_dir / 'training_history.json', 'w') as f:
            json.dump(clean_history, f, indent=2)

        return history

def load_jepa_data(split='train', data_dir='D:/Architecture/data'):
    """Load pre-built JEPA dataset"""
    data = np.load(Path(data_dir) / f'jepa_{split}.npz')
    return data['static'], data['sequence'], data['drift'], data['labels']

def create_dataloaders(batch_size=256, num_workers=0):
    """Create train/val dataloaders"""
    static_tr, seq_tr, drift_tr, labels_tr = load_jepa_data('train')
    static_val, seq_val, drift_val, labels_val = load_jepa_data('val')

    train_ds = TensorDataset(*[torch.tensor(x) for x in [static_tr, seq_tr, drift_tr, labels_tr]])
    val_ds = TensorDataset(*[torch.tensor(x) for x in [static_val, seq_val, drift_val, labels_val]])

    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True, num_workers=num_workers)
    val_loader = DataLoader(val_ds, batch_size=batch_size, shuffle=False, num_workers=num_workers)

    return train_loader, val_loader

if __name__ == '__main__':
    # Quick test
    print("Testing VICReg loss...")
    loss_fn = DrawPreservingVICReg()
    B, D = 32, 128
    z_ctx = torch.randn(B, D)
    z_tgt = torch.randn(B, D)
    logits = torch.randn(B, 3)
    labels = torch.randint(0, 3, (B,))

    loss, comps = loss_fn(z_ctx, z_tgt, logits, labels)
    print(f"Total loss: {loss.item():.4f}")
    for k, v in comps.items():
        print(f"  {k}: {v:.4f}")
    print("VICReg test passed")
