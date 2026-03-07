use std::io::Cursor;
use std::sync::Arc;

use async_trait::async_trait;
use songbird::events::{Event, EventContext, EventHandler, TrackEvent};
use songbird::input::{Input, RawAdapter};
use tokio::sync::{mpsc, oneshot, Mutex};
use tracing::{info, warn};

use crate::tts::TtsChunk;

/// Play TTS audio through a Songbird call and wait for it to finish.
///
/// Blocks until the track ends so sequential TTS requests don't overlap.
pub async fn play_tts(call: &Arc<Mutex<songbird::Call>>, mut chunk_rx: mpsc::Receiver<TtsChunk>) {
    // Collect all chunks into a contiguous buffer.
    let mut pcm = Vec::new();
    while let Some(chunk) = chunk_rx.recv().await {
        pcm.extend_from_slice(&chunk.pcm_f32_48k_stereo);
    }

    if pcm.is_empty() {
        info!("TTS playback: no audio to play");
        return;
    }

    let duration_ms = (pcm.len() as f64 / 2.0 / 48000.0) * 1000.0;
    info!(
        samples = pcm.len(),
        duration_ms = duration_ms as u64,
        "TTS playback: queuing audio"
    );

    // Convert f32 samples to bytes for the raw adapter
    let mut raw_bytes = Vec::with_capacity(pcm.len() * 4);
    for sample in &pcm {
        raw_bytes.extend_from_slice(&sample.to_le_bytes());
    }

    info!(raw_bytes = raw_bytes.len(), "TTS playback: raw bytes ready");

    // RawAdapter wraps a MediaSource of interleaved f32 LE PCM
    let cursor = Cursor::new(raw_bytes);
    let adapter = RawAdapter::new(cursor, 48000, 2);
    let input = Input::from(adapter);

    let (end_tx, end_rx) = oneshot::channel::<()>();

    {
        let mut handler = call.lock().await;
        let track = handler.play_input(input);
        let notifier = TrackEndNotifier {
            sender: Mutex::new(Some(end_tx)),
        };
        if let Err(e) = track.add_event(Event::Track(TrackEvent::End), notifier) {
            warn!("Failed to register track end event: {e:?}");
        }
        info!(?track, "TTS playback: track started");
    }

    // Wait for the track to finish playing
    let _ = end_rx.await;

    // Small gap between consecutive utterances so they don't run together
    tokio::time::sleep(tokio::time::Duration::from_millis(250)).await;
}

struct TrackEndNotifier {
    sender: Mutex<Option<oneshot::Sender<()>>>,
}

#[async_trait]
impl EventHandler for TrackEndNotifier {
    async fn act(&self, _ctx: &EventContext<'_>) -> Option<Event> {
        if let Some(tx) = self.sender.lock().await.take() {
            let _ = tx.send(());
        }
        None
    }
}
