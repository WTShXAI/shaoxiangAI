"""验证 残留②: bridge_service.py:413 的 detail 已非裸对象(list[dict]) → str.

做法:
  1. 运行时校验 core.error_envelope.coerce_message 对任意对象(含 list[dict])恒返回 str;
  2. 静态校验 bridge_service.py 的校验失败处理器已改为 detail=coerce_message(exc.errors())。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.error_envelope import coerce_message  # noqa: E402


def main() -> None:
    # 1) 运行时: 对象型异常(当年白屏同源) → 强制 str
    obj = [{"loc": ["body", "odds"], "msg": "数值错误", "type": "value_error"}]
    out = coerce_message(obj)
    assert isinstance(out, str), "coerce_message(list[dict]) 必须返回 str"
    assert not isinstance(out, (list, dict)), "不得返回裸对象(list/dict)"
    print("PASS residual2 runtime: coerce_message(list[dict]) -> str:", out[:80])

    # 2) 静态: bridge_service.py 校验失败处理器已改为 "detail": coerce_message(exc.errors())
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bs_path = os.path.join(root, "bridge_service.py")
    with open(bs_path, encoding="utf-8") as f:
        lines = f.readlines()
    hit = None
    for ln in lines:
        if "coerce_message(exc.errors())" in ln:
            hit = ln.strip()
            break
    assert hit is not None, "bridge_service.py 未找到 coerce_message(exc.errors())"
    # 确认不再是当年白屏同源的裸对象写法 detail=exc.errors()
    assert '"detail": exc.errors()' not in hit, "不得仍是裸对象 detail=exc.errors()"
    print("PASS residual2 static: bridge_service.py ->", hit)
    print("RESIDUAL2: detail IS str (NOT list[dict])")


if __name__ == "__main__":
    main()
