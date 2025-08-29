# -*- coding: utf-8 -*-
import tkinter as tk
from tkinter import ttk

root = tk.Tk()
root.title("kabuS – New UI (mock)")
root.geometry("1280x800")
root.minsize(1100, 680)

# ========== style ==========
style = ttk.Style()
try: style.theme_use("clam")
except Exception: pass

style.configure("Hdr.TLabel", font=("Meiryo UI", 28, "bold"))
style.configure("SubHdr.TLabel", font=("Meiryo UI", 12))
style.configure("Small.TLabel", font=("Meiryo UI", 9))
style.configure("Group.TLabelframe.Label", font=("Meiryo UI", 10, "bold"))
style.configure("ColHdr.TLabel", font=("Meiryo UI", 10, "bold"))
style.configure("Good.TLabel", foreground="#188038")
style.configure("Warn.TLabel", foreground="#d93025")
style.configure("Card.TLabelframe.Label", font=("Meiryo UI", 10, "bold"))
style.configure("Card.TLabelframe", padding=8)

# ========== layout split ==========
pw = ttk.Panedwindow(root, orient="horizontal")
pw.pack(fill="both", expand=True)

left = ttk.Frame(pw, padding=(8, 8))
right = ttk.Frame(pw, padding=(8, 8))
pw.add(left, weight=1)
pw.add(right, weight=4)

# ========== LEFT: controls ==========
lf_ctrl = ttk.LabelFrame(left, text="操作 / 設定", padding=8, style="Group.TLabelframe")
lf_ctrl.pack(fill="x", pady=(0, 8))

arm_var = tk.IntVar(value=0)
dbg_var = tk.IntVar(value=0)
ttk.Checkbutton(lf_ctrl, text="ARM（実弾許可）", variable=arm_var).pack(anchor="w")
ttk.Checkbutton(lf_ctrl, text="DEBUG（ログ詳細）", variable=dbg_var).pack(anchor="w", pady=(2,6))
ttk.Button(lf_ctrl, text="AUTO ON/OFF").pack(fill="x", pady=(0,6))
ttk.Button(lf_ctrl, text="設定保存").pack(fill="x")
ttk.Button(lf_ctrl, text="使い方 / HELP").pack(fill="x", pady=(6,0))

lf_sym = ttk.LabelFrame(left, text="メイン銘柄", padding=8, style="Group.TLabelframe")
lf_sym.pack(fill="x", pady=(8, 8))
ttk.Label(lf_sym, text="8136（サンリオ）").pack(anchor="w")

lf_qty = ttk.LabelFrame(left, text="数量（デフォルト株数）", padding=8, style="Group.TLabelframe")
lf_qty.pack(fill="x", pady=(0, 8))
ttk.Spinbox(lf_qty, from_=100, to=100000, increment=100, width=10, state="readonly").pack(anchor="w")

lf_filters = ttk.LabelFrame(left, text="補助フィルタ（メイン監視）", padding=8, style="Group.TLabelframe")
lf_filters.pack(fill="x", pady=(0, 8))
v_vwap = tk.IntVar(value=1)
v_sma  = tk.IntVar(value=0)
v_macd = tk.IntVar(value=1)
v_rsi  = tk.IntVar(value=0)
v_swg  = tk.IntVar(value=0)
ttk.Checkbutton(lf_filters, text="VWAP（順張り）", variable=v_vwap).pack(anchor="w")
ttk.Checkbutton(lf_filters, text="SMA25/5m（順張り）", variable=v_sma).pack(anchor="w")
ttk.Checkbutton(lf_filters, text="MACD(12,26,9)", variable=v_macd).pack(anchor="w")
ttk.Checkbutton(lf_filters, text="RSI(14)", variable=v_rsi).pack(anchor="w")
ttk.Checkbutton(lf_filters, text="Swing（高値切下げ/安値切上げ）", variable=v_swg).pack(anchor="w")

# ========== RIGHT: header / summary ==========
summary = ttk.Frame(right)
summary.pack(fill="x")

# 左：銘柄・現在値・前日比
hdr_l = ttk.Frame(summary)
hdr_l.pack(side="left", anchor="w", fill="x", expand=True)
ttk.Label(hdr_l, text="8136  サンリオ  7,711.0", style="Hdr.TLabel").pack(anchor="w")
ttk.Label(hdr_l, text="-6.0 (-0.08%)", style="Good.TLabel").pack(anchor="w")

