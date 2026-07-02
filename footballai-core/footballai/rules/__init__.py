# footballai-core/footballai/rules — 代理到主 rules/ 包 (2026-06-28)
# 所有规则文件统一存储于 D:\Architecture v4.0\rules\
# footballai-core 在此处仅做 re-export

import sys as _sys
from pathlib import Path as _Path

# 确保项目根在 sys.path 中
_project_root = str(_Path(__file__).resolve().parents[2])  # footballai-core/ 的上两层 = 项目根
if _project_root not in _sys.path:
    _sys.path.insert(0, _project_root)

from rules.d_gate_utils import ALL_RESULTS, COVER_DB, STAR_PLAYERS  # noqa
from rules.d_gate_engine import apply_dgate_v51  # noqa
from rules.drawgate_v53 import apply_drawgate, imp_from_odds, detect_match_type  # noqa

# 向后兼容: 让 footballai.rules.d_gate_engine / drawgate_v53 / d_gate_utils 指向主 rules/
# (v5.2兼容已废弃: d_gate_v52 → d_gate_utils, 2026-07-01)
import rules.d_gate_engine as _d_gate_engine
import rules.drawgate_v53 as _drawgate_v53
import rules.d_gate_utils as _d_gate_utils

_sys.modules['footballai.rules.d_gate_v52'] = _d_gate_utils  # DEPRECATED 向后兼容
_sys.modules['footballai.rules.d_gate_engine'] = _d_gate_engine
_sys.modules['footballai.rules.drawgate_v53'] = _drawgate_v53
_sys.modules['footballai.rules.d_gate_utils'] = _d_gate_utils
