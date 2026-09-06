"""refresh_leisu_session.py — 方案C: 从 Edge cookie 数据库抽取平台会话。

用法:
  1. 在 Edge 手动登录平台 (ylsvq5.vip / realcpf.com 任一口), 通过验证码
  2. **关闭所有 Edge 窗口** (不退出登录, 仅关窗)
  3. 运行: .venv/Scripts/python.exe refresh_leisu_session.py [选项]

选项:
  (无)    抽取 X-API-UUID / X-API-TOKEN / TRACK-HOUR, 写入 config/leisu_session.json
  --all   列出 Edge 里所有平台相关 cookie (诊断模式, 看实际存了什么)

解密: v10/v11=AES-256-GCM(cryptography库) / 旧版=DPAPI / 明文value列优先
"""
import argparse, sqlite3, shutil, os, json, base64, ctypes, sys, time
from ctypes import wintypes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

EDGE_USER_DATA = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")
WANT = ("X-API-UUID", "X-API-TOKEN", "TRACK-HOUR")
PLAT_FRAGS = ("ylsvq5", "realcpf", "u92tiil", "wnbtmel", "vk3whcw",
              "duddmlr", "q8zmruv", "08a2zp", "leyu")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "leisu_session.json")


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def crypt_unprotect(data):
    buf = ctypes.create_string_buffer(data)
    bi = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    bo = DATA_BLOB()
    if ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(bi), None, None, None, None, 0, ctypes.byref(bo)):
        out = ctypes.string_at(bo.pbData, bo.cbData)
        ctypes.windll.kernel32.LocalFree(bo.pbData)
        return out
    return None


def load_aes_key(ls_path):
    with open(ls_path, encoding="utf-8") as f:
        ls = json.load(f)
    blob = base64.b64decode(ls["os_crypt"]["encrypted_key"])
    return crypt_unprotect(blob[5:]) if blob[:5] == b"DPAPI" else None


def decrypt_value(val, enc, aes_key):
    if val:
        return val
    if not enc:
        return ""
    raw = enc if isinstance(enc, bytes) else bytes(enc)
    if raw[:3] in (b"v10", b"v11") and aes_key and len(aes_key) >= 32:
        data = raw[3:]
        if len(data) >= 28:
            nonce, ct = data[:12], data[12:]
            try:
                return AESGCM(aes_key[:32]).decrypt(nonce, ct, None).decode("utf-8", "ignore")
            except Exception:
                pass
    try:
        return (crypt_unprotect(raw) or b"").decode("utf-8", "ignore")
    except Exception:
        return ""


def open_db(db_path):
    """返回 (conn, source_label) 或 (None, 原因)。"""
    if not os.path.exists(db_path):
        return None, "文件不存在"
    dst = f"D:\\Architecture\\_ext_edge_{os.path.basename(db_path)}.db"
    try:
        shutil.copy2(db_path, dst)
        return sqlite3.connect(dst), "副本"
    except Exception as e:
        return sqlite3.connect(db_path), f"直读({e})"


def scan_all(db_path, aes_key, only_want=True):
    """返回 [(name, host, value, status)]。status = 'OK' / 'encrypted' / 'empty'."""
    conn, _ = open_db(db_path)
    if not conn:
        return []
    if only_want:
        ph = ",".join("?" * len(WANT))
        sql = f"SELECT name, host_key, value, encrypted_value FROM cookies WHERE name IN ({ph})"
        params = WANT
    else:
        ph = ",".join("?" * len(PLAT_FRAGS))
        sql = f"SELECT name, host_key, value, encrypted_value FROM cookies WHERE host_key LIKE '%' || ? || '%'"
        params = PLAT_FRAGS
        # 改为 OR 链更准
        ors = " OR ".join(["host_key LIKE ?"] * len(PLAT_FRAGS))
        sql = f"SELECT name, host_key, value, encrypted_value FROM cookies WHERE {ors}"
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    out = []
    for name, host, val, enc in rows:
        v = decrypt_value(val, enc, aes_key)
        if v:
            out.append((name, host, v, "OK"))
        elif val:
            out.append((name, host, val, "OK(明文)"))
        elif enc:
            prefix = enc[:4].hex() if enc else ""
            out.append((name, host, "", f"加密未解({prefix})"))
        else:
            out.append((name, host, "", "空"))
    return out


