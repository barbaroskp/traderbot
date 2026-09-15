import warnings, logging; warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
import pandas as pd
from research.replay.run import load, universe_for
from research.replay.sweep import split_run
from src.config import Settings
m5,m15,spreads,funding=load(40)
times=next(iter(m5.values())).t; cut=times.iloc[len(times)//2]
base=dict(_env_file=None,db_path=":memory:",log_file="",initial_capital_usdt=10000.0,
          risk_state_disabled=False)
Z=0.0
VARIANTS=[
 ("baseline (24 indikator)", {}),
 ("sadece vwap atildi", {"weight_vwap":Z}),
 ("en kotu 5 atildi", {"weight_vwap":Z,"weight_taker_ratio":Z,"weight_squeeze":Z,
                        "weight_macd":Z,"weight_momentum":Z}),
 ("YAPISAL: orderflow atildi", {"weight_orderbook":Z,"weight_taker_ratio":Z,"weight_whale":Z}),
 ("sadece en iyi 5 kaldi", {k:Z for k in
    ["weight_vwap","weight_taker_ratio","weight_squeeze","weight_macd","weight_momentum",
     "weight_orderbook","weight_whale","weight_obv","weight_oi","weight_velocity",
     "weight_liq_cascade","weight_stoch_rsi","weight_williams_r","weight_breakout"]}),
 ("YAPISAL: olculemeyenler atildi", {"weight_orderbook":Z,"weight_taker_ratio":Z,
    "weight_whale":Z,"weight_oi":Z,"weight_liq_cascade":Z}),
]
cfg0=Settings(**base); uni=universe_for(cfg0,spreads,m5)
print(f"evren {len(uni)} sembol · fit {times.iloc[0]:%d %b} → {cut:%d %b} · "
      f"test {cut:%d %b} → {times.iloc[-1]:%d %b}\n")
print(f"  {'varyant':32s} {'FIT':>9s} {'islem':>7s} {'TEST':>9s} {'islem':>7s} {'isabet':>7s}")
print("  "+"-"*76)
for lbl,over in VARIANTS:
    cfg=Settings(**{**base,**over})
    try: fit,test=split_run(cfg,m5,m15,spreads,funding,uni,10000.0,cut)
    except Exception as e:
        print(f"  {lbl:32s} HATA: {e}"); continue
    sf,st=fit.stats(10000.0),test.stats(10000.0)
    print(f"  {lbl:32s} {sf.get('total',0)*100:+8.2f}% {sf.get('trades',0):7d} "
          f"{st.get('total',0)*100:+8.2f}% {st.get('trades',0):7d} "
          f"{st.get('win_rate',0)*100:6.0f}%")
