"""ETF Sentiment Aggregator — CSDN network news sentiment → ETF-level scores.

Converts CSDN daily stock-level sentiment data into ETF-level scores by:
1. Mapping each stock to its tracking ETF via index components
2. Aggregating positive/neutral/negative news counts per ETF per day
3. Normalizing to 0-100 scale compatible with existing theme_score format

Data source: CSDN 网络新闻量化统计（按自然日）, 2001-2023
Format: Scode, Coname, Date, Newsnum_Title, Newsnum_Cont,
        Posnews_All, Neunews_All, Negnews_All,
        Posnews_Ori, Neunews_Ori, Negnews_Ori
"""

from __future__ import annotations

import os, json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import zipfile

# ═══════════════════════════════════════════════════
# ETF → Index component mapping (sourced from Tushare index_weight)
# Commodity ETFs (518880 gold, 159985 soybean) are excluded - no equity components
# ═══════════════════════════════════════════════════

ETF_INDEX_MAP: dict[str, str] = {
    "510300": "000300.SH",  # 沪深300ETF
    "510330": "000300.SH",  # 华夏沪深300ETF
    "510050": "000016.SH",  # 上证50ETF
    "510500": "000905.SH",  # 中证500ETF
    "159915": "399006.SZ",  # 创业板ETF
    "159949": "399006.SZ",  # 创业板50ETF
    "512880": "399975.SZ",  # 证券ETF
    "512010": "000933.SH",  # 医药ETF (中证医药)
    "510880": "000922.SH",  # 红利ETF (中证红利)
    # 588000: 000688.SH (科创50) — covered separately if data abundant
}

CSDN_DATA_DIR = Path("C:/Users/32872/Desktop/etf智能体/骏/网络财经新闻库")
INDEX_WEIGHTS_CACHE = Path(__file__).resolve().parent / "data" / "etf_components.json"


# ═══════════════════════════════════════════════════
# Step 1: Fetch index component weights
# ═══════════════════════════════════════════════════

def fetch_index_components(use_cache: bool = True) -> dict[str, dict[str, float]]:
    """Build {etf_code: {stock_code: weight}} mapping from Tushare.

    Caches to etf_components.json to avoid repeated API calls.
    """
    if use_cache and INDEX_WEIGHTS_CACHE.exists():
        return json.loads(INDEX_WEIGHTS_CACHE.read_text(encoding="utf-8"))

    import tushare as ts
    token = os.environ.get("TUSHARE_TOKEN", "51a6abcf6ea12364b1a78f5c782c1058ba4e9839f6cb43853e8ca1da")
    pro = ts.pro_api(token)

    # Use 2023-12-29 as reference date (latest available for CSDN data range)
    ref_date = "20231229"

    result: dict[str, dict[str, float]] = {}
    # Deduplicate indices
    seen_indices: dict[str, str] = {}
    for etf_code, idx_code in ETF_INDEX_MAP.items():
        if idx_code not in seen_indices:
            seen_indices[idx_code] = etf_code

    for idx_code, etf_code in seen_indices.items():
        try:
            df = pro.index_weight(index_code=idx_code, start_date=ref_date, end_date=ref_date)
            if len(df) == 0:
                print(f"[SentAgg] {idx_code}: 0 components, skipping")
                continue
            weights = {}
            for _, row in df.iterrows():
                con_code = str(row["con_code"])
                weight = float(row.get("weight", 0))
                if weight > 0:
                    weights[con_code] = weight / 100.0  # Tushare weight is in percentage
            result[etf_code] = weights
            print(f"[SentAgg] {etf_code}({idx_code}): {len(weights)} components")
        except Exception as e:
            print(f"[SentAgg] {idx_code}: fetch failed — {e}")

    # Copy shared indices to sibling ETFs
    for etf_code, idx_code in ETF_INDEX_MAP.items():
        master = seen_indices.get(idx_code)
        if master and master != etf_code and master in result:
            result[etf_code] = result[master]

    INDEX_WEIGHTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    INDEX_WEIGHTS_CACHE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


