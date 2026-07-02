"""
哨响AI v4.0 — L1 截图/图片输入解析 (Image Input Parser)
===========================================================
为6层架构的L1用户输入层, 补充图片/截图输入入口。
支持从博彩APP截图(波胆/胜平负/让球/大小球)中自动提取比赛数据。

核心能力:
  1. 竖列格式截图解析 — 常见博彩APP的胜平负/让球/波胆竖排布局
  2. OCR文字提取 — 依托pytesseract或外部OCR服务
  3. 结构化输出 — 提取的比赛数据直接喂给6层预测引擎
  4. 多场比赛批量解析 — 一张截图含多场时可全部提取

支持的截图格式:
  ┌─────────────────┐
  │ 比赛    胜  平  负│
  │ 巴西vs阿根廷 2.1 3.3 3.6 │
  │ 让球 -0.5 1.9 1.9 │
  │ 大小球 2.5 1.9 1.9 │
  │ 波胆 2-1 @7.5     │
  └─────────────────┘

用法:
  python -m modules.image_input --image screenshot.png
  python -m modules.image_input --text "巴西vs阿根廷\n胜平负: 2.10 3.30 3.60"

作者: Architecture · L1 Phase
日期: 2026-06-19
"""
from __future__ import annotations
import os, re, logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger('ImageInput')

# ═══════════════════════════════════════════════════════════════
# 1. 数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class ExtractedMatch:
    """从截图中提取的比赛信息"""
    home: str = ""
    away: str = ""
    league: str = ""

    # 1X2
    odds_h: float = 0.0
    odds_d: float = 0.0
    odds_a: float = 0.0

    # 让球
    handicap_line: float = 0.0
    handicap_h: float = 0.0
    handicap_a: float = 0.0

    # 大小球
    ou_line: float = 2.5
    ou_over: float = 0.0
    ou_under: float = 0.0

    # 波胆 (最多3个)
    correct_scores: List[Tuple[str, float]] = field(default_factory=list)

    # 提取质量
    confidence: float = 0.0       # 提取置信度 [0,1]
    source_type: str = "text"     # text / image_ocr
    raw_text: str = ""

    def to_odds_dict(self) -> Dict[str, float]:
        return {'home': self.odds_h, 'draw': self.odds_d, 'away': self.odds_a}

    def is_valid(self) -> bool:
        return (self.home and self.away and 
                self.odds_h > 1.0 and self.odds_d > 1.0 and self.odds_a > 1.0)

@dataclass
class ImageParseResult:
    """图片解析完整结果"""
    matches: List[ExtractedMatch] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    source: str = ""         # 图片路径或文本来源
    parse_time_ms: float = 0.0
    ocr_available: bool = False

    @property
    def valid_count(self) -> int:
        return sum(1 for m in self.matches if m.is_valid())

# ═══════════════════════════════════════════════════════════════
# 2. 文本解析引擎 (核心 — 不依赖OCR)
# ═══════════════════════════════════════════════════════════════

