"""
哨响AI 操盘终端 — 打包构建脚本
==============================
一键构建独立 exe 文件。

用法:
    python desktop_app/build_exe.py

产物:
    dist/哨响AI_操盘终端.exe (约 150-250MB, 含全部依赖)
"""
import subprocess, sys, os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_PATH = os.path.join(PROJECT_ROOT, "desktop_app", "app.spec")

def main():
    print("=" * 60)
    print("  哨响AI 操盘手辅助决策终端 — PyInstaller 打包")
    print("=" * 60)

    # 确保 pyinstaller 可用
    try:
        import PyInstaller
    except ImportError:
        print("[错误] 未安装 pyinstaller, 请先执行:")
        print("  pip install pyinstaller")
        sys.exit(1)

    print(f"\n[1/3] 规范文件: {SPEC_PATH}")
    print(f"[2/3] 项目目录: {PROJECT_ROOT}")
    print(f"[3/3] 开始打包...\n")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--clean", "--noconfirm",
        "--distpath", os.path.join(PROJECT_ROOT, "dist"),
        "--workpath", os.path.join(PROJECT_ROOT, "build"),
        SPEC_PATH,
    ]

    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    if result.returncode == 0:
        exe_path = os.path.join(PROJECT_ROOT, "dist", "哨响AI_操盘终端.exe")
        size_mb = os.path.getsize(exe_path) / (1024 * 1024) if os.path.exists(exe_path) else 0
        print(f"\n✅ 打包成功!")
        print(f"   产物: {exe_path}")
        print(f"   大小: {size_mb:.1f} MB")
        print(f"\n   运行: 双击 '哨响AI_操盘终端.exe' 或命令行执行")
    else:
        print(f"\n❌ 打包失败, 返回码: {result.returncode}")
        sys.exit(result.returncode)

if __name__ == "__main__":
    main()