def _gq_token():
    try:
        for _l in io.open(r"gq/.env", encoding="utf-8"):
            if _l.strip().startswith("GQ_REQUEST_ID="):
                return _l.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="列出 Edge 里所有平台相关 cookie (诊断模式)")
    parser.add_argument("--force", action="store_true", help="跳过 Edge 进程检测, 直接读 cookie DB")
    args = parser.parse_args()

    if not os.path.exists(EDGE_USER_DATA):
        print(f"[FAIL] Edge profile 不存在: {EDGE_USER_DATA}")
        return False

    if not args.force:
        import subprocess
        try:
            r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq msedge.exe', '/FO', 'CSV'],
                               capture_output=True, timeout=5,
                               encoding='utf-8', errors='replace')
            if sum(1 for l in r.stdout.strip().split('\n') if 'msedge' in l.lower()) > 0:
                print("[FAIL] Edge 仍在运行。请关闭所有 Edge 窗口后重试 (或用 --force 强制)。")
                return False
        except Exception:
            pass

    ls_path = os.path.join(EDGE_USER_DATA, "Local State")
    db1 = os.path.join(EDGE_USER_DATA, "Default", "Network", "Cookies")
    db2 = os.path.join(EDGE_USER_DATA, "Default", "Cookies")
    aes_key = load_aes_key(ls_path) if os.path.exists(ls_path) else None
    if aes_key:
        print(f"✓ AES key 加载成功 (len={len(aes_key)})")
    else:
        print("⚠ AES key 加载失败 — 加密 cookie 将无法解密")

    if args.all:
        # 诊断模式: 列出所有平台相关 cookie
        print(f"\n=== 诊断模式: 列出所有平台相关 cookie ===")
        all_rows = scan_all(db1, aes_key, only_want=False)
        # 也扫 db2 如果存在
        if os.path.exists(db2):
            all_rows += scan_all(db2, aes_key, only_want=False)
        if not all_rows:
            print("\n[!] Edge 里完全没有任何平台相关 cookie。")
            print("    可能原因: 未登录 / 登录后被清除 / 平台用 localStorage 而非 cookie")
            return False
        for name, host, val, status in all_rows:
            shown = val[:40] if val else ""
            print(f"  {host:30s} | {name:20s} | {status:18s} | {shown}")
        return True

    # 正常模式: 找 X-API-* 三件套
    print("\n=== 扫描 Network/Cookies ===")
    rows1 = scan_all(db1, aes_key, only_want=True)
    found = {n: v for n, h, v, s in rows1 if s.startswith("OK")}
    for n, h, v, s in rows1:
        print(f"  {h} | {n} = {v[:30] if v else '...'} [{s}]")
    if os.path.exists(db2):
        print("\n=== 扫描 Default/Cookies ===")
        rows2 = scan_all(db2, aes_key, only_want=True)
        for n, h, v, s in rows2:
            if s.startswith("OK"):
                found.setdefault(n, v)
            print(f"  {h} | {n} = {v[:30] if v else '...'} [{s}]")

    missing = [n for n in WANT if n not in found]
    if missing:
        print(f"\n[FAIL] 缺少必要 cookie: {missing}")
        print("→ 建议运行: .venv/Scripts/python.exe refresh_leisu_session.py --all")
        print("  以查看 Edge 里实际有哪些平台 cookie")
        return False

    cfg = {n: found[n] for n in WANT}
    cfg["token"] = _gq_token()
    cfg["captured_at"] = int(time.time())
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(cfg, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n[OK] 已写入 {OUT}")
    print("→ 重启 bridge_service 后赛程列表即有真实数据")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
