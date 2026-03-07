use crate::gateway::ActivationMode;

/// Parsed voice command from a transcript.
pub enum VoiceCommand {
    /// Switch active agent to the given name.
    SwitchAgent(String),
    /// List all available agents.
    ListAgents,
    /// Request a cross-channel briefing.
    Briefing,
    /// Stop current TTS playback.
    Stop,
    /// Leave the voice channel.
    Leave,
    /// Change activation mode.
    SetMode(ActivationMode),
    /// Everything else — forward to the active agent.
    AgentMessage(String),
}

/// Parse a voice command from a transcript.
///
/// Commands are detected by prefix/keyword matching on the lowercased text.
/// Anything that doesn't match a known command is forwarded as-is to the
/// active agent (preserving original casing).
pub fn parse_command(transcript: &str) -> VoiceCommand {
    let t = transcript.trim().to_lowercase();

    if let Some(name) = t.strip_prefix("switch to ") {
        return VoiceCommand::SwitchAgent(name.trim().to_string());
    }
    if let Some(name) = t.strip_prefix("talk to ") {
        return VoiceCommand::SwitchAgent(name.trim().to_string());
    }
    if t.contains("list") && (t.contains("channel") || t.contains("agent")) {
        return VoiceCommand::ListAgents;
    }
    if t.contains("briefing") || t.starts_with("what happened") || t.starts_with("what's new") {
        return VoiceCommand::Briefing;
    }
    if t == "stop" || t == "cancel" || t == "shut up" || t == "be quiet" {
        return VoiceCommand::Stop;
    }
    if t == "leave" || t == "voice off" || t == "disconnect" {
        return VoiceCommand::Leave;
    }

    VoiceCommand::AgentMessage(transcript.trim().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn switch_agent() {
        match parse_command("switch to code-review") {
            VoiceCommand::SwitchAgent(name) => assert_eq!(name, "code-review"),
            _ => panic!("Expected SwitchAgent"),
        }
    }

    #[test]
    fn talk_to_agent() {
        match parse_command("talk to axi-master") {
            VoiceCommand::SwitchAgent(name) => assert_eq!(name, "axi-master"),
            _ => panic!("Expected SwitchAgent"),
        }
    }

    #[test]
    fn list_agents() {
        assert!(matches!(
            parse_command("list my agents"),
            VoiceCommand::ListAgents
        ));
        assert!(matches!(
            parse_command("list all channels"),
            VoiceCommand::ListAgents
        ));
    }

    #[test]
    fn briefing() {
        assert!(matches!(
            parse_command("give me a briefing"),
            VoiceCommand::Briefing
        ));
        assert!(matches!(
            parse_command("what happened while I was gone"),
            VoiceCommand::Briefing
        ));
    }

    #[test]
    fn stop_commands() {
        assert!(matches!(parse_command("stop"), VoiceCommand::Stop));
        assert!(matches!(parse_command("cancel"), VoiceCommand::Stop));
        assert!(matches!(parse_command("shut up"), VoiceCommand::Stop));
    }

    #[test]
    fn leave_commands() {
        assert!(matches!(parse_command("leave"), VoiceCommand::Leave));
        assert!(matches!(parse_command("voice off"), VoiceCommand::Leave));
    }

    #[test]
    fn agent_message_fallback() {
        match parse_command("please review this pull request") {
            VoiceCommand::AgentMessage(msg) => {
                assert_eq!(msg, "please review this pull request");
            }
            _ => panic!("Expected AgentMessage"),
        }
    }

    #[test]
    fn preserves_original_casing_for_messages() {
        match parse_command("Fix the BUG in MyModule") {
            VoiceCommand::AgentMessage(msg) => {
                assert_eq!(msg, "Fix the BUG in MyModule");
            }
            _ => panic!("Expected AgentMessage"),
        }
    }
}
