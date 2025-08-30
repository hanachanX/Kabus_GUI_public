# spoofing.py — lightweight detector module + App integration hooks
# ---------------------------------------------------------------
# Drop this file next to Kabus_gui_v4_1.py and import it from there.
# Minimal deps, thread-safe enough for Tk after-call use.
#
# Public API:
#   d = SpoofDetector(cfg: dict)
#   d.update(ts_ms, best_bid, best_ask, best_bidq, best_askq, levels=None, last_trade=None) -> dict|None
#   d.format_badge(state) -> str  e.g., "買★★☆(layer)"
#   d.apply_gate(proposed_side: str, entry_confidence: float) -> (allow: bool, adj_conf: float, reason: str)
#   d.get_log_fields() -> dict for training logs (spoof_side, spoof_type, spoof_score, spoof_age_ms, spoof_peak_size)
#   d.update_config(**kwargs)
#
# Integration snippets for Kabus_gui_v4_1.py are at the bottom of this file.

from collections import deque
import math
import time
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple

# ------------------------- utilities -------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)

_DEF_CFG = dict(
    enabled=True,
    window_ms=3000,           # ring buffer horizon (ms)
    buffer_points=150,        # max snapshots retained
    k_big=3.5,                # big order threshold (x rolling mean size on that side)
    min_lifespan_ms=80,       # ignore super-noise (< this)
    flash_max_ms=800,         # flash: visible < 800ms
    layer_levels=5,           # depth levels considered per side
    layer_need=3,             # >= this many heavy levels stacked
    layer_drop_ms=900,        # stacked levels vanish together within this
    walk_window_ms=1400,      # look-back window for step-back detection
    walk_steps_need=3,        # need >= 3 outward steps
    score_threshold=0.70,     # AUTO gate threshold
    suppress_weight=0.20,     # confidence reduction weight below threshold
)

@dataclass
class Snapshot:
    t: int
    bid: float
    ask: float
    bq: float
    aq: float
    levels_b: Optional[List[Tuple[float, float]]] = None  # [(price, qty), ...] best -> deeper
    levels_a: Optional[List[Tuple[float, float]]] = None
    last_print_side: Optional[str] = None  # 'B' or 'S'

@dataclass
class State:
    side: str                 # 'B' or 'S' (where spoof suspected)
    type: str                 # 'flash'|'layer'|'walk'|'ping'
    score: float
    age_ms: int
    peak_size: float

