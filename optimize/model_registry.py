"""
哨响AI - 模型注册表 v1.0
======================
跟踪所有已训练的模型版本及其评估指标，
支持 A/B 测试、模型回滚、部署标记。

使用:
    registry = ModelRegistry()
    registry.register(model_path, metrics)          # 注册新模型
    registry.list_models(status='active')           # 列出活跃模型
    registry.deploy(model_id)                       # 部署到生产
    registry.get_best_by_metric('accuracy')         # 获取最佳模型
"""
import os, json, logging, shutil
from datetime import datetime
from typing import Dict, List, Optional, Any
from collections import defaultdict

logger = logging.getLogger('ModelRegistry')


class ModelRegistry:
    """模型版本注册表 — 持久化为 JSON"""

    def __init__(self, registry_path: str = None):
        if registry_path is None:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            registry_path = os.path.join(root, 'saved_models', 'model_registry.json')

        self.registry_path = registry_path
        self._data = self._load()

    def _load(self) -> Dict:
        """加载注册表，自动适配两种格式"""
        if os.path.exists(self.registry_path):
            try:
                with open(self.registry_path, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                # 检测格式：ensemble_trainer 格式 (有 versions 列表)
                if 'versions' in raw:
                    return self._normalize_v1(raw)
                # ModelRegistry 原生格式 (有 models 字典)
                if 'models' in raw:
                    return raw
                logger.warning(f"注册表格式未知，创建新注册表")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"注册表读取失败: {e}，创建新注册表")
        return self._empty_registry()

    def _empty_registry(self) -> Dict:
        return {
            'version': '1.0',
            'created_at': datetime.now().isoformat(),
            'updated_at': datetime.now().isoformat(),
            'models': {},
            'current_production': None,
            'next_id': 1,
        }

    def _normalize_v1(self, raw: Dict) -> Dict:
        """将 ensemble_trainer 的 {active, current, versions} 格式转为内部格式"""
        registry = self._empty_registry()
        seen_prod = False

        for i, entry in enumerate(raw.get('versions', [])):
            model_id = f"v{i+1:04d}"
            ver = entry.get('version', f'{i+1}')
            registry['models'][model_id] = {
                'model_id': model_id,
                'model_path': entry.get('model_path', ''),
                'model_type': 'ensemble',
                'source': 'auto_pipeline',
                'status': 'active',
                'version': ver,
                'registered_at': entry.get('timestamp', registry['created_at']),
                'metrics': {
                    'accuracy': entry.get('accuracy'),
                    'draw_f1': entry.get('draw_f1'),
                    'auc': entry.get('auc'),
                    'mcc': entry.get('mcc'),
                    'n_features': entry.get('n_features'),
                },
                'tags': entry.get('models', []),
                'stacking': entry.get('stacking', False),
            }
            # 标记 production: 匹配 current 条目
            current = raw.get('current', {})
            active = raw.get('active', '')
            if not seen_prod and (
                (current and entry.get('version') == current.get('version'))
                or (active and entry.get('version') == active)
            ):
                registry['models'][model_id]['status'] = 'production'
                registry['current_production'] = model_id
                seen_prod = True

        registry['next_id'] = len(raw.get('versions', [])) + 1
        registry['updated_at'] = datetime.now().isoformat()
        logger.info(f"注册表已适配: {len(registry['models'])} 个模型")
        return registry

    def _save(self):
        """持久化注册表 — 写入 ensemble_trainer 兼容格式"""
        self._data['updated_at'] = datetime.now().isoformat()
        os.makedirs(os.path.dirname(self.registry_path), exist_ok=True)

        # 转换为 ensemble_trainer 兼容格式
        models = self._data.get('models', {})
        versions = []
        for mid in sorted(models.keys()):
            m = models[mid]
            versions.append({
                'version': m.get('version', mid),
                'timestamp': m.get('registered_at', ''),
                'accuracy': m.get('metrics', {}).get('accuracy'),
                'auc': m.get('metrics', {}).get('auc'),
                'mcc': m.get('metrics', {}).get('mcc'),
                'draw_f1': m.get('metrics', {}).get('draw_f1'),
                'n_features': m.get('metrics', {}).get('n_features'),
                'models': m.get('tags', []),
                'stacking': m.get('stacking', False),
            })

        output = {
            'active': self._data.get('current_production', ''),
            'current': versions[-1] if versions else {},
            'versions': versions,
        }

        # 先写临时文件，再原子替换
        tmp_path = self.registry_path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp_path, self.registry_path)

    # ══════════════════════════════════════════════════
    # 注册与查询
    # ══════════════════════════════════════════════════

    def register(
        self, model_path: str, metrics: Dict[str, Any],
        model_type: str = 'ensemble',
        source: str = 'manual',
        tags: List[str] = None,
        **extra,
    ) -> str:
        """
        注册一个新模型。

        Args:
            model_path: 模型文件路径
            metrics: 评估指标字典 (accuracy, draw_f1, brier, mcc, etc.)
            model_type: 'ensemble' / 'xgboost' / 'ridge' / 'lightgbm'
            source: 'step4' / 'optuna' / 'auto_pipeline' / 'manual'
            tags: 自定义标签
            **extra: 额外字段 (semver, description, training_data_info 等)

        Returns:
            model_id: 模型唯一标识
        """
        model_id = f"v{self._data['next_id']:04d}"
        self._data['next_id'] += 1

        record = {
            'model_id': model_id,
            'model_path': model_path,
            'model_type': model_type,
            'source': source,
            'tags': tags or [],
            'registered_at': datetime.now().isoformat(),
            'file_exists': os.path.exists(model_path),
            'file_size_mb': round(os.path.getsize(model_path) / (1024*1024), 2) if os.path.exists(model_path) else 0,
            'status': 'active',  # active / deprecated / error
            'metrics': {
                'accuracy': metrics.get('accuracy', 0),
                'draw_f1': metrics.get('draw_f1', 0),
                'draw_recall': metrics.get('draw_recall', 0),
                'brier': metrics.get('brier', 0),
                'log_loss': metrics.get('log_loss', 0),
                'mcc': metrics.get('mcc', 0),
                'test_samples': metrics.get('test_samples', 0),
                'n_features': metrics.get('n_features', 0),
            },
            'ensemble_weights': metrics.get('ensemble_weights'),
            'custom_params': metrics.get('custom_params'),
        }

        # 吸收额外字段 (semver, description, training_data_info 等)
        if 'semver' in extra:
            record['semver'] = extra['semver']
        if 'description' in extra:
            record['description'] = extra['description']
        if 'training_data_info' in extra:
            record['training_data_info'] = extra['training_data_info']

        self._data['models'][model_id] = record
        self._save()

        logger.info(f"模型注册: {model_id} | 准确率={metrics.get('accuracy',0):.1f}% "
                     f"平局F1={metrics.get('draw_f1',0):.1f}% | {model_path}")

        return model_id

    def get(self, model_id: str) -> Optional[Dict]:
        """获取模型记录"""
        return self._data['models'].get(model_id)

    def list_models(
        self, status: str = None, model_type: str = None,
        sort_by: str = 'registered_at', descending: bool = True,
        limit: int = 20,
    ) -> List[Dict]:
        """
        列出模型。

        Args:
            status: 过滤状态 ('active' / 'deprecated' / None=全部)
            model_type: 过滤类型
            sort_by: 排序字段 ('accuracy' / 'draw_f1' / 'registered_at' / 'brier')
            descending: 是否降序
            limit: 最大返回数
        """
        models = list(self._data['models'].values())

        if status:
            models = [m for m in models if m['status'] == status]
        if model_type:
            models = [m for m in models if m['model_type'] == model_type]

        if sort_by.startswith('metrics.'):
            key = lambda m: m['metrics'].get(sort_by.split('.')[1], 0)
        elif sort_by == 'registered_at':
            key = lambda m: m['registered_at']
        else:
            key = lambda m: m['metrics'].get(sort_by, 0)

        models.sort(key=key, reverse=descending)
        return models[:limit]

    def get_production_version(self) -> Optional[Dict]:
        """返回当前生产模型记录（无则返回 None）"""
        prod_id = self._data.get('current_production')
        if not prod_id:
            return None
        return self._data['models'].get(prod_id)

    def get_best_by_metric(self, metric: str = 'accuracy') -> Optional[Dict]:
        """获取指定指标最优的活跃模型"""
        active = self.list_models(status='active', sort_by=metric, descending=True)
        return active[0] if active else None

    # ══════════════════════════════════════════════════
    # 部署与生命周期
    # ══════════════════════════════════════════════════

    def deploy(self, model_id: str) -> bool:
        """
        将模型标记为生产版本。
        复制到 production 目录，供 prediction_service 加载。
        """
        model = self.get(model_id)
        if not model:
            logger.error(f"模型 {model_id} 不存在")
            return False

        # 更新状态
        old_prod = self._data.get('current_production')
        if old_prod and old_prod in self._data['models']:
            self._data['models'][old_prod]['status'] = 'active'

        model['status'] = 'production'
        model['deployed_at'] = datetime.now().isoformat()
        self._data['current_production'] = model_id

        # 复制到生产路径
        prod_dir = os.path.join(os.path.dirname(self.registry_path), 'production')
        os.makedirs(prod_dir, exist_ok=True)
        prod_path = os.path.join(prod_dir, f"football_model_v{model_id[1:]}.joblib")

        if os.path.exists(model['model_path']):
            shutil.copy2(model['model_path'], prod_path)
            logger.info(f"部署完成: {model_id} → {prod_path}")

        self._save()
        return True

    def rollback(self) -> Optional[str]:
        """回滚到上一个生产版本"""
        current = self._data.get('current_production')
        active_models = self.list_models(
            status='active', sort_by='registered_at', descending=True
        )

        if not active_models:
            logger.warning("无可用回滚版本")
            return None

        prev = active_models[0]['model_id']
        logger.info(f"回滚: {current} → {prev}")
        self.deploy(prev)
        return prev

    def auto_promote(self, min_accuracy_gain: float = 0.5) -> Optional[str]:
        """
        自动晋升最优模型到生产（如果它显著优于当前生产版本）
        """
        prod_id = self._data.get('current_production')
        best = self.get_best_by_metric('accuracy')
        if not best:
            logger.info("无模型可晋升")
            return None

        if prod_id and prod_id in self._data['models']:
            prod_acc = self._data['models'][prod_id]['metrics'].get('accuracy', 0)
            gain = best['metrics']['accuracy'] - prod_acc
            if gain < min_accuracy_gain:
                logger.info(f"增益不足: +{gain:.2f}% < {min_accuracy_gain}%")
                return None
            logger.info(f"自动晋升: {prod_id}({prod_acc:.1f}%) → {best['model_id']}({best['metrics']['accuracy']:.1f}%)")
        else:
            logger.info(f"首次部署: {best['model_id']}")

        self.deploy(best['model_id'])
        return best['model_id']

    def deprecate(self, model_id: str) -> bool:
        """废弃模型"""
        model = self.get(model_id)
        if not model:
            return False

        if model['status'] == 'production':
            logger.warning(f"不能废弃生产模型。请先 deploy 另一个模型")
            return False

        model['status'] = 'deprecated'
        model['deprecated_at'] = datetime.now().isoformat()
        self._save()
        logger.info(f"模型已废弃: {model_id}")
        return True

    # ══════════════════════════════════════════════════
    # 报告
    # ══════════════════════════════════════════════════

    def get_summary(self) -> Dict:
        """生成注册表摘要"""
        all_models = list(self._data['models'].values())
        active = [m for m in all_models if m['status'] in ('active', 'production')]
        prod = self._data.get('current_production')

        if active:
            avg_acc = sum(m['metrics']['accuracy'] for m in active) / len(active)
            best_acc = max(m['metrics']['accuracy'] for m in active)
            best_draw = max(m['metrics']['draw_f1'] for m in active)
        else:
            avg_acc = best_acc = best_draw = 0

        return {
            'total_models': len(all_models),
            'active_models': len(active),
            'current_production': prod,
            'avg_accuracy': round(avg_acc, 1),
            'best_accuracy': round(best_acc, 1),
            'best_draw_f1': round(best_draw, 1),
            'by_source': defaultdict(int, {
                m['source']: sum(1 for m in all_models if m['source'] == s)
                for s in set(m['source'] for m in all_models)
            }),
        }

    def get_improvement_history(self) -> List[Dict]:
        """准确率改进历史"""
        models = self.list_models(
            status='active', sort_by='registered_at', descending=False
        )
        history = []
        prev_acc = 0

        for m in models:
            acc = m['metrics']['accuracy']
            improvement = acc - prev_acc if prev_acc > 0 else 0
            history.append({
                'model_id': m['model_id'],
                'accuracy': acc,
                'draw_f1': m['metrics']['draw_f1'],
                'improvement_pp': round(improvement, 2),
                'registered_at': m['registered_at'],
            })
            prev_acc = acc

        return history

    # ══════════════════════════════════════════════════
    # 语义化版本 & 版本对比
    # ══════════════════════════════════════════════════

    @staticmethod
    def validate_semver(sv: str) -> bool:
        """验证语义化版本号 (如 '3.2.1')"""
        if not sv or not isinstance(sv, str):
            return False
        parts = sv.split('.')
        if len(parts) != 3:
            return False
        try:
            [int(p) for p in parts]
            return True
        except ValueError:
            return False

    @staticmethod
    def _semver_tuple(sv: str) -> tuple:
        """将语义化版本转为可比较的元组 (major, minor, patch)"""
        parts = sv.split('.')
        return (int(parts[0]), int(parts[1]), int(parts[2]))

    def compare_versions(self, model_id_a: str, model_id_b: str) -> Dict:
        """
        对比两个模型版本的综合指标。

        Returns:
            {
                'model_a': {...},
                'model_b': {...},
                'accuracy_diff': float,
                'draw_f1_diff': float,
                'verdict': 'A_better' / 'B_better' / 'tie'
            }
        """
        ma = self.get(model_id_a)
        mb = self.get(model_id_b)
        if not ma or not mb:
            return {'verdict': 'error', 'message': '模型不存在'}

        acc_diff = ma['metrics'].get('accuracy', 0) - mb['metrics'].get('accuracy', 0)
        draw_diff = ma['metrics'].get('draw_f1', 0) - mb['metrics'].get('draw_f1', 0)

        # 综合判定：准确率为主，平局F1为辅
        score_a = acc_diff * 0.7 + draw_diff * 100 * 0.3
        if score_a > 0.1:
            verdict = 'A_better'
        elif score_a < -0.1:
            verdict = 'B_better'
        else:
            verdict = 'tie'

        return {
            'model_a': {'model_id': model_id_a, 'accuracy': ma['metrics'].get('accuracy', 0), 'draw_f1': ma['metrics'].get('draw_f1', 0)},
            'model_b': {'model_id': model_id_b, 'accuracy': mb['metrics'].get('accuracy', 0), 'draw_f1': mb['metrics'].get('draw_f1', 0)},
            'accuracy_diff': round(acc_diff, 2),
            'draw_f1_diff': round(draw_diff, 4),
            'verdict': verdict,
        }


