"""
图片预测端点 — 路由归一至 api/v1/endpoints/ (2026-06-28)
====================================
迁移说明: 原 backend/routers/ → 统一至 api/v1/endpoints/
"""
import json, asyncio, os, sys, logging, tempfile
from fastapi import APIRouter, UploadFile, File
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["predict"])

@router.post("/predict/image")
async def predict_image(file: UploadFile = File(...)):
    """图片上传 → OCR识别 → 6层引擎分析 → SSE流式"""
    async def generate():
        if not file.content_type or not file.content_type.startswith("image/"):
            yield f"data: {json.dumps({'type':'error','content':'只支持 jpg/png/webp 图片格式'})}\n\n"
            yield f"data: {json.dumps({'type':'done'})}\n\n"
            return
        content = await file.read()
        size_mb = len(content) / (1024 * 1024)
        _msg = f'📸 图片已接收 ({file.filename}, {size_mb:.1f}MB)\n'
        yield f"data: {json.dumps({'type':'text','content':_msg})}\n\n"
        if size_mb > 10:
            yield f"data: {json.dumps({'type':'error','content':'图片超过10MB限制'})}\n\n"
            yield f"data: {json.dumps({'type':'done'})}\n\n"
            return
        _suffix = os.path.splitext(file.filename or "img.png")[1] or ".png"
        with tempfile.NamedTemporaryFile(suffix=_suffix, delete=False) as _tf:
            _tf.write(content)
            _tmp_path = _tf.name
        try:
            yield f"data: {json.dumps({'type':'status','content':'🔍 正在识别图片中的比赛信息...'})}\n\n"
            ocr_text = ""
            ocr_used = False
            try:
                from PIL import Image as _PILImage
                _img = _PILImage.open(_tmp_path)
                try:
                    import pytesseract
                    ocr_text = pytesseract.image_to_string(_img, lang="chi_sim+eng")
                    ocr_used = True
                except (ImportError, Exception):
                    ocr_text = "[OCR未安装] 请用文字描述比赛信息"
            except ImportError:
                ocr_text = "[PIL未安装] 图片识别需要 pillow"
            if ocr_used:
                _done_msg = f'✅ 识别完成 ({len(ocr_text)}字符)\n'
                yield f"data: {json.dumps({'type':'text','content':_done_msg})}\n\n"
                try:
                    from modules.image_input import ImageInputParser
                    parser = ImageInputParser()
                    parse_result = parser.parse(ocr_text)
                    if parse_result.valid_count > 0:
                        m = parse_result.matches[0]
                        _parse_msg = f'📋 解析: {m.home} vs {m.away}\n  赔率: {m.odds_h}/{m.odds_d}/{m.odds_a}\n'
                        yield f"data: {json.dumps({'type':'text','content':_parse_msg})}\n\n"
                        yield f"data: {json.dumps({'type':'status','content':'⚙️ 6层AI引擎分析中...'})}\n\n"
                        from modules.six_layer_conversation import SixLayerConversationEngine

                        engine = SixLayerConversationEngine(enable_l6=False)
                        _odds = {'home': m.odds_h, 'draw': m.odds_d, 'away': m.odds_a} if m.odds_h else None
                        result = engine.process(f"{m.home} vs {m.away}", m.home, m.away, m.league or "未知", _odds)
                        report = result.analysis_report
                        for chunk in [report[i:i+300] for i in range(0, len(report), 300)]:
                            yield f"data: {json.dumps({'type':'text','content':chunk})}\n\n"
                            await asyncio.sleep(0.02)
                        card = {"home":m.home,"away":m.away,"h_prob":round(result.h_prob,4),"d_prob":round(result.d_prob,4),"a_prob":round(result.a_prob,4),"d_gate":result.d_gate_result or "","time_ms":round(result.total_time_ms,1)}
                        yield f"data: {json.dumps({'type':'predict_card','data':card})}\n\n"
                    else:
                        _no_result_msg = '⚠️ 未能从图片中提取到比赛信息\n\n建议: 直接用文字描述比赛'
                        yield f"data: {json.dumps({'type':'text','content':_no_result_msg})}\n\n"
                except Exception as e:
                    _parse_err = f'⚠️ 解析失败: {e}\n\n{ocr_text[:500]}'
                    yield f"data: {json.dumps({'type':'text','content':_parse_err})}\n\n"
            else:
                _ocr_fallback = f'{ocr_text}\n\n💡 建议: 将赔率截图中的文字直接粘贴到输入框分析。'
                yield f"data: {json.dumps({'type':'text','content':_ocr_fallback})}\n\n"
        finally:
            try: os.unlink(_tmp_path)
            except (OSError, PermissionError) as e: logger.debug(f"predict_image: temp file cleanup failed: {e}")
        yield f"data: {json.dumps({'type':'done'})}\n\n"
    return StreamingResponse(generate(), media_type="text/event-stream")
