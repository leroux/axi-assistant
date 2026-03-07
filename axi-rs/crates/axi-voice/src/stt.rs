use async_trait::async_trait;
use bytes::Bytes;
use futures_util::{SinkExt, StreamExt};
use tokio::sync::{mpsc, oneshot};
use tokio_tungstenite::tungstenite::Message;
use tracing::{debug, error, info, warn};

/// A transcript result from the STT provider.
pub struct Transcript {
    pub text: String,
    /// Deepgram `is_final` — this alternative won't change further.
    pub is_final: bool,
    /// Deepgram `speech_final` — end of utterance (use for turn detection).
    pub speech_final: bool,
}

/// Handle to a running STT session.
pub struct SttSession {
    /// Send 16 kHz mono s16le PCM chunks here.
    pub audio_tx: mpsc::Sender<Bytes>,
    /// Receive transcripts.
    pub transcript_rx: mpsc::Receiver<Transcript>,
    /// Send to cleanly shut down the session.
    pub shutdown: oneshot::Sender<()>,
}

/// STT provider abstraction.
#[async_trait]
pub trait SttProvider: Send + Sync {
    /// Open a new streaming session. Returns handles for audio in / transcripts out.
    async fn connect(&self) -> anyhow::Result<SttSession>;
}

// ---------------------------------------------------------------------------
// Deepgram Nova-3
// ---------------------------------------------------------------------------

pub struct DeepgramStt {
    api_key: String,
}

impl DeepgramStt {
    pub fn new(api_key: String) -> Self {
        Self { api_key }
    }
}

#[async_trait]
impl SttProvider for DeepgramStt {
    async fn connect(&self) -> anyhow::Result<SttSession> {
        let url = "wss://api.deepgram.com/v1/listen\
            ?model=nova-3\
            &encoding=linear16\
            &sample_rate=16000\
            &channels=1\
            &punctuate=true\
            &interim_results=true";

        let request = http::Request::builder()
            .uri(url)
            .header("Authorization", format!("Token {}", self.api_key))
            .header("Sec-WebSocket-Key", tungstenite_key())
            .header("Sec-WebSocket-Version", "13")
            .header("Connection", "Upgrade")
            .header("Upgrade", "websocket")
            .header("Host", "api.deepgram.com")
            .body(())?;

        let (ws, _resp) =
            tokio_tungstenite::connect_async(request).await?;
        let (mut ws_sink, mut ws_stream) = ws.split();

        info!("Deepgram WebSocket connected");

        let (audio_tx, mut audio_rx) = mpsc::channel::<Bytes>(256);
        let (transcript_tx, transcript_rx) = mpsc::channel::<Transcript>(64);
        let (shutdown_tx, mut shutdown_rx) = oneshot::channel::<()>();

        // Audio sender task: PCM bytes → WebSocket binary frames
        // Sends KeepAlive every 5s when no audio is flowing.
        let send_task = tokio::spawn(async move {
            let keepalive_interval = tokio::time::Duration::from_secs(5);
            let keepalive_msg = r#"{"type":"KeepAlive"}"#;
            let mut bytes_sent: u64 = 0;
            let mut frames_sent: u64 = 0;
            loop {
                tokio::select! {
                    Some(pcm) = audio_rx.recv() => {
                        let len = pcm.len();
                        if let Err(e) = ws_sink.send(Message::Binary(pcm.to_vec().into())).await {
                            warn!("Deepgram send error: {e}");
                            break;
                        }
                        bytes_sent += len as u64;
                        frames_sent += 1;
                        if frames_sent % 250 == 1 {
                            debug!(bytes_sent, frames_sent, "Deepgram audio streaming");
                        }
                    }
                    _ = tokio::time::sleep(keepalive_interval) => {
                        if let Err(e) = ws_sink.send(Message::Text(keepalive_msg.into())).await {
                            warn!("Deepgram keepalive send error: {e}");
                            break;
                        }
                        debug!("Deepgram keepalive sent");
                    }
                    _ = &mut shutdown_rx => {
                        info!("Deepgram session shutting down (sent {bytes_sent} bytes in {frames_sent} frames)");
                        let close = serde_json::json!({"type": "CloseStream"});
                        let _ = ws_sink.send(Message::Text(close.to_string().into())).await;
                        break;
                    }
                }
            }
        });

        // Transcript receiver task: WebSocket JSON → Transcript channel
        tokio::spawn(async move {
            let mut msg_count: u64 = 0;
            while let Some(msg) = ws_stream.next().await {
                match msg {
                    Ok(Message::Text(text)) => {
                        msg_count += 1;
                        // Log first few messages and all non-Results for debugging
                        if msg_count <= 3 {
                            info!(msg_count, "Deepgram message: {}", &text[..text.len().min(200)]);
                        }
                        if let Some(transcript) = parse_deepgram_result(&text) {
                            info!(
                                text = %transcript.text,
                                is_final = transcript.is_final,
                                speech_final = transcript.speech_final,
                                "Deepgram transcript"
                            );
                            if transcript_tx.send(transcript).await.is_err() {
                                break; // receiver dropped
                            }
                        }
                    }
                    Ok(Message::Close(frame)) => {
                        warn!("Deepgram WebSocket closed: {frame:?}");
                        break;
                    }
                    Err(e) => {
                        error!("Deepgram recv error: {e}");
                        break;
                    }
                    _ => {}
                }
            }
            info!("Deepgram receiver task exiting (received {msg_count} messages)");
            send_task.abort();
        });

        Ok(SttSession {
            audio_tx,
            transcript_rx,
            shutdown: shutdown_tx,
        })
    }
}

