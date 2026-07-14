import io
p = 'pipeline/reverse_odds_engine.py'
s = open(p, encoding='utf-8').read()
reps = [
    ('    rlm_proxy: Optional[float] = None    # 跨机构分歧度(代理RLM, 非真bet-split)\n',
     '    rlm_proxy: Optional[float] = None    # 跨机构分歧度(代理RLM, 非真bet-split)\n'
     '    rlm_real: Optional[dict] = None     # 真 bet-split 源(dict: home/draw/away_pct+sharp_side), G4 接入; None=用代理\n'),
    ('    def analyze_multi(self, books: List[OddsInput]) -> AnalysisResult:\n',
     '    @staticmethod\n'
     '    def _to_rlm_dict(rlm_real):\n'
     '        if rlm_real is None:\n'
     '            return None\n'
     '        if hasattr(rlm_real, \'home_pct\'):\n'
     '            return {\'home_pct\': rlm_real.home_pct, \'draw_pct\': rlm_real.draw_pct,\n'
     '                    \'away_pct\': rlm_real.away_pct, \'sharp_side\': rlm_real.sharp_side()}\n'
     '        if isinstance(rlm_real, dict):\n'
     '            d = dict(rlm_real)\n'
     '            if \'sharp_side\' not in d and \'home_pct\' in d:\n'
     '                mx = max(d[\'home_pct\'], d[\'draw_pct\'], d[\'away_pct\'])\n'
     '                d[\'sharp_side\'] = \'H\' if mx == d[\'home_pct\'] else (\'D\' if mx == d[\'draw_pct\'] else \'A\')\n'
     '            return d\n'
     '        return None\n'
     '\n'
     '    def analyze_multi(self, books: List[OddsInput], rlm_real: Optional[object] = None) -> AnalysisResult:\n'),
    ('        if rlm_proxy is not None:\n'
     '            verdict += f" | RLM代理(跨机构离散){rlm_proxy:.3f}(高=分歧, 非真bet-split)"\n',
     '        if rlm_real is not None:\n'
     '            rd = self._to_rlm_dict(rlm_real)\n'
     '            if rd:\n'
     '                ss = rd.get(\'sharp_side\')\n'
     '                verdict += (f" | RLM真源: 投注集中{ss} "\n'
     '                            f"(H{rd[\'home_pct\']:.0%}/D{rd[\'draw_pct\']:.0%}/A{rd[\'away_pct\']:.0%})")\n'
     '        elif rlm_proxy is not None:\n'
     '            verdict += f" | RLM代理(跨机构离散){rlm_proxy:.3f}(高=分歧, 非真bet-split)"\n'),
    ('            cross_book_sync=ci["cross_book_sync"], clv_beat=clv, rlm_proxy=rlm_proxy,\n',
     '            cross_book_sync=ci["cross_book_sync"], clv_beat=clv, rlm_proxy=rlm_proxy,\n'
     '            rlm_real=self._to_rlm_dict(rlm_real),\n'),
    ('            cross_book_sync=None, clv_beat=None, rlm_proxy=None,\n',
     '            cross_book_sync=None, clv_beat=None, rlm_proxy=None, rlm_real=None,\n'),
]
for i, (o, n) in enumerate(reps):
    assert o in s, f'pattern {i} not found in {p}'
    s = s.replace(o, n, 1)
open(p, 'w', encoding='utf-8').write(s)
print('patched', len(reps), 'places in', p)