class BettingTextParser:
    """
    从博彩文本中提取比赛数据 (不依赖OCR)

    支持格式:
      - "巴西 vs 阿根廷 2.10 3.30 3.60"
      - 竖排: "巴西\n阿根廷\n2.10\n3.30\n3.60"
      - 带标签: "主胜: 2.10 平局: 3.30 客胜: 3.60"
      - 对阵行: "巴西(主) vs 阿根廷(客)"
      - Interwetten格式: "Interwetten 1.35 4.80 8.50"
      - 让球: "让球 -0.5 1.90 1.90"
      - 大小球: "大小球 2.5 1.88 1.92"
      - 波胆: "2-1 @7.50" / "1:1 (6.5)"
    """

    # ── 队名提取模式 ──
    VS_PATTERNS = [
        r'(?P<home>.{1,25}?)\s+vs\.?\s+(?P<away>.{1,25}?)(?:\s|\$|，|,|（|\()',
        r'(?P<home>.{1,25}?)\s+对\s+(?P<away>.{1,25}?)(?:\s|\$|，|,|（|\()',
        r'(?P<home>.{1,15}?)(?:\(主\))?\s*[vV][sS]\s*(?P<away>.{1,15}?)',
    ]

    # ── 赔率提取模式 ──
    ODDS_PATTERNS = [
        r'(?:胜|主|H|1)[^\d]*?(?P<h>\d+\.\d+)\s+(?:平|和|D|X)[^\d]*?(?P<d>\d+\.\d+)\s+(?:负|客|A|2)[^\d]*?(?P<a>\d+\.\d+)',
        r'(?<!\d)(?P<h>[1-9]\d*\.\d{2})\s+(?P<d>[1-9]\d*\.\d{2})\s+(?P<a>[1-9]\d*\.\d{2})(?!\d)',
        r'Interwetten.*?(?P<h>\d+\.\d+)\s+(?P<d>\d+\.\d+)\s+(?P<a>\d+\.\d+)',
        # 松散格式: 队名后跟赔率 (如: 英格兰 vs 克罗地亚  2.30  3.10  3.30)
        r'vs\.?\s+\S+\s+(?P<h>\d+\.\d{2})\s+(?P<d>\d+\.\d{2})\s+(?P<a>\d+\.\d{2})',
    ]

    # ── 让球模式 ──
    HANDICAP_PATTERNS = [
        r'(?:让球|亚盘|AH|Handicap)[^\d]*(?P<line>[+-]?\d+\.?\d*)\s+(?P<h>\d+\.\d+)\s+(?P<a>\d+\.\d+)',
    ]

    # ── 大小球模式 ──
    OU_PATTERNS = [
        r'(?:大小球|OU|Over.Under|总进球)[^\d]*(?P<line>\d+\.?\d*)\s+(?P<over>\d+\.\d+)\s+(?P<under>\d+\.\d+)',
    ]

    # ── 波胆模式 ──
    CORRECT_SCORE_PATTERNS = [
        r'(?P<hg>\d+)[:\-](?P<ag>\d+)\s*(?:@|赔|赔率)?\s*(?P<odds>\d+\.?\d*)',
    ]

    def parse(self, text: str) -> List[ExtractedMatch]:
        """
        从文本中提取所有比赛

        Args:
            text: 截图识别文本或手动输入文本

        Returns:
            提取的比赛列表
        """
        matches = []
        text = self._clean_text(text)

        # 尝试按比赛分段 (空行或特定分隔符)
        segments = self._split_matches(text)

        for seg in segments:
            match = self._parse_single(seg)
            if match and match.is_valid():
                matches.append(match)
            elif match and match.home:
                # 部分提取: 有队名但赔率不全
                match.confidence = 0.3
                matches.append(match)

        return matches

    def _clean_text(self, text: str) -> str:
        """清洗文本 — 保留竖列格式和多场边界"""
        lines = text.split('\n')
        has_odds_lines = sum(1 for l in lines if re.search(r'\d+\.\d{2}', l))
        is_vertical = len(lines) >= 3 and has_odds_lines >= 3
        vs_count = sum(1 for l in lines if re.search(r'(?:vs\.?\s|对\s)', l, re.IGNORECASE))
        is_multi_match = vs_count >= 2

        if is_vertical or is_multi_match:
            # 保留换行结构, 保留空行作为分隔
            text = '\n'.join(l.strip() for l in lines)
        else:
            text = re.sub(r'\s+', ' ', text).strip()

        text = re.sub(r'[vV]\s*[sS]', ' vs ', text)
        text = re.sub(r'[^\x00-\x7F\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\s\d\.\-:+/@\n]', '', text)
        return text

    def _split_matches(self, text: str) -> List[str]:
        """将文本按比赛分段"""
        # 策略1: 按双空行分割
        if '\n\n' in text:
            segs = [s.strip() for s in re.split(r'\n\s*\n', text) if s.strip()]
            if len(segs) >= 2 and all(len(s) > 10 for s in segs):
                return segs

        # 策略2: 按 vs/对 关键词分割
        vs_positions = [m.start() for m in re.finditer(r'(?:vs\.?\s|对\s)', text)]
        if len(vs_positions) > 1:
            segments = []
            for i, pos in enumerate(vs_positions):
                end = vs_positions[i+1] if i+1 < len(vs_positions) else len(text)
                seg = text[pos:end].strip()
                if seg:
                    segments.append(seg)
            if len(segments) >= 2:
                return segments

        # 单场比赛
        return [text]

    def _parse_single(self, text: str) -> Optional[ExtractedMatch]:
        """解析单场比赛文本"""
        match = ExtractedMatch(raw_text=text[:200])

        # ── 1. 提取队名 ──
        for pattern in self.VS_PATTERNS:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                match.home = m.group('home').strip()
                match.away = m.group('away').strip()
                break

        if not match.home:
            # 备选: 竖排格式
            lines = [l.strip() for l in text.split('\n') if l.strip()]
            if len(lines) >= 3:
                name_lines = []
                for line in lines:
                    # 去掉行末赔率数字, 看剩余文本是否为队名
                    cleaned = re.sub(r'\s*\d+\.\d{2}\s*$', '', line).strip()
                    if not cleaned:
                        name_lines.append('')  # 纯赔率行
                        continue
                    if any(kw in cleaned for kw in ['让球','大小球','波胆','正确比分','亚盘','欧赔','Interwetten']):
                        continue
                    if len(cleaned) <= 20:
                        name_lines.append(cleaned)
                # 取前两个非空的队名行
                teams = [n for n in name_lines if n]
                if len(teams) >= 2:
                    match.home = teams[0]
                    match.away = teams[1]

        # ── 2. 提取1X2赔率 ──
        odds_found = False
        for pattern in self.ODDS_PATTERNS:
            m = re.search(pattern, text)
            if m:
                h = float(m.group('h'))
                d = float(m.group('d'))
                a = float(m.group('a'))
                if 1.01 < h < 50 and 1.01 < d < 50 and 1.01 < a < 50:
                    match.odds_h = h
                    match.odds_d = d
                    match.odds_a = a
                    odds_found = True
                    break

        # 竖排赔率: 从竖列行中提取赔率 (只取1X2部分的, 不含让球/大小球)
        if not odds_found:
            lines = text.split('\n')
            odds_lines = []
            for line in lines:
                stripped = line.strip()
                # 遇到让球/大小球标签就停止提取1X2赔率
                if any(kw in stripped for kw in ['让球','大小球','波胆','亚盘','OU','Handicap']):
                    break
                m = re.search(r'(\d+\.\d{2})\s*$', stripped)
                if m:
                    odds_lines.append(float(m.group(1)))
            if len(odds_lines) >= 3:
                match.odds_h = odds_lines[-3] if len(odds_lines) >= 3 else odds_lines[0]
                match.odds_d = odds_lines[-2] if len(odds_lines) >= 3 else odds_lines[1]
                match.odds_a = odds_lines[-1] if len(odds_lines) >= 3 else odds_lines[2]
                odds_found = True

        # ── 3. 提取让球 ──
        for pattern in self.HANDICAP_PATTERNS:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                match.handicap_line = float(m.group('line'))
                match.handicap_h = float(m.group('h'))
                match.handicap_a = float(m.group('a'))
                break

        # ── 4. 提取大小球 ──
        for pattern in self.OU_PATTERNS:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                match.ou_line = float(m.group('line'))
                match.ou_over = float(m.group('over'))
                match.ou_under = float(m.group('under'))
                break

        # ── 5. 提取波胆 ──
        scores = re.findall(
            r'(?P<hg>\d+)[:\-](?P<ag>\d+)\s*(?:@|赔|赔率)?\s*(?P<odds>\d+\.?\d*)',
            text)
        for hg, ag, odds in scores[:3]:
            match.correct_scores.append((f"{hg}-{ag}", float(odds)))

        # ── 6. 置信度评估 ──
        match.confidence = 0.0
        if match.home and match.away:
            match.confidence += 0.3
        if match.odds_h > 0:
            match.confidence += 0.4
        if match.handicap_line != 0:
            match.confidence += 0.1
        if match.correct_scores:
            match.confidence += 0.1
        if match.ou_over > 0:
            match.confidence += 0.1

        return match

