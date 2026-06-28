def build_stacking_vector(lam_h, lam_a, ph, pd, pa, score_trap, max_rp, w_hist, w_tactic, tact_mod, inj_core, s_league):
    return [lam_h, lam_a, lam_h-lam_a, ph, pd, pa, score_trap, max_rp, w_hist, w_tactic, tact_mod, inj_core, s_league]

if __name__ == "__main__":
    v = build_stacking_vector(2.00, 0.85, 0.55, 0.28, 0.17, 1.5, 1.2, 0.66, 0.24, 0.35, 0.0, 1.05)
    print(f"dim={len(v)}, vector={v}")
