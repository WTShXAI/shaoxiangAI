import re
lines = open(r"D:\Architecture\read_cs3.log", encoding="utf-8").read().splitlines()
out = []
for ln in lines:
    # 含 比分(数字:数字) 且 含赔率小数
    if re.search(r"\d+[:：]\d+", ln) and re.search(r"\d+\.\d{2,3}", ln):
        out.append(ln.strip())
print("波胆/比分相关行数:", len(out))
for o in out[:60]:
    print(o[:300])
    print("-"*60)
