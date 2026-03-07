use async_trait::async_trait;

use tokio::sync::mpsc;
use tokio_stream::StreamExt;
use tracing::{debug, error, info};

use crate::resample;

/// A chunk of audio ready for Songbird playback (48 kHz stereo f32).
pub struct TtsChunk {
    pub pcm_f32_48k_stereo: Vec<f32>,
}

/// TTS provider abstraction.
///
/// Implementations are responsible for converting their native output format
/// to 48 kHz stereo f32 (Songbird's expected format).
#[async_trait]
pub trait TtsProvider: Send + Sync {
    /// Synthesize text and return a receiver of audio chunks.
    async fn synthesize(&self, text: &str) -> anyhow::Result<mpsc::Receiver<TtsChunk>>;
}

// ---------------------------------------------------------------------------
// OpenAI TTS
// ---------------------------------------------------------------------------

pub struct OpenAiTts {
    api_key: String,
    model: String,
    voice: String,
    client: reqwest::Client,
}

impl OpenAiTts {
    pub fn new(api_key: String) -> Self {
        Self {
            api_key,
            model: "tts-1".to_string(),
            voice: "alloy".to_string(),
            client: reqwest::Client::new(),
        }
    }

    #[must_use]
    pub fn with_model(mut self, model: &str) -> Self {
        self.model = model.to_string();
        self
    }

    #[must_use]
    pub fn with_voice(mut self, voice: &str) -> Self {
        self.voice = voice.to_string();
        self
    }
}

#[async_trait]
impl TtsProvider for OpenAiTts {
    async fn synthesize(&self, text: &str) -> anyhow::Result<mpsc::Receiver<TtsChunk>> {
        let (tx, rx) = mpsc::channel(32);

        let body = serde_json::json!({
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": "pcm",
        });

        let response = self
            .client
            .post("https://api.openai.com/v1/audio/speech")
            .header("Authorization", format!("Bearer {}", self.api_key))
            .json(&body)
            .send()
            .await?;

        if !response.status().is_success() {
            let status = response.status();
            let body_text = response.text().await.unwrap_or_default();
            anyhow::bail!("OpenAI TTS error {status}: {body_text}");
        }

        // Stream response body → convert to Songbird format
        tokio::spawn(async move {
            let mut stream = response.bytes_stream();
            let mut leftover: Vec<u8> = Vec::new();

            while let Some(result) = stream.next().await {
                match result {
                    Ok(bytes) => {
                        leftover.extend_from_slice(&bytes);

                        // Process complete s16le samples (2 bytes each)
                        let complete_samples = leftover.len() / 2;
                        if complete_samples == 0 {
                            continue;
                        }

                        let pcm_bytes = &leftover[..complete_samples * 2];
                        let pcm_s16: Vec<i16> = pcm_bytes
                            .chunks_exact(2)
                            .map(|c| i16::from_le_bytes([c[0], c[1]]))
                            .collect();

                        // Keep any trailing odd byte
                        let consumed = complete_samples * 2;
                        leftover = leftover[consumed..].to_vec();

                        // OpenAI TTS outputs 24 kHz mono → convert to 48 kHz stereo f32
                        let resampled =
                            resample::upsample_24k_mono_to_48k_stereo_f32(&pcm_s16);

                        if tx
                            .send(TtsChunk {
                                pcm_f32_48k_stereo: resampled,
                            })
                            .await
                            .is_err()
                        {
                            debug!("TTS consumer dropped, stopping synthesis");
                            break;
                        }
                    }
                    Err(e) => {
                        error!("OpenAI TTS stream error: {e}");
                        break;
                    }
                }
            }
        });

        Ok(rx)
    }
}

// ---------------------------------------------------------------------------
// Piper TTS (local neural, natural-sounding)
// ---------------------------------------------------------------------------

pub struct PiperTts {
    model_path: String,
}

impl PiperTts {
    pub fn new(model_path: String) -> Self {
        Self { model_path }
    }
}

