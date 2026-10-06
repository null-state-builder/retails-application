#!/usr/bin/env python3
"""
Hazaribagh store report — item-wise stock holding (MRP), sale-through, discount,
actual sales and category-wise margin.

Run:  python3 build-report.py      (needs pandas + openpyxl; reads the two .xlsx in this folder)
Out:  Hazaribagh-Stock-Sales-Margin.html
"""
#!/usr/bin/env python3
"""Hazaribagh: item-wise stock / sell-through / discount / net sales / category margin."""
import pandas as pd, numpy as np, json, re, os

HERE=os.path.dirname(os.path.abspath(__file__))
SALES=os.path.join(HERE,'Sales_Hazaribagh.xlsx')
SOHF =os.path.join(HERE,'SOH_Hazaribagh.xlsx')

ITEM_MERGE={"SAREES":"SAREE","SWEAT SHIRT":"SWEATSHIRT","TROUSERS":"TROUSER","KURTA SET":"KURTI SET",
            "SWEATER":"SWEATSHIRT" if False else "SWEATER","TROLLEY":"TROLLY","HARD TROLLY":"TROLLY",
            "DUFFLE BAG":"DUFFEL BAG","BELTS":"BELT","HANDKERCHIEFS":"HANDKERCHIEF","JACKET (FS)":"JACKET",
            "T SHIRT":"T-SHIRT","TSHIRT":"T-SHIRT","SILK SAREE":"SAREE","LADIES PURSE":"PURSE"}
BRAND_ALIAS={"LP":"LOUIS PHILIPPE","LY":"LOUIS PHILIPPE","LR":"LOUIS PHILIPPE","VH":"VAN HEUSEN",
             "VS":"VAN HEUSEN","VD":"VAN HEUSEN","VF":"VAN HEUSEN","VX":"VAN HEUSEN","AS":"ALLEN SOLLY",
             "PE":"PETER ENGLAND","USPA":"U.S. POLO","U. S. POLO":"U.S. POLO","US POLO":"U.S. POLO"}
CAT_FIX={"FORMAL WAER":"FORMAL WEAR","PARTY WAER":"PARTY WEAR","ACCE":"ACCESSORIES",
         "ACCM":"ACCESSORIES","ACCESSORIES & OTHER ITEMS":"ACCESSORIES","SILK SAREE":"TRADITIONAL WEAR",
         "SAREE":"TRADITIONAL WEAR","SAREEM":"TRADITIONAL WEAR","BLOUSE":"TRADITIONAL WEAR",
         "LEHP":"TRADITIONAL WEAR","TROUSER":"CASUAL WEAR","FULL SET":"CASUAL WEAR","CAPE":"WINTER WEAR"}
VALID={"CASUAL WEAR","FORMAL WEAR","PARTY WEAR","SEASONAL WEAR","NIGHTWEAR","ACCESSORIES",
       "TRADITIONAL WEAR","INNERWEAR","SPORTS WEAR","WINTER WEAR","WINTER SET","FABRIC","PROMOTIONAL"}
PACKAGING={"CARRY BAG","POLY BAG","PROMO BAG"}

U=lambda s:s.astype(str).str.upper().str.strip()
def nitem(s): return U(s).replace(ITEM_MERGE)
def nbrand(s): return U(s).replace(BRAND_ALIAS)

# ---------- load SOH ----------
soh=pd.read_excel(SOHF,sheet_name='Sheet1')
soh=soh[soh['Barcode'].notna() & soh['Item Name'].notna()].copy()      # drops hidden grand-total row
for c in ['Tqty','Mrp','Rate','Amount']: soh[c]=pd.to_numeric(soh[c],errors='coerce').fillna(0)
soh['item']=nitem(soh['Item Name']); soh['brand']=nbrand(soh['Brand'])
soh['cat_raw']=U(soh['Category']).replace(CAT_FIX)
soh['cat']=soh['cat_raw'].where(soh['cat_raw'].isin(VALID))
soh['bc']=U(soh['Barcode'])
soh['stk_mrp']=soh['Tqty']*soh['Mrp']; soh['stk_cost']=soh['Tqty']*soh['Rate']

# ---------- load SALES ----------
s=pd.read_excel(SALES,sheet_name='Sheet1')
s=s[s['Bill Date'].notna() & s['Item'].notna()].copy()                 # drops trailing total rows
for c in ['Bill No','Customer','Phone']: s[c]=s[c].ffill()
for c in ['Qty','Rate','Gross Amt','Disc%','Disc Amt','Net Amount']: s[c]=pd.to_numeric(s[c],errors='coerce')
s=s[s['Qty'].notna()].copy()
s['Bill Date']=pd.to_datetime(s['Bill Date'],errors='coerce')
s['ym']=s['Bill Date'].dt.to_period('M').astype(str)
s['item']=nitem(s['Item']); s['brand']=nbrand(s['Brand']); s['bc']=U(s['Barcode'])
s['disc']=-s['Disc Amt'].fillna(0)                                     # discount as a positive number
s['Gross Amt']=s['Gross Amt'].fillna(0); s['Net Amount']=s['Net Amount'].fillna(0)

# ---------- category map: SOH is the authority; fill gaps by item ----------
w=soh[soh['cat'].notna()]
cat_bc  =w.groupby('bc')['cat'].agg(lambda x:x.mode().iat[0])
cat_bi  =w.groupby(['brand','item'])['cat'].agg(lambda x:x.mode().iat[0])
cat_i   =w.groupby('item')['cat'].agg(lambda x:x.mode().iat[0])
def fill_cat(df, have=None):
    out = have.copy() if have is not None else pd.Series(np.nan,index=df.index,dtype=object)
    out = out.fillna(df['bc'].map(cat_bc))
    out = out.fillna(pd.Series(list(zip(df['brand'],df['item'])),index=df.index).map(cat_bi))
    out = out.fillna(df['item'].map(cat_i))
    return out
soh['cat']=fill_cat(soh, soh['cat'])
s['cat']  =fill_cat(s)
s.loc[s['item'].isin(PACKAGING),'cat']='PACKAGING'; soh.loc[soh['item'].isin(PACKAGING),'cat']='PACKAGING'
cat_cov = s['cat'].notna().mean()
s['cat']=s['cat'].fillna('OTHER / UNCLASSIFIED'); soh['cat']=soh['cat'].fillna('OTHER / UNCLASSIFIED')

