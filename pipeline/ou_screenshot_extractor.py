"""
2026WC Screenshot OU Extractor v1.0
====================================
固定路径: D:\Architecture v4.0\2026WC\{date}/*.png
用途: 从赛前截图提取外围OU大小球盘口, 用于OU约束层

原理: 庄家Poisson模型算出的OU线是市场共识, 精度远高于我们自己的λ估计。
6/13截图→6/28比赛: 证明庄家提前14天就能定OU, 因为球队GF/GA短期不变。

用法:
    python pipeline/ou_screenshot_extractor.py 6.28
"""
import os, re, json, sys
from pathlib import Path
from typing import Dict, Optional, List, Tuple

PROJECT_ROOT = Path(__file__).parent.parent
SCREENSHOT_DIR = PROJECT_ROOT / '2026WC'

# 已知匹配: 文件名关键词 → 标准化对阵
# 因为OCR可能无法完美识别中文队名, 用文件名兜底
MATCH_FROM_FILENAME = {
    '克罗地亚vs加纳': ('克罗地亚', '加纳'),
    '巴拿马vs英格兰': ('巴拿马', '英格兰'),
    '哥伦比亚vs葡萄牙': ('哥伦比亚', '葡萄牙'),
    '民主刚果vs乌兹别克斯坦': ('民主刚果', '乌兹别克斯坦'),
    '民主刚果vs乌兹别克': ('民主刚果', '乌兹别克斯坦'),
    '刚果vs乌兹别克': ('民主刚果', '乌兹别克斯坦'),
    '刚果金vs乌兹别克': ('民主刚果', '乌兹别克斯坦'),
    '阿尔及利亚vs奥地利': ('阿尔及利亚', '奥地利'),
    '约旦vs阿根廷': ('约旦', '阿根廷'),
}

def extract_ou_from_text(text: str) -> Optional[Dict]:
    """
    从OCR文本中提取OU盘口数据
    
    期望格式 (外围盘口截图常见):
      大 2.5  1.95
      小 2.5  1.85
    或:
      Over 2.5 @1.95
      Under 2.5 @1.85
    或中文:
      大于2.5  1.92
      小于2.5  1.88
    """
    ou_line = None
    ou_over = None
    ou_under = None
    
    # Pattern 1: "大 X.XX Y.YY" + "小 X.XX Z.ZZ"
    m = re.search(r'[大Oo]ver?\s*[：:]?\s*(\d+\.?\d*)\s*[@]?\s*(\d+\.\d+)', text, re.I)
    if m:
        ou_line = float(m.group(1))
        ou_over = float(m.group(2))
    
    m = re.search(r'[小Uu]nder?\s*[：:]?\s*(\d+\.?\d*)\s*[@]?\s*(\d+\.\d+)', text, re.I)
    if m:
        if ou_line is None:
            ou_line = float(m.group(1))
        ou_under = float(m.group(2))
    
    # Pattern 2: Chinese "大于X.X" / "小于X.X"  
    if ou_line is None:
        m = re.search(r'[大于超][于过]?\s*(\d+\.?\d*)\s*球?\s*[@]?\s*(\d+\.\d+)', text)
        if m:
            ou_line = float(m.group(1))
            ou_over = float(m.group(2))
        m = re.search(r'[小于低][于过]?\s*(\d+\.?\d*)\s*球?\s*[@]?\s*(\d+\.\d+)', text)
        if m:
            if ou_line is None:
                ou_line = float(m.group(1))
            ou_under = float(m.group(2))
    
    # Pattern 3: OU line in range 1.5-5.0 with odds around it
    if ou_line is None:
        candidates = re.findall(r'(\d+\.\d)\s+(\d+\.\d{2})', text)
        for val, odd in candidates:
            v = float(val)
            if 1.5 <= v <= 5.0 and v in [1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.25, 3.5, 4.0, 4.5, 5.0]:
                if ou_line is None:
                    ou_line = v
                    ou_over = float(odd)
                elif ou_under is None:
                    ou_under = float(odd)
                    break
    
    if ou_line:
        return {
            'ou_line': ou_line,
            'ou_over': ou_over,
            'ou_under': ou_under,
            'source': 'screenshot_ocr'
        }
    return None

def parse_screenshots(date_str: str = '6.28', use_ocr: bool = True) -> Dict[str, Dict]:
    """
    扫描 2026WC/{date}/ 下的所有截图, 提取OU数据
    
    Returns:
        {match_key: {ou_line, ou_over, ou_under, source}}
    """
    scan_dir = SCREENSHOT_DIR / date_str
    if not scan_dir.exists():
        print(f'[OU Extractor] 目录不存在: {scan_dir}')
        return {}
    
    results = {}
    png_files = sorted(scan_dir.glob('*.png'))
    
    for fp in png_files:
        fname = fp.stem  # 文件名不含扩展名
        match_info = None
        
        # 从文件名匹配
        for key, (home, away) in MATCH_FROM_FILENAME.items():
            if key in fname or key.replace('vs', 'vs') in fname:
                match_info = {'home': home, 'away': away, 'match': f'{home}vs{away}'}
                break
        
        if not match_info:
            # 尝试拆分 "XXvsYY" 格式
            parts = re.split(r'[vV][sS]', fname, maxsplit=1)
            if len(parts) == 2:
                match_info = {'home': parts[0].strip(), 'away': parts[1].strip(), 
                             'match': f'{parts[0].strip()}vs{parts[1].strip()}'}
        
        if not match_info:
            continue
        
        ou_data = None
        
        if use_ocr:
            try:
                import easyocr
                reader = easyocr.Reader(['ch_sim', 'en'], gpu=False, verbose=False)
                ocr_result = reader.readtext(str(fp), detail=0)
                text = '\n'.join(ocr_result)
                ou_data = extract_ou_from_text(text)
            except ImportError:
                print(f'[OU Extractor] easyocr未安装, 跳过OCR')
                use_ocr = False
            except Exception as e:
                print(f'[OU Extractor] OCR失败 {fname}: {e}')
        
        results[match_info['match']] = ou_data or {'error': 'OCR_failed'}
        
        if ou_data:
            print(f'  {fname}: OU={ou_data["ou_line"]} Over={ou_data["ou_over"]} Under={ou_data["ou_under"]}')
        else:
            print(f'  {fname}: 未提取到OU数据')
    
    return results

def integrate_ou_to_pipeline(date_str: str = '6.28') -> bool:
    """
    将提取的OU数据写入管道可读取的位置
    链3.5 OU约束层会读取此数据
    """
    results = parse_screenshots(date_str, use_ocr=True)
    if not results:
        return False
    
    # 保存
    out_path = PROJECT_ROOT / 'data' / f'ou_screenshot_{date_str.replace(".", "_")}.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    print(f'\n[OU Extractor] 已保存: {out_path} ({len(results)}场)')
    return True

if __name__ == '__main__':
    date = sys.argv[1] if len(sys.argv) > 1 else '6.28'
    integrate_ou_to_pipeline(date)
