use std::sync::atomic::AtomicBool;
use std::sync::Arc;

use bytes::Bytes;
use serenity::model::id::{ChannelId, GuildId, UserId};
use serenity::prelude::Context;
use songbird::events::CoreEvent;
use songbird::Call;
use tokio::sync::{mpsc, oneshot, Mutex, RwLock};
use tokio_util::sync::CancellationToken;
use tracing::{debug, error, info, warn};

use crate::playback;
use crate::receive::VoiceReceiveHandler;
use crate::stt::{SttProvider, Transcript};
use crate::tts::TtsProvider;

/// Configuration for joining a voice channel.
pub struct VoiceConfig {
    pub guild_id: GuildId,
    pub channel_id: ChannelId,
    pub authorized_user: UserId,
    pub stt: Box<dyn SttProvider>,
    pub tts: Arc<dyn TtsProvider>,
}

/// Activation mode for voice input.
pub enum ActivationMode {
    /// Always listening (push-to-talk managed externally or always on).
    AlwaysOn,
    /// Wake word required before each utterance (Phase 4).
    WakeWord,
}

/// A pending TTS request with priority ordering.
struct TtsRequest {
    text: String,
}

/// A live voice session — one per guild, manages audio I/O and STT/TTS.
pub struct VoiceSession {
    pub guild_id: GuildId,
    pub voice_channel_id: ChannelId,
    pub call: Arc<Mutex<Call>>,
    pub stt_audio_tx: mpsc::Sender<Bytes>,
    pub tts: Arc<dyn TtsProvider>,
    pub mode: RwLock<ActivationMode>,
    pub is_listening: Arc<AtomicBool>,
    pub authorized_user_id: UserId,
    pub cancel: CancellationToken,
    tts_queue_tx: mpsc::Sender<TtsRequest>,
    /// Keep the STT shutdown handle alive — dropping it triggers session close.
    _stt_shutdown: Option<oneshot::Sender<()>>,
}

impl VoiceSession {
    /// Join a voice channel and start the audio pipeline.
    ///
    /// Returns the session and a receiver of filtered transcripts (final utterances only).
    /// The host is responsible for consuming the receiver and routing transcripts.
    pub async fn join(
        ctx: &Context,
        config: VoiceConfig,
    ) -> anyhow::Result<(Arc<Self>, mpsc::Receiver<String>)> {
        let manager = songbird::get(ctx)
            .await
            .ok_or_else(|| anyhow::anyhow!("Songbird not registered"))?;

        // Connect STT — split into audio_tx (stored) and transcript_rx (consumed by filter)
        let stt_session = config.stt.connect().await?;
        let stt_audio_tx = stt_session.audio_tx;
        let stt_transcript_rx = stt_session.transcript_rx;
        let stt_shutdown = stt_session.shutdown;

        let is_listening = Arc::new(AtomicBool::new(true));
        let cancel = CancellationToken::new();

        // Register event handlers BEFORE joining — some events fire during join.
        // Uses get_or_insert to get a Call handle without connecting yet.
        // Both handlers share the same state (authorized_ssrc) via VoiceReceiveShared.
        {
            let call = manager.get_or_insert(config.guild_id);
            let mut handler = call.lock().await;

            let shared = crate::receive::VoiceReceiveShared::new(
                config.authorized_user,
                stt_audio_tx.clone(),
                Arc::clone(&is_listening),
            );

            handler.add_global_event(
                CoreEvent::SpeakingStateUpdate.into(),
                VoiceReceiveHandler::new(shared.clone()),
            );
            handler.add_global_event(
                CoreEvent::VoiceTick.into(),
                VoiceReceiveHandler::new(shared),
            );
        }

        // Now join the voice channel
        let call = manager.join(config.guild_id, config.channel_id).await?;

        // TTS queue
        let (tts_queue_tx, tts_queue_rx) = mpsc::channel::<TtsRequest>(32);

        let session = Arc::new(Self {
            guild_id: config.guild_id,
            voice_channel_id: config.channel_id,
            call: Arc::clone(&call),
            stt_audio_tx,
            tts: config.tts,
            mode: RwLock::new(ActivationMode::AlwaysOn),
            is_listening,
            authorized_user_id: config.authorized_user,
            cancel: cancel.clone(),
            tts_queue_tx,
            _stt_shutdown: Some(stt_shutdown),
        });

        // Spawn TTS consumer task
        let session_ref = Arc::clone(&session);
        let cancel_tts = cancel.clone();
        tokio::spawn(async move {
            tts_consumer(session_ref, tts_queue_rx, cancel_tts).await;
        });

        // Spawn transcript filter → channel
        let (transcript_tx, transcript_rx) = mpsc::channel::<String>(32);
        let cancel_filter = cancel.clone();
        tokio::spawn(async move {
            transcript_filter(stt_transcript_rx, transcript_tx, cancel_filter).await;
        });

        info!(
            guild = %config.guild_id,
            channel = %config.channel_id,
            "Voice session started"
        );

        Ok((session, transcript_rx))
    }

    /// Disconnect from the voice channel and clean up.
    pub async fn leave(&self, ctx: &Context) {
        self.cancel.cancel();

        let manager = songbird::get(ctx).await;
        if let Some(manager) = manager {
            if let Err(e) = manager.remove(self.guild_id).await {
                warn!("Error leaving voice channel: {e}");
            }
        }

        info!(guild = %self.guild_id, "Voice session ended");
    }

    /// Queue text to be spoken via TTS.
    pub async fn speak(&self, text: String) {
        let req = TtsRequest { text };
        if self.tts_queue_tx.send(req).await.is_err() {
            warn!("TTS queue closed");
        }
    }
}

/// Background task: filters STT transcripts and sends final utterances to the channel.
///
/// Only passes through speech_final transcripts that are non-empty after trimming.
/// The host consumes the channel and decides how to route the text.
async fn transcript_filter(
    mut transcript_rx: mpsc::Receiver<Transcript>,
    tx: mpsc::Sender<String>,
    cancel: CancellationToken,
) {
    info!("Transcript filter started, waiting for speech...");

    loop {
        tokio::select! {
            Some(transcript) = transcript_rx.recv() => {
                // Only act on final utterances (speech_final = end of turn)
                if !transcript.speech_final {
                    if transcript.is_final {
                        debug!(text = %transcript.text, "Interim final transcript");
                    }
                    continue;
                }

                let text = transcript.text.trim().to_string();
                if text.is_empty() {
                    continue;
                }

                info!(text = %text, "User said (voice)");

                if tx.send(text).await.is_err() {
                    debug!("Transcript consumer dropped, stopping filter");
                    break;
                }
            }
            () = cancel.cancelled() => {
                debug!("Transcript filter shutting down");
                break;
            }
        }
    }
}

/// Background task that consumes TTS requests and plays them through Songbird.
async fn tts_consumer(
    session: Arc<VoiceSession>,
    mut rx: mpsc::Receiver<TtsRequest>,
    cancel: CancellationToken,
) {
    loop {
        tokio::select! {
            Some(req) = rx.recv() => {
                debug!(text = %req.text, "TTS request");
                match session.tts.synthesize(&req.text).await {
                    Ok(chunk_rx) => {
                        playback::play_tts(&session.call, chunk_rx).await;
                    }
                    Err(e) => {
                        error!("TTS synthesis failed: {e}");
                    }
                }
            }
            () = cancel.cancelled() => {
                debug!("TTS consumer shutting down");
                break;
            }
        }
    }
}
