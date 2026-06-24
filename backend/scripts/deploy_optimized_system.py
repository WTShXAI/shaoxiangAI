import logging
"""
部署优化系统 — 将优化后的模型和配置文件复制到生产目录
"""
import argparse
import sys
import os
import shutil
import json
from pathlib import Path
from datetime import datetime
logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

PRODUCTION_DIR = 'saved_models/production'
REQUIRED_FILES = [
    'footballai_compressed.joblib',
    'smart_integrated.joblib',
    'footballai_compressed_features.json',
]


def deploy_model(model_path: str, prod_dir: str, label: str = None):
    """部署单个模型到生产目录"""
    if not os.path.exists(model_path):
        logger.info(f"  ⚠️ 模型不存在: {model_path}")
        return False

    dest = os.path.join(prod_dir, os.path.basename(model_path))
    shutil.copy2(model_path, dest)
    logger.info(f"  ✓ {os.path.basename(model_path)} → {label or dest}")
    return True


def deploy_feature_list(feature_json: str, prod_dir: str):
    """部署特征列表"""
    if not os.path.exists(feature_json):
        logger.info(f"  ⚠️ 特征列表不存在: {feature_json}")
        return False

    dest = os.path.join(prod_dir, os.path.basename(feature_json))
    shutil.copy2(feature_json, dest)
    logger.info(f"  ✓ {os.path.basename(feature_json)} → {dest}")
    return True


def create_deploy_manifest(prod_dir: str, deployed_files: list):
    """创建部署清单"""
    manifest = {
        'deploy_time': datetime.now().isoformat(),
        'production_dir': prod_dir,
        'files': deployed_files,
        'file_sizes': {},
    }

    for f in deployed_files:
        path = os.path.join(prod_dir, f)
        if os.path.exists(path):
            manifest['file_sizes'][f] = os.path.getsize(path)

    manifest_path = os.path.join(prod_dir, 'deploy_manifest.json')
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    logger.info(f"\n  📋 部署清单 → {manifest_path}")

    return manifest


def backup_existing(prod_dir: str):
    """备份现有生产目录"""
    if os.path.exists(prod_dir) and os.listdir(prod_dir):
        backup_dir = f"{prod_dir}_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copytree(prod_dir, backup_dir)
        logger.info(f"  📦 备份 → {backup_dir}")
        return backup_dir
    return None


def main():
    parser = argparse.ArgumentParser(description='部署优化系统')
    parser.add_argument('--compressed-model',
                        default='saved_models/footballai_compressed.joblib',
                        help='压缩特征模型路径')
    parser.add_argument('--integrated-model',
                        default='saved_models/smart_integrated.joblib',
                        help='集成模型路径')
    parser.add_argument('--features',
                        default='saved_models/footballai_compressed_features.json',
                        help='特征列表路径')
    parser.add_argument('--prod-dir', default=PRODUCTION_DIR,
                        help='生产目录')
    parser.add_argument('--no-backup', action='store_true',
                        help='跳过备份')
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("🚀 优化系统部署")
    logger.info(f"  生产目录: {args.prod_dir}")
    logger.info("=" * 60)

    # 1. 创建生产目录
    os.makedirs(args.prod_dir, exist_ok=True)

    # 2. 备份现有内容
    if not args.no_backup:
        backup_existing(args.prod_dir)

    # 3. 部署模型文件
    logger.info(f"\n[1/3] 部署模型文件...")
    deployed = []

    if os.path.exists(args.compressed_model):
        deploy_model(args.compressed_model, args.prod_dir, 'compressed')
        deployed.append(os.path.basename(args.compressed_model))

    if os.path.exists(args.integrated_model):
        deploy_model(args.integrated_model, args.prod_dir, 'integrated')
        deployed.append(os.path.basename(args.integrated_model))

    # 4. 部署特征列表
    logger.info(f"\n[2/3] 部署特征配置...")
    if os.path.exists(args.features):
        deploy_feature_list(args.features, args.prod_dir)
        deployed.append(os.path.basename(args.features))

    # 5. 清理旧文件 (保留最多3个版本)
    logger.info(f"\n[3/3] 生成部署清单...")
    create_deploy_manifest(args.prod_dir, deployed)

    # 6. 最终检查
    logger.info(f"\n{'='*60}")
    logger.info(f"📦 部署完成!")
    logger.info(f"  生产目录: {os.path.abspath(args.prod_dir)}")
    logger.info(f"  部署文件: {len(deployed)}")
    for f in deployed:
        size = os.path.getsize(os.path.join(args.prod_dir, f))
        logger.info(f"    - {f} ({size:,} bytes)")
    logger.info(f"{'='*60}")

    return 0


if __name__ == '__main__':
    sys.exit(main())