# 右：LIVE/SIMの“保有＆株数”のみ（成績は各タブに表示）
hdr_r = ttk.Frame(summary)
hdr_r.pack(side="right", anchor="e")

card_live = ttk.LabelFrame(hdr_r, text="LIVE", style="Card.TLabelframe")
card_live.grid(row=0, column=0, sticky="e", padx=(0,8))
ttk.Label(card_live, text="保有：—").grid(row=0, column=0, sticky="w")
ttk.Label(card_live, text="株数：100").grid(row=1, column=0, sticky="w")

card_sim = ttk.LabelFrame(hdr_r, text="SIM", style="Card.TLabelframe")
card_sim.grid(row=0, column=1, sticky="e")
ttk.Label(card_sim, text="保有：—").grid(row=0, column=0, sticky="w")
ttk.Label(card_sim, text="株数：100").grid(row=1, column=0, sticky="w")

# インジ行
ttk.Label(
    right,
    text="pushes=0    VWAP:—   SMA25:—   MACD:—/—   RSI:—",
    style="Small.TLabel"
).pack(fill="x", pady=(6, 8))

# ========== Tabs ==========
tabs = ttk.Notebook(right)
tabs.pack(fill="both", expand=True)

# --- Tab: 板・歩み値 ---
tab_board = ttk.Frame(tabs)
tabs.add(tab_board, text="板・歩み値")

ttk.Label(tab_board, text="板（10本）", style="ColHdr.TLabel").pack(anchor="w", pady=(6, 2))
book = ttk.Frame(tab_board); book.pack(fill="x")
col_ask = ttk.Frame(book); col_ask.pack(side="left", fill="both", expand=True, padx=(0, 4))
col_bid = ttk.Frame(book); col_bid.pack(side="left", fill="both", expand=True, padx=(4, 0))
ttk.Label(col_ask, text="売り(Ask)", style="ColHdr.TLabel").grid(row=0, column=0, padx=(0,12))
ttk.Label(col_bid, text="買い(Bid)", style="ColHdr.TLabel").grid(row=0, column=0, padx=(0,12))
ask_rows = [("7728.0","21,900"),("7727.0","1,800"),("7726.0","800"),("7725.0","800"),("7724.0","800"),
            ("7723.0","500"),("7722.0","500"),("7721.0","5,100"),("7719.0","100"),("7718.0","100")]
bid_rows = [("7711.0","12,700"),("7710.0","7,500"),("7709.0","3,200"),("7708.0","1,100"),("7707.0","600"),
            ("7706.0","23,300"),("7705.0","300"),("7704.0","100"),("7703.0","300"),("7702.0","900")]
for r,(p,q) in enumerate(ask_rows,1):
    ttk.Label(col_ask, text=p, width=8, anchor="e").grid(row=r, column=0, sticky="e")
    ttk.Label(col_ask, text=q, width=8, anchor="e").grid(row=r, column=1, sticky="e")
for r,(p,q) in enumerate(bid_rows,1):
    ttk.Label(col_bid, text=p, width=8, anchor="e").grid(row=r, column=0, sticky="e")
    ttk.Label(col_bid, text=q, width=8, anchor="e").grid(row=r, column=1, sticky="e")

lf_tape = ttk.LabelFrame(tab_board, text="歩み値（最新表示）", padding=6, style="Group.TLabelframe")
lf_tape.pack(fill="both", expand=True, pady=(8, 6))
tape = ttk.Treeview(lf_tape, columns=("t","side","px","qty"), show="headings", height=8)
tape.pack(fill="both", expand=True)
for c, w, a in (("t",90,"center"), ("side",60,"center"), ("px",90,"e"), ("qty",90,"e")):
    tape.heading(c, text={"t":"時刻","side":"売買","px":"価格","qty":"数量"}[c])
    tape.column(c, width=w, anchor=a)
for it in [("13:31:35","SELL","7,755.0","100"), ("13:31:34","BUY","7,752.0","100")]:
    tape.insert("", "end", values=it)

# --- Tab: 資金（残高などダミー） ---
tab_funds = ttk.Frame(tabs)
tabs.add(tab_funds, text="資金")
ttk.Label(tab_funds, text="資金情報（ダミー表示）", style="SubHdr.TLabel").pack(anchor="w", pady=8)

