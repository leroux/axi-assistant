use serde_json::{Map, Value};

/// Extract a JSON object from Claude's response text.
///
/// Tries three strategies in order:
/// 1. Parse the entire text as JSON
/// 2. Extract from a markdown code block (```json ... ``` or ``` ... ```)
/// 3. Find the first `{` to last `}` substring and parse that
///
/// Only accepts JSON objects (not arrays or primitives).
pub fn extract_json(text: &str) -> Option<Map<String, Value>> {
    let trimmed = text.trim();

    // Strategy 1: whole text
    if let Some(obj) = try_parse(trimmed) {
        return Some(obj);
    }

    // Strategy 2: markdown code block
    if let Some(obj) = extract_code_block(trimmed) {
        return Some(obj);
    }

    // Strategy 3: first { to last }
    extract_brace_match(trimmed)
}

fn try_parse(text: &str) -> Option<Map<String, Value>> {
    match serde_json::from_str::<Value>(text) {
        Ok(Value::Object(map)) => Some(map),
        _ => None,
    }
}

fn extract_code_block(text: &str) -> Option<Map<String, Value>> {
    // Look for ```json\n...\n``` or ```\n...\n```
    let mut search_from = 0;
    while let Some(start) = text[search_from..].find("```") {
        let start = search_from + start;
        let after_backticks = start + 3;

        // Skip optional language tag (e.g., "json")
        let content_start = if text[after_backticks..].starts_with("json") {
            after_backticks + 4
        } else {
            after_backticks
        };

        // Skip whitespace/newline after the opening fence
        let content_start = text[content_start..]
            .find(|c: char| !c.is_whitespace() || c == '{')
            .map_or(content_start, |i| content_start + i);

        // Find closing ```
        if let Some(end) = text[content_start..].find("```") {
            let block = text[content_start..content_start + end].trim();
            if let Some(obj) = try_parse(block) {
                return Some(obj);
            }
        }

        search_from = after_backticks;
    }
    None
}

fn extract_brace_match(text: &str) -> Option<Map<String, Value>> {
    let first_brace = text.find('{')?;
    let last_brace = text.rfind('}')?;
    if first_brace >= last_brace {
        return None;
    }
    try_parse(&text[first_brace..=last_brace])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn whole_text_json() {
        let text = r#"{"key": "value", "count": 42}"#;
        let obj = extract_json(text).unwrap();
        assert_eq!(obj["key"], "value");
        assert_eq!(obj["count"], 42);
    }

    #[test]
    fn markdown_code_block() {
        let text = "Here's the result:\n```json\n{\"key\": \"value\"}\n```\nDone.";
        let obj = extract_json(text).unwrap();
        assert_eq!(obj["key"], "value");
    }

    #[test]
    fn markdown_no_lang_tag() {
        let text = "Result:\n```\n{\"key\": \"value\"}\n```";
        let obj = extract_json(text).unwrap();
        assert_eq!(obj["key"], "value");
    }

    #[test]
    fn brace_match() {
        let text = "The output is {\"key\": \"value\"} and that's it.";
        let obj = extract_json(text).unwrap();
        assert_eq!(obj["key"], "value");
    }

    #[test]
    fn rejects_array() {
        let text = "[1, 2, 3]";
        assert!(extract_json(text).is_none());
    }

    #[test]
    fn rejects_primitive() {
        assert!(extract_json("42").is_none());
        assert!(extract_json("\"hello\"").is_none());
    }

    #[test]
    fn no_json_at_all() {
        assert!(extract_json("just plain text").is_none());
    }

    #[test]
    fn whitespace_around() {
        let text = "  \n  {\"key\": \"value\"}  \n  ";
        let obj = extract_json(text).unwrap();
        assert_eq!(obj["key"], "value");
    }

    #[test]
    fn nested_json() {
        let text = r#"{"outer": {"inner": true}}"#;
        let obj = extract_json(text).unwrap();
        assert!(obj["outer"]["inner"].as_bool().unwrap());
    }

    #[test]
    fn multiple_code_blocks_first_wins() {
        let text = "```json\n{\"a\": 1}\n```\n```json\n{\"b\": 2}\n```";
        let obj = extract_json(text).unwrap();
        assert_eq!(obj["a"], 1);
    }
}
