# --------------------------------------------------
# 互換メソッド（V3名）自動抽出スタブ
# ※ 既存実装がある場合は重複させないでください
# ※ 引数は握りつぶして互換性確保のため (*args, **kwargs) に統一しています
# --------------------------------------------------

class CompatStubsMixin:

    # --- logging ---
    def _log(self, *args, **kwargs):
        pass

    def _log_exc(self, *args, **kwargs):
        pass

    def start_training_log(self, *args, **kwargs):
        pass

    def stop_training_log(self, *args, **kwargs):
        pass

    def _log_training_row(self, *args, **kwargs):
        pass


    # --- ui_build ---
    def _update_limits_from_symbol(self, *args, **kwargs):
        pass

    def _update_special_from_board(self, *args, **kwargs):
        pass

    def ui_after(self, *args, **kwargs):
        pass

    def ui_call(self, *args, **kwargs):
        pass

    def _define_presets(self, *args, **kwargs):
        pass

    def apply_preset(self, *args, **kwargs):
        pass

    def _get_tree(self, *args, **kwargs):
        pass

    def _get_sim_tree(self, *args, **kwargs):
        pass

    def _update_stats_from_tree(self, *args, **kwargs):
        pass

    def _update_sim_stats_from_tree(self, *args, **kwargs):
        pass

    def _build_history_panel(self, *args, **kwargs):
        pass

    def _build_ui(self, *args, **kwargs):
        pass

    def _layout(self, *args, **kwargs):
        pass

    def _build_preset_menu(self, *args, **kwargs):
        pass

    def _update_sim_labels(self, *args, **kwargs):
        pass

    def _update_metrics_ui(self, *args, **kwargs):
        pass

    def _update_sim_stats_label(self, *args, **kwargs):
        pass

    def _update_simpos(self, *args, **kwargs):
        pass

    def _update_price_bar(self, *args, **kwargs):
        pass

    def _update_bars_and_indicators(self, *args, **kwargs):
        pass

    def _update_summary(self, *args, **kwargs):
        pass

    def _update_dom_tables(self, *args, **kwargs):
        pass

    def _update_summary_title(self, *args, **kwargs):
        pass

    def _update_simpos_summary(self, *args, **kwargs):
        pass


    # --- http_ws ---
    def _base_url(self, *args, **kwargs):
        pass

    def _ws_url(self, *args, **kwargs):
        pass

    def _set_ws_state(self, *args, **kwargs):
        pass

    def _rows_from_tree(self, *args, **kwargs):
        pass

    def _sim_rows_from_tree(self, *args, **kwargs):
        pass

    def _register_symbol_safe(self, *args, **kwargs):
        pass

    def _http_get(self, *args, **kwargs):
        pass

    def _snapshot_combo(self, *args, **kwargs):
        pass

    def _snapshot_board(self, *args, **kwargs):
        pass

    def _snapshot_symbol_once(self, *args, **kwargs):
        pass

    def _connect_ws(self, *args, **kwargs):
        pass

    def _ws_watchdog_loop(self, *args, **kwargs):
        pass


    # --- ws_health ---
    def _start_http_fallback(self, *args, **kwargs):
        pass

    def _stop_http_fallback(self, *args, **kwargs):
        pass


    # --- symbol_state ---
    def _reset_symbol_state(self, *args, **kwargs):
        pass

    def _push_symbol(self, *args, **kwargs):
        pass

    def _get_current_symbol(self, *args, **kwargs):
        pass

    def _get_symbol_name(self, *args, **kwargs):
        pass

    def _resolve_symbol_name(self, *args, **kwargs):
        pass


    # --- sim ---
    def export_sim_history_csv(self, *args, **kwargs):
        pass

    def export_sim_history_xlsx(self, *args, **kwargs):
        pass

    def _sim_close_market(self, *args, **kwargs):
        pass

    def _ensure_sim_history(self, *args, **kwargs):
        pass

    def _record_sim_trade(self, *args, **kwargs):
        pass

    def _simpos_text(self, *args, **kwargs):
        pass

    def _append_sim_history(self, *args, **kwargs):
        pass

    def _sim_open(self, *args, **kwargs):
        pass

    def _sim_on_tick(self, *args, **kwargs):
        pass

    def _sim_close(self, *args, **kwargs):
        pass

    def _sim_enter(self, *args, **kwargs):
        pass

    def _sim_flatten(self, *args, **kwargs):
        pass


    # --- live ---
    def _append_live_history(self, *args, **kwargs):
        pass

    def update_live_history(self, *args, **kwargs):
        pass

    def save_live_csv(self, *args, **kwargs):
        pass

    def save_live_xlsx(self, *args, **kwargs):
        pass


    # --- orders ---
    def sweep_orphan_close_orders(self, *args, **kwargs):
        pass

    def _order_mode_params(self, *args, **kwargs):
        pass

    def _send_entry_order(self, *args, **kwargs):
        pass

    def update_orders(self, *args, **kwargs):
        pass

    def _fill_orders(self, *args, **kwargs):
        pass


    # --- positions ---
    def update_positions(self, *args, **kwargs):
        pass

    def _fill_positions(self, *args, **kwargs):
        pass


    # --- screener ---
    def _show_preset_menu(self, *args, **kwargs):
        pass

    def _open_preset_tuner(self, *args, **kwargs):
        pass

    def update_preset_names(self, *args, **kwargs):
        pass

    def start_scan(self, *args, **kwargs):
        pass

    def stop_scan(self, *args, **kwargs):
        pass

    def _fill_scan(self, *args, **kwargs):
        pass

    def set_main_from_scan_selection(self, *args, **kwargs):
        pass


    # --- ml ---
    def _on_ml_toggle(self, *args, **kwargs):
        pass


    # --- export ---
    def export_history_csv(self, *args, **kwargs):
        pass

    def export_history_xlsx(self, *args, **kwargs):
        pass


    # --- stats ---
    def _stats_heartbeat(self, *args, **kwargs):
        pass

    def _recalc_sim_stats_from_tree(self, *args, **kwargs):
        pass

    def toggle_chart_window(self, *args, **kwargs):
        pass


    # --- chart ---
    def _draw_chart_if_open(self, *args, **kwargs):
        pass


    # --- risk ---
    def _ensure_peak_state_vars(self, *args, **kwargs):
        pass

    def _guard_peak_and_limits(self, *args, **kwargs):
        pass

    def _wire_send_entry_guard(self, *args, **kwargs):
        pass

    def _arm_real_trade_prompt(self, *args, **kwargs):
        pass

    def _disarm_real_trade(self, *args, **kwargs):
        pass

    def _peak_guard(self, *args, **kwargs):
        pass


    # --- helpers ---
    def _pick(self, *args, **kwargs):
        pass

    def _is_real_trade_armed(self, *args, **kwargs):
        pass

    def _to_float(self, *args, **kwargs):
        pass

    def _nowstr_full(self, *args, **kwargs):
        pass

    def _ensure_real_trade_armed(self, *args, **kwargs):
        pass

    def _init_context_menu(self, *args, **kwargs):
        pass

    def _get_token(self, *args, **kwargs):
        pass

    def _trace(self, *args, **kwargs):
        pass

    def _emit_trace(self, *args, **kwargs):
        pass

    def _auto_decision_once(self, *args, **kwargs):
        pass

    def _sync_auto_cached(self, *args, **kwargs):
        pass

    def _normalize_code(self, *args, **kwargs):
        pass

    def _codes_match(self, *args, **kwargs):
        pass

    def _derive_book_metrics(self, *args, **kwargs):
        pass

    def _on_debug_toggle(self, *args, **kwargs):
        pass

    def _debug_auto(self, *args, **kwargs):
        pass

    def _recalc_top_metrics_and_update(self, *args, **kwargs):
        pass

    def _set_summary_title(self, *args, **kwargs):
        pass

    def _refresh_summary_title(self, *args, **kwargs):
        pass

    def _set_summary_price(self, *args, **kwargs):
        pass

    def _refresh_summary_price(self, *args, **kwargs):
        pass

    def _loop(self, *args, **kwargs):
        pass

    def _handle_push(self, *args, **kwargs):
        pass

    def _set_best_quote(self, *args, **kwargs):
        pass

    def _apply_startup_options(self, *args, **kwargs):
        pass

    def _boot_seq(self, *args, **kwargs):
        pass

    def _infer_tick_size(self, *args, **kwargs):
        pass

    def _sma(self, *args, **kwargs):
        pass

    def _ema_series(self, *args, **kwargs):
        pass

    def _macd(self, *args, **kwargs):
        pass

    def _rsi(self, *args, **kwargs):
        pass

    def _append_tape(self, *args, **kwargs):
        pass

    def _normalize_sym(self, *args, **kwargs):
        pass

    def _append_history(self, *args, **kwargs):
        pass

    def _wire_history_scrollbar(self, *args, **kwargs):
        pass

    def place_server_bracket(self, *args, **kwargs):
        pass

    def arm_after_fill(self, *args, **kwargs):
        pass

    def self_check(self, *args, **kwargs):
        pass

    def toggle_auto(self, *args, **kwargs):
        pass

    def _recent_momentum(self, *args, **kwargs):
        pass

    def _microprice(self, *args, **kwargs):
        pass

    def _filters_ok(self, *args, **kwargs):
        pass

    def _auto_loop(self, *args, **kwargs):
        pass

    def reset_sim(self, *args, **kwargs):
        pass

    def _round_tick(self, *args, **kwargs):
        pass

    def refresh_hist_table(self, *args, **kwargs):
        pass

    def _clamp_price_for_side(self, *args, **kwargs):
        pass

    def write_training_row(self, *args, **kwargs):
        pass

    def update_wallets(self, *args, **kwargs):
        pass

    def save_hist_csv(self, *args, **kwargs):
        pass

    def save_hist_xlsx(self, *args, **kwargs):
        pass

    def _open_help(self, *args, **kwargs):
        pass

    def _fmt_ticks(self, *args, **kwargs):
        pass

    def _help_text_ja(self, *args, **kwargs):
        pass

    def _help_text_en(self, *args, **kwargs):
        pass

    def _on_close(self, *args, **kwargs):
        pass