# --- Tab: 建玉 ---
tab_pos = ttk.Frame(tabs)
tabs.add(tab_pos, text="建玉")
pos = ttk.Treeview(tab_pos, columns=("sym","side","qty","entry","pnl"), show="headings", height=10)
pos.pack(fill="both", expand=True, padx=6, pady=6)
for k,txt,w,a in (("sym","銘柄",80,"center"),("side","売買",60,"center"),
                  ("qty","数量",80,"e"),("entry","建値",90,"e"),("pnl","評価損益",100,"e")):
    pos.heading(k, text=txt); pos.column(k, width=w, anchor=a)

# --- Tab: 注文 ---
tab_ord = ttk.Frame(tabs)
tabs.add(tab_ord, text="注文")
ordv = ttk.Treeview(tab_ord, columns=("id","sym","name","side","qty","price","st"), show="headings", height=12)
ordv.pack(fill="both", expand=True, padx=6, pady=6)
for k,txt,w,a in (("id","ID",120,"w"),("sym","銘柄",60,"center"),("name","名称",160,"w"),
                  ("side","売買",60,"center"),("qty","数量",80,"e"),("price","価格",90,"e"),("st","状態",100,"w")):
    ordv.heading(k, text=txt); ordv.column(k, width=w, anchor=a)

# --- Tab: LIVE履歴（成績＋保存ボタン） ---
tab_live = ttk.Frame(tabs)
tabs.add(tab_live, text="LIVE履歴")
bar_live = ttk.Frame(tab_live); bar_live.pack(fill="x", pady=(6,2), padx=6)
ttk.Label(bar_live, text="LIVE: Trades: 0 | Win: 0 (0.0%) | P&L: ¥0 | Avg: 0.00t",
          style="Small.TLabel").pack(side="left")
ttk.Frame(bar_live).pack(side="left", padx=8)  # spacer
ttk.Button(bar_live, text="CSV保存").pack(side="right", padx=(6,0))
ttk.Button(bar_live, text="XLSX保存").pack(side="right")
live = ttk.Treeview(tab_live, columns=("t","sym","side","qty","entry","exit","ticks","pnl","reason"),
                    show="headings", height=12)
live.pack(fill="both", expand=True, padx=6, pady=6)
for k,txt,w,a in (("t","時刻",130,"w"),("sym","銘柄",60,"center"),("side","売買",60,"center"),
                  ("qty","数量",70,"e"),("entry","建値",90,"e"),("exit","決済",90,"e"),
                  ("ticks","tick",60,"e"),("pnl","損益(円)",90,"e"),("reason","理由",160,"w")):
    live.heading(k, text=txt); live.column(k, width=w, anchor=a)

# --- Tab: SIM履歴（成績＋保存ボタン） ---
tab_sim = ttk.Frame(tabs)
tabs.add(tab_sim, text="SIM履歴")
bar_sim = ttk.Frame(tab_sim); bar_sim.pack(fill="x", pady=(6,2), padx=6)
ttk.Label(bar_sim, text="SIM: Trades: 0 | Win: 0 (0.0%) | P&L: ¥0 | Avg: 0.00t",
          style="Small.TLabel").pack(side="left")
ttk.Frame(bar_sim).pack(side="left", padx=8)  # spacer
ttk.Button(bar_sim, text="CSV保存").pack(side="right", padx=(6,0))
ttk.Button(bar_sim, text="XLSX保存").pack(side="right")
sim = ttk.Treeview(tab_sim, columns=("t","sym","side","qty","entry","exit","ticks","pnl","reason"),
                   show="headings", height=12)
sim.pack(fill="both", expand=True, padx=6, pady=6)
for k,txt,w,a in (("t","時刻",130,"w"),("sym","銘柄",60,"center"),("side","売買",60,"center"),
                  ("qty","数量",70,"e"),("entry","建値",90,"e"),("exit","決済",90,"e"),
                  ("ticks","tick",60,"e"),("pnl","損益(円)",90,"e"),("reason","理由",160,"w")):
    sim.heading(k, text=txt); sim.column(k, width=w, anchor=a)

