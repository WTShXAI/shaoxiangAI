"""哨响AI v5.0 JEPA KNN-Hybrid Server — Acc=52.4% DrawF1=0.43"""
import sys, os, logging
ROOT = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(level=logging.WARNING)

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI()

@app.get('/api/v1/chat/health')
async def health():
    return {'status': 'ok', 'version': 'v5.0-knn-hybrid', 'acc': '52.4%', 'draw_f1': '0.43'}

@app.get('/api/v1/v5/health')
async def v5_health():
    from models.jepa import JEPALite
    m = JEPALite()
    return {'status': 'ok', 'params': sum(p.numel() for p in m.parameters())}

@app.post('/api/v1/v5/predict')
async def v5_predict(request: Request):
    try:
        body = await request.json()
        ho = float(body.get('home_odds', 2.0))
        do = float(body.get('draw_odds', 3.5))
        ao = float(body.get('away_odds', 3.0))
        
        from predictors.jepa_inference import predict
        result = predict(ho, do, ao)
        
        return JSONResponse({
            'success': True,
            'version': 'v5.0-knn-hybrid',
            'prediction': result['prediction'],
            'probabilities': result['probabilities'],
            'confidence': result['confidence'],
            'draw_signal': result['draw_signal'],
            'source': result['source'],
            'jepa_draw_prob': result.get('jepa_draw_prob', 0),
            'knn_probabilities': result.get('knn_probabilities', result['probabilities']),
        })
    except Exception as e:
        return JSONResponse({'success': False, 'error': str(e)}, status_code=500)

if __name__ == '__main__':
    print("JEPA v5.0 KNN-Hybrid Server starting on port 9000...")
    uvicorn.run(app, host='0.0.0.0', port=9000, log_level='error')