# ---------- cost ratio cascade (cost as a share of MRP) ----------
v=soh[(soh['Mrp']>0)&(soh['Rate']>0)&(soh['Tqty']>0)]
def ratio(g): return g['stk_cost'].sum()/g['stk_mrp'].sum()
r_all = ratio(v)
r_bc  = v.groupby('bc').apply(ratio, include_groups=False)
r_bi  = v.groupby(['brand','item']).apply(ratio, include_groups=False)
r_i   = v.groupby('item').apply(ratio, include_groups=False)
r_b   = v.groupby('brand').apply(ratio, include_groups=False)
r_c   = v.groupby('cat').apply(ratio, include_groups=False)
tier=pd.Series('overall',index=s.index)
cr = s['bc'].map(r_bc);                                            tier[cr.notna()]='barcode'
n = cr.isna(); t2=pd.Series(list(zip(s['brand'],s['item'])),index=s.index).map(r_bi)
cr=cr.fillna(t2);                                                  tier[n & t2.notna()]='brand+item'
n = cr.isna(); t3=s['item'].map(r_i);   cr=cr.fillna(t3);          tier[n & t3.notna()]='item'
n = cr.isna(); t4=s['brand'].map(r_b);  cr=cr.fillna(t4);          tier[n & t4.notna()]='brand'
n = cr.isna(); t5=s['cat'].map(r_c);    cr=cr.fillna(t5);          tier[n & t5.notna()]='category'
cr=cr.fillna(r_all)
s['cost_ratio']=cr.clip(0.05,1.20); s['match']=tier
s['cogs']=s['Gross Amt']*s['cost_ratio']
s['margin']=s['Net Amount']-s['cogs']

# ---------- merchandise vs packaging ----------
mS=s[~s['item'].isin(PACKAGING)].copy();  pS=s[s['item'].isin(PACKAGING)].copy()
mH=soh[~soh['item'].isin(PACKAGING)].copy(); pH=soh[soh['item'].isin(PACKAGING)].copy()

def agg_sales(df,key):
    g=df.groupby(key).agg(sold_qty=('Qty','sum'),mrp_val=('Gross Amt','sum'),disc=('disc','sum'),
                          net=('Net Amount','sum'),cogs=('cogs','sum'),lines=('Qty','size'))
    return g
def agg_soh(df,key):
    return df.groupby(key).agg(stk_qty=('Tqty','sum'),stk_mrp=('stk_mrp','sum'),stk_cost=('stk_cost','sum'),
                               skus=('Tqty','size'))
def build(key):
    t=agg_soh(mH,key).join(agg_sales(mS,key),how='outer').fillna(0)
    t['margin']=t['net']-t['cogs']
    t['margin_pct']=np.where(t['net']!=0,t['margin']/t['net']*100,0)
    t['disc_pct']=np.where(t['mrp_val']!=0,t['disc']/t['mrp_val']*100,0)
    t['sellthru']=np.where((t['sold_qty']+t['stk_qty'])>0,t['sold_qty']/(t['sold_qty']+t['stk_qty'])*100,0)
    t['cover_mo']=np.where(t['sold_qty']>0,t['stk_qty']/(t['sold_qty']/11.0),np.inf)
    return t.sort_values('stk_mrp',ascending=False)

items=build('item'); cats=build('cat'); brands=build('brand')

# ---------- headline ----------
n_mo=mS['ym'].nunique()
H=dict(
 period_from=str(mS['Bill Date'].min().date()), period_to=str(mS['Bill Date'].max().date()), months=int(n_mo),
 bills=int(mS['Bill No'].nunique()), lines=int(len(mS)),
 sold_qty=float(mS['Qty'].sum()), mrp_val=float(mS['Gross Amt'].sum()), disc=float(mS['disc'].sum()),
 net=float(mS['Net Amount'].sum()), cogs=float(mS['cogs'].sum()),
 stk_qty=float(mH['Tqty'].sum()), stk_mrp=float(mH['stk_mrp'].sum()), stk_cost=float(mH['stk_cost'].sum()),
 skus=int(len(mH)), cost_ratio=float(r_all), cat_cov=float(cat_cov),
 pack_lines=int(len(pS)), pack_qty=float(pS['Qty'].sum()), pack_net=float(pS['Net Amount'].sum()),
 ret_lines=int((mS['Qty']<0).sum()), ret_qty=float(mS.loc[mS['Qty']<0,'Qty'].sum()),
 ret_net=float(mS.loc[mS['Qty']<0,'Net Amount'].sum()),
)
H['margin']=H['net']-H['cogs']; H['margin_pct']=H['margin']/H['net']*100
H['disc_pct']=H['disc']/H['mrp_val']*100
H['sellthru']=H['sold_qty']/(H['sold_qty']+H['stk_qty'])*100
H['full_margin_pct']=(1-r_all)*100
H['breakeven_disc']=(1-r_all)*100
H['stk_margin_pot']=H['stk_mrp']-H['stk_cost']
H['match_mix']={k:float(v) for k,v in (mS.groupby('match')['cogs'].sum()/mS['cogs'].sum()).items()}
H['bc_match_lines']=float((mS['match']=='barcode').mean())

monthly=mS.groupby('ym').agg(qty=('Qty','sum'),mrp=('Gross Amt','sum'),disc=('disc','sum'),
                             net=('Net Amount','sum'),cogs=('cogs','sum'),bills=('Bill No','nunique'))
monthly['margin']=monthly['net']-monthly['cogs']
monthly['disc_pct']=monthly['disc']/monthly['mrp']*100
monthly['margin_pct']=monthly['margin']/monthly['net']*100

print(json.dumps(H,indent=1,default=str))
print("\nMONTHLY\n",monthly.round(0).to_string())
print("\nCATS\n",cats.round(1).to_string())
print("\nTOP ITEMS\n",items.head(35).round(1).to_string())


# ---------- extra cuts ----------
mS['band']=pd.cut(mS['Disc%'],[-0.01,0.01,10,20,30,36.68,50,70,99.99,100.01],
   labels=['0% (full price)','1-10%','10-20%','20-30%','30-36.7%','36.7-50%','50-70%','70-99%','100% (free)'])
bands=mS.groupby('band',observed=True).agg(qty=('Qty','sum'),mrp=('Gross Amt','sum'),disc=('disc','sum'),
        net=('Net Amount','sum'),cogs=('cogs','sum'),lines=('Qty','size'))
bands['margin']=bands['net']-bands['cogs']
bands['margin_pct']=np.where(bands['net']!=0,bands['margin']/bands['net']*100,0)
bands['share_net']=bands['net']/bands['net'].sum()*100
print("\nDISCOUNT BANDS\n",bands.round(1).to_string())

