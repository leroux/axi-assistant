use std::collections::HashMap;

/// Evaluate a branch condition string against the current variables.
///
/// Supports:
/// - Simple variable truthiness: `fullyImplemented`
/// - Negation: `!hasErrors`
/// - Comparisons: `exitCode == 0`, `count > 5`, `status != "done"`
///
/// Template substitution (`{{var}}`, `$N`) should be done *before* calling this.
pub fn evaluate(condition: &str, variables: &HashMap<String, String>) -> bool {
    let cond = condition.trim();

    // Negation: !varname
    if let Some(inner) = cond.strip_prefix('!') {
        let inner = inner.trim();
        return !is_truthy(variables.get(inner).map(String::as_str));
    }

    // Try comparison operators
    for op in &["==", "!=", ">=", "<=", ">", "<"] {
        if let Some((lhs_raw, rhs_raw)) = cond.split_once(op) {
            let lhs_raw = lhs_raw.trim();
            let rhs_raw = rhs_raw.trim();

            // Strip surrounding quotes from RHS
            let rhs_str = rhs_raw
                .strip_prefix('"')
                .and_then(|s| s.strip_suffix('"'))
                .or_else(|| {
                    rhs_raw
                        .strip_prefix('\'')
                        .and_then(|s| s.strip_suffix('\''))
                })
                .unwrap_or(rhs_raw);

            // Resolve LHS from variables (could be a variable name or literal)
            let lhs_val = variables
                .get(lhs_raw)
                .map_or(lhs_raw, String::as_str);

            // Try numeric comparison first
            if let (Ok(lhs_num), Ok(rhs_num)) =
                (lhs_val.parse::<f64>(), rhs_str.parse::<f64>())
            {
                return compare_f64(lhs_num, rhs_num, op);
            }

            // String comparison fallback
            return compare_str(lhs_val, rhs_str, op);
        }
    }

    // Simple variable lookup (truthiness)
    is_truthy(variables.get(cond).map(String::as_str))
}

/// Test truthiness matching the Python behavior.
///
/// `None`, empty string, `"false"`, `"0"`, `"no"` are all falsy.
fn is_truthy(value: Option<&str>) -> bool {
    match value {
        None => false,
        Some(s) => {
            let lower = s.trim().to_lowercase();
            !lower.is_empty() && lower != "false" && lower != "0" && lower != "no"
        }
    }
}

fn compare_f64(lhs: f64, rhs: f64, op: &str) -> bool {
    match op {
        "==" => (lhs - rhs).abs() < f64::EPSILON,
        "!=" => (lhs - rhs).abs() >= f64::EPSILON,
        ">" => lhs > rhs,
        "<" => lhs < rhs,
        ">=" => lhs >= rhs || (lhs - rhs).abs() < f64::EPSILON,
        "<=" => lhs <= rhs || (lhs - rhs).abs() < f64::EPSILON,
        _ => false,
    }
}

fn compare_str(lhs: &str, rhs: &str, op: &str) -> bool {
    match op {
        "==" => lhs == rhs,
        "!=" => lhs != rhs,
        ">" => lhs > rhs,
        "<" => lhs < rhs,
        ">=" => lhs >= rhs,
        "<=" => lhs <= rhs,
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn vars(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs.iter().map(|(k, v)| ((*k).into(), (*v).into())).collect()
    }

    #[test]
    fn truthy_variable() {
        let v = vars(&[("flag", "true")]);
        assert!(evaluate("flag", &v));
    }

    #[test]
    fn falsy_missing_variable() {
        let v = HashMap::new();
        assert!(!evaluate("flag", &v));
    }

    #[test]
    fn falsy_false_string() {
        let v = vars(&[("flag", "false")]);
        assert!(!evaluate("flag", &v));
    }

    #[test]
    fn falsy_zero_string() {
        let v = vars(&[("flag", "0")]);
        assert!(!evaluate("flag", &v));
    }

    #[test]
    fn falsy_no_string() {
        let v = vars(&[("flag", "no")]);
        assert!(!evaluate("flag", &v));
    }

    #[test]
    fn truthy_yes_string() {
        let v = vars(&[("flag", "yes")]);
        assert!(evaluate("flag", &v));
    }

    #[test]
    fn negation() {
        let v = vars(&[("hasErrors", "true")]);
        assert!(!evaluate("!hasErrors", &v));
    }

    #[test]
    fn negation_missing() {
        let v = HashMap::new();
        assert!(evaluate("!missing", &v));
    }

    #[test]
    fn numeric_equality() {
        let v = vars(&[("exitCode", "0")]);
        assert!(evaluate("exitCode == 0", &v));
    }

    #[test]
    fn numeric_inequality() {
        let v = vars(&[("exitCode", "1")]);
        assert!(evaluate("exitCode != 0", &v));
    }

    #[test]
    fn numeric_less_than() {
        let v = vars(&[("i", "2")]);
        assert!(evaluate("i < 3", &v));
    }

    #[test]
    fn numeric_greater_than() {
        let v = vars(&[("count", "10")]);
        assert!(evaluate("count > 5", &v));
    }

    #[test]
    fn string_equality_quoted() {
        let v = vars(&[("status", "done")]);
        assert!(evaluate("status == \"done\"", &v));
    }

    #[test]
    fn string_inequality() {
        let v = vars(&[("status", "running")]);
        assert!(evaluate("status != \"done\"", &v));
    }

    #[test]
    fn numeric_gte() {
        let v = vars(&[("x", "5")]);
        assert!(evaluate("x >= 5", &v));
        assert!(evaluate("x >= 4", &v));
        assert!(!evaluate("x >= 6", &v));
    }

    #[test]
    fn numeric_lte() {
        let v = vars(&[("x", "5")]);
        assert!(evaluate("x <= 5", &v));
        assert!(evaluate("x <= 6", &v));
        assert!(!evaluate("x <= 4", &v));
    }

    #[test]
    fn empty_string_is_falsy() {
        let v = vars(&[("flag", "")]);
        assert!(!evaluate("flag", &v));
    }
}
