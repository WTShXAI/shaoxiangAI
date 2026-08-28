import base64, binascii
from Crypto.Cipher import AES

API_RAW = "uh/LZT2pBfFTzjlRGc/l4qnUR13UCb5Sh12gTXvNg84="
keys = [b"OBTY20220712OBTY", b"panda1234_1234ob"]
inputs = {
    "full": API_RAW,
    "no_uh": API_RAW.split("/",1)[1] if "/" in API_RAW else API_RAW,
}

def printable(b):
    try:
        s = b.decode("utf-8", "strict")
    except Exception:
        return None
    if all(32 <= ord(c) < 127 or c in "\n\r\t" for c in s):
        return s
    return None

for kn, key in enumerate(keys):
    k16 = key[:16]
    for iname, inp in inputs.items():
        try:
            ct = base64.b64decode(inp)
        except binascii.Error:
            # try url-safe / add padding
            try:
                ct = base64.b64decode(inp + "=" * ((4 - len(inp) % 4) % 4))
            except Exception:
                continue
        print(f"[key={kn} {iname}] ct_len={len(ct)}")
        # ECB
        try:
            pt = AES.new(k16, AES.MODE_ECB).decrypt(ct)
            s = printable(pt); print("   ECB:", repr(s) if s else pt[:24].hex())
        except Exception as e:
            print("   ECB err:", e)
        # CBC zero IV
        try:
            pt = AES.new(k16, AES.MODE_CBC, b"\x00"*16).decrypt(ct)
            s = printable(pt); print("   CBC0:", repr(s) if s else pt[:24].hex())
        except Exception as e:
            print("   CBC0 err:", e)
        # CBC iv=key
        try:
            pt = AES.new(k16, AES.MODE_CBC, k16).decrypt(ct)
            s = printable(pt); print("   CBCk:", repr(s) if s else pt[:24].hex())
        except Exception as e:
            print("   CBCk err:", e)
        # CTR
        try:
            pt = AES.new(k16, AES.MODE_CTR, nonce=b"").decrypt(ct)
            s = printable(pt); print("   CTR :", repr(s) if s else pt[:24].hex())
        except Exception as e:
            print("   CTR err:", e)
        # CFB
        try:
            pt = AES.new(k16, AES.MODE_CFB, b"\x00"*16).decrypt(ct)
            s = printable(pt); print("   CFB0:", repr(s) if s else pt[:24].hex())
        except Exception as e:
            print("   CFB0 err:", e)