items['cost_ratio']=np.where(items['stk_mrp']>0,items['stk_cost']/items['stk_mrp'],np.nan)
items['be_disc']=(1-items['cost_ratio'])*100
print("\nLOSS MAKERS (margin<0, net>2L)\n",items[(items['margin']<0)&(items['net'].abs()>200000)][['sold_qty','net','disc_pct','cogs','margin','margin_pct','be_disc']].round(1).to_string())
print("\nitems count",len(items),"cats",len(cats))
print("\n100%% disc lines (merch):",(mS['Disc%']>=99.99).sum(),"qty",mS.loc[mS['Disc%']>=99.99,'Qty'].sum(),"mrp",mS.loc[mS['Disc%']>=99.99,'Gross Amt'].sum())
print("\nzero/dead stock: items with stock but no sale:")
d=items[(items['sold_qty']<=0)&(items['stk_qty']>0)]
print(d[['stk_qty','stk_mrp','stk_cost']].round(0).to_string())
print("\nTOP20 BRAND by stock mrp\n",brands.nlargest(20,'stk_mrp')[['stk_qty','stk_mrp','sold_qty','mrp_val','disc','net','cogs','margin','margin_pct','sellthru']].round(1).to_string())


# buy-margin per brand, excluding gift/promotional stock (inflated MRP, near-zero cost)
_bm=mH[mH['cat']!='PROMOTIONAL'].groupby('brand').agg(mrp=('stk_mrp','sum'),cost=('stk_cost','sum'))
_bm['buy_margin']=(1-_bm['cost']/_bm['mrp'])*100
brands['buy_margin']=_bm['buy_margin']


CSS_BASE = r'''
  :root{
    --ink:#16233d; --ink2:#33415c; --muted:#6b7488; --line:#e6e3dc;
    --paper:#f7f6f2; --card:#ffffff; --gold:#b8860b; --gold-soft:#f3ead2;
    --red:#b5341f; --red-soft:#fbeae6; --green:#1f7a4d; --green-soft:#e7f3ec;
    --blue:#2b5b8c; --blue-soft:#e9f0f7; --amber:#b9791a; --amber-soft:#fbf0dd;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--paper);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    line-height:1.55;-webkit-font-smoothing:antialiased}
  .wrap{max-width:1040px;margin:0 auto;padding:0 22px}
  h1,h2,h3{line-height:1.2;margin:0}
  .num{font-variant-numeric:tabular-nums;font-feature-settings:"tnum"}
  header{background:linear-gradient(135deg,#16233d 0%,#243a5e 100%);color:#fff;padding:46px 0 38px}
  header .kick{letter-spacing:.18em;text-transform:uppercase;font-size:12px;color:#c7d2e2;font-weight:600}
  header h1{font-size:32px;margin:10px 0 6px;font-weight:700}
  header .sub{color:#cfd9e8;font-size:15px;max-width:820px}
  header .meta{margin-top:18px;display:flex;flex-wrap:wrap;gap:10px;font-size:12.5px}
  header .meta span{background:rgba(255,255,255,.10);border:1px solid rgba(255,255,255,.18);padding:5px 11px;border-radius:20px;color:#e8eef6}
  section{padding:34px 0;border-bottom:1px solid var(--line)}
  .eyebrow{display:inline-block;letter-spacing:.13em;text-transform:uppercase;font-size:11.5px;font-weight:700;color:var(--gold);margin-bottom:8px}
  h2{font-size:23px;margin-bottom:6px}
  .lead{color:var(--ink2);font-size:15.5px;max-width:820px}
  p{color:var(--ink2);font-size:14.5px}
  .kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:8px}
  .kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 16px 14px;box-shadow:0 1px 2px rgba(20,33,61,.04)}
  .kpi .lab{font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600}
  .kpi .big{font-size:26px;font-weight:700;margin-top:5px;letter-spacing:-.5px}
  .kpi .note{font-size:12.5px;color:var(--muted);margin-top:3px}
  .kpi.good .big{color:var(--green)} .kpi.warn .big{color:var(--red)} .kpi.flag .big{color:var(--amber)}
  .delta{font-size:12px;font-weight:700;margin-top:2px} .delta.up{color:var(--green)} .delta.down{color:var(--red)} .delta.flat{color:var(--muted)}
  .call{border-left:4px solid var(--blue);background:var(--blue-soft);border-radius:0 10px 10px 0;padding:14px 18px;margin:16px 0;font-size:14.5px;color:var(--ink)}
  .call.red{border-color:var(--red);background:var(--red-soft)}
  .call.green{border-color:var(--green);background:var(--green-soft)}
  .call.gold{border-color:var(--gold);background:var(--gold-soft)}
  .call b{color:var(--ink)}
  .chart{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-top:16px}
  .chart h3{font-size:15px;margin-bottom:3px}
  .chart .cap{font-size:12.5px;color:var(--muted);margin-bottom:14px}
  .hbar{display:grid;grid-template-columns:160px 1fr 92px;align-items:center;gap:10px;margin:7px 0;font-size:13px}
  .hbar .lbl{color:var(--ink2);font-weight:600;text-align:right;font-size:12.5px}
  .track{background:#eef0f3;border-radius:6px;height:20px;position:relative;overflow:hidden}
  .fill{height:100%;border-radius:6px;background:linear-gradient(90deg,#2b5b8c,#3f78b0)}
  .fill.g{background:linear-gradient(90deg,#1f7a4d,#37a169)}
  .fill.r{background:linear-gradient(90deg,#b5341f,#d05a44)}
  .fill.gold{background:linear-gradient(90deg,#a9780a,#cda233)}
  .val{font-weight:700;text-align:left;font-size:12.5px;color:var(--ink)}
  .months{display:flex;align-items:flex-end;gap:7px;height:200px;margin-top:8px;padding-top:10px}
  .mcol{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%}
  .mcol .b{width:78%;background:linear-gradient(180deg,#37a169,#1f7a4d);border-radius:4px 4px 0 0;min-height:2px}
  .mcol .b.loss{background:linear-gradient(180deg,#d05a44,#b5341f)}
  .mcol .b.flat{background:linear-gradient(180deg,#cda233,#a9780a)}
  .mcol .ml{font-size:10px;color:var(--muted);margin-top:6px;white-space:nowrap}
  .mcol .mv{font-size:9.5px;color:var(--ink2);font-weight:700;margin-bottom:3px}
  .mcol .mp{font-size:9px;font-weight:700;margin-top:2px}
  table{width:100%;border-collapse:collapse;margin-top:14px;font-size:13px;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
  th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--line)}
  th:first-child,td:first-child{text-align:left}
  thead th{background:#f0eee8;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--ink2);font-weight:700}
  tbody tr:last-child td{border-bottom:none}
  tbody tr:nth-child(even){background:#faf9f6}
  tfoot td{font-weight:700;background:#f0eee8;border-top:2px solid #d8d3c8}
  .tag{display:inline-block;padding:2px 8px;border-radius:11px;font-size:11px;font-weight:700}
  .tag.g{background:var(--green-soft);color:var(--green)} .tag.r{background:var(--red-soft);color:var(--red)} .tag.a{background:var(--amber-soft);color:var(--amber)} .tag.b{background:var(--blue-soft);color:var(--blue)}
  .pos{color:var(--green);font-weight:700} .neg{color:var(--red);font-weight:700}
  .act{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--gold);border-radius:0 12px 12px 0;padding:16px 18px;margin:13px 0}
  .act h3{font-size:16px;display:flex;align-items:center;gap:9px}
  .act .pr{font-size:11px;font-weight:800;letter-spacing:.04em;color:#fff;background:var(--red);padding:2px 9px;border-radius:5px}
  .act .pr.med{background:var(--amber)} .act .pr.low{background:var(--blue)}
  .act p{margin:8px 0 0;font-size:13.5px}
  .two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  ul.tight{margin:10px 0;padding-left:20px} ul.tight li{font-size:14px;color:var(--ink2);margin:6px 0}
  .foot{padding:26px 0 60px;color:var(--muted);font-size:12px} .foot b{color:var(--ink2)}
  .legend{display:flex;gap:16px;font-size:11.5px;color:var(--muted);margin-top:10px;flex-wrap:wrap}
  .legend i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:5px;vertical-align:-1px}
  .cov{display:grid;grid-template-columns:repeat(2,1fr);gap:6px 22px;margin-top:10px;font-size:12.5px;color:var(--ink2)}
  .cov span{padding:3px 0;border-bottom:1px dotted var(--line)}
  @media(max-width:720px){.kpis{grid-template-columns:1fr 1fr}.two,.cov{grid-template-columns:1fr}.hbar{grid-template-columns:104px 1fr 76px}.months{height:170px}}
  @page{size:A4;margin:0}
  @media print{
    *{-webkit-print-color-adjust:exact !important;print-color-adjust:exact !important}
    body{background:#fff}.wrap{padding:0 14mm}
    .chart,.kpi,table,.act{box-shadow:none}section{padding:22px 0}
    .kpi,.chart,.act,.call,table,.two>div,.months{break-inside:avoid;page-break-inside:avoid}
    h2,h3,.eyebrow{break-after:avoid;page-break-after:avoid}header{break-inside:avoid}tr{break-inside:avoid}
  }
'''

