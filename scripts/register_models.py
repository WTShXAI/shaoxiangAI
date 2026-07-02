#!/usr/bin/env python3
"""注册生产模型到后端 API (2026-07-01)"""
import sys, os, json, urllib.request, urllib.error

BASE = 'http://localhost:9000/api/v1'

def register_model(model_path, name, version, accuracy):
    """调用模型注册API"""
    url = f'{BASE}/models/register'
    data = json.dumps({
        'model_path': model_path,
        'model_name': name,
        'version': version,
        'accuracy': accuracy,
        'status': 'active',
    }).encode('utf-8')

    req = urllib.request.Request(url, data=data,
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer admin'})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        print(f'  ✅ {name}: {resp.status}')
    except urllib.error.HTTPError as e:
        print(f'  ⚠ {name}: {e.code} — {e.read().decode()[:100]}')

if __name__ == '__main__':
    print('注册模型...')
    register_model('football_v4.1_production.joblib', 'v4.1 Production', '4.1', 0.6243)
    register_model('models/jepa/checkpoints/best_model_lite.pt', 'JEPA v5 Lite', '5.0', 0.5648)
    print('Done')
