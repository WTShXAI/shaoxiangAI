#!/usr/bin/env python3
"""
哨响AI 神经网络模型训练器 v1.0

基于PyTorch的足球比赛预测神经网络，72维特征输入 → 3维概率输出(H/D/A)。
使用与 ensemble_trainer 相同的数据管道和特征工程，确保特征空间对齐。

架构: 72 → 256 → 128 → 64 → 3 (ReLU + Dropout + BatchNorm + Softmax)
训练策略: Early Stopping, ReduceLROnPlateau, Class Weights, 时序交叉验证

输出: saved_models/football_nn_{timestamp}.pth
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timezone
from typing import Tuple, Dict, Optional

import numpy as np
import pandas as pd
import joblib

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset, WeightedRandomSampler
from sklearn.metrics import (
    accuracy_score, roc_auc_score, matthews_corrcoef,
    classification_report, confusion_matrix, f1_score
)
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler

# ── 路径设置 ──
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('FootballNN')

class FootballNN(nn.Module):
    """足球预测神经网络：72维 → 多层 → 3维(HDA概率)"""

    def __init__(
        self,
        input_dim: int = 72,
        hidden_dims: list = None,
        dropout: float = 0.35,
        activation: str = 'relu',
        use_batch_norm: bool = True,
    ):
        super().__init__()
        if hidden_dims is None:
            hidden_dims = [256, 128, 64]

        layers = []
        prev_dim = input_dim

        act_fn = nn.ReLU() if activation == 'relu' else nn.GELU()

        for i, h_dim in enumerate(hidden_dims):
            layers.append(nn.Linear(prev_dim, h_dim))
            if use_batch_norm:
                layers.append(nn.BatchNorm1d(h_dim))
            layers.append(act_fn)
            layers.append(nn.Dropout(dropout * (0.7 ** i)))  # 深层dropout递减
            prev_dim = h_dim

        # 输出层
        layers.append(nn.Linear(prev_dim, 3))
        # 注意：不在这里加Softmax，用CrossEntropyLoss自带

        self.network = nn.Sequential(*layers)
        self._init_weights()

    def _init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.kaiming_normal_(m.weight, mode='fan_in', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm1d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

    def forward(self, x):
        return self.network(x)

    def predict_proba(self, x):
        """返回 H/D/A 概率"""
        self.eval()
        with torch.no_grad():
            logits = self.forward(x)
            return torch.softmax(logits, dim=1).cpu().numpy()

    def predict(self, x):
        """返回预测类别 0/1/2"""
        proba = self.predict_proba(x)
        return np.argmax(proba, axis=1)

def load_training_data() -> Tuple[np.ndarray, np.ndarray, StandardScaler]:
    """
    使用 EnsembleTrainer 的数据管道加载训练数据。
    返回 (X, y, scaler)，与生产模型的特征空间完全对齐。
    """
    logger.info("加载训练数据（使用EnsembleTrainer管道）...")

    # 导入并初始化EnsembleTrainer
    from ensemble_trainer import EnsembleTrainer

    trainer = EnsembleTrainer()

    # 加载原始数据
    df = trainer.load_training_data()
    logger.info(f"原始数据: {len(df)} 条")

    # 扩展训练集（可选，312K条）
    if trainer.config['data']['extended_training'].get('enabled', True):
        try:
            df_ext = trainer.load_extended_odds_data()
            if len(df_ext) > 0:
                # 只使用扩展训练集作为赔率专家模型的训练数据
                # 神经网络用主训练集
                logger.info(f"扩展训练集: {len(df_ext)} 条（不参与NN训练）")
        except Exception as e:
            logger.warning("加载扩展训练集失败: %s", e)

    # 特征工程
    X, y = trainer.prepare_features(df, add_interactions=True)

    logger.info(f"特征矩阵: {X.shape}")
    logger.info(f"标签分布: H={int((y==0).sum())}, D={int((y==1).sum())}, A={int((y==2).sum())}")

    # 标准化
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X.values)

    return X_scaled, y.values, scaler, list(X.columns)

def temporal_train_test_split(
    X: np.ndarray, y: np.ndarray, test_ratio: float = 0.10
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """时序分割：前90%训练，后10%测试"""
    split_idx = int(len(X) * (1 - test_ratio))
    return X[:split_idx], X[split_idx:], y[:split_idx], y[split_idx:]

def compute_class_weights(y: np.ndarray) -> torch.Tensor:
    """计算类别权重（平衡不平衡数据）"""
    unique, counts = np.unique(y, return_counts=True)
    total = len(y)
    weights = total / (len(unique) * counts)
    return torch.tensor(weights, dtype=torch.float32)

def train_epoch(
    model: nn.Module,
    loader: DataLoader,
    optimizer: optim.Optimizer,
    criterion: nn.Module,
    device: torch.device,
) -> float:
    model.train()
    total_loss = 0.0
    for batch_x, batch_y in loader:
        batch_x, batch_y = batch_x.to(device), batch_y.to(device)
        optimizer.zero_grad()
        logits = model(batch_x)
        loss = criterion(logits, batch_y)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
        optimizer.step()
        total_loss += loss.item()
    return total_loss / len(loader)

@torch.no_grad()
def evaluate(
    model: nn.Module,
    loader: DataLoader,
    criterion: nn.Module,
    device: torch.device,
) -> Dict:
    model.eval()
    all_logits = []
    all_labels = []
    total_loss = 0.0

    for batch_x, batch_y in loader:
        batch_x, batch_y = batch_x.to(device), batch_y.to(device)
        logits = model(batch_x)
        loss = criterion(logits, batch_y)
        total_loss += loss.item()
        all_logits.append(logits.cpu())
        all_labels.append(batch_y.cpu())

    logits = torch.cat(all_logits)
    labels = torch.cat(all_labels)
    probs = torch.softmax(logits, dim=1).numpy()
    preds = torch.argmax(logits, dim=1).numpy()
    labels_np = labels.numpy()

    # 指标
    acc = accuracy_score(labels_np, preds)

    # AUC (macro)
    try:
        auc = roc_auc_score(labels_np, probs, multi_class='ovr', average='macro')
    except (ValueError, KeyError, IndexError):
        auc = 0.5

    mcc = matthews_corrcoef(labels_np, preds)

    # 各类F1
    f1_per = f1_score(labels_np, preds, average=None, zero_division=0)
    f1_macro = f1_score(labels_np, preds, average='macro', zero_division=0)

    return {
        'loss': total_loss / len(loader),
        'accuracy': acc * 100,
        'auc': auc,
        'mcc': mcc,
        'f1_h': f1_per[0] if len(f1_per) > 0 else 0,
        'f1_d': f1_per[1] if len(f1_per) > 1 else 0,
        'f1_a': f1_per[2] if len(f1_per) > 2 else 0,
        'f1_macro': f1_macro,
        'n_samples': len(labels_np),
    }

def train_model(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_test: np.ndarray,
    y_test: np.ndarray,
    config: Dict = None,
) -> Tuple[FootballNN, Dict, StandardScaler]:
    """训练主函数"""
    if config is None:
        config = {}

    # v1.1: 强制CPU（CUDA 12.4与GPU驱动不兼容，RuntimeError: no kernel image available）
    device = torch.device('cpu')
    logger.info(f"使用设备: {device} (GPU不可用时自动回退CPU)")

    input_dim = X_train.shape[1]
    logger.info(f"输入维度: {input_dim}, 训练样本: {len(X_train)}, 测试样本: {len(X_test)}")

    # 模型配置
    hidden_dims = config.get('hidden_dims', [256, 128, 64])
    dropout = config.get('dropout', 0.35)
    lr = config.get('learning_rate', 0.001)
    batch_size = config.get('batch_size', 128)
    epochs = config.get('epochs', 200)
    early_stop_patience = config.get('early_stop_patience', 30)
    lr_patience = config.get('lr_patience', 10)
    weight_decay = config.get('weight_decay', 1e-4)

    # 创建 DataLoader
    train_dataset = TensorDataset(
        torch.tensor(X_train, dtype=torch.float32),
        torch.tensor(y_train, dtype=torch.long)
    )
    test_dataset = TensorDataset(
        torch.tensor(X_test, dtype=torch.float32),
        torch.tensor(y_test, dtype=torch.long)
    )

    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, drop_last=True)
    test_loader = DataLoader(test_dataset, batch_size=batch_size * 2, shuffle=False)

    # 模型
    model = FootballNN(
        input_dim=input_dim,
        hidden_dims=hidden_dims,
        dropout=dropout,
    ).to(device)

    n_params = sum(p.numel() for p in model.parameters())
    logger.info(f"模型参数: {n_params:,}")

    # 类别权重
    class_weights = compute_class_weights(y_train).to(device)
    criterion = nn.CrossEntropyLoss(weight=class_weights)

    # 优化器
    optimizer = optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)

    # 调度器
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode='min', factor=0.5, patience=lr_patience, verbose=True
    )

    # 训练循环
    best_val_loss = float('inf')
    best_metrics = None
    patience_counter = 0
    train_losses = []
    val_losses = []

    logger.info(f"开始训练 ({epochs} epochs)...")

    for epoch in range(epochs):
        train_loss = train_epoch(model, train_loader, optimizer, criterion, device)
        train_losses.append(train_loss)

        val_metrics = evaluate(model, test_loader, criterion, device)
        val_losses.append(val_metrics['loss'])

        scheduler.step(val_metrics['loss'])

        if val_metrics['loss'] < best_val_loss:
            best_val_loss = val_metrics['loss']
            best_metrics = val_metrics
            patience_counter = 0
            # 保存最佳模型
            best_state = {
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'input_dim': input_dim,
                'hidden_dims': hidden_dims,
                'dropout': dropout,
                'metrics': val_metrics,
            }
        else:
            patience_counter += 1

        if epoch % 10 == 0 or epoch == epochs - 1:
            logger.info(
                f"Epoch {epoch:3d}/{epochs} | "
                f"train_loss: {train_loss:.4f} | val_loss: {val_metrics['loss']:.4f} | "
                f"acc: {val_metrics['accuracy']:.1f}% | auc: {val_metrics['auc']:.4f} | "
                f"mcc: {val_metrics['mcc']:.4f} | D_f1: {val_metrics['f1_d']:.4f}"
            )

        if patience_counter >= early_stop_patience:
            logger.info(f"Early stopping at epoch {epoch + 1}")
            break

    # 恢复最佳模型
    if best_state:
        model.load_state_dict(best_state['model_state_dict'])

    return model, best_metrics, best_state

def run_temporal_cv(
    X: np.ndarray,
    y: np.ndarray,
    config: Dict = None,
    n_splits: int = 5,
) -> list:
    """时序交叉验证"""
    tscv = TimeSeriesSplit(n_splits=n_splits)
    results = []

    logger.info(f"\n{'='*60}")
    logger.info(f"时序交叉验证 ({n_splits}-fold)")
    logger.info(f"{'='*60}")

    for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
        X_tr, X_val = X[train_idx], X[val_idx]
        y_tr, y_val = y[train_idx], y[val_idx]

        logger.info(f"\n--- Fold {fold + 1}/{n_splits}: train={len(X_tr)}, val={len(X_val)} ---")
        model, metrics, _ = train_model(X_tr, y_tr, X_val, y_val, config)
        results.append(metrics)

        logger.info(f"Fold {fold + 1}: acc={metrics['accuracy']:.1f}%, auc={metrics['auc']:.4f}, "
                    f"D_f1={metrics['f1_d']:.4f}")

    # 汇总
    accs = [r['accuracy'] for r in results]
    aucs = [r['auc'] for r in results]
    mccs = [r['mcc'] for r in results]
    d_f1s = [r['f1_d'] for r in results]

    logger.info(f"\n{'='*60}")
    logger.info(f"CV汇总 ({n_splits}-fold)")
    logger.info(f"  Accuracy:  {np.mean(accs):.2f}% ± {np.std(accs):.2f}%")
    logger.info(f"  AUC:       {np.mean(aucs):.4f} ± {np.std(aucs):.4f}")
    logger.info(f"  MCC:       {np.mean(mccs):.4f} ± {np.std(mccs):.4f}")
    logger.info(f"  Draw F1:   {np.mean(d_f1s):.4f} ± {np.std(d_f1s):.4f}")
    logger.info(f"{'='*60}")

    return results

def save_model(model: FootballNN, state: Dict, scaler: StandardScaler, feature_names: list, output_dir: str) -> str:
    """保存模型到文件"""
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    filename = f'football_nn_{timestamp}.pth'
    filepath = os.path.join(output_dir, filename)

    # 保存完整包
    save_pkg = {
        **state,
        'model_state_dict': model.state_dict(),
        'feature_names': feature_names,
        'scaler_mean': scaler.mean_.tolist(),
        'scaler_scale': scaler.scale_.tolist(),
        'train_date': timestamp,
    }
    torch.save(save_pkg, filepath)
    logger.info(f"模型已保存: {filepath}")

    # 同时保存为 joblib 格式（兼容现有加载器）
    jl_path = filepath.replace('.pth', '.joblib')
    joblib.dump({
        'nn_model_path': filepath,
        'input_dim': state['input_dim'],
        'hidden_dims': state['hidden_dims'],
        'feature_names': feature_names,
        'scaler': scaler,
        'metrics': state['metrics'],
        'train_date': timestamp,
    }, jl_path)
    logger.info(f"兼容文件已保存: {jl_path}")

    return filepath

def compare_with_ensemble(nn_metrics: Dict, ensemble_metrics: Dict) -> str:
    """对比NN与集成模型"""
    lines = []
    lines.append("=" * 60)
    lines.append("神经网络 vs 集成模型 对比")
    lines.append("=" * 60)

    compare_keys = [
        ('accuracy', 'Accuracy (%)', 'acc'),
        ('auc', 'AUC', 'auc'),
        ('mcc', 'MCC', 'mcc'),
        ('f1_h', 'H F1', 'h_f1'),
        ('f1_d', 'D F1', 'd_f1'),
        ('f1_a', 'A F1', 'a_f1'),
    ]

    lines.append(f"{'指标':<15} {'神经网络':<12} {'集成模型':<12} {'差异':<10}")
    lines.append("-" * 49)

    for nn_k, label, _ in compare_keys:
        nn_v = nn_metrics.get(nn_k, 0)
        # ensemble_metrics from production model
        if label == 'Accuracy (%)':
            ens_v = ensemble_metrics.get('accuracy', 0)
        elif label == 'AUC':
            ens_v = ensemble_metrics.get('auc_macro', 0)
        elif label == 'MCC':
            ens_v = ensemble_metrics.get('mcc', 0)
        elif label == 'D F1':
            ens_v = ensemble_metrics.get('per_class', {}).get('D', {}).get('f1', 0)
        elif label == 'H F1':
            ens_v = ensemble_metrics.get('per_class', {}).get('H', {}).get('f1', 0)
        elif label == 'A F1':
            ens_v = ensemble_metrics.get('per_class', {}).get('A', {}).get('f1', 0)
        else:
            ens_v = ensemble_metrics.get(nn_k, 0)

        diff = nn_v - ens_v
        sign = '+' if diff > 0 else ''
        lines.append(f"{label:<15} {nn_v:<12.4f} {ens_v:<12.4f} {sign}{diff:<9.4f}")

    lines.append("=" * 60)
    return '\n'.join(lines)

def main():
    logger.info("=" * 60)
    logger.info("哨响AI 神经网络模型训练器 v1.0")
    logger.info("=" * 60)

    # 训练配置
    train_config = {
        'hidden_dims': [256, 128, 64],
        'dropout': 0.35,
        'learning_rate': 0.001,
        'batch_size': 128,
        'epochs': 200,
        'early_stop_patience': 30,
        'lr_patience': 10,
        'weight_decay': 1e-4,
    }

    # 1. 加载数据
    X, y, scaler, feature_names = load_training_data()
    logger.info(f"特征数: {len(feature_names)}, 标签: 0=H, 1=D, 2=A")

    # 2. 时序分割
    X_train, X_test, y_train, y_test = temporal_train_test_split(X, y)
    logger.info(f"训练集: {len(X_train)}, 测试集: {len(X_test)}")

    # 3. 训练最终模型
    logger.info("\n训练最终模型...")
    model, nn_metrics, best_state = train_model(X_train, y_train, X_test, y_test, train_config)

    logger.info("\n最终模型测试集指标:")
    logger.info(f"  Accuracy:  {nn_metrics['accuracy']:.2f}%")
    logger.info(f"  AUC:       {nn_metrics['auc']:.4f}")
    logger.info(f"  MCC:       {nn_metrics['mcc']:.4f}")
    logger.info(f"  H F1:      {nn_metrics['f1_h']:.4f}")
    logger.info(f"  D F1:      {nn_metrics['f1_d']:.4f}")
    logger.info(f"  A F1:      {nn_metrics['f1_a']:.4f}")
    logger.info(f"  Macro F1:  {nn_metrics['f1_macro']:.4f}")

    # 4. 交叉验证
    cv_results = run_temporal_cv(X, y, train_config)

    # 5. 与集成模型对比
    logger.info("\n加载集成模型对比...")
    try:
        ens_model = joblib.load(os.path.join(ROOT, 'saved_models', 'football_balanced_production.joblib'))
        ens_metrics = ens_model.get('eval_metrics', {})
        comparison = compare_with_ensemble(nn_metrics, ens_metrics)
        logger.info('\n' + comparison)
    except (FileNotFoundError, KeyError, TypeError) as e:
        logger.warning(f"无法加载集成模型进行对比: {e}")

    # 6. 保存模型
    output_dir = os.path.join(ROOT, 'saved_models')
    os.makedirs(output_dir, exist_ok=True)
    save_path = save_model(model, best_state, scaler, feature_names, output_dir)

    # 7. 保存训练报告
    report = {
        'train_config': train_config,
        'final_metrics': nn_metrics,
        'cv_results': [
            {k: float(v) if isinstance(v, (np.floating, float)) else v for k, v in r.items()}
            for r in cv_results
        ],
        'feature_count': len(feature_names),
        'feature_names': feature_names,
        'data_info': {
            'n_samples': len(y),
            'n_train': len(X_train),
            'n_test': len(X_test),
            'label_distribution': {'H': int((y == 0).sum()), 'D': int((y == 1).sum()), 'A': int((y == 2).sum())},
        },
        'model_path': save_path,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }
    report_path = os.path.join(output_dir, 'nn_training_report.json')
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    logger.info(f"训练报告已保存: {report_path}")

    logger.info("\n训练完成!")
    return model, nn_metrics, cv_results

if __name__ == '__main__':
    main()