import datetime, html as H_

M=H  # headline dict from the engine section above

# ---- formatting ----
def inr(x):
    x=float(x); sign='-' if x<0 else ''; x=abs(round(x))
    s=str(int(x))
    if len(s)>3:
        h,t=s[-3:],s[:-3]; parts=[]
        while len(t)>2: parts.insert(0,t[-2:]); t=t[:-2]
        if t: parts.insert(0,t)
        s=','.join(parts)+','+h
    return f"{sign}₹{s}"
def cr(x):
    return f"₹{x/1e7:.2f} Cr" if abs(x)>=1e7 else f"₹{x/1e5:.1f} L"
def L(x):  return f"₹{x/1e5:.1f} L"
def LN(x): return f"{x/1e5:,.1f}"
def pc(x,d=1): return f"{x:.{d}f}%"
def q(x): return f"{int(round(x)):,}"
def cls(v,good=0,bad=0): return 'pos' if v>good else ('neg' if v<bad else '')
MN={'01':'Jan','02':'Feb','03':'Mar','04':'Apr','05':'May','06':'Jun','07':'Jul','08':'Aug','09':'Sep','10':'Oct','11':'Nov','12':'Dec'}
def ml(ym): y,m=ym.split('-'); return f"{MN[m]} {y[2:]}"

BE=M['breakeven_disc']
CAT_ORDER=cats.sort_values('stk_mrp',ascending=False)
ITEMS=items.sort_values('stk_mrp',ascending=False)

# ================= KPI =================
def kpi(lab,big,note,c=''):
    return f'<div class="kpi {c}"><div class="lab">{lab}</div><div class="big num">{big}</div><div class="note">{note}</div></div>'

kpis=''.join([
 kpi("1 · Stock holding (MRP)",cr(M['stk_mrp']),f"{q(M['stk_qty'])} pcs · {q(M['skus'])} SKU lines · cost {cr(M['stk_cost'])}"),
 kpi("2 · Sale-through",pc(M['sellthru']),f"{q(M['sold_qty'])} pcs sold vs {q(M['stk_qty'])} pcs still on hand","flag"),
 kpi("3 · Discount given",cr(M['disc']),f"{pc(M['disc_pct'])} off the tag value of what was sold","warn"),
 kpi("4 · Actual sales",cr(M['net']),f"tag value {cr(M['mrp_val'])} less discount","good"),
 kpi("5 · Margin earned",cr(M['margin']),f"{pc(M['margin_pct'])} of sales · would be {pc(M['full_margin_pct'],0)} at full price","good" if M['margin_pct']>20 else "flag"),
 kpi("Break-even discount",pc(BE,0),"goods cost %d%% of tag — beyond this you sell below cost"%round(M['cost_ratio']*100),"warn"),
])

# ================= category table =================
def cat_rows():
    r=''
    for name,x in CAT_ORDER.iterrows():
        if x['stk_mrp']==0 and x['net']==0: continue
        mp=x['margin_pct']; st=x['sellthru']
        tag='g' if mp>=30 else ('a' if mp>=15 else 'r')
        r+=(f'<tr><td><b>{H_.escape(str(name).title())}</b></td>'
            f'<td class="num">{q(x["stk_qty"])}</td><td class="num">{LN(x["stk_mrp"])}</td>'
            f'<td class="num">{q(x["sold_qty"])}</td><td class="num">{LN(x["mrp_val"])}</td>'
            f'<td class="num">{pc(x["sellthru"],0)}</td>'
            f'<td class="num">{LN(x["disc"])}</td><td class="num">{pc(x["disc_pct"],0)}</td>'
            f'<td class="num"><b>{LN(x["net"])}</b></td><td class="num">{LN(x["cogs"])}</td>'
            f'<td class="num {cls(x["margin"])}">{LN(x["margin"])}</td>'
            f'<td class="num"><span class="tag {tag}">{pc(mp,0)}</span></td></tr>')
    t=CAT_ORDER
    r+=(f'<tr class="tot"><td>TOTAL</td><td class="num">{q(t["stk_qty"].sum())}</td><td class="num">{LN(t["stk_mrp"].sum())}</td>'
        f'<td class="num">{q(t["sold_qty"].sum())}</td><td class="num">{LN(t["mrp_val"].sum())}</td>'
        f'<td class="num">{pc(M["sellthru"],0)}</td><td class="num">{LN(t["disc"].sum())}</td><td class="num">{pc(M["disc_pct"],0)}</td>'
        f'<td class="num">{LN(t["net"].sum())}</td><td class="num">{LN(t["cogs"].sum())}</td>'
        f'<td class="num">{LN(t["margin"].sum())}</td><td class="num">{pc(M["margin_pct"],0)}</td></tr>')
    return r

