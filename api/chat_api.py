"""
哨响AI v4.1 — FastAPI 对话/图片端点
"""
import json, time, asyncio, os, sys, io, logging

# 确保项目根在路径中
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import StreamingResponse

logger = logging.getLogger('ChatAPI')

router = APIRouter(tags=["chat"])

@router.get("/chat/health")
async def chat_health():
    from config.settings import get_setting
    return {"status":"ok","version":"v4.1","pure_v32":get_setting("global_switches.pure_v32_mode",False)}

async def _stream_response(generator_func):
    """通用SSE流式包装"""
    async def event_stream():
        async for chunk in generator_func():
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        yield "data: {\"type\": \"done\"}\n\n"
    return StreamingResponse(event_stream(), media_type="text/event-stream")

@router.post("/chat")
async def chat_endpoint(request: Request):
    """
    文本对话接口 — SSE流式输出

    Body: {"message": "巴西vs阿根廷谁赢", "session_id": "xxx"}
    Response: SSE 事件流
      data: {"type": "text", "content": "..."}
      data: {"type": "predict_card", "data": {...}}
      data: {"type": "done"}
    """
    try:
        body = await request.json()
        except json.JSONDecodeError:
            body = {}
    message = body.get("message", "")
    session_id = body.get("session_id", "default")

    async def generate():
        start = time.perf_counter()
        try:
            from modules.six_layer_conversation import SixLayerConversationEngine
            engine = SixLayerConversationEngine(enable_l6=False)

            # 从消息中提取比赛信息
            import re
            teams = re.findall(r'(.+?)\s+(?:vs|VS|对)\s+(.+?)(?:\s|$|，|,)', message)
            home = teams[0][0].strip() if teams else ""
            away = teams[0][1].strip() if teams else ""

            # 尝试提取赔率
            odds_match = re.findall(r'(\d+\.\d+)', message)
            odds = None
            if len(odds_match) >= 3:
                odds = {'home': float(odds_match[0]), 'draw': float(odds_match[1]), 'away': float(odds_match[2])}

            yield {"type": "text", "content": "🔍 哨响AI v4.1 分析中...\n\n"}

            result = engine.process(message, home, away, "世界杯" if not home else "", odds)

            # 流式输出分析报告 (按行逐条发送, 模拟打字机)
            report = result.analysis_report
            lines = report.split('\n')
            buffer = ""
            for line in lines:
                buffer += line + "\n"
                # 每3行发送一次, 模拟流式
                if len(buffer) > 200 or line.startswith('═') or line.startswith('─'):
                    yield {"type": "text", "content": buffer}
                    buffer = ""
                    await asyncio.sleep(0.05)
            if buffer:
                yield {"type": "text", "content": buffer}

            # 发送结构化卡片
            card_data = {
                "home": home or "?",
                "away": away or "?",
                "h_prob": round(result.h_prob, 4),
                "d_prob": round(result.d_prob, 4),
                "a_prob": round(result.a_prob, 4),
                "d_gate": result.d_gate_result or "",
                "risk_tags": result.risk_tags if hasattr(result, 'risk_tags') else [],
                "expert_insights": result.expert_insights[:5],
                "recommendation": result.recommendation,
                "time_ms": round(result.total_time_ms, 1),
            }
            yield {"type": "predict_card", "data": card_data}

            elapsed = (time.perf_counter() - start) * 1000
            yield {"type": "text", "content": f"\n⏱ 推理耗时: {elapsed:.0f}ms"}

        except Exception as e:
            logger.error(f"对话异常: {e}")
            yield {"type": "error", "content": f"分析异常: {str(e)}"}

    return await _stream_response(generate)

@router.post("/predict/image")
async def predict_image(file: UploadFile = File(...)):
    """
    图片预测接口 — 上传截图, OCR识别后调用6层引擎

    Body: multipart/form-data, field: file
    Response: SSE 事件流 (同 /chat)
    """
    async def generate():
        yield {"type": "text", "content": "📸 正在处理图片...\n"}

        try:
            # 读取图片
            contents = await file.read()
            import tempfile, os
            with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp:
                tmp.write(contents)
                tmp_path = tmp.name

            # OCR + 解析
            from modules.image_input import ImageInputParser
            parser = ImageInputParser()
            result = parser.parse(tmp_path, is_image=True)
            os.unlink(tmp_path)

            if result.valid_count == 0:
                # 回退: 尝试纯文本解析
                yield {"type": "text", "content": "⚠️ 图片中未识别到有效比赛数据\n"}
                yield {"type": "text", "content": "请确保截图包含: 球队名 + 赔率(H/D/A)\n"}
                yield {"type": "text", "content": "提示: 也可以直接粘贴文本格式的赔率数据\n"}
                return

            yield {"type": "text", "content": f"✅ 识别到 {result.valid_count} 场比赛\n\n"}

            # 对每场比赛跑6层引擎
            from modules.six_layer_conversation import SixLayerConversationEngine
            engine = SixLayerConversationEngine(enable_l6=False)

            for i, match in enumerate(result.matches):
                if not match.is_valid():
                    continue
                yield {"type": "text", "content": f"## 第{i+1}场: {match.home} vs {match.away}\n"}
                pred = engine.process(f"{match.home} vs {match.away} 谁赢",
                                      match.home, match.away, "",
                                      match.to_odds_dict())
                yield {"type": "text", "content": pred.analysis_report + "\n"}

                card_data = {
                    "home": match.home, "away": match.away,
                    "h_prob": round(pred.h_prob, 4),
                    "d_prob": round(pred.d_prob, 4),
                    "a_prob": round(pred.a_prob, 4),
                    "d_gate": pred.d_gate_result or "",
                }
                yield {"type": "predict_card", "data": card_data}

        except Exception as e:
            logger.error(f"图片预测异常: {e}")
            yield {"type": "error", "content": f"处理异常: {str(e)}"}

    return await _stream_response(generate)

@router.get("/health")
async def health_check():
    """健康检查"""
    from config.settings import get_setting
    return {
        "status": "ok",
        "version": "v4.1",
        "pure_v32": get_setting("global_switches.pure_v32_mode", False),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