# ══════════════════════════════════════════════════
# 全局单例
# ══════════════════════════════════════════════════

_registry_instance = None


def get_registry() -> ModelRegistry:
    """获取全局注册表单例"""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = ModelRegistry()
    return _registry_instance


# ══════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='模型注册表管理')
    parser.add_argument('action', choices=['list', 'deploy', 'rollback', 'summary', 'best'],
                        help='操作')
    parser.add_argument('--id', type=str, help='模型 ID')
    parser.add_argument('--metric', type=str, default='accuracy',
                        help='排序/选择指标 (accuracy/draw_f1/brier/mcc)')

    args = parser.parse_args()
    registry = ModelRegistry()

    if args.action == 'list':
        models = registry.list_models(status='active', limit=10)
        print(f"\n{'ID':>6}  {'类型':>10}  {'准确率':>8}  {'平局F1':>8}  {'Brier':>8}  {'日期':>20}")
        print("-" * 70)
        for m in models:
            mt = m['metrics']
            print(f"{m['model_id']:>6}  {m['model_type']:>10}  {mt['accuracy']:>7.1f}%  "
                  f"{mt['draw_f1']:>7.1f}%  {mt['brier']:>7.4f}  {m['registered_at'][:19]:>20}")

    elif args.action == 'deploy':
        if not args.id:
            best = registry.get_best_by_metric(args.metric)
            if best:
                args.id = best['model_id']
                print(f"自动选择最优模型: {args.id}")
            else:
                print("无可用模型")
                exit(1)
        registry.deploy(args.id)
        print(f"已部署: {args.id}")

    elif args.action == 'rollback':
        result = registry.rollback()
        print(f"回滚到: {result}" if result else "回滚失败")

    elif args.action == 'summary':
        summary = registry.get_summary()
        print(f"\n模型注册表摘要:")
        print(f"  总模型数: {summary['total_models']}")
        print(f"  活跃: {summary['active_models']}")
        print(f"  生产: {summary['current_production'] or '无'}")
        print(f"  平均准确率: {summary['avg_accuracy']}%")
        print(f"  最高准确率: {summary['best_accuracy']}%")
        print(f"  最高平局F1: {summary['best_draw_f1']}%")
        print(f"  按来源: {dict(summary['by_source'])}")

        improvement = registry.get_improvement_history()
        if improvement:
            print(f"\n准确率改进历史:")
            for h in improvement:
                arrow = f"+{h['improvement_pp']:.1f}pp" if h['improvement_pp'] > 0 else ""
                print(f"  {h['model_id']}: {h['accuracy']:.1f}% {arrow}")

    elif args.action == 'best':
        best = registry.get_best_by_metric(args.metric)
        if best:
            print(f"\n最优模型 ({args.metric}):")
            print(f"  ID: {best['model_id']}")
            print(f"  路径: {best['model_path']}")
            print(f"  指标: {json.dumps(best['metrics'], indent=4)}")
        else:
            print("无可用模型")