# margin bar chart by category (top 8 by net)
def cat_bars():
    d=CAT_ORDER.nlargest(9,'net'); mx=d['margin'].max()
    out=''
    for n,x in d.iterrows():
        w=max(1,x['margin']/mx*100) if mx>0 else 1
        f='g' if x['margin_pct']>=30 else ('gold' if x['margin_pct']>=15 else 'r')
        out+=(f'<div class="hbar"><div class="lbl">{H_.escape(str(n).title())}</div>'
              f'<div class="track"><div class="fill {f}" style="width:{w:.1f}%"></div></div>'
              f'<div class="val">{L(x["margin"])} <span style="color:#6b7488;font-weight:600">({pc(x["margin_pct"],0)})</span></div></div>')
    return out
def cat_st_bars():
    d=CAT_ORDER.nlargest(9,'stk_mrp'); out=''
    for n,x in d.iterrows():
        w=max(1,x['sellthru']); f='g' if x['sellthru']>=50 else ('gold' if x['sellthru']>=35 else 'r')
        out+=(f'<div class="hbar"><div class="lbl">{H_.escape(str(n).title())}</div>'
              f'<div class="track"><div class="fill {f}" style="width:{w:.1f}%"></div></div>'
              f'<div class="val">{pc(x["sellthru"],0)} <span style="color:#6b7488;font-weight:600">({L(x["stk_mrp"])} left)</span></div></div>')
    return out

# ================= item table =================
def item_rows(df):
    r=''
    for name,x in df.iterrows():
        mp=x['margin_pct'] if x['net']!=0 else 0
        tag='g' if mp>=30 else ('a' if mp>=15 else 'r')
        st=x['sellthru']; stc='pos' if st>=50 else ('neg' if st<25 else '')
        cov='—' if not np.isfinite(x['cover_mo']) else (f'{x["cover_mo"]:.0f} mo' if x['cover_mo']<200 else '200+ mo')
        r+=(f'<tr><td><b>{H_.escape(str(name).title())}</b></td>'
            f'<td class="num">{q(x["stk_qty"])}</td><td class="num">{LN(x["stk_mrp"])}</td>'
            f'<td class="num">{q(x["sold_qty"])}</td>'
            f'<td class="num {stc}">{pc(st,0)}</td><td class="num">{cov}</td>'
            f'<td class="num">{LN(x["disc"])}</td><td class="num">{pc(x["disc_pct"],0)}</td>'
            f'<td class="num"><b>{LN(x["net"])}</b></td>'
            f'<td class="num {cls(x["margin"])}">{LN(x["margin"])}</td>'
            f'<td class="num"><span class="tag {tag}">{pc(mp,0)}</span></td></tr>')
    return r
TOP=ITEMS.head(30); REST=ITEMS.iloc[30:]
rest_row=('<tr class="tot"><td>All other %d items</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
  '<td class="num">%s</td><td class="num">—</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
  '<td class="num">%s</td><td class="num">%s</td></tr>')%(
  len(REST),q(REST['stk_qty'].sum()),LN(REST['stk_mrp'].sum()),q(REST['sold_qty'].sum()),
  pc(REST['sold_qty'].sum()/(REST['sold_qty'].sum()+REST['stk_qty'].sum())*100,0),
  LN(REST['disc'].sum()),pc(REST['disc'].sum()/REST['mrp_val'].sum()*100,0),LN(REST['net'].sum()),
  LN(REST['margin'].sum()),pc(REST['margin'].sum()/REST['net'].sum()*100,0))
grand=('<tr class="tot"><td>TOTAL — all items</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
  '<td class="num">%s</td><td class="num">—</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
  '<td class="num">%s</td><td class="num">%s</td></tr>')%(
  q(M['stk_qty']),LN(M['stk_mrp']),q(M['sold_qty']),pc(M['sellthru'],0),LN(M['disc']),pc(M['disc_pct'],0),
  LN(M['net']),LN(M['margin']),pc(M['margin_pct'],0))

# ================= discount bands =================
def band_rows():
    r=''
    for n,x in bands.iterrows():
        bad=x['margin']<0
        r+=(f'<tr><td><b>{H_.escape(str(n))}</b></td><td class="num">{q(x["qty"])}</td>'
            f'<td class="num">{LN(x["mrp"])}</td><td class="num">{LN(x["disc"])}</td>'
            f'<td class="num"><b>{LN(x["net"])}</b></td><td class="num">{pc(x["share_net"],0)}</td>'
            f'<td class="num">{LN(x["cogs"])}</td>'
            f'<td class="num {cls(x["margin"])}">{LN(x["margin"])}</td>'
            f'<td class="num"><span class="tag {"r" if bad else ("g" if x["margin_pct"]>=25 else "a")}">{pc(x["margin_pct"],0)}</span></td></tr>')
    return r
below=bands[bands['margin']<0]

# ================= monthly =================
def month_cols():
    mx=monthly['net'].max(); out=''
    for ym,x in monthly.iterrows():
        h=max(2,x['net']/mx*100); c='' if x['margin']>x['net']*0.2 else ('flat' if x['margin']>0 else 'loss')
        out+=(f'<div class="mcol"><div class="mv">{x["net"]/1e5:.0f}L</div>'
              f'<div class="b {c}" style="height:{h:.0f}%"></div>'
              f'<div class="ml">{ml(ym)}</div><div class="mp" style="color:{"#1f7a4d" if x["margin"]>0 else "#b5341f"}">{x["margin_pct"]:.0f}%</div></div>')
    return out