#[async_trait]
impl TtsProvider for PiperTts {
    async fn synthesize(&self, text: &str) -> anyhow::Result<mpsc::Receiver<TtsChunk>> {
        let (tx, rx) = mpsc::channel(4);
        let text = text.to_string();
        let model_path = self.model_path.clone();

        tokio::spawn(async move {
            // Piper needs its bundled libs and espeak-ng data
            let piper_dir = "/usr/local/lib/piper";
            let result = tokio::process::Command::new("/usr/local/bin/piper")
                .args([
                    "--model",
                    &model_path,
                    "--output_raw",
                    "--espeak_data",
                    &format!("{piper_dir}/espeak-ng-data"),
                    "--quiet",
                ])
                .env("LD_LIBRARY_PATH", piper_dir)
                .stdin(std::process::Stdio::piped())
                .stdout(std::process::Stdio::piped())
                .stderr(std::process::Stdio::piped())
                .spawn();

            let mut child = match result {
                Ok(c) => c,
                Err(e) => {
                    error!("Failed to spawn piper: {e}");
                    return;
                }
            };

            // Write text to stdin then close it
            if let Some(mut stdin) = child.stdin.take() {
                use tokio::io::AsyncWriteExt;
                if let Err(e) = stdin.write_all(text.as_bytes()).await {
                    error!("Failed to write to piper stdin: {e}");
                    return;
                }
                // stdin drops here, closing the pipe
            }

            let output = match child.wait_with_output().await {
                Ok(o) => o,
                Err(e) => {
                    error!("Piper process error: {e}");
                    return;
                }
            };

            if !output.status.success() {
                error!(
                    "Piper failed ({}): {}",
                    output.status,
                    String::from_utf8_lossy(&output.stderr)
                );
                return;
            }

            // --output_raw produces raw s16le PCM at 22050 Hz mono (no WAV header)
            let pcm_bytes = &output.stdout;
            let pcm_s16: Vec<i16> = pcm_bytes
                .chunks_exact(2)
                .map(|c| i16::from_le_bytes([c[0], c[1]]))
                .collect();

            let duration_ms = pcm_s16.len() as f64 / 22050.0 * 1000.0;
            info!(
                samples = pcm_s16.len(),
                duration_ms = duration_ms as u64,
                "Piper TTS synthesized"
            );

            let resampled = resample::upsample_22k_mono_to_48k_stereo_f32(&pcm_s16);
            let _ = tx
                .send(TtsChunk {
                    pcm_f32_48k_stereo: resampled,
                })
                .await;
        });

        Ok(rx)
    }
}

// ---------------------------------------------------------------------------
// espeak-ng TTS (local, free, robotic fallback)
// ---------------------------------------------------------------------------

pub struct EspeakTts;

impl EspeakTts {
    pub fn new() -> Self {
        Self
    }
}

#[async_trait]
impl TtsProvider for EspeakTts {
    async fn synthesize(&self, text: &str) -> anyhow::Result<mpsc::Receiver<TtsChunk>> {
        let (tx, rx) = mpsc::channel(4);
        let text = text.to_string();

        tokio::spawn(async move {
            let result = tokio::process::Command::new("espeak-ng")
                .args(["--stdout", &text])
                .output()
                .await;

            match result {
                Ok(output) => {
                    if !output.status.success() {
                        error!(
                            "espeak-ng failed: {}",
                            String::from_utf8_lossy(&output.stderr)
                        );
                        return;
                    }

                    let wav = &output.stdout;
                    if wav.len() < 44 {
                        error!("espeak-ng output too short for WAV header");
                        return;
                    }

                    // Skip 44-byte WAV header, rest is 22050 Hz mono s16le PCM
                    let pcm_bytes = &wav[44..];
                    let pcm_s16: Vec<i16> = pcm_bytes
                        .chunks_exact(2)
                        .map(|c| i16::from_le_bytes([c[0], c[1]]))
                        .collect();

                    info!("espeak-ng: {} samples at 22050 Hz", pcm_s16.len());

                    let resampled = resample::upsample_22k_mono_to_48k_stereo_f32(&pcm_s16);
                    let _ = tx
                        .send(TtsChunk {
                            pcm_f32_48k_stereo: resampled,
                        })
                        .await;
                }
                Err(e) => {
                    error!("Failed to run espeak-ng: {e}");
                }
            }
        });

        Ok(rx)
    }
}