# ----------------------- core detector -----------------------
class SpoofDetector:
    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = {**_DEF_CFG, **(cfg or {})}
        self.buf: Deque[Snapshot] = deque(maxlen=self.cfg['buffer_points'])
        # candidate lifecycle per side for flash
        self._cand: Dict[str, Optional[Dict]] = {'B': None, 'S': None}
        # walk-away tracking
        self._steps: Deque[Tuple[int, str, float]] = deque(maxlen=12)  # (t, side, best_price)
        self._last_state: Optional[State] = None

    # --------------- public API ---------------
    def update(self, ts_ms: int, best_bid: float, best_ask: float, best_bidq: float, best_askq: float,
               levels: Optional[Dict[str, List[Tuple[float, float]]]] = None,
               last_trade: Optional[Dict] = None) -> Optional[Dict]:
        if not self.cfg.get('enabled', True):
            return None
        snap = Snapshot(
            t=ts_ms, bid=best_bid, ask=best_ask, bq=float(best_bidq or 0), aq=float(best_askq or 0),
            levels_b=(levels or {}).get('B'), levels_a=(levels or {}).get('S'),
            last_print_side=(last_trade or {}).get('side')
        )
        self.buf.append(snap)
        # compute signals
        s_flash = self._detect_flash(snap)
        s_layer = self._detect_layer(snap)
        s_walk  = self._detect_walk(snap)
        s_ping  = self._detect_ping(snap)  # best-effort (optional)
        # choose highest score
        best = max([s for s in (s_flash, s_layer, s_walk, s_ping) if s], key=lambda x: x.score, default=None)
        self._last_state = best
        return self._state_to_dict(best) if best else None

    def format_badge(self, state: Optional[Dict]) -> str:
        if not state:
            return "なし"
        side = '買' if state['side'] == 'B' else '売'
        star = self._stars(state['score'])
        return f"{side}{star}({state['type']})"

    def apply_gate(self, proposed_side: str, entry_confidence: float) -> Tuple[bool, float, str]:
        s = self._last_state
        if not s:
            return True, entry_confidence, ''
        thr = self.cfg['score_threshold']
        reason = ''
        allow = True
        adj = entry_confidence
        # strict gate
        if s.score >= thr and s.side == (proposed_side or ''):
            allow = False
            reason = f"見送り: 見せ板疑い {self.format_badge(self._state_to_dict(s))} (>= {thr:.2f})"
        else:
            # soft penalty
            adj = max(0.0, entry_confidence - self.cfg['suppress_weight'] * s.score)
            reason = f"弱抑制: {self.format_badge(self._state_to_dict(s))} → conf {entry_confidence:.2f}→{adj:.2f}"
        return allow, adj, reason

    def get_log_fields(self) -> Dict:
        s = self._last_state
        if not s:
            return {"spoof_side":"", "spoof_type":"", "spoof_score":"", "spoof_age_ms":"", "spoof_peak_size":""}
        return {
            "spoof_side": 'B' if s.side=='B' else 'S',
            "spoof_type": s.type,
            "spoof_score": round(float(s.score), 3),
            "spoof_age_ms": int(s.age_ms),
            "spoof_peak_size": float(s.peak_size or 0),
        }

    def update_config(self, **kwargs):
        self.cfg.update(kwargs)
        self.buf = deque(self.buf, maxlen=self.cfg['buffer_points'])

    # --------------- detectors ---------------
    def _rolling_mean_sizes(self, ms: int) -> Tuple[float, float]:
        if not self.buf:
            return 0.0, 0.0
        t_now = self.buf[-1].t
        bsz = [s.bq for s in self.buf if (t_now - s.t) <= ms]
        asz = [s.aq for s in self.buf if (t_now - s.t) <= ms]
        def mean(x):
            return sum(x)/len(x) if x else 0.0
        return mean(bsz), mean(asz)

    def _detect_flash(self, snap: Snapshot) -> Optional[State]:
        # detect short-lived big size at best that vanishes quickly
        window_ms = self.cfg['window_ms']
        avg_b, avg_a = self._rolling_mean_sizes(window_ms)
        eps = 1e-9
        out = None
        for side, size, avg in (('B', snap.bq, max(avg_b, eps)), ('S', snap.aq, max(avg_a, eps))):
            cand = self._cand.get(side)
            big_now = (size >= self.cfg['k_big'] * avg) if avg > 0 else False
            t = snap.t
            if cand is None:
                if big_now:
                    self._cand[side] = dict(start=t, peak=size, last=size)
            else:
                # update peak
                cand['peak'] = max(cand['peak'], size)
                cand['last'] = size
                age = t - cand['start']
                # termination conditions (drop or timeout)
                dropped = (not big_now) and (size <= 0.6 * avg)
                timeout = age > self.cfg['flash_max_ms']
                if dropped or timeout:
                    life = max(1, t - cand['start'])
                    if life >= self.cfg['min_lifespan_ms'] and life <= self.cfg['flash_max_ms'] and dropped:
                        # score = relative size factor * short-life factor
                        rel = float(cand['peak']) / max(avg, eps)
                        life_fac = max(0.0, 1.0 - (life / self.cfg['flash_max_ms']))
                        score = math.tanh(0.35 * rel) * life_fac
                        out = State(side=side, type='flash', score=float(score), age_ms=int(life), peak_size=float(cand['peak']))
                    self._cand[side] = None
        return out

    def _detect_layer(self, snap: Snapshot) -> Optional[State]:
        # find stacked heavy levels that vanish together as price approaches
        lb = snap.levels_b or []
        la = snap.levels_a or []
        if not lb and not la:
            return None
        t_now = snap.t
        def stack_score(levels: List[Tuple[float, float]]) -> Tuple[int, float, float]:
            if not levels:
                return 0, 0.0, 0.0
            qtys = [q for _, q in levels[: self.cfg['layer_levels']]]
            if not qtys:
                return 0, 0.0, 0.0
            base = (sum(qtys)/len(qtys)) or 1.0
            heavy = [(p, q) for p, q in levels[: self.cfg['layer_levels']] if q >= self.cfg['k_big'] * base]
            return len(heavy), base, sum(q for _, q in heavy)
        nb, base_b, sum_b = stack_score(lb)
        na, base_a, sum_a = stack_score(la)
        # require approach + synchronized drop shortly after (check previous snapshot)
        prev = self.buf[-2] if len(self.buf) >= 2 else None
        if prev is None:
            return None
        res = []
        if nb >= self.cfg['layer_need']:
            # approaching bid-side (price falling toward bid)
            approaching = (prev.bid - snap.bid) >= 0.0 and (snap.ask - prev.ask) <= 0.0
            # drop: total heavy sum shrank notably vs prev
            prev_lb = prev.levels_b or []
            _, _, prev_sum_b = (0,0,0)
            if prev_lb:
                qb = [q for _, q in prev_lb[: self.cfg['layer_levels']]]
                base_prev = (sum(qb)/len(qb)) or 1.0
                heavy_prev = [q for _, q in prev_lb[: self.cfg['layer_levels']] if q >= self.cfg['k_big'] * base_prev]
                prev_sum_b = sum(heavy_prev) if heavy_prev else 0.0
            drop = (prev_sum_b > 0 and sum_b <= 0.5 * prev_sum_b and (snap.t - prev.t) <= self.cfg['layer_drop_ms'])
            if approaching and drop:
                rel = (prev_sum_b / max(base_b, 1.0)) if base_b else 0.0
                score = math.tanh(0.10 * rel)
                res.append(State(side='B', type='layer', score=float(score), age_ms=int(snap.t - prev.t), peak_size=float(prev_sum_b)))
        if na >= self.cfg['layer_need']:
            approaching = (snap.ask - prev.ask) <= 0.0 and (prev.ask - snap.ask) >= 0.0 or (snap.bid - prev.bid) >= 0.0
            prev_la = prev.levels_a or []
            _, _, prev_sum_a = (0,0,0)
            if prev_la:
                qa = [q for _, q in prev_la[: self.cfg['layer_levels']]]
                base_prev = (sum(qa)/len(qa)) or 1.0
                heavy_prev = [q for _, q in prev_la[: self.cfg['layer_levels']] if q >= self.cfg['k_big'] * base_prev]
                prev_sum_a = sum(heavy_prev) if heavy_prev else 0.0
            drop = (prev_sum_a > 0 and sum_a <= 0.5 * prev_sum_a and (snap.t - prev.t) <= self.cfg['layer_drop_ms'])
            if approaching and drop:
                rel = (prev_sum_a / max(base_a, 1.0)) if base_a else 0.0
                score = math.tanh(0.10 * rel)
                res.append(State(side='S', type='layer', score=float(score), age_ms=int(snap.t - prev.t), peak_size=float(prev_sum_a)))
        if not res:
            return None
        return max(res, key=lambda x: x.score)

    def _detect_walk(self, snap: Snapshot) -> Optional[State]:
        # walk-away: as price approaches, best price on spoof side nets outward 1–2 ticks repeatedly
        t = snap.t
        self._steps.append((t, 'B', snap.bid))
        self._steps.append((t, 'S', snap.ask))
        if len(self._steps) < 6:
            return None
        t_now = t
        window = self.cfg['walk_window_ms']
        steps_b = [p for (ts, side, p) in self._steps if side=='B' and (t_now - ts) <= window]
        steps_s = [p for (ts, side, p) in self._steps if side=='S' and (t_now - ts) <= window]
        out = None
        # crude step detection using unique direction changes
        def count_outward(seq, outward='down'):
            cnt = 0
            for i in range(1, len(seq)):
                if outward=='down' and seq[i] < seq[i-1]:
                    cnt += 1
                if outward=='up' and seq[i] > seq[i-1]:
                    cnt += 1
            return cnt
        # toward bid (price falling) and bid retreats (down ticks)
        if len(steps_b) >= 4 and (self.buf[-2].bid - snap.bid) >= 0:
            cnt = count_outward(steps_b[-6:], outward='down')
            if cnt >= self.cfg['walk_steps_need']:
                score = min(1.0, 0.2 * cnt)
                out = State(side='B', type='walk', score=score, age_ms=self.cfg['walk_window_ms'], peak_size=max(s.bq for s in self.buf[-6:]))
        # toward ask (price rising) and ask retreats (up ticks)
        if len(steps_s) >= 4 and (snap.ask - self.buf[-2].ask) >= 0:
            cnt = count_outward(steps_s[-6:], outward='up')
            sc2 = min(1.0, 0.2 * cnt)
            st2 = State(side='S', type='walk', score=sc2, age_ms=self.cfg['walk_window_ms'], peak_size=max(s.aq for s in self.buf[-6:]))
            if out is None or st2.score > out.score:
                out = st2
        return out

    def _detect_ping(self, snap: Snapshot) -> Optional[State]:
        # Best-effort: big spike then vanish (<300ms) followed quickly by opposite-side print
        s = None
        prev = self.buf[-2] if len(self.buf) >= 2 else None
        if not prev:
            return None
        dt = snap.t - prev.t
        if dt > 350:
            return None
        # side B ping: bid size big then drop, and last print was S (aggressive sell), vice-versa
        avg_b, avg_a = self._rolling_mean_sizes(self.cfg['window_ms'])
        def is_big_drop(curr, prev, avg):
            return (prev >= self.cfg['k_big'] * max(avg,1e-9)) and (curr <= 0.6 * max(avg,1e-9))
        if is_big_drop(snap.bq, prev.bq, avg_b) and (snap.last_print_side == 'S'):
            rel = (prev.bq / max(avg_b,1e-9)) if avg_b>0 else 0
            score = math.tanh(0.25 * rel)
            s = State(side='B', type='ping', score=score, age_ms=dt, peak_size=prev.bq)
        if is_big_drop(snap.aq, prev.aq, avg_a) and (snap.last_print_side == 'B'):
            rel = (prev.aq / max(avg_a,1e-9)) if avg_a>0 else 0
            score = math.tanh(0.25 * rel)
            s2 = State(side='S', type='ping', score=score, age_ms=dt, peak_size=prev.aq)
            if (s is None) or (s2.score > s.score):
                s = s2
        return s

    # --------------- helpers ---------------
    def _stars(self, score: float) -> str:
        n = max(0, min(3, int(round(score * 3))))
        return '★'*n + '☆'*(3-n)

    def _state_to_dict(self, s: Optional[State]) -> Optional[Dict]:
        if not s:
            return None
        return dict(side=s.side, type=s.type, score=float(s.score), age_ms=int(s.age_ms), peak_size=float(s.peak_size or 0))