def month_rows():
    r=''
    for ym,x in monthly.iterrows():
        r+=(f'<tr><td><b>{ml(ym)}</b></td><td class="num">{q(x["bills"])}</td><td class="num">{q(x["qty"])}</td>'
            f'<td class="num">{LN(x["mrp"])}</td><td class="num">{LN(x["disc"])}</td>'
            f'<td class="num">{pc(x["disc_pct"],0)}</td><td class="num"><b>{LN(x["net"])}</b></td>'
            f'<td class="num">{LN(x["cogs"])}</td><td class="num {cls(x["margin"])}">{LN(x["margin"])}</td>'
            f'<td class="num"><span class="tag {"r" if x["margin"]<0 else ("g" if x["margin_pct"]>=25 else "a")}">{pc(x["margin_pct"],0)}</span></td></tr>')
    return r

# ================= brands =================
BT=brands.nlargest(18,'stk_mrp')
def brand_rows():
    r=''
    for n,x in BT.iterrows():
        mp=x['margin_pct'] if x['net']!=0 else 0
        tag='g' if mp>=25 else ('a' if mp>=10 else 'r')
        r+=(f'<tr><td><b>{H_.escape(str(n).title())}</b></td><td class="num">{q(x["stk_qty"])}</td>'
            f'<td class="num">{LN(x["stk_mrp"])}</td><td class="num">{pc(x["sellthru"],0)}</td>'
            f'<td class="num">{pc(x["disc_pct"],0)}</td><td class="num"><b>{LN(x["net"])}</b></td>'
            f'<td class="num {cls(x["margin"])}">{LN(x["margin"])}</td>'
            f'<td class="num"><span class="tag {tag}">{pc(mp,0)}</span></td>'
            f'<td class="num">{pc(x["buy_margin"],0) if np.isfinite(x.get("buy_margin",np.nan)) else "—"}</td></tr>')
    return r

# thin-margin items with big stock
thin=ITEMS[(ITEMS['stk_mrp']>1e6)&(ITEMS['margin_pct']<15)].sort_values('stk_mrp',ascending=False)
slow=ITEMS[(ITEMS['stk_mrp']>3e5)&(ITEMS['sellthru']<30)].sort_values('stk_mrp',ascending=False)
star=ITEMS[(ITEMS['net']>5e5)&(ITEMS['margin_pct']>30)].sort_values('margin',ascending=False)

def li(df,f):  return ''.join(f'<li>{f(n,x)}</li>' for n,x in df.iterrows())

TITLE="Hazaribagh — Stock, Sale-Through, Discount &amp; Margin"
today=datetime.date.today().strftime('%d %b %Y')
mix=M['match_mix']

CSS = CSS_BASE
CSS += """
  .tot td{font-weight:700;background:#f0eee8;border-top:2px solid #d8d3c8}
  .scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
  .scroll table{min-width:900px}
  .ans{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-top:16px}
  .ans .a{background:var(--card);border:1px solid var(--line);border-top:3px solid var(--gold);border-radius:0 0 10px 10px;padding:13px 14px}
  .ans .a .n{font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--gold);font-weight:800}
  .ans .a .q{font-size:12.5px;color:var(--muted);margin:3px 0 6px;min-height:32px}
  .ans .a .v{font-size:19px;font-weight:700;letter-spacing:-.4px}
  .ans .a .s{font-size:11.5px;color:var(--ink2);margin-top:3px}
  @media(max-width:900px){.ans{grid-template-columns:1fr 1fr}}
"""