# ═══════════════════════════════════════════════════════════════
# 3. OCR图片识别 (可选 — 需要 pytesseract)
# ═══════════════════════════════════════════════════════════════

class ImageOCR:
    """图片OCR识别 — 博彩截图→文本"""

    OCR_AVAILABLE = False

    @classmethod
    def check_available(cls) -> bool:
        """检查OCR是否可用"""
        if cls.OCR_AVAILABLE:
            return True
        try:
            import pytesseract
            from PIL import Image
            cls.OCR_AVAILABLE = True
            logger.info("[ImageInput] OCR模块可用 (pytesseract + PIL)")
        except ImportError:
            logger.info("[ImageInput] OCR模块不可用 (需要安装 pytesseract + pillow)")
        return cls.OCR_AVAILABLE

    @classmethod
    def extract_text(cls, image_path: str, lang: str = 'chi_sim+eng') -> str:
        """
        从图片提取文字

        Args:
            image_path: 图片路径
            lang: OCR语言 (chi_sim=简体中文, eng=英文)

        Returns:
            提取的文字
        """
        if not cls.check_available():
            return f"[OCR不可用] 请安装: pip install pytesseract pillow\n图片路径: {image_path}"

        try:
            from PIL import Image
            import pytesseract
            img = Image.open(image_path)
            text = pytesseract.image_to_string(img, lang=lang)
            logger.info(f"[ImageInput] OCR完成: {len(text)}字符")
            return text
        except Exception as e:
            logger.error(f"[ImageInput] OCR失败: {e}")
            return f"[OCR错误] {e}"

# ═══════════════════════════════════════════════════════════════
# 4. 统一解析入口
# ═══════════════════════════════════════════════════════════════

