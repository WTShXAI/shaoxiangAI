"""Debug model loading issue"""
import sys, os
sys.path.insert(0, 'D:/Architecture v4.0')
sys.path.insert(0, 'D:/Architecture v4.0/predictors')
sys.path.insert(0, 'D:/Architecture v4.0/predictors/components')

# Try loading EnsembleTrainer directly
try:
    from predictors.components.ensemble_trainer import EnsembleTrainer
    print('EnsembleTrainer import OK')
except Exception as e:
    print(f'Import FAIL: {e}')

# Try loading the model file
import joblib
model_path = 'D:/Architecture v4.0/saved_models/football_v4.1_production.joblib'
try:
    trainer = joblib.load(model_path)
    print('Model load OK')
    has_de = hasattr(trainer, 'draw_expert_model')
    print(f'Has draw_expert_model: {has_de}')
    if has_de:
        print(f'draw_expert_model type: {type(trainer.draw_expert_model)}')
except Exception as e:
    print(f'Model load FAIL: {e}')
    import traceback
    traceback.print_exc()
