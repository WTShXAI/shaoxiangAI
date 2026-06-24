def apply_trap_correction(p_h, p_d, p_a, score_trap):
    if score_trap < 2.0: delta = 0.04
    elif score_trap <= 2.8: delta = 0
    elif score_trap <= 3.5: delta = -0.03
    else: delta = -0.06
    p_h_new = max(0.01, p_h + delta)
    total = p_h_new + p_d + p_a
    return p_h_new/total, p_d/total, p_a/total

if __name__ == "__main__":
    # 西班牙0-0佛得角 (score=5.5) vs 澳大利亚2-0土耳其 (score=0.9)
    for name, ph, pd, pa, sc in [("西班牙0-0",0.55,0.25,0.20,5.5),("澳大利亚2-0",0.62,0.22,0.16,0.9)]:
        h,d,a = apply_trap_correction(ph,pd,pa,sc)
        print(f"{name} (score={sc}): H={h:.3f} D={d:.3f} A={a:.3f}")