BODY = f"""
<section>
  <div class="wrap">
    <span class="eyebrow">The five answers</span>
    <h2>Everything asked, on one line each</h2>
    <p class="lead">Period {ml(monthly.index[0])} – {ml(monthly.index[-1])} ({M['months']} months, {q(M['bills'])} bills).
    Stock is the current snapshot. Packaging (carry bags) is excluded from every merchandise number.</p>
    <div class="ans">
      <div class="a"><div class="n">1 · Stock holding</div><div class="q">Item-wise stock on hand, valued at MRP</div>
        <div class="v num">{cr(M['stk_mrp'])}</div><div class="s">{q(M['stk_qty'])} pcs · cost {cr(M['stk_cost'])}</div></div>
      <div class="a"><div class="n">2 · Sale-through</div><div class="q">Share of goods handled that actually sold</div>
        <div class="v num">{pc(M['sellthru'])}</div><div class="s">{q(M['sold_qty'])} sold / {q(M['sold_qty']+M['stk_qty'])} handled</div></div>
      <div class="a"><div class="n">3 · Discount given</div><div class="q">Given away against the tag price of goods sold</div>
        <div class="v num">{cr(M['disc'])}</div><div class="s">{pc(M['disc_pct'])} of {cr(M['mrp_val'])} tag value</div></div>
      <div class="a"><div class="n">4 · Actual sales</div><div class="q">What the customer actually paid, after discount &amp; returns</div>
        <div class="v num">{cr(M['net'])}</div><div class="s">avg bill {inr(M['net']/M['bills'])}</div></div>
      <div class="a"><div class="n">5 · Margin earned</div><div class="q">Actual sales less the cost of the goods sold</div>
        <div class="v num">{cr(M['margin'])}</div><div class="s">{pc(M['margin_pct'])} of sales</div></div>
    </div>
    <div class="call gold"><b>The one number that explains the rest.</b> Goods are bought at about
      <b>{M['cost_ratio']*100:.0f}% of the tag price</b>, so the most this store can ever earn is <b>{pc(M['full_margin_pct'],0)}</b>.
      It actually earned <b>{pc(M['margin_pct'])}</b>. The whole gap is discount: <b>{cr(M['disc'])}</b> was given away
      on {cr(M['mrp_val'])} of tag value. <b>Any discount above {pc(BE,0)} sells the item below cost.</b></div>
  </div>
</section>

<section>
  <div class="wrap">
    <span class="eyebrow">Answer 5 · category wise</span>
    <h2>Margin earned, category wise</h2>
    <p class="lead">All money columns in <b>₹ lakh</b>. Margin = actual sales − cost of the goods sold.
    Margin % is on actual sales. Sale-through = sold ÷ (sold + still in stock).</p>
    <div class="scroll">
    <table>
      <thead><tr><th>Category</th><th>Stock qty</th><th>Stock @ MRP</th><th>Sold qty</th><th>Sold @ MRP</th>
      <th>Sale-thru</th><th>Discount</th><th>Disc %</th><th>Actual sales</th><th>Cost of goods</th><th>Margin</th><th>Margin %</th></tr></thead>
      <tbody>{cat_rows()}</tbody>
    </table></div>
    <div class="two" style="margin-top:18px">
      <div class="chart"><h3>Where the margin actually comes from</h3>
        <div class="cap">Rupees of margin earned, by category (₹ lakh). Green = healthy 30%+, gold = 15–30%, red = under 15%.</div>
        {cat_bars()}</div>
      <div class="chart"><h3>How much of each category sold</h3>
        <div class="cap">Sale-through %, biggest stock holdings first. Value in brackets = stock still on hand at MRP.</div>
        {cat_st_bars()}</div>
    </div>
    <div class="call red"><b>Casual wear is the problem.</b> It is half the shop —
      {L(CAT_ORDER.loc['CASUAL WEAR','stk_mrp'])} of stock at MRP and {L(CAT_ORDER.loc['CASUAL WEAR','net'])} of sales —
      but it is discounted <b>{pc(CAT_ORDER.loc['CASUAL WEAR','disc_pct'],0)}</b> and returns only
      <b>{pc(CAT_ORDER.loc['CASUAL WEAR','margin_pct'],0)}</b> margin. It contributes
      {pc(CAT_ORDER.loc['CASUAL WEAR','net']/M['net']*100,0)} of sales but only
      {pc(CAT_ORDER.loc['CASUAL WEAR','margin']/M['margin']*100,0)} of the margin.</div>
    <div class="call green"><b>Party, seasonal and traditional wear carry the store.</b> Together they are
      {pc((CAT_ORDER.loc[['PARTY WEAR','SEASONAL WEAR','TRADITIONAL WEAR'],'net'].sum())/M['net']*100,0)} of sales but
      {pc((CAT_ORDER.loc[['PARTY WEAR','SEASONAL WEAR','TRADITIONAL WEAR'],'margin'].sum())/M['margin']*100,0)} of the margin —
      because they are barely discounted (5–15%) and bought at a better rate.</div>
  </div>
</section>

<section>
  <div class="wrap">
    <span class="eyebrow">Answers 1–4 · item wise</span>
    <h2>Item-wise stock holding, sale-through, discount and sales</h2>
    <p class="lead">Every item type, biggest stock holding first. Money in <b>₹ lakh</b>.
    <b>Cover</b> = how many months the stock on hand would last at the current selling rate.</p>
    <div class="scroll">
    <table>
      <thead><tr><th>Item</th><th>Stock qty</th><th>Stock @ MRP</th><th>Sold qty</th><th>Sale-thru</th><th>Cover</th>
      <th>Discount</th><th>Disc %</th><th>Actual sales</th><th>Margin</th><th>Margin %</th></tr></thead>
      <tbody>{item_rows(TOP)}{rest_row}{grand}</tbody>
    </table></div>
    <div class="two" style="margin-top:16px">
      <div class="act"><h3><span class="pr">Fix first</span> Big stock, thin margin</h3>
        <p>These hold heavy stock at MRP but return under 15% margin because of the discount they carry:</p>
        <ul class="tight">{li(thin.head(6),lambda n,x: f"<b>{str(n).title()}</b> — {L(x['stk_mrp'])} in stock, sold {L(x['net'])} at {pc(x['disc_pct'],0)} discount, margin <b>{pc(x['margin_pct'],0)}</b>")}</ul></div>
      <div class="act"><h3><span class="pr med">Watch</span> Slow movers</h3>
        <p>Over {L(3e5)} of stock at MRP with under 30% sale-through — money sitting still:</p>
        <ul class="tight">{li(slow.head(6),lambda n,x: f"<b>{str(n).title()}</b> — {L(x['stk_mrp'])} in stock, only {pc(x['sellthru'],0)} sold through ({q(x['stk_qty'])} pcs left)")}</ul></div>
    </div>
    <div class="act"><h3><span class="pr low">Protect</span> The earners</h3>
      <p>Items doing over {L(5e5)} of sales at 30%+ margin — these pay for the shop. Do not put them into blanket sale schemes:</p>
      <ul class="tight">{li(star.head(6),lambda n,x: f"<b>{str(n).title()}</b> — {L(x['net'])} sales, {pc(x['disc_pct'],0)} discount, margin <b>{L(x['margin'])} ({pc(x['margin_pct'],0)})</b>")}</ul></div>
  </div>
</section>

<section>
  <div class="wrap">
    <span class="eyebrow">Where the margin leaks</span>
    <h2>Every rupee of sale, sorted by how deep the discount was</h2>
    <p class="lead">Goods cost {M['cost_ratio']*100:.0f}% of the tag, so <b>{pc(BE,0)} is the break-even discount</b>.
    Everything sold below that line was sold at a loss. Money in <b>₹ lakh</b>.</p>
    <div class="scroll">
    <table>
      <thead><tr><th>Discount band</th><th>Qty</th><th>Sold @ MRP</th><th>Discount</th><th>Actual sales</th><th>% of sales</th><th>Cost of goods</th><th>Margin</th><th>Margin %</th></tr></thead>
      <tbody>{band_rows()}</tbody>
    </table></div>
    <div class="call red"><b>{L(abs(below['margin'].sum()))} of margin was destroyed below the line.</b>
      {pc(below['net'].sum()/M['net']*100,0)} of sales ({L(below['net'].sum())}) happened at discounts deeper than {pc(BE,0)} —
      and that slice lost <b>{L(abs(below['margin'].sum()))}</b>. Without it the store's margin would have been
      <b>{pc((M['margin']+abs(below['margin'].sum()))/(M['net']-below['net'].sum())*100,0)}</b> instead of {pc(M['margin_pct'],0)}.</div>
    <div class="call green"><b>Full-price selling is {pc(bands.loc['0% (full price)','share_net'],0)} of sales and earns {pc(bands.loc['0% (full price)','margin_pct'],0)}.</b>
      Half this store already sells at tag price. The margin problem is entirely in the deep-discount tail, not in the everyday business.</div>
  </div>
</section>

<section>
  <div class="wrap">
    <span class="eyebrow">Month by month</span>
    <h2>When the discount was given</h2>
    <p class="lead">Bar height = actual sales. The number under each bar is that month's margin %. Money in <b>₹ lakh</b>.</p>
    <div class="chart">
      <div class="cap">Sales by month, with margin % underneath. Red = the month lost money on goods.</div>
      <div class="months">{month_cols()}</div>
      <div class="legend"><span><i style="background:#1f7a4d"></i>margin above 20%</span><span><i style="background:#a9780a"></i>thin margin</span><span><i style="background:#b5341f"></i>sold below cost</span></div>
    </div>
    <div class="scroll">
    <table>
      <thead><tr><th>Month</th><th>Bills</th><th>Qty</th><th>Sold @ MRP</th><th>Discount</th><th>Disc %</th><th>Actual sales</th><th>Cost of goods</th><th>Margin</th><th>Margin %</th></tr></thead>
      <tbody>{month_rows()}</tbody>
    </table></div>
    <div class="call red"><b>January is where the year was lost.</b> {L(monthly.loc['2026-01','disc'])} of discount in one month
      ({pc(monthly.loc['2026-01','disc_pct'],0)} off tag) turned {L(monthly.loc['2026-01','net'])} of sales into a
      <b>{L(abs(monthly.loc['2026-01','margin']))} loss</b> on goods. July is heading the same way at
      {pc(monthly.loc['2026-07','disc_pct'],0)} discount and {pc(monthly.loc['2026-07','margin_pct'],0)} margin.
      The clean months — Sep, Oct, Nov, Mar, Apr, May — all ran 30%+ margin at under 10% discount.</div>
  </div>
</section>

<section>
  <div class="wrap">
    <span class="eyebrow">Brand view</span>
    <h2>The same five measures, brand wise</h2>
    <p class="lead">Top 18 brands by stock held at MRP. <b>Buy margin</b> is what the brand gives you before any discount —
    it is the ceiling on what that brand can ever earn. Money in <b>₹ lakh</b>.</p>
    <div class="scroll">
    <table>
      <thead><tr><th>Brand</th><th>Stock qty</th><th>Stock @ MRP</th><th>Sale-thru</th><th>Disc %</th><th>Actual sales</th><th>Margin</th><th>Margin %</th><th>Buy margin</th></tr></thead>
      <tbody>{brand_rows()}</tbody>
    </table></div>
    <div class="call red"><b>The national menswear brands are bought thin and discounted deep.</b>
      Van Heusen, Arrow, U.S. Polo, Killer, Peter England, Levis, Spykar and Allen Solly are bought at a
      <b>26–35% buy margin</b> — and then sold at 25–35% discount. That is exactly why Arrow, Killer, Levis and
      U.S. Polo Kids show a <b>negative margin</b> despite large sales: the discount eats the entire buy margin and then some.
      Either the buy terms improve, or these brands stay out of deep schemes.</div>
  </div>
</section>

<section>
  <div class="wrap">
    <span class="eyebrow">How these numbers were built</span>
    <h2>Method &amp; honest caveats</h2>
    <div class="two">
      <div>
        <ul class="tight">
          <li><b>Stock @ MRP</b> = stock qty × MRP from the stock file. Cost value = qty × the file's Rate column.</li>
          <li><b>Actual sales</b> = the Net Amount column, i.e. after discount and net of {q(abs(M['ret_qty']))} returned pieces ({L(abs(M['ret_net']))}).</li>
          <li><b>Sale-through</b> = sold qty ÷ (sold qty + stock qty). Opening stock was not supplied, so this uses the closing snapshot.</li>
          <li><b>Cost of goods sold is estimated.</b> The sales export carries no cost column, so each sold line was matched to the stock file for its cost-to-MRP ratio: barcode first, then brand+item, then item, then brand.</li>
          <li>Match quality: {pc(mix.get('barcode',0)*100,0)} of cost matched on exact barcode, {pc(mix.get('brand+item',0)*100,0)} on brand+item, {pc(mix.get('item',0)*100,0)} on item, {pc((mix.get('brand',0)+mix.get('category',0)+mix.get('overall',0))*100,1)} on brand/overall average.</li>
        </ul>
      </div>
      <div>
        <ul class="tight">
          <li><b>Carry bags excluded</b> — {q(M['pack_lines'])} lines / {q(M['pack_qty'])} pcs of packaging, {inr(M['pack_net'])} of sales. They are not merchandise.</li>
          <li>The stock file's own grand-total row was dropped (it otherwise doubles every total).</li>
          <li>Item names were normalised across the two files (Sarees→Saree, Trousers→Trouser, Sweat Shirt→Sweatshirt) so stock and sales line up.</li>
          <li>Categories come from the stock file. Around {pc((1-M['cat_cov'])*100,1)} of sold lines carried a junk category code and were classified from their item name instead.</li>
          <li><b>Not included:</b> rent, salary, power and other running costs. Margin here is gross — goods only.</li>
          <li><b>Gift / promotional stock</b> ({q(CAT_ORDER.loc['PROMOTIONAL','stk_qty'])} pcs, {L(CAT_ORDER.loc['PROMOTIONAL','stk_mrp'])} at MRP) carries an inflated tag against a near-zero cost — it is kept out of the brand buy-margin column so it does not flatter the brands.</li>
          <li>Free/gift items ({q(bands.loc['100% (free)','qty'])} pcs, {L(bands.loc['100% (free)','mrp'])} at MRP) show as 100% discount and a full-cost loss — that is the real cost of the gift scheme.</li>
        </ul>
      </div>
    </div>
    <div class="call"><b>One data-quality flag.</b> The sales export's own trailing total row does not reconcile with its line
      items, and the stock file's total row is {inr(abs(38550679.28-M['stk_cost']))} above the sum of its rows.
      Everything in this report is computed from the line items, which are internally consistent.</div>
  </div>
</section>
"""

