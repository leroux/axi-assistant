use std::collections::HashMap;

/// Interpolate `{{variable}}` and `$N` references in a template string.
///
/// - `{{name}}` is replaced with the value of `variables["name"]` (or empty string if missing).
/// - `$1`, `$2`, etc. are replaced with `variables["$1"]`, `variables["$2"]`, etc.
/// - Whole-number floats in values (e.g. "3.0") are rendered as integers ("3").
pub fn interpolate(template: &str, variables: &HashMap<String, String>) -> String {
    let bytes = template.as_bytes();
    let len = bytes.len();
    let mut result = String::with_capacity(template.len());
    let mut i = 0;

    while i < len {
        // Check for {{variable}} — safe because '{' is ASCII (single byte)
        if i + 1 < len && bytes[i] == b'{' && bytes[i + 1] == b'{' {
            if let Some(end) = find_closing_braces(template, i + 2) {
                let var_name = &template[i + 2..end];
                if !var_name.is_empty() && var_name.chars().all(|c| c.is_alphanumeric() || c == '_')
                {
                    result.push_str(&format_value(variables.get(var_name)));
                    i = end + 2; // skip past }}
                    continue;
                }
            }
            // Not a valid variable reference — emit literally
            result.push('{');
            i += 1;
            continue;
        }

        // Check for $N (positional argument) — safe because '$' and digits are ASCII
        if bytes[i] == b'$' && i + 1 < len && bytes[i + 1].is_ascii_digit() {
            let start = i + 1;
            let mut end = start;
            while end < len && bytes[end].is_ascii_digit() {
                end += 1;
            }
            let key = &template[i..end]; // e.g. "$1"
            result.push_str(&format_value(variables.get(key)));
            i = end;
            continue;
        }

        // Emit the current character, advancing by its full UTF-8 length
        let ch = template[i..].chars().next().expect("non-empty slice");
        result.push(ch);
        i += ch.len_utf8();
    }

    result
}

/// Find the position of `}}` starting from `start`.
fn find_closing_braces(s: &str, start: usize) -> Option<usize> {
    let bytes = s.as_bytes();
    let mut i = start;
    while i + 1 < bytes.len() {
        if bytes[i] == b'}' && bytes[i + 1] == b'}' {
            return Some(i);
        }
        i += 1;
    }
    None
}

/// Format a variable value for template substitution.
/// Whole-number floats (e.g. "3.0") are rendered without the decimal.
fn format_value(value: Option<&String>) -> String {
    match value {
        None => String::new(),
        Some(v) => {
            // Try to detect whole-number floats like "3.0" and render as "3"
            if let Ok(f) = v.parse::<f64>()
                && f.fract() == 0.0
                && v.contains('.')
            {
                #[allow(clippy::cast_possible_truncation)]
                return (f as i64).to_string();
            }
            v.clone()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn vars(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs.iter().map(|(k, v)| ((*k).into(), (*v).into())).collect()
    }

    #[test]
    fn simple_variable() {
        let v = vars(&[("name", "World")]);
        assert_eq!(interpolate("Hello {{name}}!", &v), "Hello World!");
    }

    #[test]
    fn positional_arg() {
        let v = vars(&[("$1", "Alice")]);
        assert_eq!(interpolate("Hello $1", &v), "Hello Alice");
    }

    #[test]
    fn missing_variable_is_empty() {
        let v = HashMap::new();
        assert_eq!(interpolate("Hello {{name}}", &v), "Hello ");
    }

    #[test]
    fn whole_number_float() {
        let v = vars(&[("x", "3.0")]);
        assert_eq!(interpolate("count={{x}}", &v), "count=3");
    }

    #[test]
    fn non_whole_float_preserved() {
        let v = vars(&[("x", "3.5")]);
        assert_eq!(interpolate("val={{x}}", &v), "val=3.5");
    }

    #[test]
    fn multiple_substitutions() {
        let v = vars(&[("$1", "main"), ("env", "staging")]);
        assert_eq!(
            interpolate("Deploy $1 to {{env}}", &v),
            "Deploy main to staging"
        );
    }

    #[test]
    fn no_substitutions() {
        let v = HashMap::new();
        assert_eq!(interpolate("plain text", &v), "plain text");
    }

    #[test]
    fn unclosed_braces() {
        let v = vars(&[("x", "val")]);
        assert_eq!(interpolate("{{x} text", &v), "{{x} text");
    }

    #[test]
    fn multi_digit_positional() {
        let v = vars(&[("$12", "twelve")]);
        assert_eq!(interpolate("arg=$12", &v), "arg=twelve");
    }

    #[test]
    fn non_ascii_text_preserved() {
        let v = vars(&[("name", "monde")]);
        assert_eq!(
            interpolate("Écrivez sur le {{name}}!", &v),
            "Écrivez sur le monde!"
        );
    }

    #[test]
    fn emoji_in_template() {
        let v = vars(&[("x", "42")]);
        assert_eq!(interpolate("Result: {{x}} 🎉", &v), "Result: 42 🎉");
    }
}