# ═══════════════════════════════════════════════════
# Step 2: Load CSDN daily data
# ═══════════════════════════════════════════════════

def load_csdn_year(year: int) -> pd.DataFrame:
    """Load a single year of CSDN network sentiment data from zip.

    Returns DataFrame with columns: Scode, Date, Pos/Neu/Neg counts.
    """
    zip_path = CSDN_DATA_DIR / "网络新闻量化统计（按自然日）.zip"
    if not zip_path.exists():
        raise FileNotFoundError(f"CSDN data not found: {zip_path}")

    fname = f"网络新闻量化统计（按自然日）-{year}.xlsx"

    with zipfile.ZipFile(zip_path) as z:
        members = [m for m in z.namelist() if m.endswith(fname)]
        if not members:
            # Try alternate naming
            members = [m for m in z.namelist() if f"{year}" in m and m.endswith('.xlsx')]
        if not members:
            print(f"[SentAgg] {year}: not found in zip")
            return pd.DataFrame()

        member = members[0]
        with z.open(member) as f:
            df = pd.read_excel(f)

    # Filter header rows
    df = df[df["Scode"] != "股票代码"].copy()

    # Normalize stock code: strip .SH/.SZ suffix, pad to 6 digits
    df["Scode"] = df["Scode"].astype(str).str.strip()
    df["Scode"] = df["Scode"].str.replace(r"\.(SH|SZ)$", "", regex=True)

    # Convert numeric columns
    for col in ["Posnews_All", "Neunews_All", "Negnews_All", "Newsnum_Cont", "Newsnum_Title"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date", "Scode"])

    return df


def load_csdn_range(start_year: int, end_year: int) -> pd.DataFrame:
    """Load CSDN data for a range of years."""
    frames = []
    for y in range(start_year, end_year + 1):
        df = load_csdn_year(y)
        if len(df) > 0:
            frames.append(df)
            print(f"[SentAgg] {y}: {len(df)} rows, {df['Scode'].nunique()} stocks, "
                  f"{df['Date'].nunique()} dates")
        else:
            print(f"[SentAgg] {y}: no data")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ═══════════════════════════════════════════════════
# Step 3: Aggregate stock sentiment → ETF scores
# ═══════════════════════════════════════════════════

def _compute_etf_sentiment(
    daily_stocks: pd.DataFrame,
    components: dict[str, float],
) -> float | None:
    """Compute sentiment score for one ETF on one day.

    Formula: (Σ(Pos-Neg) / Σ(Pos+Neu+Neg)) * 100, weighted by component weight.
    Returns 0-100 score, or None if no component stocks have data.
    """
    total_pos = 0.0
    total_neg = 0.0
    total_weight = 0.0

    for _, row in daily_stocks.iterrows():
        scode = str(row["Scode"])
        if scode not in components:
            continue
        weight = components[scode]
        pos = int(row.get("Posnews_All", 0) or 0)
        neg = int(row.get("Negnews_All", 0) or 0)
        neu = int(row.get("Neunews_All", 0) or 0)

        # Sentiment signal = (positive - negative) / total, weighted
        total_mentions = pos + neg + neu
        if total_mentions > 0:
            sentiment = (pos - neg) / total_mentions
            total_pos += sentiment * weight
            total_weight += weight

    if total_weight < 0.001:
        return None

    # Normalize: -1~+1 sentiment → 0-100 score
    raw = total_pos / total_weight
    # Map to neutral=50, scale up to 100/-100
    score = 50 + raw * 60  # max range ~[-10, 110], clipped below
    return float(np.clip(score, 0, 100))


def aggregate_etf_scores(
    csdn_df: pd.DataFrame,
    components: dict[str, dict[str, float]],
    date_str: str | None = None,
) -> dict[str, float]:
    """Compute ETF sentiment scores for a specific date.

    Args:
        csdn_df: Full CSDN data (pre-filtered to relevant date)
        components: {etf_code: {stock_code: weight}}
        date_str: YYYY-MM-DD date to filter, or None to use all data

    Returns:
        {etf_code: score_0_to_100} compatible with theme_score format
    """
    if len(csdn_df) == 0:
        return {}

    if date_str:
        date_mask = csdn_df["Date"] == pd.to_datetime(date_str)
        daily = csdn_df[date_mask]
    else:
        daily = csdn_df

    if len(daily) == 0:
        return {}

    scores: dict[str, float] = {}
    for etf_code, comps in components.items():
        score = _compute_etf_sentiment(daily, comps)
        if score is not None:
            scores[etf_code] = round(score, 2)

    return scores


# ═══════════════════════════════════════════════════
# Step 4: Backtest-grade daily scoring (cached)
# ═══════════════════════════════════════════════════

def build_csdn_score_cache(
    csdn_df: pd.DataFrame,
    components: dict[str, dict[str, float]],
    cache_dir: Path | None = None,
) -> dict[str, dict[str, float]]:
    """Pre-compute daily ETF scores for all dates in CSDN data.

    Returns {date_str: {etf_code: score}} for fast backtest lookup.
    Saves to JSON cache if cache_dir provided.
    """
    if cache_dir is None:
        cache_dir = Path(__file__).resolve().parent / "data" / "csdn_scores"

    cache_file = cache_dir / "csdn_daily_scores.json"
    if cache_file.exists():
        print(f"[SentAgg] Loading cached scores from {cache_file}")
        return json.loads(cache_file.read_text(encoding="utf-8"))

    cache_dir.mkdir(parents=True, exist_ok=True)

    all_dates = sorted(csdn_df["Date"].dropna().unique())
    result: dict[str, dict[str, float]] = {}

    for i, dt in enumerate(all_dates):
        date_str = pd.Timestamp(dt).strftime("%Y-%m-%d")
        scores = aggregate_etf_scores(csdn_df, components, date_str=date_str)
        if scores:
            result[date_str] = scores
        if (i + 1) % 60 == 0:
            print(f"[SentAgg] Processed {i+1}/{len(all_dates)} dates...")

    print(f"[SentAgg] Cached {len(result)} dates with ETF scores")
    cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


# ═══════════════════════════════════════════════════
# Main entrypoint
# ═══════════════════════════════════════════════════

def main():
    """Build the complete sentiment aggregation pipeline."""
    print("=" * 60)
    print("CSDN → ETF Sentiment Aggregator")
    print("=" * 60)

    # 1. Fetch ETF components
    print("\n[1/3] Fetching ETF index components...")
    components = fetch_index_components(use_cache=True)
    print(f"  ETFs with components: {list(components.keys())}")

    # 2. Load CSDN data
    print("\n[2/3] Loading CSDN network sentiment data (2020-2023)...")
    csdn_df = load_csdn_range(2020, 2023)
    print(f"  Total: {len(csdn_df):,} rows, {csdn_df['Date'].nunique()} dates")

    if len(csdn_df) == 0:
        print("  ERROR: No CSDN data loaded!")
        return

    # 3. Build daily score cache
    print("\n[3/3] Building daily ETF score cache...")
    cache = build_csdn_score_cache(csdn_df, components)
    print(f"  Final cache: {len(cache)} dates")

    # Quick stats
    print("\n--- Sentiment Stats ---")
    for etf in sorted(cache[list(cache.keys())[0]].keys()):
        scores = [d[etf] for d in cache.values() if etf in d]
        if scores:
            print(f"  {etf}: mean={np.mean(scores):.1f}, std={np.std(scores):.1f}, "
                  f"min={np.min(scores):.1f}, max={np.max(scores):.1f}")


if __name__ == "__main__":
    main()