meta=' '.join(f'<span>{x}</span>' for x in [
  f"Period · {ml(monthly.index[0])} – {ml(monthly.index[-1])}",
  f"{M['bills']:,} bills · {q(M['lines'])} lines",
  f"Stock as on the export date · {q(M['skus'])} SKU lines",
  f"Prepared {today}"])

OUT=f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hazaribagh — Stock, Sale-Through, Discount &amp; Margin</title>
<style>{CSS}</style></head><body>
<header><div class="wrap">
  <div class="kick">KDPS Lifestyle Pvt Ltd · Store report</div>
  <h1>Hazaribagh — stock, sale-through, discount &amp; margin</h1>
  <div class="sub">Item-wise stock holding at MRP, how much of it sold, what was given away in discount,
  what the store actually sold, and the margin it earned — category wise.</div>
  <div class="meta">{meta}</div>
</div></header>
<section><div class="wrap"><div class="kpis">{kpis}</div></div></section>
{BODY}
<div class="wrap foot"><b>Hazaribagh store report.</b> Built from the store's POS sales export and stock-on-hand export.
Cost of goods — and therefore margin — is an estimate: the sales export has no cost column, so every sold line was matched
to the stock file for its cost. Margin is gross (goods only); rent, salary and power are not deducted.
Generated {today}.</div>
</body></html>"""
open(os.path.join(HERE,'Hazaribagh-Stock-Sales-Margin.html'),'w').write(OUT)
print("OK", len(OUT))
