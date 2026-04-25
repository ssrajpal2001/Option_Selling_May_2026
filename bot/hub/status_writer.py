import json
import os
import time
import threading
from pathlib import Path
from utils.logger import logger


import queue

class StatusWriter:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.last_write_ts = 0
        self.write_interval = 5

        # PERFORMANCE: Dedicated IO queue and worker to avoid asyncio stalls or thread explosion
        self._io_queue = queue.Queue()
        self._running = True
        self._worker = threading.Thread(target=self._io_worker, daemon=True, name=f"StatusIO_{orchestrator.instrument_name}")
        self._worker.start()

        client_id = os.environ.get('CLIENT_ID')
        if os.environ.get('UI_BACKTEST_MODE') == 'True':
            self.status_path = Path(f"config/backtest_status_ui_{client_id}.json") if client_id else Path("config/backtest_status_ui.json")
            self.legacy_status_path = self.status_path
        elif client_id:
            # Multi-instrument support: write one file per instrument
            self.status_path = Path(f'config/bot_status_client_{client_id}_{orchestrator.instrument_name}.json')
            # For backward compatibility and single-instrument UI, also write the main file
            self.legacy_status_path = Path(f'config/bot_status_client_{client_id}.json')
        else:
            self.status_path = Path(f'config/bot_status_{orchestrator.instrument_name}.json')
            self.legacy_status_path = Path('config/bot_status.json')
        self.last_error = None

    def record_error(self, error_msg):
        self.last_error = {"message": str(error_msg), "ts": time.time()}

    def maybe_write(self, timestamp, current_atm, force=False):
        now = time.monotonic()
        is_ui_bt = os.environ.get('UI_BACKTEST_MODE') == 'True'

        # PERFORMANCE: Enforce a minimum wall-clock throttle (100ms) for UI Backtests
        # to prevent overloading the frontend/disk during fast simulation loops.
        throttle = 0.1 if is_ui_bt else self.write_interval

        if not force and (now - self.last_write_ts) < throttle:
            return
        self.last_write_ts = now

        # Push to background worker
        # Use put_nowait to ensure the main loop NEVER blocks on status updates
        try:
            self._io_queue.put_nowait((timestamp, current_atm))
        except queue.Full:
            pass # Skip update if IO is backed up

    def _io_worker(self):
        """Single background worker thread for all Status I/O."""
        while self._running:
            try:
                item = self._io_queue.get(timeout=1.0)
                if not item: continue

                timestamp, current_atm = item
                try:
                    self._write(timestamp, current_atm)
                except Exception as e:
                    logger.warning(f"[StatusWriter] Failed to write status: {e}")

                self._io_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"[StatusWriter] IO Worker Error: {e}")

    def _write(self, timestamp, current_atm):
        """Internal write method with strict initialization to prevent UnboundLocalError."""
        orch = self.orchestrator

        # --- 1. Top-level variable initialization ---
        trade_history = []
        rsi = roc = slope = None
        m_rsi = m_vwap = g_roc = None
        g_roc_tf = 15
        updated_at = str(timestamp)

        # Check Trading Toggle Status for UI
        trading_active = True
        client_id = os.environ.get('CLIENT_ID')
        broker_name = os.environ.get('CLIENT_BROKER', 'zerodha')
        funds = 0.0
        broker_positions = []

        if client_id:
            toggle_file = Path(f'config/trading_enabled_{client_id}.json')
            if toggle_file.exists():
                try:
                    with open(toggle_file, 'r') as f:
                        toggle_data = json.load(f)
                        trading_active = toggle_data.get('enabled', True)
                except: pass

            # Fetch Funds & Positions from Broker
            if not orch.is_backtest:
                # We use the first loaded broker for this client
                primary_broker = next(iter(orch.broker_manager.brokers.values()), None)
                if primary_broker:
                    # These are async calls, but _write is called from a thread worker
                    # We can use asyncio.run or create a new loop if needed,
                    # but easiest is to use a thread-safe approach.
                    import asyncio
                    try:
                        loop = asyncio.new_event_loop()
                        funds = loop.run_until_complete(primary_broker.get_funds())
                        broker_positions = loop.run_until_complete(primary_broker.get_positions())
                        loop.close()
                    except Exception as e:
                        logger.debug(f"Failed to fetch live broker data: {e}")

        sm = orch.state_manager
        sell_mgr = getattr(orch, 'sell_manager', None)
        oi_mon = getattr(orch, 'oi_exit_monitor', None)

        # Populate trade history from log if available
        if getattr(orch, 'trade_log', None):
            try:
                trade_history = orch.trade_log.to_list(50)
            except Exception as e:
                logger.debug(f"[StatusWriter] Trade log retrieval failed: {e}")
                trade_history = []

        # Convert timestamp to str/isoformat safely
        if hasattr(timestamp, 'isoformat'):
            updated_at = timestamp.isoformat()
        elif hasattr(timestamp, 'strftime'):
            updated_at = timestamp.strftime('%Y-%m-%dT%H:%M:%S')

        buy_data = {}
        for side in ['CALL', 'PUT']:
            pos_data = {"status": "IDLE"}
            for session in orch.user_sessions.values():
                pos = (session.state_manager.call_position if side == 'CALL'
                       else session.state_manager.put_position)
                if pos:
                    inst_key = pos.get('instrument_key')
                    ltp = (session.state_manager.option_prices.get(inst_key, 0)
                           if inst_key else 0)
                    entry = pos.get('entry_price', 0) or 0
                    qty = pos.get('quantity', 1) or 1
                    qty_mult = pos.get('quantity_multiplier', 1) or 1
                    lot_size = getattr(sm, 'lot_size', 1) or 1
                    total_qty = qty * qty_mult * lot_size
                    entry_type = pos.get('entry_type', 'BUY')
                    if entry_type == 'SELL':
                        pnl = (entry - ltp) * total_qty
                    else:
                        pnl = (ltp - entry) * total_qty
                    pos_data = {
                        "status": "ACTIVE",
                        "strike": pos.get('strike_price'),
                        "entry": round(float(entry), 2),
                        "ltp": round(float(ltp), 2),
                        "pnl": round(float(pnl), 2),
                        "symbol": pos.get('instrument_symbol', ''),
                        "direction": "CE" if side == 'CALL' else "PE",
                        "entry_type": entry_type
                    }
                    break
            buy_data[side] = pos_data

        sell_lot_size = getattr(sm, 'lot_size', 1) or 1
        try:
            ref_broker = next(iter(orch.broker_manager.brokers.values()), None)
            sell_broker_qty = (ref_broker.config_manager.get_int(
                ref_broker.instance_name, 'quantity', 1) if ref_broker else 1)
        except Exception:
            sell_broker_qty = 1
        sell_total_qty = sell_lot_size * sell_broker_qty

        sell_data = {}
        from hub.sell_manager_v3 import SellManagerV3
        if isinstance(sell_mgr, SellManagerV3):
            # Resolve global lot multiplier from config
            try:
                ref_broker = next(iter(orch.broker_manager.brokers.values()), None)
                v3_qty_mult = (ref_broker.config_manager.get_int(
                    ref_broker.instance_name, 'quantity', 1) if ref_broker else 1)
            except Exception:
                v3_qty_mult = 1

            for side in ['CE', 'PE']:
                trade = sell_mgr.active_trades.get(side)
                if trade:
                    ltp = sm.option_prices.get(trade['key'], 0)
                    # PnL = (Entry - LTP) * LotSize * Multiplier
                    e_time = trade.get('entry_time')
                    if hasattr(e_time, 'strftime'):
                        e_time_str = e_time.strftime('%H:%M:%S')
                    else:
                        # Handle case where it was loaded as string from state JSON
                        e_time_str = str(e_time).split(' ')[-1].split('.')[0] if e_time else None

                    sell_data[side] = {
                        "placed": True,
                        "strike": trade['strike'],
                        "entry": round(float(trade['entry_price']), 2),
                        "ltp": round(float(ltp), 2),
                        "pnl": round((float(trade['entry_price']) - float(ltp)) * trade['lot_size'] * v3_qty_mult, 2),
                        "qty": int(trade['lot_size'] * v3_qty_mult),
                        "entry_time": e_time_str,
                        "entry_index": trade.get('entry_index_price'),
                    }
                else:
                    sell_data[side] = {"placed": False}

            # V3 Dashboard Extras
            v3_extras = getattr(sell_mgr, 'v3_dashboard_data', {})
            sell_data['v3_extras'] = v3_extras

            # Map V3 extras to top-level stats for the UI
            sell_data['stats'] = {
                "rsi": v3_extras.get('combined_rsi'),
                "roc": v3_extras.get('combined_roc'),
                "slope": v3_extras.get('slope_status'),
                "price": v3_extras.get('combined_price'),
                "vwap": v3_extras.get('combined_vwap'),
                "ce_strike": v3_extras.get('ce_strike'),
                "pe_strike": v3_extras.get('pe_strike'),
                "entry_reason": v3_extras.get('entry_reason', 'SCANNING')
            }
        else:
            for side in ['CE', 'PE']:
                placed = getattr(sell_mgr, f"{'ce' if side == 'CE' else 'pe'}_placed", False)
                if sell_mgr and placed:
                    inst_key = getattr(sell_mgr, f'sell_{side.lower()}_key', None)
                    entry = getattr(sell_mgr, f'sell_{side.lower()}_entry_ltp', None) or 0
                    ltp = sm.option_prices.get(inst_key, 0) if inst_key else 0
                    strike = getattr(sell_mgr, f'sell_{side.lower()}_strike', None)
                    sell_data[side] = {
                        "placed": True,
                        "strike": strike,
                        "entry": round(float(entry), 2),
                        "ltp": round(float(ltp), 2),
                        "pnl": round((float(entry) - float(ltp)) * sell_total_qty, 2),
                    }
                else:
                    sell_data[side] = {"placed": False}

        oi_snap = {}
        if oi_mon:
            oi_snap = {
                "call_oi": getattr(oi_mon, 'prev_call_oi', 0) or 0,
                "put_oi": getattr(oi_mon, 'prev_put_oi', 0) or 0,
            }

        session_pnl = 0.0
        trade_count = 0
        is_bt = getattr(orch, 'is_backtest', False)

        if is_bt and getattr(orch, 'pnl_tracker', None):
            # In Backtest, pnl_tracker is the single source of truth for realized PnL
            for t in orch.pnl_tracker.trade_history:
                session_pnl += float(t.get('pnl', 0) or 0)
            trade_count = len(orch.pnl_tracker.trade_history)
        else:
            for session in orch.user_sessions.values():
                session_pnl += float(getattr(session.state_manager, 'total_pnl', 0) or 0)
                trade_count += int(getattr(session.state_manager, 'trade_count', 0) or 0)

        # Resolve metrics for active backtest trade enrichment
        v3_extras = sell_data.get('v3_extras', {})

        rsi = v3_extras.get('combined_rsi')
        roc = v3_extras.get('combined_roc')
        slope = v3_extras.get('slope_status')
        entry_tf = v3_extras.get('entry_tf', 1)

        # Exit Metrics
        m_rsi = v3_extras.get('macro_rsi')
        m_vwap = "FAIL" if v3_extras.get('macro_vwap_fail') else "OK"
        g_roc = v3_extras.get('guardrail_roc')
        g_roc_tf = sell_mgr._v3_cfg('guardrail_roc.tf', 15, int) if isinstance(sell_mgr, SellManagerV3) else 15

        # Indicators for Enrichment
        entry_details = v3_extras.get('entry_details', [])
        exit_details = v3_extras.get('exit_details', [])

        if entry_details:
            cur_entry_ind = " | ".join([f"{d['label']}:{d['val']}" for d in entry_details])
        else:
            rsi_s = f"{rsi:.1f}" if rsi is not None else "--"
            roc_s = f"{roc:.2f}" if roc is not None else "--"
            cur_entry_ind = f"RSI:{rsi_s}, ROC:{roc_s}, Slope:{slope or '--'}"

        if exit_details:
            cur_exit_ind = " | ".join([f"{d['label']}:{d['val']}" for d in exit_details])
        else:
            m_rsi_s = f"{m_rsi:.1f}" if m_rsi is not None else "--"
            g_roc_s = f"{g_roc:.2f}" if g_roc is not None else "--"
            cur_exit_ind = f"RSI:{m_rsi_s}, VWAP:{m_vwap}, ROC({g_roc_tf}m):{g_roc_s}"

        # Add open PnL
        for side, pos_info in buy_data.items():
            if pos_info.get('status') == 'ACTIVE':
                session_pnl += float(pos_info.get('pnl', 0) or 0)
                # In backtest UI or live mode, we want to see the active trade in the order book for better visibility
                e_type = pos_info.get('entry_type', 'BUY')
                pnl_pts = (pos_info.get('ltp') - pos_info.get('entry')) if e_type == 'BUY' else (pos_info.get('entry') - pos_info.get('ltp'))

                trade_history.insert(0, {
                    'time': updated_at.split('T')[-1][:8],
                    'type': pos_info.get('entry_type', 'BUY'),
                    'direction': pos_info.get('direction'),
                    'strike': pos_info.get('strike'),
                    'index_price': getattr(sm, 'index_price', None),
                    'entry_price': pos_info.get('entry'),
                    'exit_price': pos_info.get('ltp'),
                    'pnl_pts': round(pnl_pts, 2),
                    'pnl_rs': round(float(pos_info.get('pnl', 0)), 2),
                    'reason': "RUNNING",
                    'entry_indicators': cur_entry_ind,
                    'exit_indicators': cur_exit_ind
                })

        for side, s_info in sell_data.items():
            if s_info.get('placed'):
                session_pnl += float(s_info.get('pnl', 0) or 0)
                # In backtest UI or live mode, we want to see the active trade in the order book for better visibility
                pnl_pts = s_info.get('entry') - s_info.get('ltp')
                entry_reason = v3_extras.get('entry_reason', 'SCANNING')

                # Check for exit rule reversal status to show in order book
                exit_rule_status = v3_extras.get('exit_rule_status', 'OK')
                status_label = "REVERSING" if exit_rule_status != "OK" else "RUNNING"

                trade_history.insert(0, {
                    'time': s_info.get('entry_time', '--'),
                    'type': 'SELL',
                    'direction': side,
                    'strike': s_info.get('strike'),
                    'index_price': s_info.get('entry_index'),
                    'entry_price': s_info.get('entry'),
                    'exit_price': s_info.get('ltp'),
                    'pnl_pts': round(pnl_pts, 2),
                    'pnl_rs': round(float(s_info.get('pnl', 0)), 2),
                    'reason': f"{status_label} | {entry_reason}",
                    'entry_indicators': cur_entry_ind,
                    'exit_indicators': cur_exit_ind
                })

        cfg = getattr(orch, 'config_manager', None)
        mode = cfg.get('settings', 'trading_mode', fallback='live') if cfg else 'live'

        log_lines = []
        try:
            client_id = os.environ.get('CLIENT_ID')
            client_broker = os.environ.get('CLIENT_BROKER', 'zerodha')
            if client_id:
                log_file = f'logs/client_{client_id}_{client_broker}.log'
            else:
                log_file = cfg.get('app', 'log_file', fallback='bot.log') if cfg else 'bot.log'

            if os.path.exists(log_file):
                # Efficiently read only the last few lines to avoid loading large log files
                import collections
                with open(log_file, 'rb') as f:
                    try:
                        f.seek(0, os.SEEK_END)
                        size = f.tell()
                        # Start by reading the last 16KB of the file
                        offset = min(size, 16384)
                        f.seek(size - offset)
                        content = f.read(offset).decode('utf-8', errors='ignore')
                        log_lines = content.splitlines()[-50:]
                    except Exception:
                        # Fallback for small or unseekable files
                        f.seek(0)
                        log_lines = [l.decode('utf-8', errors='ignore').rstrip() for l in f.readlines()[-50:]]
        except Exception as e:
            logger.warning(f"[StatusWriter] Failed to read log tail: {e}")

        status = {
            "updated_at": updated_at,
            "heartbeat": time.time(),
            "pid": os.getpid(),
            "bot_running": True,
            "trading_active": trading_active,
            "broker_name": broker_name,
            "funds": funds,
            "broker_positions": broker_positions,
            "instrument": orch.instrument_name,
            "atm": current_atm,
            "spot_price": getattr(sm, 'spot_price', None),
            "index_price": getattr(sm, 'index_price', None),
            "mode": mode.upper(),
            "buy": buy_data,
            "sell": sell_data,
            "oi_snapshot": oi_snap,
            "session_pnl": round(session_pnl, 2),
            "trade_count": trade_count,
            "last_error": self.last_error,
            "log_tail": log_lines,
            "trade_history": trade_history,
        }

        try:
            # Ensure config directory exists
            os.makedirs(self.status_path.parent, exist_ok=True)

            tmp = self.status_path.with_suffix('.tmp')
            with open(tmp, 'w') as f:
                json.dump(status, f, default=str, indent=2)
            os.replace(tmp, self.status_path)
        except Exception as e:
            logger.warning(f"[StatusWriter] Failed to write status file {self.status_path}: {e}")

        # Sync to legacy path if this is the only or primary instrument
        try:
            import shutil
            # Ensure we don't copy backtest status to production status files unless in UI Backtest mode
            if os.environ.get('UI_BACKTEST_MODE') == 'True' or 'backtest' not in str(self.status_path):
                 shutil.copy2(self.status_path, self.legacy_status_path)
        except: pass