# --- Tab: スクリーニング ---
tab_scan = ttk.Frame(tabs)
tabs.add(tab_scan, text="スクリーニング")
bar = ttk.Frame(tab_scan); bar.pack(fill="x", pady=(8,2), padx=6)
ttk.Button(bar, text="スクリーニング開始").pack(side="left")
ttk.Button(bar, text="停止").pack(side="left", padx=(6,0))
scan = ttk.Treeview(tab_scan, columns=("sym","name","score","note"), show="headings", height=12)
scan.pack(fill="both", expand=True, padx=6, pady=6)
for k,txt,w,a in (("sym","銘柄",70,"center"),("name","名称",160,"w"),
                  ("score","Score",70,"e"),("note","備考",300,"w")):
    scan.heading(k, text=txt); scan.column(k, width=w, anchor=a)

# --- Tab: 資金管理 ---
tab_rm = ttk.Frame(tabs)
tabs.add(tab_rm, text="資金管理")
frm = ttk.Frame(tab_rm); frm.pack(fill="x", padx=10, pady=10)

# 左列
colL = ttk.Frame(frm); colL.pack(side="left", fill="x", expand=True)
ttk.Label(colL, text="日次損失上限（円）").grid(row=0, column=0, sticky="w", pady=2)
ttk.Entry(colL, width=12).grid(row=0, column=1, sticky="w")
ttk.Label(colL, text="1トレード最大損失（円）").grid(row=1, column=0, sticky="w", pady=2)
ttk.Entry(colL, width=12).grid(row=1, column=1, sticky="w")
ttk.Label(colL, text="連敗クールダウン（回）").grid(row=2, column=0, sticky="w", pady=2)
ttk.Spinbox(colL, from_=1, to=10, width=10, state="readonly").grid(row=2, column=1, sticky="w")
ttk.Label(colL, text="クールダウン時間（分）").grid(row=3, column=0, sticky="w", pady=2)
ttk.Spinbox(colL, from_=1, to=60, width=10, state="readonly").grid(row=3, column=1, sticky="w")

# 右列
colR = ttk.Frame(frm); colR.pack(side="left", fill="x", expand=True, padx=(24,0))
ttk.Label(colR, text="同時ポジション上限（本）").grid(row=0, column=0, sticky="w", pady=2)
ttk.Spinbox(colR, from_=1, to=10, width=10, state="readonly").grid(row=0, column=1, sticky="w")
ttk.Label(colR, text="寄付きエントリー停止（分）").grid(row=1, column=0, sticky="w", pady=2)
ttk.Spinbox(colR, from_=0, to=60, width=10, state="readonly").grid(row=1, column=1, sticky="w")
ttk.Label(colR, text="日次停止ON").grid(row=2, column=0, sticky="w", pady=2)
ttk.Checkbutton(colR).grid(row=2, column=1, sticky="w")
ttk.Button(tab_rm, text="資金管理の設定を保存").pack(anchor="e", padx=10, pady=(8,10))

# --- Tab: ログ ---
tab_log = ttk.Frame(tabs)
tabs.add(tab_log, text="ログ")
lf_reason = ttk.LabelFrame(tab_log, text="理由（最新）", padding=6, style="Group.TLabelframe")
lf_reason.pack(fill="x", padx=6, pady=(8,6))
ttk.Label(lf_reason, text="見せ板検出（ASK）：サイズ 5,100／寿命 0.8秒／接近撤収 → プライスプル傾向（score 72）",
          style="Small.TLabel", wraplength=1100, justify="left").pack(anchor="w")
lf_log = ttk.LabelFrame(tab_log, text="ログ", padding=6, style="Group.TLabelframe")
lf_log.pack(fill="both", expand=True, padx=6, pady=(0,8))
logv = ttk.Treeview(lf_log, columns=("msg",), show="headings", height=12)
logv.pack(fill="both", expand=True)
logv.heading("msg", text="メッセージ"); logv.column("msg", anchor="w")
for line in [
    "[16:01:01] [INFO] 起動（モックUI）",
    "[16:01:02] [INFO] ARM=SAFE",
    "[16:01:05] [WARN] 見せ板スコア 72（ASK）→ 発注ガード（ダミー）",
]:
    logv.insert("", "end", values=(line,))

right.rowconfigure(2, weight=1)
tabs.enable_traversal()

root.mainloop()
