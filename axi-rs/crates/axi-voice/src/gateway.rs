use std::future::Future;
use std::pin::Pin;
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

/// Callback to send a voice transcript to the agent system.
/// Fire-and-forget — the response comes back through the bridge's voice forwarding.
pub type ChatSendFn =
    Arc<dyn Fn(String) -> Pin<Box<dyn Future<Output = ()> + Send>> + Send + Sync>;

/// Activation mode for voice input.
pub enum ActivationMode {
    /// Always listening (push-to-talk managed externally or always on).
    AlwaysOn,
    /// Wake word required before each utterance (Phase 4).
    WakeWord,
}

/// A pending TTS request with priority ordering.
pub struct TtsRequest {
    pub text: String,
    /// 0 = agent response, 1 = notification, 2 = briefing
    pub priority: u8,
    pub interruptible: bool,
}

/// A live voice session — one per guild, manages audio I/O and STT/TTS.
pub struct VoiceSession {
    pub guild_id: GuildId,
    pub voice_channel_id: ChannelId,
    pub call: Arc<Mutex<Call>>,
    pub stt_audio_tx: mpsc::Sender<Bytes>,
    pub tts: Arc<dyn TtsProvider>,
    pub active_agent: RwLock<String>,
    pub mode: RwLock<ActivationMode>,
    pub is_listening: Arc<AtomicBool>,
    pub authorized_user_id: UserId,
    pub cancel: CancellationToken,
    /// Send TTS requests here; the consumer task plays them in order.
    pub tts_queue_tx: mpsc::Sender<TtsRequest>,
    /// Keep the STT shutdown handle alive — dropping it triggers session close.
    _stt_shutdown: Option<oneshot::Sender<()>>,
}

impl VoiceSession {
    /// Join a voice channel and start the audio pipeline.
    pub async fn join(
        ctx: &Context,
        guild_id: GuildId,
        voice_channel_id: ChannelId,
        authorized_user_id: UserId,
        stt_provider: &dyn SttProvider,
        tts_provider: Arc<dyn TtsProvider>,
        initial_agent: String,
        chat_send: ChatSendFn,
    ) -> anyhow::Result<Arc<Self>> {
        let manager = songbird::get(ctx)
            .await
            .ok_or_else(|| anyhow::anyhow!("Songbird not registered"))?;

        // Connect STT — split into audio_tx (stored) and transcript_rx (consumed by loop)
        let stt_session = stt_provider.connect().await?;
        let stt_audio_tx = stt_session.audio_tx;
        let stt_transcript_rx = stt_session.transcript_rx;
        let stt_shutdown = stt_session.shutdown;

        let is_listening = Arc::new(AtomicBool::new(true));
        let cancel = CancellationToken::new();

        // Register event handlers BEFORE joining — some events fire during join.
        // Uses get_or_insert to get a Call handle without connecting yet.
        // Both handlers share the same state (authorized_ssrc) via VoiceReceiveShared.
        {
            let call = manager.get_or_insert(guild_id);
            let mut handler = call.lock().await;

            let shared = crate::receive::VoiceReceiveShared::new(
                authorized_user_id,
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
        let call = manager.join(guild_id, voice_channel_id).await?;

        // TTS queue
        let (tts_queue_tx, tts_queue_rx) = mpsc::channel::<TtsRequest>(32);

        let session = Arc::new(Self {
            guild_id,
            voice_channel_id,
            call: Arc::clone(&call),
            stt_audio_tx,
            tts: tts_provider,
            active_agent: RwLock::new(initial_agent),
            mode: RwLock::new(ActivationMode::AlwaysOn),
            is_listening,
            authorized_user_id,
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

        // Spawn transcript → agent loop
        let session_chat = Arc::clone(&session);
        let cancel_chat = cancel.clone();
        tokio::spawn(async move {
            transcript_loop(session_chat, stt_transcript_rx, cancel_chat, chat_send).await;
        });

        info!(
            guild = %guild_id,
            channel = %voice_channel_id,
            "Voice session started"
        );

        // Greet after a short delay for DAVE readiness
        let session_greet = Arc::clone(&session);
        tokio::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_secs(3)).await;
            session_greet
                .speak("Hello! I'm listening.".to_string(), 0, false)
                .await;
        });

        Ok(session)
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
    pub async fn speak(&self, text: String, priority: u8, interruptible: bool) {
        let req = TtsRequest {
            text,
            priority,
            interruptible,
        };
        if self.tts_queue_tx.send(req).await.is_err() {
            warn!("TTS queue closed");
        }
    }

    /// Get the currently active agent name.
    pub async fn active_agent(&self) -> String {
        self.active_agent.read().await.clone()
    }

    /// Switch to a different agent.
    pub async fn set_active_agent(&self, name: String) {
        *self.active_agent.write().await = name;
    }
}

/// Background task: reads STT transcripts and sends them to the agent via callback.
/// Responses come back through the bridge's voice forwarding (bridge.rs adds TTS).
async fn transcript_loop(
    _session: Arc<VoiceSession>,
    mut transcript_rx: mpsc::Receiver<Transcript>,
    cancel: CancellationToken,
    chat_send: ChatSendFn,
) {
    info!("Transcript loop started, waiting for speech...");

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

                // Send to agent with voice prefix so it responds concisely
                let voice_msg = format!(
                    "[Voice message — respond in 1-2 short spoken sentences, no markdown or formatting]\n{}",
                    text
                );
                chat_send(voice_msg).await;
            }
            () = cancel.cancelled() => {
                debug!("Transcript loop shutting down");
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
                debug!(text = %req.text, priority = req.priority, "TTS request");
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
