"""
哨响AI v4.1 — 豆包OCR代理 (火山引擎)
=====================================
后端 HMAC-SHA256 签名方案: AK/SK 仅存服务端。
端点: POST /api/v1/ocr/upload

⚠️ 废弃声明 (2026-06-28 路由归一):
   此文件在 backend/main.py 中动态加载，建议后续迁移至
   backend/api/v1/endpoints/ 下的统一路由。
"""

import base64, hashlib, hmac, logging, os
from datetime import datetime, timezone
from fastapi import APIRouter, UploadFile, File, HTTPException
from urllib.parse import urlencode
import requests

logger = logging.getLogger("OCR")
ocr_router = APIRouter()

# OCR 凭据从环境变量读取（.env 或系统环境），不再硬编码
AK = os.getenv("OCR_AK", "")
SK = os.getenv("OCR_SK", "")
HOST = "visual.volcengineapi.com"
if not AK or not SK:
    logger.warning("OCR_AK/OCR_SK 环境变量未设置，OCR 功能将不可用")
REGION = "cn-north-1"
SERVICE = "cv"
API_VERSION = "2020-08-26"

def _sign(method, body_str, ak, sk):
    """HMAC-SHA256 签名 (无需 VOLC_ 前缀)"""
    now = datetime.now(timezone.utc)
    date = now.strftime("%Y%m%d")
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    payload_hash = hashlib.sha256(body_str.encode()).hexdigest()

    canonical_headers = f"content-type:application/x-www-form-urlencoded\nhost:{HOST}\n"
    signed_headers = "content-type;host"
    canonical_request = f"{method}\n/\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"

    scope = f"{date}/{REGION}/{SERVICE}/request"
    string_to_sign = f"HMAC-SHA256\n{ts}\n{scope}\n{hashlib.sha256(canonical_request.encode()).hexdigest()}"

    k_date = hmac.new(sk.encode(), date.encode(), hashlib.sha256).digest()
    k_region = hmac.new(k_date, REGION.encode(), hashlib.sha256).digest()
    k_service = hmac.new(k_region, SERVICE.encode(), hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"request", hashlib.sha256).digest()
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    return {
        "Authorization": f"HMAC-SHA256 Credential={ak}/{scope}, SignedHeaders={signed_headers}, Signature={signature}",
        "X-Date": ts,
    }

@ocr_router.post("/api/v1/ocr/upload")
async def ocr_proxy(file: UploadFile = File(...)):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "只支持 jpg/png/webp 图片格式")

    try:
        img_bytes = await file.read()
        img_b64 = base64.b64encode(img_bytes).decode()

        body_data = {
            "Action": "OCRNormal",
            "Version": "2020-08-26",
            "image_base64": img_b64,
            "detect_layout": "true",   # 表格版面分析
            "sort_page": "true",       # 按阅读顺序排序
        }
        body_str = urlencode(body_data)  # 正确URL编码 base64 中的 +/= 字符

        headers = {"Content-Type": "application/x-www-form-urlencoded", "Host": HOST}
        headers.update(_sign("POST", body_str, AK, SK))

        # 修复P0-7: 同步requests→异步httpx, 避免阻塞事件循环15秒
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"https://{HOST}/", data=body_str, headers=headers)
        data = resp.json()

        # 错误检查
        err = data.get("ResponseMetadata", {}).get("Error", {})
        if err and err.get("Code") != "NoError":
            raise HTTPException(502, f"OCR失败: {err.get('Message', '未知')}")

        # 解析: 优先 data.chars (二维字符数组), 兜底 Result.TextBlocks
        texts = []
        chars_2d = data.get("data", {}).get("chars", [])
        if chars_2d:
            for line in chars_2d:
                if isinstance(line, list):
                    line_text = "".join(c.get("char", "") if isinstance(c, dict) else str(c) for c in line)
                    texts.append(line_text)
        else:
            blocks = data.get("Result", {}).get("TextBlocks", [])
            texts = [b.get("Text", "") for b in blocks] if blocks else []

        full_text = "\n".join(texts)

        logger.info(f"[OCR] {len(texts)}行 {len(full_text)}字符")
        return {"success": True, "text": full_text, "blocks": texts, "block_count": len(texts)}

    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(504, "OCR服务超时")
    except httpx.RequestError as e:
        raise HTTPException(502, f"OCR不可达: {e}")
    except Exception as e:
        logger.error(f"[OCR] {e}", exc_info=True)
        raise HTTPException(500, f"OCR内部错误: {e}")
