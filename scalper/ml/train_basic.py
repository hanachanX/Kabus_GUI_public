# scalper/ml/train_basic.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import glob
import datetime as dt
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd

# scikit-learn
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from joblib import dump

import scalper  # ルート検出に使用
from scalper.ml.features import FEATURE_COLUMNS

# ===================== 設定 =====================
# 保存ディレクトリ
ROOT = Path(scalper.__file__).resolve().parent
FEATURE_DIRS = [
    ROOT / "sim_logs" / "features",   # パッケージ側
    Path.cwd() / "sim_logs" / "features",  # 作業ディレクトリ側
]
MODELS_DIR = ROOT / "ml" / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

# ラベリング方法:
#   "auto"   … v2(ラベルあり)ならゲート学習, v1(ラベルなし)ならTP/SLで結果学習
#   "gate"   … v2の label 列（1=ENTER, 0=SKIP）を目的変数に
#   "outcome"… TP/SLの先達成で目的変数を作る（v1/v2どちらでも可）
TRAIN_TARGET = "auto"

LOOKAHEAD_SEC = 10.0  # 結果学習(outcome)時: 何秒先までにTP/SLどちらが先に付くか

# v2固定列
V2_FIXED = ["ts", "symbol", "side", "label", "skip_reason", "tp_ticks", "sl_ticks"]
V1_FIXED = ["ts", "symbol", "side_hint", "tp_ticks", "sl_ticks"]
# =================================================


