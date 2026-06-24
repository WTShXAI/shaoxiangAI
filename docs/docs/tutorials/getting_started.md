# 哨响AI 快速开始教程

> 预计时间: 15 分钟

---

## 前提条件

- Python 3.10+
- Git
- (可选) CUDA GPU + PyTorch CUDA 版本

---

## 1. 安装

```bash
# 克隆项目
git clone <repo-url> football-ai
cd football-ai

# 创建虚拟环境
python -m venv venv
source venv/bin/activate    # Linux/Mac
# 或
venv\Scripts\activate       # Windows

# 安装依赖
pip install -r requirements.txt
```

---

## 2. 配置

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env，填入你的 API Key
# FOOTBALL_DATA_API_KEY=your_key_here
# (可选) 其他 API: THE_ODDS_API_KEY
```

获取免费 API Key: [football-data.org](https://www.football-data.org/) (10次/分钟)

---

## 3. 初始化数据

```bash
# 初始化数据库表
python database/init_db.py

# 拉取历史数据 (首次约需10-30分钟，取决于数据量)
python scripts/pull_historical_data.py --start 2024

# 验证数据
python -c "
from ensemble_trainer import EnsembleTrainer
trainer = EnsembleTrainer()
df = trainer.load_training_data()
print(f'已加载 {len(df)} 场比赛数据')
print(f'日期范围: {df[\"match_date\"].min()} ~ {df[\"match_date\"].max()}')
"
```

---

## 4. 首次训练

```bash
# 快速训练 (5000样本, 200树, ~2分钟)
python scripts/calibration_fix.py --step 1

# 完整训练 (全量数据, 1000树, ~10-30分钟)
python scripts/final_train_fix.py

# 查看训练结果
python optimize/model_registry.py summary
```

---

## 5. 启动预测服务

```bash
# 方式1: 直接启动 Flask
python api/prediction_service.py

# 方式2: 使用部署脚本
bash scripts/deploy.sh   # Linux
# 或
powershell scripts/deploy.ps1  # Windows

# 方式3: Docker
docker compose up -d
```

---

## 6. 测试预测

```bash
# 健康检查
curl http://localhost:8080/api/monitor/health

# 获取预测
curl http://localhost:8080/api/predict/next-match | python -m json.tool

# 批量预测
curl http://localhost:8080/api/predict/batch | python -m json.tool
```

---

## 7. 日常维护

```bash
# 每日更新最新比赛数据
python scripts/update_latest_data.py

# 更新后重新预测
python auto_pipeline.py

# 定期重训练 (建议每周)
python training/training_pipeline.py --data latest --trees 1000

# 数据质量检查
python -c "
from database.db_manager import DBManager
from utils.data_quality_checker import DataQualityChecker
checker = DataQualityChecker(DBManager())
r = checker.run_full_check()
print(f'数据健康分数: {r[\"summary\"][\"health_score\"]}')
"
```

---

## 下一步

- 📖 [API 参考文档](../docs/API_REFERENCE.md) — 所有 API 端点
- 🔧 [故障排除指南](../docs/TROUBLESHOOTING.md) — 常见问题解决方案
- 🏗️ [架构文档](../docs/ARCHITECTURE.md) — 系统架构说明
- 🔄 [迁移指南](../docs/MIGRATION_GUIDE.md) — 从旧模型迁移
- 📊 [CI/CD 部署指南](../docs/CICD_DEPLOY_GUIDE.md) — 自动化部署

---

## 常见问题

**Q: 没有 API Key 能使用吗？**
A: 可以。已有 `data/football_data.db` 包含历史数据，可直接训练和预测。但不能获取新比赛。

**Q: 训练需要 GPU 吗？**
A: 不需要。CPU 训练也能达到相同准确率，只是稍慢。PyTorch GPU 加速为可选。

**Q: 预测结果准确吗？**
A: 当前模型准确率约 45%（足球预测的三分类基线为 33%）。持续改进中。

**Q: 如何贡献？**
A: 请阅读 `docs/ARCHITECTURE.md` 了解架构，在 `scripts/` 下添加诊断脚本，并使用 `model_registry.py` 注册新模型版本。
