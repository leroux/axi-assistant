use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

use async_trait::async_trait;
use bytes::Bytes;
use serenity::model::id::UserId;
use songbird::events::context_data::VoiceTick;
use songbird::events::{EventContext, EventHandler};
use songbird::model::payload::Speaking;
use songbird::Event;
use tokio::sync::{mpsc, RwLock};
use tracing::{debug, info, warn};

use crate::resample;

/// Shared state between SpeakingStateUpdate and VoiceTick handlers.
/// Both handlers need the same authorized_ssrc reference.
#[derive(Clone)]
pub struct VoiceReceiveShared {
    /// The authorized user's SSRC, learned from `SpeakingStateUpdate`.
    pub authorized_ssrc: Arc<RwLock<Option<u32>>>,
    /// The authorized Discord user ID.
    pub authorized_user_id: UserId,
    /// Channel to send resampled 16 kHz mono s16le PCM to STT.
    pub audio_tx: mpsc::Sender<Bytes>,
    /// Whether we're currently forwarding audio (PTT gate / wake word gate).
    pub is_listening: Arc<AtomicBool>,
    /// Counter for diagnostic logging (avoid spamming every tick).
    pub tick_count: Arc<AtomicU64>,
}

impl VoiceReceiveShared {
    pub fn new(
        authorized_user_id: UserId,
        audio_tx: mpsc::Sender<Bytes>,
        is_listening: Arc<AtomicBool>,
    ) -> Self {
        Self {
            authorized_ssrc: Arc::new(RwLock::new(None)),
            authorized_user_id,
            audio_tx,
            is_listening,
            tick_count: Arc::new(AtomicU64::new(0)),
        }
    }
}

/// Songbird event handler that captures the authorized user's audio,
/// resamples it to 16 kHz mono, and forwards it to the STT provider.
pub struct VoiceReceiveHandler {
    shared: VoiceReceiveShared,
}

impl VoiceReceiveHandler {
    pub fn new(shared: VoiceReceiveShared) -> Self {
        Self { shared }
    }
}

#[async_trait]
impl EventHandler for VoiceReceiveHandler {
    async fn act(&self, ctx: &EventContext<'_>) -> Option<Event> {
        match ctx {
            EventContext::SpeakingStateUpdate(speaking) => {
                handle_speaking_update(
                    speaking,
                    self.shared.authorized_user_id,
                    &self.shared.authorized_ssrc,
                )
                .await;
            }
            EventContext::VoiceTick(tick) => {
                let count = self.shared.tick_count.fetch_add(1, Ordering::Relaxed);
                // Log diagnostics on first tick and every 500 ticks (~10 sec)
                if count == 0 || count % 500 == 0 {
                    let ssrc = *self.shared.authorized_ssrc.read().await;
                    let speaking_count = tick.speaking.len();
                    let silent_count = tick.silent.len();
                    info!(
                        tick_count = count,
                        authorized_ssrc = ?ssrc,
                        speaking = speaking_count,
                        silent = silent_count,
                        "Voice tick diagnostic"
                    );
                }

                if self.shared.is_listening.load(Ordering::Relaxed) {
                    handle_voice_tick(
                        tick,
                        &self.shared.authorized_ssrc,
                        &self.shared.audio_tx,
                        &self.shared.tick_count,
                    )
                    .await;
                }
            }
            _ => {}
        }
        None // keep handler registered
    }
}

async fn handle_speaking_update(
    speaking: &Speaking,
    authorized_user_id: UserId,
    authorized_ssrc: &RwLock<Option<u32>>,
) {
    // serenity_voice_model::UserId and serenity::model::id::UserId are distinct types.
    // Compare by raw u64 value.
    if speaking.user_id.map(|u| u.0) == Some(authorized_user_id.get()) {
        let mut ssrc = authorized_ssrc.write().await;
        if *ssrc != Some(speaking.ssrc) {
            *ssrc = Some(speaking.ssrc);
            info!(ssrc = speaking.ssrc, user_id = %authorized_user_id, "Mapped authorized user SSRC");
        }
    }
}

async fn handle_voice_tick(
    tick: &VoiceTick,
    authorized_ssrc: &RwLock<Option<u32>>,
    audio_tx: &mpsc::Sender<Bytes>,
    tick_count: &AtomicU64,
) {
    let target_ssrc = *authorized_ssrc.read().await;
    let Some(ssrc) = target_ssrc else { return };

    // Log every 500 ticks what we see for this SSRC
    let count = tick_count.load(Ordering::Relaxed);
    let in_speaking = tick.speaking.contains_key(&ssrc);
    let in_silent = tick.silent.contains(&ssrc);
    if count <= 5 || count % 500 == 0 {
        info!(
            ssrc = ssrc,
            in_speaking = in_speaking,
            in_silent = in_silent,
            total_speaking = tick.speaking.len(),
            total_silent = tick.silent.len(),
            "Voice tick detail for authorized SSRC"
        );
    }

    let Some(voice_data) = tick.speaking.get(&ssrc) else {
        return;
    };

    let Some(ref decoded) = voice_data.decoded_voice else {
        // Log when we see data but no decoded voice (wrong decode mode)
        if count <= 10 || count % 100 == 0 {
            let pkt_len = voice_data.packet.as_ref().map(|p| p.packet.len());
            warn!(
                ssrc = ssrc,
                has_packet = voice_data.packet.is_some(),
                packet_len = ?pkt_len,
                "Voice data present but decoded_voice is None (decode mode may be wrong)"
            );
        }
        return;
    };

    // Log first successful decode
    if count <= 5 || count % 500 == 0 {
        info!(
            ssrc = ssrc,
            samples = decoded.len(),
            "Decoded voice data received"
        );
    }

    // decoded: &Vec<i16>, 48 kHz stereo interleaved, ~1920 samples per 20 ms
    let resampled = resample::downsample_48k_stereo_to_16k_mono(decoded);
    let bytes = Bytes::copy_from_slice(bytemuck::cast_slice(&resampled));
    if let Err(e) = audio_tx.try_send(bytes) {
        if count <= 5 || count % 500 == 0 {
            warn!("Failed to send audio to STT: {e}");
        }
    }
}
