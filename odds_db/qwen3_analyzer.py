#!/usr/bin/env python3
"""
qwen3:14b 本地分析桥接器
=========================
哨响AI·渡庄生 博弈分析管线本地推理引擎
通过 Ollama API 调用 qwen3:14b 执行结构化分析任务

使用:
  python qwen3_analyzer.py 分析 "分析目标" [--context JSON]
  python qwen3_analyzer.py 复核    "赔率摘要" [--context JSON]
  python qwen3_analyzer.py 推理    "提示词"   [--temp 0.7] [--max-tokens 4096]

依赖: curl（或 requests），无额外第三方库
"""

import json, sys, os, subprocess, argparse
from typing import Optional

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
MODEL = "qwen3:14b"

# ======================== 分析模板 ========================

ANALYSIS_PROMPTS = {
    "分析": """你是一名专业的足球博弈分析师（渡庄生，哨响AI智囊团）。
你的任务是使用"机构陷阱区分"框架分析以下比赛数据，输出结构化的博弈解读。

## 分析框架
1. 亚庄多档盘口结构 → 陷阱藏于"最顺人性的线"
2. 1X2 极低赔 = 免费局（非陷阱）
3. 低水方向 = 庄家在护的"聪明边"（铁律：无漂移时绝不可反判诱多）
4. 必发资金 vs 赔率隐含概率的背离度
5. 真庄 William Hill vs Interwetten 分歧
6. 凯利值分布（正EV / 负EV）
7. 大小球真庄低水方向
8. 波胆 OIP 聚焦

## 输出格式（纯 JSON，不要额外文字）
{{
  "verdict": "诚实热端|真陷阱|均衡|待定",
  "confidence": "高|中|低",
  "key_evidence": ["发现1", "发现2", ...],
  "trap_mechanism": "描述陷阱机制（如无陷阱填null）",
  "value_status": "正EV|溢价|过热|枯竭",
  "betting_stance": "注码姿态描述",
  "kelly_summary": "全线<0|部分正EV|正EV明显",
  "insurance": "反人性保险选项"
}}

【比赛数据】
{data}""",

    "复核": """你是哨响AI的博弈分析师渡庄生，负责独立复核已有的模型判定。

已有判定: {previous_verdict}
你的任务是:
1. 独立判断：这是"诚实热端"还是"真陷阱"？
2. 如与前判定一致，强化证据链
3. 如不一致，说明分歧点

【比赛数据】
{data}

输出JSON:
{{
  "agreement": true|false,
  "my_verdict": "诚实热端|真陷阱",
  "strengthened_evidence": ["强化证据1", ...],
  "divergence": "不一致的原因（如一致填null）"
}}""",

    "推理": """{prompt}

回答要求：
- 结构化输出
- 基于前述分析框架
- 分点呈现
""",
}

# ======================== Ollama API 调用 ========================

def call_ollama(prompt: str, system_prompt: str = "",
                temperature: float = 0.6, max_tokens: int = 4096,
                stream: bool = False) -> dict:
    """调用本地 qwen3:14b 模型"""
    data = {
        "model": MODEL,
        "prompt": prompt,
        "system": system_prompt,
        "stream": stream,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
            "top_k": 20,
            "top_p": 0.95,
        }
    }

    # 使用 curl 调用（零第三方依赖）
    cmd = [
        "curl", "-s", "-X", "POST",
        f"{OLLAMA_HOST}/api/generate",
        "-d", json.dumps(data, ensure_ascii=False)
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return {"error": f"curl failed: {result.stderr}"}

        response = json.loads(result.stdout)
        return response
    except subprocess.TimeoutExpired:
        return {"error": "推理超时（>120s）"}
    except json.JSONDecodeError as e:
        return {"error": f"JSON解析失败: {e}"}
    except Exception as e:
        return {"error": str(e)}


def info() -> dict:
    """查询模型和服务状态"""
    # 检查服务
    try:
        r = subprocess.run(
            ["curl", "-s", f"{OLLAMA_HOST}/api/tags"],
            capture_output=True, text=True, timeout=5
        )
        tags = json.loads(r.stdout)
        models = [m["name"] for m in tags.get("models", [])]
    except:
        models = []

    # 模型参数
    try:
        r = subprocess.run(
            ["curl", "-s", f"{OLLAMA_HOST}/api/show", "-d", json.dumps({"model": MODEL})],
            capture_output=True, text=True, timeout=5
        )
        show = json.loads(r.stdout)
        params = show.get("parameters", "")
    except:
        params = ""

    return {
        "service": f"{OLLAMA_HOST}",
        "model": MODEL,
        "available_models": models,
        "model_loaded": MODEL in models,
        "parameters": params,
        "capabilities": ["分析", "复核", "推理", "OIP生成"]
    }


# ======================== CLI ========================

def main():
    parser = argparse.ArgumentParser(description="qwen3:14b 本地分析引擎")
    parser.add_argument("mode", choices=["分析", "复核", "推理", "info"],
                        help="分析模式")
    parser.add_argument("input", nargs="?", default="",
                        help="分析目标/赔率摘要/提示词")
    parser.add_argument("--data", "-d", default="",
                        help="比赛数据 JSON 或其文件路径（@file）")
    parser.add_argument("--context", "-c", default="",
                        help="额外上下文 JSON")
    parser.add_argument("--temp", type=float, default=0.6,
                        help="温度 (0-1)")
    parser.add_argument("--max-tokens", type=int, default=4096,
                        help="最大生成 token")
    parser.add_argument("--raw", action="store_true",
                        help="输出原始 JSON 而非格式化")
    parser.add_argument("--stream", action="store_true",
                        help="流式输出")

    args = parser.parse_args()

    if args.mode == "info":
        result = info()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # 读取数据
    data = args.data or args.input
    if data.startswith("@"):
        fpath = data[1:]
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = f.read()
        except Exception as e:
            print(json.dumps({"error": f"读文件失败: {e}"}, ensure_ascii=False))
            sys.exit(1)

    # 渲染提示词
    if args.mode == "分析":
        prompt = ANALYSIS_PROMPTS["分析"].format(data=data)
        system = "你是哨响AI博弈分析师渡庄生。严格按输出格式返回纯JSON。"
    elif args.mode == "复核":
        prev = json.loads(args.context) if args.context else {"verdict": "无"}
        prompt = ANALYSIS_PROMPTS["复核"].format(
            previous_verdict=json.dumps(prev, ensure_ascii=False),
            data=data
        )
        system = "你是哨响AI博弈分析师渡庄生。独立复核，输出纯JSON。"
    else:  # 推理
        prompt = ANALYSIS_PROMPTS["推理"].format(prompt=args.input)
        system = args.context or ""

    # 调用
    response = call_ollama(prompt, system, args.temp, args.max_tokens, args.stream)

    if args.stream:
        # 流式模式：逐块输出
        for chunk_raw in response:
            print(chunk_raw, end="", flush=True)
        print()
        return

    if args.raw:
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return

    # 尝试从 response 中提取 JSON
    text = response.get("response", "")
    try:
        # 找第一个 { 和最后一个 }
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(text[start:end])
            print(json.dumps(parsed, ensure_ascii=False, indent=2, default=str))
        else:
            print(text)
    except json.JSONDecodeError:
        print(text)


if __name__ == "__main__":
    main()
