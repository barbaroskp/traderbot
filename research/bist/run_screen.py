import warnings; warnings.filterwarnings("ignore")
import pandas as pd, numpy as np, pickle
from src.bist.forensics import analyse
from src.bist.screen import Candidate, ScreenConfig, score, build_portfolio, explain
from research.bist.universe import TICKERS
UNIV=sorted(set(TICKERS)|{"GESAN","EUPWR","ASTOR","SMRTG","REEDR","KONTR","ENERY","TERA"})
S=pickle.load(open("research/bist/data/stmts2.pkl","rb"))
I=pickle.load(open("research/bist/data/info2.pkl","rb"))
PX=pickle.load(open("research/bist/data/px2.pkl","rb"))
cands=[]
for t in UNIV:
    i=I.get(t) or {}
    if not i.get("marketCap"): continue
    fs=S.get(t); fo=analyse(t,*fs) if fs and fs[0] is not None and not fs[0].empty else None
    d=PX.get(t); mom=np.nan; tv=np.nan
    if d is not None and len(d)>270:
        c=d["Close"].values; mom=c[-21]/c[-252]-1
        tv=float((d["Volume"]*d["Close"]).tail(21).mean())
    cands.append(Candidate(ticker=t,market_cap=i.get("marketCap"),turnover=tv,
        pb=i.get("priceToBook") or np.nan,pe=i.get("trailingPE") or np.nan,
        roe_nominal=i.get("returnOnEquity") if i.get("returnOnEquity") is not None else np.nan,
        profit_margin=i.get("profitMargins") if i.get("profitMargins") is not None else np.nan,
        debt_to_equity=i.get("debtToEquity") or np.nan,mom_12_1=mom,forensics=fo))
R=score(cands,ScreenConfig()); el=R[R.eligible]
print(f"{len(cands)} aday -> {len(el)} uygun")
print("elenme: "+" · ".join(f"{k} {v}" for k,v in R[~R.eligible].reject.value_counts().items()))
print(f"\nreel ROE > 0 olan: {(el.roe_real>0).sum()}/{len(el)} · uygun evren medyani %{el.roe_real.median()*100:+.1f}\n")
print(f"  {'#':>2s} {'hisse':7s} {'skor':>5s} {'deger':>6s} {'kalite':>7s} {'mom':>5s} {'P/B':>5s} {'F/K':>6s} {'reelROE':>8s} {'sev':>4s}")
for n,(_,r) in enumerate(el.head(15).iterrows(),1):
    print(f"  {n:2d} {r.tic:7s} {r.total:5.3f} {r.value:6.2f} {r.quality:7.2f} {r.momentum:5.2f} {r.pb:5.2f} {r.pe:6.1f} {r.roe_real*100:+7.1f}% {r.sev:4.0f}")
h=el.head(15)
print(f"\n  medyan F/K {h.pe.median():.1f} · P/B {h.pb.median():.2f} · reel ROE %{h.roe_real.median()*100:+.1f} · adli severity medyan {h.sev.median():.0f}")
v=R[R.reject=="forensic veto"].sort_values("sev",ascending=False)
print(f"\nADLI VETO ({len(v)}): "+", ".join(f"{r.tic}({r.sev:.0f})" for _,r in v.iterrows()))
b=R[R.reject.isin(["implausible P/B","implausible P/E"])]
if len(b): print(f"VERI HATASI ELENEN: "+", ".join(f"{r.tic}(P/B {r.pb:.1f})" for _,r in b.iterrows()))