class ImageInputParser:
    """
    L1 截图/图片输入解析器

    统一入口: 文本 or 图片 → 结构化比赛数据 → 6层引擎
    """

    def __init__(self):
        self.text_parser = BettingTextParser()
        logger.info("[ImageInput] L1截图输入解析器 就绪")

    def parse(self, source: str, is_image: bool = False) -> ImageParseResult:
        """
        解析输入

        Args:
            source: 文本内容或图片路径
            is_image: True=图片路径, False=文本内容

        Returns:
            ImageParseResult
        """
        result = ImageParseResult(source=source)

        # 获取文字
        if is_image:
            result.ocr_available = ImageOCR.check_available()
            text = ImageOCR.extract_text(source)
            result.source = f"image:{os.path.basename(source)}"
        else:
            text = source
            result.source = "text_input"

        # 解析
        matches = self.text_parser.parse(text)
        result.matches = matches

        if not matches:
            result.errors.append("未能从输入中提取有效比赛数据")
        elif result.valid_count == 0:
            result.errors.append(f"提取到{len(matches)}条记录但赔率数据不完整")

        return result

    def parse_and_predict(self, source: str, is_image: bool = False) -> List[Dict]:
        """
        解析并预测 — 直接对接6层引擎

        Returns:
            [{match, prediction, ...}, ...]
        """
        result = self.parse(source, is_image)
        outputs = []
        for m in result.matches:
            if m.is_valid():
                outputs.append({
                    'home': m.home,
                    'away': m.away,
                    'odds': m.to_odds_dict(),
                    'handicap': m.handicap_line if m.handicap_line else None,
                    'ou_line': m.ou_line if m.ou_over else None,
                    'correct_scores': m.correct_scores,
                    'confidence': m.confidence,
                })
        return outputs

    # ═══════════════════════════════════════════════════════════
    # 前端集成接口 (给杨界面用)
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def frontend_api_spec() -> Dict:
        """
        返回前端集成的API规范 — 杨界面可直接调用
        """
        return {
            "endpoint": "POST /api/v1/parse-image",
            "description": "上传截图或粘贴文本, 返回结构化比赛数据",
            "request": {
                "type": "multipart/form-data or application/json",
                "fields": {
                    "image": "图片文件 (multipart) [可选]",
                    "text": "文本内容 (json body) [可选]",
                    "league_hint": "联赛提示 (可选, 帮助识别)",
                }
            },
            "response": {
                "matches": [
                    {
                        "home": "巴西",
                        "away": "阿根廷",
                        "odds": {"home": 2.10, "draw": 3.30, "away": 3.60},
                        "handicap": {"line": -0.5, "home_odds": 1.90, "away_odds": 1.90},
                        "ou": {"line": 2.5, "over": 1.88, "under": 1.92},
                        "prediction": {"h_prob": 0.45, "d_prob": 0.28, "a_prob": 0.27},
                        "confidence": 0.85
                    }
                ],
                "parse_confidence": 0.9,
                "ocr_available": True
            }
        }

# ═══════════════════════════════════════════════════════════════
# 5. CLI入口
# ═══════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="哨响AI L1截图输入解析")
    parser.add_argument("--image", type=str, help="图片路径")
    parser.add_argument("--text", type=str, help="文本内容")
    parser.add_argument("--predict", action="store_true", help="解析后直接预测")

    args = parser.parse_args()
    ip = ImageInputParser()

    if args.image:
        result = ip.parse(args.image, is_image=True)
    elif args.text:
        result = ip.parse(args.text, is_image=False)
    else:
        # 演示模式
        demo_text = """
        巴西 vs 阿根廷
        胜平负: 2.10 3.30 3.60
        让球 -0.5 1.90 1.90
        大小球 2.5 1.88 1.92
        波胆: 2-1 @7.50
        """
        result = ip.parse(demo_text)

    print(f"解析完成: {result.valid_count}/{len(result.matches)} 场有效")
    for m in result.matches:
        print(f"\n  {m.home} vs {m.away}")
        print(f"  1X2: {m.odds_h:.2f} / {m.odds_d:.2f} / {m.odds_a:.2f}")
        if m.handicap_line:
            print(f"  让球: {m.handicap_line:+.2f} ({m.handicap_h:.2f}/{m.handicap_a:.2f})")
        if m.ou_over:
            print(f"  大小球: {m.ou_line} ({m.ou_over:.2f}/{m.ou_under:.2f})")
        if m.correct_scores:
            print(f"  波胆: {', '.join(f'{s}@{o:.1f}' for s,o in m.correct_scores)}")
        print(f"  置信度: {m.confidence:.0%}")

if __name__ == "__main__":
    main()