# ---------- ログ検出/読み込み ----------
def _collect_files(day: str | None) -> List[Path]:
    files: List[Path] = []

    # 1) day指定なら当日フォルダのみ
    if day:
        for d in FEATURE_DIRS:
            dd = d / day
            if dd.is_dir():
                files += list(dd.glob("*.csv"))

    # 2) 指定なし→各ルートの最新日付を1つ
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
    → どちらも v2互換の形式に正規化して返す（不足列は補う）
    戻り: (df, feature_cols)
    """
    cols = set(df.columns)
    schema = _detect_schema(cols)

    df = df.copy()
    if schema == "v1":
        df = df.rename(columns={"side_hint": "side"})
        if "label" not in df.columns:
            # v1は ENTER ログのみ前提 → label=1 で補完
            df["label"] = 1
        if "skip_reason" not in df.columns:
            df["skip_reason"] = ""
        if "pushes_per_min" not in df.columns:
            df["pushes_per_min"] = np.nan

    # ts を datetime へ
    if not np.issubdtype(df["ts"].dtype, np.datetime64):
        try:
            df["ts"] = pd.to_datetime(df["ts"])
        except Exception:
            # エポック秒等にも対応
            df["ts"] = pd.to_datetime(df["ts"], unit="s", errors="coerce")

    # 特徴量列: まず FEATURE_COLUMNS を優先採用
    feature_cols = list(FEATURE_COLUMNS)

    # pushes_per_min があれば特徴量へ追加（重複は除く）
    if "pushes_per_min" in df.columns and "pushes_per_min" not in feature_cols:
        feature_cols = feature_cols + ["pushes_per_min"]

    # side を数値特徴として使いたければ side_enc を追加（任意）
    if "side" in df.columns and "side_enc" not in df.columns:
        side_map = {"BUY": 1.0, "SELL": -1.0}
        df["side_enc"] = df["side"].map(side_map).fillna(0.0).astype(float)
        # 先頭に入れたいなら feature_cols = ["side_enc"] + feature_cols
        # 既定では含めないが、必要なら上の行を有効化
        # feature_cols = ["side_enc"] + feature_cols

    # 数値化（存在するものだけ）
    for c in feature_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        else:
            # 欠損列は後で0埋めできるように作っておく
            df[c] = np.nan
    df[feature_cols] = df[feature_cols].astype(float).fillna(0.0)

    # ソート
    df = df.sort_values(["symbol", "ts"]).reset_index(drop=True)

    return df, feature_cols


def load_logs(day: str | None = None) -> Tuple[pd.DataFrame, List[str], bool]:
    """
    v1/v2 混在ログを読み込み、全て v2 互換に正規化。
    戻り: (df_all, feature_cols, has_label)
      has_label=True なら v2ラベルが有る（ゲート学習に使える）
    """
    files = _collect_files(day)
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

    df = pd.concat(dfs, ignore_index=True)

    # ts で再ソート
    df = df.sort_values(["symbol", "ts"]).reset_index(drop=True)

    return df, (feature_cols_last or list(FEATURE_COLUMNS)), has_label


# ---------- 結果ラベリング（TP/SL先達成） ----------
def label_by_tp_sl(df: pd.DataFrame, lookahead_sec: float = LOOKAHEAD_SEC) -> pd.DataFrame:
    """
    各行の last を起点に、tp_ticks/sl_ticks と tick_size から TP/SL 価格を計算。
    ts から lookahead_sec 以内に先に到達した方を y とする（1=TP先達成, 0=SL先達成）。
    """
    out = df.copy()
    out["y"] = np.nan

    for sym, g in out.groupby("symbol", sort=False):
        idx = g.index.values
        ts = g["ts"].values.astype("datetime64[ns]")
        last = g["last"].astype(float).values
        tp_ticks = g["tp_ticks"].astype(int).values
        sl_ticks = g["sl_ticks"].astype(int).values
        tick = g.get("tick_size", 0.5)
        tick = (tick.astype(float).values if hasattr(tick, "values") else np.full_like(last, 0.5))

        n = len(idx)
        for i in range(n):
            entry = last[i]
            tp_px = entry + tp_ticks[i] * tick[i]
            sl_px = entry - sl_ticks[i] * tick[i]
            t_limit = ts[i] + np.timedelta64(int(lookahead_sec * 1e9), "ns")

            j = i + 1
            hit = None
            while j < n and ts[j] <= t_limit:
                px = last[j]
                if px >= tp_px:
                    hit = 1; break
                if px <= sl_px:
                    hit = 0; break
                j += 1
            if hit is not None:
                out.loc[idx[i], "y"] = hit

    out = out.dropna(subset=["y"]).copy()
    out["y"] = out["y"].astype(int)
    return out


# ---------- 学習/保存 ----------
def train_and_save_gate(df: pd.DataFrame, feature_cols: List[str]) -> None:
    """ゲート学習（v2の label 列を目的変数に）。"""
    y = df["label"].astype(int).values
    X = df[feature_cols].astype(float).values

    # 時系列: 末尾20%を検証
    n = len(df)
    cut = max(1, int(n * 0.8))
    Xtr, Xva = X[:cut], X[cut:]
    ytr, yva = y[:cut], y[cut:]

    clf = LogisticRegression(max_iter=300)
    clf.fit(Xtr, ytr)

    auc = None
    try:
        proba = clf.predict_proba(Xva)[:, 1]
        auc = roc_auc_score(yva, proba)
    except Exception:
        pass

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    model_path = MODELS_DIR / f"gate_{stamp}{'' if auc is None else f'_auc{auc:.3f}'}.joblib"
    dump({"model": clf, "features": feature_cols}, model_path)
    latest = MODELS_DIR / "model_latest_gate.joblib"
    try:
        latest.unlink(missing_ok=True)
        latest.symlink_to(model_path.name)  # 相対
    except Exception:
        # Windows で symlink 不可の場合はコピー的に上書き保存
        dump({"model": clf, "features": feature_cols}, latest)

    print(f"[SAVE] {model_path} | rows={n} | feats={len(feature_cols)} | AUC={auc}")


def train_and_save_outcome(df: pd.DataFrame, feature_cols: List[str]) -> None:
    """結果学習（TP/SL先達成）"""
    df_y = label_by_tp_sl(df, LOOKAHEAD_SEC)
    if df_y.empty:
        raise RuntimeError("No rows labeled by TP/SL (increase LOOKAHEAD_SEC or check data).")

    y = df_y["y"].astype(int).values
    X = df_y[feature_cols].astype(float).values

    n = len(df_y)
    cut = max(1, int(n * 0.8))
    Xtr, Xva = X[:cut], X[cut:]
    ytr, yva = y[:cut], y[cut:]

    clf = LogisticRegression(max_iter=300)
    clf.fit(Xtr, ytr)

    auc = None
    try:
        proba = clf.predict_proba(Xva)[:, 1]
        auc = roc_auc_score(yva, proba)
    except Exception:
        pass

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    model_path = MODELS_DIR / f"outcome_{stamp}{'' if auc is None else f'_auc{auc:.3f}'}.joblib"
    dump({"model": clf, "features": feature_cols}, model_path)
    latest = MODELS_DIR / "model_latest_outcome.joblib"
    try:
        latest.unlink(missing_ok=True)
        latest.symlink_to(model_path.name)
    except Exception:
        dump({"model": clf, "features": feature_cols}, latest)

    print(f"[SAVE] {model_path} | rows={n} | feats={len(feature_cols)} | AUC={auc}")


# ---------- メイン ----------
def main(day: str | None = None):
    df, feature_cols, has_label = load_logs(day)

    # 学習モードの決定
    mode = TRAIN_TARGET
    if mode == "auto":
        mode = "gate" if has_label else "outcome"

    print(f"[INFO] rows={len(df)}  features={len(feature_cols)}  mode={mode}  "
          f"cols={df.columns.tolist()[:10]}...")

    if mode == "gate":
        # v2: label が 0/1 両方含まれる。必要なら下でクラス重み調整も可
        train_and_save_gate(df, feature_cols)
    elif mode == "outcome":
        train_and_save_outcome(df, feature_cols)
    else:
        raise ValueError(f"Unknown TRAIN_TARGET: {mode}")


if __name__ == "__main__":
    # 直近日付フォルダを自動選択。特定日を使うなら main("20250827") のように渡す
    main(day=None)
