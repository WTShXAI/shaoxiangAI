#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FootballNN — 足球三分类神经网络模型

架构: 72 → 256 → 128 → 64 → 3 (H/D/A)
- 3层全连接 + BatchNorm + ReLU + Dropout(0.35)
- 输出: raw logits (需softmax获取概率)
- 训练指标: Acc≈54.7%, D-F1≈0.356, AUC≈0.723
"""
import torch
import torch.nn as nn
import torch.nn.functional as F


class FootballNN(nn.Module):
    """
    三分类足球预测神经网络

    Architecture (from football_nn_20260616_125617.pth):
        input (72) → Linear(256) → BN → ReLU → Dropout(0.35)
                   → Linear(128) → BN → ReLU → Dropout(0.35)
                   → Linear(64)  → BN → ReLU → Dropout(0.35)
                   → Linear(3)   → output logits (H/D/A)

    Parameters
    ----------
    input_dim : int, default 72
        输入特征维度
    hidden_dims : list, default [256, 128, 64]
        隐藏层维度
    dropout : float, default 0.35
        Dropout 比率
    """

    def __init__(self, input_dim=72, hidden_dims=None, dropout=0.35):
        super().__init__()
        if hidden_dims is None:
            hidden_dims = [256, 128, 64]

        layers = []
        prev_dim = input_dim

        for h_dim in hidden_dims:
            layers.append(nn.Linear(prev_dim, h_dim))
            layers.append(nn.BatchNorm1d(h_dim))
            layers.append(nn.ReLU(inplace=True))
            layers.append(nn.Dropout(dropout))
            prev_dim = h_dim

        # 输出层: 3-class logits (H, D, A)
        layers.append(nn.Linear(prev_dim, 3))

        self.network = nn.Sequential(*layers)

    def forward(self, x):
        """
        Parameters
        ----------
        x : torch.Tensor, shape (batch_size, input_dim)

        Returns
        -------
        logits : torch.Tensor, shape (batch_size, 3)
            原始logits, 需 softmax 获取概率
        """
        return self.network(x)

    def predict_proba(self, x):
        """
        返回 softmax 概率

        Returns
        -------
        probs : torch.Tensor, shape (batch_size, 3)
            [P(H), P(D), P(A)]
        """
        self.eval()
        with torch.no_grad():
            logits = self.forward(x)
            return F.softmax(logits, dim=-1)

    def predict(self, x):
        """
        返回预测类别

        Returns
        -------
        class_idx : torch.Tensor, shape (batch_size,)
            0=H, 1=D, 2=A
        """
        probs = self.predict_proba(x)
        return probs.argmax(dim=-1)


# ═══════════════════════════════════════
# 训练函数 (供 standalone 训练使用)
# ═══════════════════════════════════════

class FootballNNTrainer:
    """NN训练器 — 用于独立训练脚本"""

    def __init__(self, model, device=None, class_weights=None, lr=1e-3):
        self.model = model
        self.device = device or torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.model.to(self.device)

        if class_weights is not None:
            class_weights = torch.tensor(class_weights, dtype=torch.float32).to(self.device)
        self.criterion = nn.CrossEntropyLoss(weight=class_weights)
        self.optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
        self.scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            self.optimizer, mode='min', factor=0.5, patience=10
        )

    def train_epoch(self, train_loader):
        self.model.train()
        total_loss, correct, total = 0, 0, 0
        for X_batch, y_batch in train_loader:
            X_batch = X_batch.to(self.device)
            y_batch = y_batch.to(self.device)
            self.optimizer.zero_grad()
            logits = self.model(X_batch)
            loss = self.criterion(logits, y_batch)
            loss.backward()
            self.optimizer.step()
            total_loss += loss.item() * X_batch.size(0)
            preds = logits.argmax(dim=-1)
            correct += (preds == y_batch).sum().item()
            total += X_batch.size(0)
        return total_loss / total, correct / total

    def validate(self, val_loader):
        self.model.eval()
        total_loss, correct, total = 0, 0, 0
        with torch.no_grad():
            for X_batch, y_batch in val_loader:
                X_batch = X_batch.to(self.device)
                y_batch = y_batch.to(self.device)
                logits = self.model(X_batch)
                loss = self.criterion(logits, y_batch)
                total_loss += loss.item() * X_batch.size(0)
                preds = logits.argmax(dim=-1)
                correct += (preds == y_batch).sum().item()
                total += X_batch.size(0)
        return total_loss / total, correct / total


if __name__ == '__main__':
    # Quick test
    model = FootballNN(input_dim=72)
    x = torch.randn(4, 72)
    logits = model(x)
    probs = model.predict_proba(x)
    print(f'Input:  {list(x.shape)}')
    print(f'Logits: {list(logits.shape)}  {logits[:2]}')
    print(f'Probs:  {list(probs.shape)}  {probs[:2]}')
    print(f'Preds:  {model.predict(x)}')
    print(f'Params: {sum(p.numel() for p in model.parameters()):,}')