/// Parse a Deepgram streaming JSON result.
fn parse_deepgram_result(json: &str) -> Option<Transcript> {
    let v: serde_json::Value = serde_json::from_str(json).ok()?;

    // Only process "Results" messages
    if v.get("type").and_then(|t| t.as_str()) != Some("Results") {
        return None;
    }

    let channel = v.get("channel")?;
    let alternatives = channel.get("alternatives")?.as_array()?;
    let first = alternatives.first()?;
    let text = first.get("transcript")?.as_str()?.to_string();

    if text.is_empty() {
        return None;
    }

    let is_final = v.get("is_final").and_then(|v| v.as_bool()).unwrap_or(false);
    let speech_final = v
        .get("speech_final")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);

    Some(Transcript {
        text,
        is_final,
        speech_final,
    })
}

/// Generate a random WebSocket key for the handshake.
fn tungstenite_key() -> String {
    use std::time::SystemTime;
    let nanos = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap_or_default()
        .subsec_nanos();
    let mut bytes = [0u8; 16];
    for (i, b) in bytes.iter_mut().enumerate() {
        *b = ((nanos.wrapping_mul((i as u32) + 1)) & 0xFF) as u8;
    }
    use base64::Engine;
    base64::engine::general_purpose::STANDARD.encode(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_final_transcript() {
        let json = r#"{
            "type": "Results",
            "channel": {
                "alternatives": [{"transcript": "hello world", "confidence": 0.98}]
            },
            "is_final": true,
            "speech_final": true
        }"#;
        let t = parse_deepgram_result(json).unwrap();
        assert_eq!(t.text, "hello world");
        assert!(t.is_final);
        assert!(t.speech_final);
    }

    #[test]
    fn parse_interim_transcript() {
        let json = r#"{
            "type": "Results",
            "channel": {
                "alternatives": [{"transcript": "hel", "confidence": 0.5}]
            },
            "is_final": false,
            "speech_final": false
        }"#;
        let t = parse_deepgram_result(json).unwrap();
        assert_eq!(t.text, "hel");
        assert!(!t.is_final);
        assert!(!t.speech_final);
    }

    #[test]
    fn parse_empty_transcript_returns_none() {
        let json = r#"{
            "type": "Results",
            "channel": {
                "alternatives": [{"transcript": "", "confidence": 0.0}]
            },
            "is_final": false,
            "speech_final": false
        }"#;
        assert!(parse_deepgram_result(json).is_none());
    }

    #[test]
    fn parse_non_results_type_returns_none() {
        let json = r#"{"type": "Metadata", "request_id": "abc"}"#;
        assert!(parse_deepgram_result(json).is_none());
    }
}