# ----------------- App integration snippets -----------------
# Paste the following diffs into Kabus_gui_v4_1.py (approximate anchors shown):

INTEGRATION_GUIDE = r"""
(1) import & init

# top of file
from spoofing import SpoofDetector

# inside App.__init__(...) after other state inits
self.spoof_cfg = {
    'enabled': True, 'window_ms': 3000, 'buffer_points': 150, 'k_big': 3.5,
    'min_lifespan_ms': 80, 'flash_max_ms': 800, 'layer_levels': 5, 'layer_need': 3,
    'layer_drop_ms': 900, 'walk_window_ms': 1400, 'walk_steps_need': 3,
    'score_threshold': 0.70, 'suppress_weight': 0.20,
}
self.spoof = SpoofDetector(self.spoof_cfg)
self._last_spoof_str = ""

(2) UI badge
# in summary header creation, add a small label to the right
self.lbl_spoof = ttk.Label(parent_summary_right, text="見せ板: なし", width=18, anchor='e')
self.lbl_spoof.pack(side='right', padx=(6,0))

(3) book push hook
# in _on_book_push(self, raw): after you parsed best_bid/ask, best_bidq/askq and optional depth levels
levels = None
if hasattr(self, 'last_levels_b') and hasattr(self, 'last_levels_a'):
    levels = {'B': self.last_levels_b, 'S': self.last_levels_a}

state = self.spoof.update(
    ts_ms=int(raw.get('timestamp_ms') or time.time()*1000),
    best_bid=best_bid, best_ask=best_ask,
    best_bidq=best_bidq, best_askq=best_askq,
    levels=levels,
    last_trade=getattr(self, 'last_print', None),
)

# after _derive_book_metrics(...), refresh badge & log (rate-limit if you like)
spoof_str = self.spoof.format_badge(state)
if spoof_str != self._last_spoof_str:
    self._last_spoof_str = spoof_str
    self.lbl_spoof.configure(text=f"見せ板: {spoof_str}")
    if state and state.get('score',0) >= 0.66:
        self._log("AUTO", f"見せ板？{ '買' if state['side']=='B' else '売' } {state['type']} ★★★"[:8] + f"(score={state['score']:.2f}) size={state['peak_size']:.0f} age={state['age_ms']}ms")

(4) AUTO gate integration
# in your entry decision path just before sending the order
allow, new_conf, reason = self.spoof.apply_gate(proposed_side=('B' if side=='BUY' else 'S'), entry_confidence=entry_conf)
entry_conf = new_conf
if not allow:
    reason_ctx.append(reason)
    self._log("AUTO", reason)
    return  # skip entry
else:
    if reason:
        self._log("AUTO", reason)

(5) training / ML log columns
# when writing a row, extend columns once at header creation
extra_cols = ["spoof_side","spoof_type","spoof_score","spoof_age_ms","spoof_peak_size"]
# add to your CSV writer header if missing

# per-row
spoof_fields = self.spoof.get_log_fields()
row.update(spoof_fields)  # if row is dict-like
# or append in fixed order if row is list-like

(6) settings UI (tiny block near 決済設定)
frm = ttk.LabelFrame(parent_settings, text="見せ板検出")
var_en = tk.BooleanVar(value=self.spoof_cfg['enabled'])
var_thr = tk.DoubleVar(value=self.spoof_cfg['score_threshold'])
var_k   = tk.DoubleVar(value=self.spoof_cfg['k_big'])
var_win = tk.IntVar(value=self.spoof_cfg['window_ms'])

cb = ttk.Checkbutton(frm, text="有効", variable=var_en, command=lambda: self._apply_spoof_ui())
cb.grid(row=0, column=0, sticky='w', padx=4, pady=2)
ttk.Label(frm, text="閾値").grid(row=0, column=1, sticky='e'); ttk.Spinbox(frm, from_=0.5, to=0.95, increment=0.05, width=5, textvariable=var_thr).grid(row=0, column=2)
ttk.Label(frm, text="巨大K").grid(row=1, column=1, sticky='e'); ttk.Spinbox(frm, from_=2.0, to=6.0, increment=0.5, width=5, textvariable=var_k).grid(row=1, column=2)
ttk.Label(frm, text="窓ms").grid(row=1, column=3, sticky='e'); ttk.Spinbox(frm, from_=1000, to=6000, increment=250, width=7, textvariable=var_win).grid(row=1, column=4)
frm.pack(fill='x', padx=6, pady=4)

# handler
def _apply_spoof_ui(self):
    self.spoof_cfg['enabled'] = var_en.get()
    self.spoof_cfg['score_threshold'] = float(var_thr.get())
    self.spoof_cfg['k_big'] = float(var_k.get())
    self.spoof_cfg['window_ms'] = int(var_win.get())
    self.spoof.update_config(**self.spoof_cfg)
    self._log('CFG', f"spoof cfg updated: thr={self.spoof_cfg['score_threshold']} K={self.spoof_cfg['k_big']} win={self.spoof_cfg['window_ms']}ms en={self.spoof_cfg['enabled']}")

(7) optional: dev simulator (bind to hidden menu)
# quick synthetic patterns to validate logs
from threading import Thread

def _dev_spoof_sim(self, kind='flash', side='B'):
    import random, time
    # requires your feed to accept manual pushes; else directly call detector.update
    t0 = int(time.time()*1000)
    bid, ask = getattr(self,'best_bid',1000.0), getattr(self,'best_ask',1000.5)
    bq, aq = 100.0, 100.0
    for i in range(50):
        t = t0 + i*40
        if kind=='flash' and i==10:
            if side=='B': bq *= 5
            else: aq *= 5
        if kind=='flash' and i==25:
            if side=='B': bq = 80.0
            else: aq = 80.0
        if kind=='walk' and i in (10,18,26):
            if side=='S': ask += 0.1
            else: bid -= 0.1
        if kind=='layer' and 8<=i<=14:
            # emulate stacked levels then vanish
            levels={'B':[ (bid - 0.1*k, 400.0) for k in range(5)], 'S':[ (ask + 0.1*k, 80.0) for k in range(5)] } if side=='B' else \
                   {'B':[ (bid - 0.1*k, 80.0) for k in range(5)], 'S':[ (ask + 0.1*k, 400.0) for k in range(5)] }
        else:
            levels=None
        st = self.spoof.update(t, bid, ask, bq, aq, levels=levels)
        self.lbl_spoof.configure(text=f"見せ板: {self.spoof.format_badge(st)}")
        time.sleep(0.04)
    self._log('DEV', f"simulated {kind}/{side}")

# add a dev menu item to trigger

"""
