use pyo3::prelude::*;

/// High-performance RSI (Relative Strength Index) calculation for trading.
/// Uses Wilder's Smoothing (EMA with alpha = 1/period).
#[pyfunction]
pub fn calculate_rsi(prices: Vec<f64>, period: usize) -> Option<f64> {
    if prices.len() <= period { return None; }
    let mut gains = Vec::with_capacity(prices.len() - 1);
    let mut losses = Vec::with_capacity(prices.len() - 1);
    for i in 1..prices.len() {
        let delta = prices[i] - prices[i - 1];
        if delta > 0.0 { gains.push(delta); losses.push(0.0); }
        else { gains.push(0.0); losses.push(-delta); }
    }
    let mut avg_gain: f64 = gains[0..period].iter().sum::<f64>() / period as f64;
    let mut avg_loss: f64 = losses[0..period].iter().sum::<f64>() / period as f64;
    let alpha = 1.0 / period as f64;
    for i in period..gains.len() {
        avg_gain = avg_gain * (1.0 - alpha) + gains[i] * alpha;
        avg_loss = avg_loss * (1.0 - alpha) + losses[i] * alpha;
    }
    if avg_loss == 0.0 { return Some(100.0); }
    let rs = avg_gain / avg_loss;
    Some(100.0 - (100.0 / (1.0 + rs)))
}

/// High-performance VWAP calculation.
#[pyfunction]
pub fn calculate_vwap(highs: Vec<f64>, lows: Vec<f64>, closes: Vec<f64>, volumes: Vec<f64>) -> Option<f64> {
    if highs.is_empty() || highs.len() != lows.len() || highs.len() != closes.len() || highs.len() != volumes.len() {
        return None;
    }
    let mut cumulative_pv = 0.0;
    let mut cumulative_vol = 0.0;
    for i in 0..highs.len() {
        let typical_price = (highs[i] + lows[i] + closes[i]) / 3.0;
        cumulative_pv += typical_price * volumes[i];
        cumulative_vol += volumes[i];
    }
    if cumulative_vol == 0.0 { return None; }
    Some(cumulative_pv / cumulative_vol)
}

/// High-performance ROC (Rate of Change) calculation.
#[pyfunction]
pub fn calculate_roc(prices: Vec<f64>, length: usize) -> Option<f64> {
    if prices.len() <= length { return None; }
    let current = prices[prices.len() - 1];
    let past = prices[prices.len() - 1 - length];
    if past == 0.0 { return None; }
    Some(100.0 * (current - past) / past)
}

/// High-performance Combined Slope calculation for 2 legs (e.g. Straddle).
/// Computes (V_curr - V_prev) for the sum of two instrument series.
#[pyfunction]
pub fn calculate_combined_slope_rust(
    v1_curr: f64, v1_prev: f64,
    v2_curr: f64, v2_prev: f64
) -> f64 {
    (v1_curr + v2_curr) - (v1_prev + v2_prev)
}

/// High-performance Strategy Rule Engine (Shunting-Yard Parser).
/// logic_tokens: Pre-processed boolean strings from Python (e.g. ["(", "True", "or", "False", ")"])
#[pyfunction]
pub fn evaluate_boolean_expression_rust(tokens: Vec<String>) -> PyResult<bool> {
    if tokens.is_empty() { return Ok(false); }
    let mut output_queue: Vec<String> = Vec::new();
    let mut operator_stack: Vec<String> = Vec::new();
    let precedence = |op: &str| match op {
        "or" => 1, "and" => 2, _ => 0,
    };
    for token in tokens {
        let t = token.to_lowercase();
        match t.as_str() {
            "true" | "false" => output_queue.push(t),
            "and" | "or" => {
                while let Some(top) = operator_stack.last() {
                    if top != "(" && precedence(top) >= precedence(&t) {
                        output_queue.push(operator_stack.pop().unwrap());
                    } else { break; }
                }
                operator_stack.push(t);
            },
            "(" => operator_stack.push(t),
            ")" => {
                while let Some(top) = operator_stack.pop() {
                    if top == "(" { break; }
                    output_queue.push(top);
                }
            },
            _ => {}
        }
    }
    while let Some(op) = operator_stack.pop() { output_queue.push(op); }
    let mut eval_stack: Vec<bool> = Vec::new();
    for token in output_queue {
        match token.as_str() {
            "true" => eval_stack.push(true),
            "false" => eval_stack.push(false),
            "and" => {
                let b = eval_stack.pop().unwrap_or(false);
                let a = eval_stack.pop().unwrap_or(false);
                eval_stack.push(a && b);
            },
            "or" => {
                let b = eval_stack.pop().unwrap_or(false);
                let a = eval_stack.pop().unwrap_or(false);
                eval_stack.push(a || b);
            },
            _ => {}
        }
    }
    Ok(eval_stack.pop().unwrap_or(false))
}

#[pymodule]
fn rust_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(calculate_rsi, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_vwap, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_roc, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_combined_slope_rust, m)?)?;
    m.add_function(wrap_pyfunction!(evaluate_boolean_expression_rust, m)?)?;
    Ok(())
}
