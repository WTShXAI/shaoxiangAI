def fuse_lambda(lambda_model_h, lambda_model_a, lambda_book_h, lambda_book_a,
                alpha=0.65, beta=0.35, is_tactical_shift=False, is_final=False):
    if is_tactical_shift: alpha, beta = 0.75, 0.25
    if is_final: alpha, beta = 0.55, 0.45
    return alpha*lambda_model_h+beta*lambda_book_h, alpha*lambda_model_a+beta*lambda_book_a

if __name__ == "__main__":
    # 验证: 葡萄牙vs刚果
    for name, ts, fin in [("正常",False,False),("战术剧变",True,False),("决赛",False,True)]:
        lh, la = fuse_lambda(1.90, 1.10, 2.40, 0.70, is_tactical_shift=ts, is_final=fin)
        print(f"{name}: λ_H={lh:.3f}, λ_A={la:.3f}, diff={lh-la:.3f}")
