use std::collections::HashMap;

use flowchart::Argument;

use crate::error::ExecutionError;

/// Build the initial variable map from an args string and declared arguments.
///
/// Positional args get both `$N` keys and named keys (from argument definitions).
/// Extra positional args beyond declared get `$N` keys only.
/// Missing required args with no default produce an error.
pub fn build_variables(
    args_str: &str,
    arguments: &[Argument],
) -> Result<HashMap<String, String>, ExecutionError> {
    let parts = shell_split(args_str);
    let mut vars = HashMap::new();

    for (i, arg_def) in arguments.iter().enumerate() {
        let pos = i + 1; // 1-indexed
        if let Some(value) = parts.get(i) {
            // Positional arg exists — store as both $N and named
            vars.insert(format!("${pos}"), value.clone());
            vars.insert(arg_def.name.clone(), value.clone());
        } else if let Some(default) = &arg_def.default {
            // No positional, but has default
            vars.insert(format!("${pos}"), default.clone());
            vars.insert(arg_def.name.clone(), default.clone());
        } else if arg_def.required.unwrap_or(true) {
            // Required with no default and no positional
            return Err(ExecutionError::MissingArgument {
                name: arg_def.name.clone(),
                position: pos,
            });
        }
        // Optional with no default and no positional: skip
    }

    // Extra positionals beyond declared args get $N keys only
    for (i, value) in parts.iter().enumerate().skip(arguments.len()) {
        let pos = i + 1;
        vars.insert(format!("${pos}"), value.clone());
    }

    Ok(vars)
}

/// Minimal shell-like splitting that handles basic quoting.
///
/// Supports single quotes, double quotes, and backslash escaping.
/// No glob expansion, no variable expansion — just tokenization.
fn shell_split(s: &str) -> Vec<String> {
    let s = s.trim();
    if s.is_empty() {
        return Vec::new();
    }

    let mut tokens = Vec::new();
    let mut current = String::new();
    let mut chars = s.chars();
    let mut in_single = false;
    let mut in_double = false;

    while let Some(c) = chars.next() {
        match c {
            '\'' if !in_double => {
                in_single = !in_single;
            }
            '"' if !in_single => {
                in_double = !in_double;
            }
            '\\' if !in_single => {
                // Backslash escapes next char
                if let Some(next) = chars.next() {
                    current.push(next);
                }
            }
            c if c.is_whitespace() && !in_single && !in_double => {
                if !current.is_empty() {
                    tokens.push(std::mem::take(&mut current));
                }
            }
            _ => {
                current.push(c);
            }
        }
    }

    if !current.is_empty() {
        tokens.push(current);
    }

    tokens
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_args() {
        let vars = build_variables("", &[]).unwrap();
        assert!(vars.is_empty());
    }

    #[test]
    fn positional_and_named() {
        let args = vec![
            Argument {
                name: "topic".into(),
                description: None,
                required: Some(true),
                default: None,
            },
            Argument {
                name: "style".into(),
                description: None,
                required: Some(false),
                default: Some("formal".into()),
            },
        ];
        let vars = build_variables("dragons", &args).unwrap();
        assert_eq!(vars["$1"], "dragons");
        assert_eq!(vars["topic"], "dragons");
        assert_eq!(vars["$2"], "formal");
        assert_eq!(vars["style"], "formal");
    }

    #[test]
    fn missing_required() {
        let args = vec![Argument {
            name: "topic".into(),
            description: None,
            required: Some(true),
            default: None,
        }];
        let result = build_variables("", &args);
        assert!(result.is_err());
    }

    #[test]
    fn extra_positionals() {
        let args = vec![Argument {
            name: "topic".into(),
            description: None,
            required: Some(true),
            default: None,
        }];
        let vars = build_variables("dragons medieval", &args).unwrap();
        assert_eq!(vars["$1"], "dragons");
        assert_eq!(vars["topic"], "dragons");
        assert_eq!(vars["$2"], "medieval");
        assert!(!vars.contains_key("medieval")); // no arg def for position 2
    }

    #[test]
    fn quoted_args() {
        let vars = build_variables(r#""hello world" 'foo bar'"#, &[]).unwrap();
        assert_eq!(vars["$1"], "hello world");
        assert_eq!(vars["$2"], "foo bar");
    }

    #[test]
    fn backslash_escape() {
        let vars = build_variables(r#"hello\ world"#, &[]).unwrap();
        assert_eq!(vars["$1"], "hello world");
    }

    #[test]
    fn optional_no_default_missing() {
        let args = vec![Argument {
            name: "style".into(),
            description: None,
            required: Some(false),
            default: None,
        }];
        let vars = build_variables("", &args).unwrap();
        assert!(!vars.contains_key("style"));
        assert!(!vars.contains_key("$1"));
    }
}
