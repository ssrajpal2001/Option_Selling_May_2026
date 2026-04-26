import sys
import os
import pandas as pd
import numpy as np

# Add the current directory to path to allow importing rust_core if it's in hub/sell_v3
curr_dir = os.path.dirname(os.path.abspath(__file__))
if curr_dir not in sys.path:
    sys.path.append(curr_dir)

try:
    import rust_core
    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False

from utils.logger import logger

if not RUST_AVAILABLE:
    try:
        from utils.notifier import notify_rust_fallback
        notify_rust_fallback()
    except Exception as _e:
        logger.warning(f"[RustBridge] Could not send Rust fallback Telegram alert: {_e}")

class RustBridge:
    """
    ULTRA-LOW LATENCY BRIDGE:
    Routes technical indicator calculations and boolean logic to COMPILED RUST code.
    If the Rust binary is missing, it transparently falls back to optimized Python.
    """

    @staticmethod
    def calculate_rsi(series, period):
        if RUST_AVAILABLE:
            try:
                res = rust_core.calculate_rsi(series.tolist(), int(period))
                if res is not None: return res
            except Exception as e:
                logger.debug(f"Rust RSI execution error: {e}")

        from hub.indicators.rsi import RSIIndicator
        return RSIIndicator.get_latest_value(series, period)

    @staticmethod
    def calculate_vwap(df):
        if RUST_AVAILABLE:
            try:
                res = rust_core.calculate_vwap(
                    df['high'].astype(float).tolist(),
                    df['low'].astype(float).tolist(),
                    df['close'].astype(float).tolist(),
                    df['volume'].astype(float).tolist()
                )
                if res is not None: return res
            except Exception as e:
                logger.debug(f"Rust VWAP execution error: {e}")

        from hub.indicators.vwap import VWAPIndicator
        return VWAPIndicator.get_latest_value(df)

    @staticmethod
    def calculate_roc(series, length):
        if RUST_AVAILABLE:
            try:
                res = rust_core.calculate_roc(series.tolist(), int(length))
                if res is not None: return res
            except Exception as e:
                logger.debug(f"Rust ROC execution error: {e}")

        from hub.indicators.roc import ROCIndicator
        return ROCIndicator.get_latest_value(series, length)

    @staticmethod
    def calculate_combined_slope(v1_curr, v1_prev, v2_curr, v2_prev):
        if RUST_AVAILABLE:
            try:
                return rust_core.calculate_combined_slope_rust(float(v1_curr), float(v1_prev), float(v2_curr), float(v2_prev))
            except Exception as e:
                logger.debug(f"Rust Slope calculation failed: {e}")

        return (v1_curr + v2_curr) - (v1_prev + v2_prev)

    @staticmethod
    def evaluate_boolean_logic(tokens):
        """
        Evaluates complex boolean expressions (with parentheses) in Rust.
        tokens: List of strings e.g. ["(", "True", "and", "False", ")"]
        """
        if RUST_AVAILABLE:
            try:
                # tokens must be a list of strings
                return rust_core.evaluate_boolean_expression_rust([str(t) for t in tokens if t])
            except Exception as e:
                logger.debug(f"Rust Boolean Eval error: {e}")

        # Fallback to Python Boolean Evaluator
        return RustBridge._python_boolean_eval(tokens)

    @staticmethod
    def _python_boolean_eval(tokens):
        stack = []
        output = []
        precedence = {'or': 1, 'and': 2}
        for token in tokens:
            t_lower = token.lower()
            if t_lower in ('true', 'false'):
                output.append(t_lower == 'true')
            elif t_lower in precedence:
                while stack and stack[-1] != '(' and precedence.get(stack[-1], 0) >= precedence[t_lower]:
                    output.append(stack.pop())
                stack.append(t_lower)
            elif token == '(': stack.append(token)
            elif token == ')':
                while stack and stack[-1] != '(': output.append(stack.pop())
                if stack: stack.pop()
        while stack: output.append(stack.pop())
        eval_stack = []
        for t in output:
            if isinstance(t, bool):
                eval_stack.append(t)
            elif t == 'and':
                if len(eval_stack) >= 2:
                    b = eval_stack.pop()
                    a = eval_stack.pop()
                    eval_stack.append(a and b)
                else:
                    return False
            elif t == 'or':
                if len(eval_stack) >= 2:
                    b = eval_stack.pop()
                    a = eval_stack.pop()
                    eval_stack.append(a or b)
                else:
                    return False
        return eval_stack[0] if eval_stack else False
