#![allow(warnings, clippy::all, clippy::pedantic, clippy::nursery)] // WIP: suppress lints until stabilized.

pub mod gateway;
pub mod playback;
pub mod receive;
pub mod resample;
pub mod router;
pub mod stt;
pub mod tts;

pub use gateway::VoiceConfig;

// Phase 3
// pub mod briefing;

// Phase 4
// pub mod wake;
