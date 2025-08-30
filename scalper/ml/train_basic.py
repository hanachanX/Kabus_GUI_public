# scalper/ml/train_basic.py
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
学習手順（要約）
1) v1/v2 の特徴量CSVを収集 → v2互換に正規化（FEATURE_COLUMNS を数値化・欠損0埋め）
2) OHLCがあれば読み込み（ts, symbol, high, low）→ 無ければ last を 1秒リサンプルで近似
3) 各行(ts,symbol,side,last,tp_ticks,sl_ticks,tick_size)について、lookahead 窓で TP/SL 先達成を判定
   - tieは SL優先（保守的） → y=1(TP), y=0(SL) を付与
4) 時系列CVで AUC/PR-AUC を評価、ロジスティック回帰（標準化＋class_weight=balanced）
5) 検証平均で F1 が最大になる閾値を推定 → モデルと特徴量・閾値などメタデータを保存
"""

import os
import json
import glob
import math
import datetime as dt
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd

from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score, average_precision_score, f1_score, precision_recall_curve
from sklearn.model_selection import TimeSeriesSplit
from joblib import dump

import scalper  # ルート検出に使用
from scalper.ml.features import FEATURE_COLUMNS

# ===================== 設定 =====================
ROOT = Path(scalper.__file__).resolve().parent

FEATURE_DIRS = [
    ROOT / "sim_logs" / "features",       # パッケージ側
    Path.cwd() / "sim_logs" / "features",  # 作業ディレクトリ側
]

# OHLC データ（1秒足以上）を置く候補ディレクトリ
# 例: <root>/market/ohlc/20250828/*.csv や ./market/ohlc/20250828/*.csv
OHLC_DIRS = [
    ROOT / "market" / "ohlc",
    Path.cwd() / "market" / "ohlc",
]

MODELS_DIR = ROOT / "ml" / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

# 学習ターゲット
#   "auto"    … v2(labelあり)ならゲート学習, 無ければ outcome(結果学習)
#   "gate"    … label列（1=ENTER採用,0=SKIP）を目的変数に
#   "outcome" … TP/SL 先達成で目的変数を作る
TRAIN_TARGET = "auto"

# 結果学習時のラベル窓
LOOKAHEAD_SEC = 10.0

# v2固定列
V2_FIXED = ["ts", "symbol", "side", "label", "skip_reason", "tp_ticks", "sl_ticks"]
V1_FIXED = ["ts", "symbol", "side_hint", "tp_ticks", "sl_ticks"]

# デフォルトのtick_size（未提供時のフォールバック）。本番は列で渡すこと推奨
DEFAULT_TICK_SIZE = 0.5

# CV 分割数
N_SPLITS = 5

# 乱数シード（再現性）
RANDOM_STATE = 42
# =================================================


# ---------- 共通ユーティリティ ----------
def _collect_feature_files(day: str | None) -> List[Path]:
    files: List[Path] = []

    # 1) day 指定 (e.g. "20250827")
    if day:
        for d in FEATURE_DIRS:
            dd = d / day
            if dd.is_dir():
                files += list(dd.glob("*.csv"))

    # 2) 指定なし → 各rootの最新日付フォルダから1つ
    if not files and day is None:
        for d in FEATURE_DIRS:
            if not d.is_dir():
                continue
            sub = sorted([p for p in d.iterdir() if p.is_dir() and p.name.isdigit()],
                         key=lambda p: p.name)
            for dd in reversed(sub):
                cand = list(dd.glob("*.csv"))
                if cand:
                    files += cand
                    break

    # 3) まだ無ければ再帰で全部
    if not files:
        for d in FEATURE_DIRS:
            if d.is_dir():
                files += list(d.rglob("*.csv"))

    if not files:
        raise FileNotFoundError(f"No feature CSV found under: {', '.join(map(str, FEATURE_DIRS))}")
    return files


def _collect_ohlc_files(day: str | None) -> List[Path]:
    files: List[Path] = []
    if day:
        for d in OHLC_DIRS:
            dd = d / day
            if dd.is_dir():
                files += list(dd.glob("*.csv"))
    if not files:
        for d in OHLC_DIRS:
            if d.is_dir():
                files += list(d.rglob("*.csv"))
    return files


def _detect_schema(cols: set) -> str:
    if "side" in cols and "label" in cols:
        return "v2"
    if "side_hint" in cols:
        return "v1"
    raise ValueError(f"Unknown schema: {sorted(cols)}")


def _normalize_to_v2(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    """
    v1: ts,symbol,side_hint,tp_ticks,sl_ticks + FEATURE_COLUMNS
    v2: ts,symbol,side,label,skip_reason,tp_ticks,sl_ticks + FEATURE_COLUMNS + pushes_per_min
    → どちらも v2互換に正規化して返す（不足列は補う）
    """
    cols = set(df.columns)
    schema = _detect_schema(cols)

    df = df.copy()
    if schema == "v1":
        df = df.rename(columns={"side_hint": "side"})
        if "label" not in df.columns:
            df["label"] = 1  # v1はENTERのみ想定 → label=1 で補完
        if "skip_reason" not in df.columns:
            df["skip_reason"] = ""
        if "pushes_per_min" not in df.columns:
            df["pushes_per_min"] = np.nan

    # ts to datetime
    if not np.issubdtype(df["ts"].dtype, np.datetime64):
        df["ts"] = pd.to_datetime(df["ts"], errors="coerce", unit=None)
        # 失敗時は epoch秒想定
        if df["ts"].isna().all():
            df["ts"] = pd.to_datetime(df["ts"], unit="s", errors="coerce")

    # side 正規化（文字列）
    df["side"] = df["side"].astype(str).str.upper().replace(
        {"買": "BUY", "売": "SELL", "B": "BUY", "S": "SELL", "LONG": "BUY", "SHORT": "SELL"}
    )

    # FEATURE_COLUMNS を優先採用し数値化
    feature_cols = list(FEATURE_COLUMNS)
    if "pushes_per_min" in df.columns and "pushes_per_min" not in feature_cols:
        feature_cols = feature_cols + ["pushes_per_min"]

    for c in feature_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        else:
            df[c] = np.nan
    df[feature_cols] = df[feature_cols].astype(float).fillna(0.0)

    # last / tick_size の用意（ラベリングに使用）
    if "last" not in df.columns:
        raise ValueError("feature CSV must contain 'last' price column for labeling.")
    df["last"] = pd.to_numeric(df["last"], errors="coerce")

    if "tick_size" not in df.columns:
        df["tick_size"] = DEFAULT_TICK_SIZE
    else:
        df["tick_size"] = pd.to_numeric(df["tick_size"], errors="coerce").fillna(DEFAULT_TICK_SIZE)

    # tp/sl
    if "tp_ticks" not in df.columns or "sl_ticks" not in df.columns:
        raise ValueError("feature CSV must contain 'tp_ticks' and 'sl_ticks' columns.")
    df["tp_ticks"] = pd.to_numeric(df["tp_ticks"], errors="coerce").fillna(0).astype(float)
    df["sl_ticks"] = pd.to_numeric(df["sl_ticks"], errors="coerce").fillna(0).astype(float)

    df = df.sort_values(["symbol", "ts"]).reset_index(drop=True)
    return df, feature_cols


def _assert_no_future_features(df: pd.DataFrame, feature_cols: List[str]):
    # 代表的な未来参照の名前を弾く（必要に応じて追加）
    bad = [c for c in feature_cols if any(k in c.lower() for k in ["future", "lead", "ahead", "t+"])]
    if bad:
        raise ValueError(f"Future-referencing features detected: {bad}")


def load_logs(day: str | None = None) -> Tuple[pd.DataFrame, List[str], bool]:
    files = _collect_feature_files(day)
    dfs = []
    feature_cols_last: List[str] | None = None
    has_label = False

    for p in files:
        try:
            df_raw = pd.read_csv(p)
            df_norm, feats = _normalize_to_v2(df_raw)
            dfs.append(df_norm)
            feature_cols_last = feats
            if "label" in df_norm.columns:
                has_label = True
        except Exception as e:
            print(f"[WARN] skip {p}: {e}")

    if not dfs:
        raise RuntimeError("No valid rows in feature CSVs.")

    df = pd.concat(dfs, ignore_index=True).sort_values(["symbol", "ts"]).reset_index(drop=True)

    _assert_no_future_features(df, feature_cols_last or list(FEATURE_COLUMNS))
    return df, (feature_cols_last or list(FEATURE_COLUMNS)), has_label


# ---------- OHLC 読み込み（任意） ----------
def load_ohlc(day: str | None = None) -> pd.DataFrame | None:
    """
    期待する列: ts, symbol, high, low（open/close任意）
    """
    files = _collect_ohlc_files(day)
    dfs = []
    for p in files:
        try:
            d = pd.read_csv(p)
            need = {"ts", "symbol", "high", "low"}
            if not need.issubset(set(map(str.lower, d.columns.str.lower()))):
                # ラフに列名対応
                cols = {c.lower(): c for c in d.columns}
                miss = [x for x in ["ts", "symbol", "high", "low"] if x not in cols]
                if miss:
                    continue
                d = d[[cols["ts"], cols["symbol"], cols["high"], cols["low"]]]
                d = d.rename(columns={cols["ts"]: "ts", cols["symbol"]: "symbol",
                                      cols["high"]: "high", cols["low"]: "low"})
            else:
                # すでに目的列があるケース
                pass
            d["ts"] = pd.to_datetime(d["ts"], errors="coerce")
            d["high"] = pd.to_numeric(d["high"], errors="coerce")
            d["low"]  = pd.to_numeric(d["low"], errors="coerce")
            d = d.dropna(subset=["ts", "high", "low"])
            dfs.append(d[["ts", "symbol", "high", "low"]])
        except Exception as e:
            print(f"[WARN] skip OHLC {p}: {e}")

    if not dfs:
        return None

    ohlc = pd.concat(dfs, ignore_index=True)
    ohlc = ohlc.sort_values(["symbol", "ts"]).reset_index(drop=True)
    return ohlc


# ---------- 結果ラベリング（TP/SL先達成） ----------
def label_by_tp_sl(df: pd.DataFrame, lookahead_sec: float, ohlc: pd.DataFrame | None) -> pd.DataFrame:
    """
    df: v2互換（ts,symbol,side,last,tp_ticks,sl_ticks,tick_size 含む）
    ohlc: ts,symbol,high,low（無ければ None → last を1sにリサンプルして近似）
    戻り: y(1=TP,0=SL)、exit_time, exit_price, exit_reason を付与した df（y欠損は落とす）
    """
    if "ts" not in df or "symbol" not in df or "last" not in df:
        raise ValueError("df must contain ['ts','symbol','last'].")

    out = df.copy()
    out["ts"] = pd.to_datetime(out["ts"])
    out = out.sort_values(["symbol", "ts"]).reset_index(drop=True)

    # 参照価格テーブルの構築
    if ohlc is not None:
        price = ohlc.copy()
        price["ts"] = pd.to_datetime(price["ts"])
        price = price.sort_values(["symbol", "ts"])
    else:
        # 近似: last を 1秒足でresample → rolling max/min（lookahead幅）
        tmp = out[["symbol", "ts", "last"]].dropna().drop_duplicates()
        ser = tmp.set_index("ts")
        # 記号ごと 1秒 resample
        frames = []
        for sym, g in ser.groupby(tmp["symbol"].values):
            g = g.sort_index()
            g1s = g.resample("1S").ffill().dropna()
            g1s["symbol"] = sym
            frames.append(g1s.reset_index())
        if not frames:
            raise RuntimeError("No price rows to build fallback series.")
        ser1s = pd.concat(frames, ignore_index=True)
        win = max(1, int(round(lookahead_sec)))
        # ここでは後で区間抽出するので、そのまま high/low = last で持っておく
        price = ser1s.rename(columns={"last": "high"})
        price["low"] = price["high"]

    out["y"] = np.nan
    out["exit_time"] = pd.NaT
    out["exit_price"] = np.nan
    out["exit_reason"] = ""

    # 索引用
    price = price.sort_values(["symbol", "ts"]).reset_index(drop=True)

    for sym, g in out.groupby("symbol", sort=False):
        g = g.copy()
        p = price[price["symbol"] == sym]
        if p.empty:
            continue
        p = p[["ts", "high", "low"]].set_index("ts")

        for i, row in g.iterrows():
            entry_t = row["ts"]
            side = row["side"]
            entry = float(row["last"])
            tsize = float(row.get("tick_size", DEFAULT_TICK_SIZE))
            tp_ticks = float(row["tp_ticks"])
            sl_ticks = float(row["sl_ticks"])

            if side == "BUY":
                tp_px = entry + tp_ticks * tsize
                sl_px = entry - sl_ticks * tsize
            else:  # SELL
                tp_px = entry - tp_ticks * tsize
                sl_px = entry + sl_ticks * tsize

            t_to = entry_t + pd.Timedelta(seconds=lookahead_sec)
            win_df = p.loc[(p.index > entry_t) & (p.index <= t_to)]
            if win_df.empty:
                continue

            # 先頭から走査 → 同時到達の可能性は SL優先
            hit = None
            hit_time = None
            for ts_i, pr in win_df.iterrows():
                hi = pr["high"]; lo = pr["low"]
                # 同一バー内の優先順は SL→TP
                if lo <= sl_px:
                    hit = ("SL", sl_px); hit_time = ts_i; break
                if hi >= tp_px:
                    hit = ("TP", tp_px); hit_time = ts_i; break

            if hit is None:
                continue

            reason, px = hit
            out.at[i, "y"] = 1 if reason == "TP" else 0
            out.at[i, "exit_time"] = hit_time
            out.at[i, "exit_price"] = px
            out.at[i, "exit_reason"] = reason

    out = out.dropna(subset=["y"]).copy()
    out["y"] = out["y"].astype(int)
    return out


# ---------- 学習 ----------
def _timeseries_cv_evaluate(X: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """
    時系列CVで AUC/PR-AUC と、F1 最大の平均最適閾値を返す
    """
    tscv = TimeSeriesSplit(n_splits=N_SPLITS)
    aucs, pras, thr_best = [], [], []

    pipe = Pipeline([
        ("scaler", StandardScaler(with_mean=True, with_std=True)),
        ("clf", LogisticRegression(max_iter=500, class_weight="balanced", random_state=RANDOM_STATE))
    ])

    for tr, va in tscv.split(X):
        pipe.fit(X[tr], y[tr])
        proba = pipe.predict_proba(X[va])[:, 1]
        aucs.append(roc_auc_score(y[va], proba))
        pras.append(average_precision_score(y[va], proba))

        # F1最大の閾値
        prec, rec, thr = precision_recall_curve(y[va], proba)
        f1s = np.where((prec + rec) > 0, 2 * prec * rec / (prec + rec), 0)
        if len(thr) == 0:
            thr_best.append(0.5)
        else:
            thr_best.append(float(thr[int(np.argmax(f1s))]))

    return float(np.mean(aucs)), float(np.mean(pras)), float(np.mean(thr_best))


def train_and_save_gate(df: pd.DataFrame, feature_cols: List[str]) -> None:
    y = df["label"].astype(int).values
    X = df[feature_cols].astype(float).values

    auc, prauc, best_thr = _timeseries_cv_evaluate(X, y)

    pipe = Pipeline([
        ("scaler", StandardScaler(with_mean=True, with_std=True)),
        ("clf", LogisticRegression(max_iter=500, class_weight="balanced", random_state=RANDOM_STATE))
    ])
    pipe.fit(X, y)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    model_path = MODELS_DIR / f"gate_{stamp}_auc{auc:.3f}_pra{prauc:.3f}.joblib"
    dump({"model": pipe, "features": feature_cols, "threshold": best_thr}, model_path)
    dump({"model": pipe, "features": feature_cols, "threshold": best_thr},
         MODELS_DIR / "model_latest_gate.joblib")
    # メタ
    meta = {
        "mode": "gate", "rows": int(len(df)), "features": feature_cols,
        "auc": auc, "pr_auc": prauc, "best_threshold": best_thr,
        "created_at": stamp,
    }
    (MODELS_DIR / f"gate_{stamp}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[SAVE] {model_path} | rows={len(df)} | feats={len(feature_cols)} | "
          f"AUC={auc:.3f} PR-AUC={prauc:.3f} thr~{best_thr:.3f}")


def train_and_save_outcome(df: pd.DataFrame, feature_cols: List[str], day: str | None) -> None:
    ohlc = load_ohlc(day)
    df_y = label_by_tp_sl(df, LOOKAHEAD_SEC, ohlc)
    if df_y.empty:
        raise RuntimeError("No rows labeled by TP/SL. Prepare OHLC or increase LOOKAHEAD_SEC.")

    y = df_y["y"].astype(int).values
    X = df_y[feature_cols].astype(float).values

    auc, prauc, best_thr = _timeseries_cv_evaluate(X, y)

    pipe = Pipeline([
        ("scaler", StandardScaler(with_mean=True, with_std=True)),
        ("clf", LogisticRegression(max_iter=500, class_weight="balanced", random_state=RANDOM_STATE))
    ])
    pipe.fit(X, y)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    model_path = MODELS_DIR / f"outcome_{stamp}_auc{auc:.3f}_pra{prauc:.3f}.joblib"
    dump({"model": pipe, "features": feature_cols, "threshold": best_thr,
          "lookahead_sec": LOOKAHEAD_SEC}, model_path)
    dump({"model": pipe, "features": feature_cols, "threshold": best_thr,
          "lookahead_sec": LOOKAHEAD_SEC}, MODELS_DIR / "model_latest_outcome.joblib")
    # メタ
    meta = {
        "mode": "outcome", "rows": int(len(df_y)), "features": feature_cols,
        "auc": auc, "pr_auc": prauc, "best_threshold": best_thr,
        "lookahead_sec": LOOKAHEAD_SEC, "created_at": stamp,
        "labeled_ratio": float(len(df_y) / max(1, len(df))),
    }
    (MODELS_DIR / f"outcome_{stamp}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[SAVE] {model_path} | rows={len(df_y)}/{len(df)} | feats={len(feature_cols)} | "
          f"AUC={auc:.3f} PR-AUC={prauc:.3f} thr~{best_thr:.3f} | labeled_ratio={meta['labeled_ratio']:.3f}")


# ---------- メイン ----------
def main(day: str | None = None):
    df, feature_cols, has_label = load_logs(day)

    mode = TRAIN_TARGET
    if mode == "auto":
        # v2 label が0/1両方入っているかでゲート学習可否を判断
        if has_label and df["label"].dropna().astype(int).nunique() >= 2:
            mode = "gate"
        else:
            mode = "outcome"

    print(f"[INFO] rows={len(df)}  features={len(feature_cols)}  mode={mode}  "
          f"cols={df.columns.tolist()[:12]}...")

    if mode == "gate":
        train_and_save_gate(df, feature_cols)
    elif mode == "outcome":
        train_and_save_outcome(df, feature_cols, day)
    else:
        raise ValueError(f"Unknown TRAIN_TARGET: {mode}")


if __name__ == "__main__":
    # 直近日付フォルダを自動選択。特定日だけを使うなら main(\"20250827\") のように渡す
    main(day=None)
