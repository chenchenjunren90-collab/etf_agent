"""Build CSDN cache from extracted xlsx files — fast version."""
import json, time
from pathlib import Path
import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent / "data"
comp = json.loads((DATA_DIR / "etf_components.json").read_text())
print(f'ETFs: {len(comp)}')

EXTRACT_DIR = Path("C:/Users/32872/Desktop/etf智能体/骏/extracted_net")
cache_file = DATA_DIR / "csdn_scores" / "csdn_daily_scores.json"
all_scores = {}

for year in [2020, 2021, 2022, 2023]:
    subdir = EXTRACT_DIR / str(year)
    xlsx_path = subdir / f"网络新闻量化统计（按自然日）-{year}.xlsx"
    if not xlsx_path.exists():
        print(f'{year}: file not found at {xlsx_path}')
        continue
    
    t0 = time.time()
    print(f'{year}: reading {xlsx_path.stat().st_size/1024/1024:.1f}MB...')
    df = pd.read_excel(xlsx_path)
    df = df[df.iloc[:, 0] != '股票代码'].copy()
    col_code = df.columns[0]
    df[col_code] = df[col_code].astype(str).str.replace(r'\.(SH|SZ)$', '', regex=True)
    df['_d'] = pd.to_datetime(df['Date'], errors='coerce')
    for c in ['Posnews_All', 'Neunews_All', 'Negnews_All']:
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0).astype(int)
    df = df.dropna(subset=['_d', col_code])
    print(f'  {len(df):,} rows, {df["_d"].nunique()} dates, {time.time()-t0:.1f}s')
    
    dates = sorted(df['_d'].unique())
    new = 0
    for dt in dates:
        date_str = pd.Timestamp(dt).strftime('%Y-%m-%d')
        day = df[df['_d'] == dt]
        scores = {}
        for etf, comps in comp.items():
            tp, tw = 0.0, 0.0
            batch = day[day[col_code].isin(comps.keys())]
            if len(batch) == 0: continue
            for _, row in batch.iterrows():
                w = comps[row[col_code]]
                p, n, neu = row['Posnews_All'], row['Negnews_All'], row['Neunews_All']
                tm = p + n + neu
                if tm > 0:
                    tp += (p - n) / tm * w
                    tw += w
            if tw > 0.001:
                scores[etf] = round(float(np.clip(50 + tp/tw * 60, 0, 100)), 2)
        if scores:
            all_scores[date_str] = scores
            new += 1
        if new % 60 == 0:
            print(f'  {new}/{len(dates)}')
    
    print(f'  {year} done: {new} dates cached, {time.time()-t0:.0f}s')

DATA_DIR.joinpath("csdn_scores").mkdir(exist_ok=True)
cache_file.write_text(json.dumps(all_scores, ensure_ascii=False), encoding='utf-8')
print(f'\nTotal: {len(all_scores)} dates saved')

# Quick stats
dates_list = sorted(all_scores.keys())
sample = all_scores[dates_list[len(dates_list)//2]]
etf_list = sorted(sample.keys())
scores_by_etf = {e: [all_scores[d].get(e, 50) for d in dates_list if e in all_scores[d]] for e in etf_list}
print()
print(f'{"ETF":<10} {"Count":>6} {"Mean":>7} {"Std":>7} {"Min":>7} {"Max":>7}')
for e in etf_list:
    s = scores_by_etf[e]
    if s:
        print(f'{e:<10} {len(s):>6} {np.mean(s):>7.1f} {np.std(s):>7.1f} {np.min(s):>7.1f} {np.max(s):>7.1f}')
