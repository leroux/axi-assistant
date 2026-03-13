# RFC-0018: Voice I/O

**Status:** Draft
**Created:** 2026-03-09

## Problem

Voice I/O connects Discord voice channels to the agent system via speech-to-text and text-to-speech pipelines. The system must handle real-time audio resampling, WebSocket-based STT streaming, multiple TTS backends, and a command router — all coordinated across concurrent tasks with proper shutdown semantics. Prior regressions include dropped transcripts, overlapping TTS playback, Deepgram idle timeouts, and deadlocks from double-locking. This RFC specifies the normative audio pipeline, provider contracts, and task coordination.

## Behavior

### Session Lifecycle

1. **Join.** `VoiceSession::join` connects to a Discord voice channel via Songbird, wires STT (Deepgram) and TTS providers, and returns `(Arc<VoiceSession>, mpsc::Receiver<String>)`. The host owns transcript routing — the voice library does not embed host-specific logic.

2. **Handler registration order.** Event handlers are registered BEFORE joining the voice channel (via `get_or_insert`) because some events (e.g., `SpeakingStateUpdate`) fire during the join handshake and would be missed otherwise.

3. **Leave and shutdown.** `VoiceSession::leave` triggers a `CancellationToken` that coordinates shutdown across the `transcript_filter` and `tts_consumer` background tasks.

4. **STT lifetime.** The `VoiceSession` holds the STT shutdown oneshot sender (`_stt_shutdown`). Dropping it sends a `CloseStream` message to Deepgram, cleanly terminating the WebSocket session.

### Audio Receive Pipeline

5. **Voice event handling.** `VoiceReceiveHandler` processes two event types:
   - `SpeakingStateUpdate` — maps the authorized user's Discord `UserId` to their SSRC (synchronization source identifier)
   - `VoiceTick` — extracts decoded audio for the mapped SSRC

6. **Listening gate.** The `is_listening` atomic bool gates audio forwarding in `VoiceTick`. When false, decoded audio is not sent to STT.

7. **Downsampling.** Audio from the authorized user is downsampled from 48 kHz stereo i16 to 16 kHz mono i16. The algorithm picks the middle stereo pair of each 3-pair chunk (48000/16000 = 3x decimation, selecting the center sample of each group).

8. **Diagnostic logging.** Logging fires on the first tick and every 500 ticks (~10 seconds at 50 ticks/second) to provide pipeline health visibility without spamming.

### Speech-to-Text (Deepgram)

9. **WebSocket streaming.** `DeepgramStt` opens a WebSocket to Deepgram Nova-3, sends 16 kHz mono s16le PCM as binary frames, and parses streaming JSON results into `Transcript` structs with `is_final` and `speech_final` flags.

10. **Transcript filtering.** The `transcript_filter` background task only passes through transcripts where `speech_final == true` and the text is non-empty after trimming. All interim results are discarded.

11. **KeepAlive.** The STT send task sends `{"type":"KeepAlive"}` messages on a 5-second timer when no audio is flowing, preventing Deepgram's idle timeout from closing the WebSocket.

### Text-to-Speech

12. **Provider implementations.** Three TTS providers are supported:

    | Provider | Source Format | Resampling |
    |----------|--------------|------------|
    | OpenAI TTS | 24 kHz mono s16le, streamed | 2x upsample to 48 kHz stereo f32 |
    | Piper | 22050 Hz mono s16le, subprocess | Linear interpolation to 48 kHz stereo f32 |
    | espeak-ng | 22050 Hz mono s16le, 44-byte WAV header stripped | Same as Piper |

13. **Playback.** `play_tts` collects all TTS chunks into a contiguous PCM buffer, wraps it in a `RawAdapter` (48 kHz stereo f32), plays via Songbird, and waits for the `TrackEnd` event via a oneshot channel before returning.

14. **Serialized playback.** TTS requests are serialized through a `tts_consumer` background task that reads from an mpsc channel. Each utterance completes before the next begins. A 250ms gap is inserted between utterances.

### Voice Command Router

15. **Command parsing.** The router (`router.rs`) parses transcripts into `VoiceCommand` variants via lowercased prefix/keyword matching, preserving original casing for the `AgentMessage` fallthrough:

    | Command | Trigger |
    |---------|---------|
    | `SwitchAgent` | "switch to [agent]" prefix |
    | `ListAgents` | "list agents" keyword |
    | `Briefing` | "briefing" keyword |
    | `Stop` | "stop" keyword |
    | `Leave` | "leave" keyword |
    | `SetMode` | "set mode [mode]" prefix |
    | `AgentMessage` | fallthrough — any unmatched transcript |

## Invariants

**I18.1:** Voice library must be decoupled from host — channel-based API, no callback or host-specific logic (agent routing, voice prefix formatting, greeting text). The library returns a transcript receiver; the host decides what to do with transcripts.

**I18.2:** Voice transcripts must not be silently dropped when the agent is sleeping. The host must wake the agent or provide feedback, not discard the input.

**I18.3:** TTS playback must not overlap. Each utterance must complete (TrackEnd received) before the next starts. The `tts_consumer` task with oneshot-based completion tracking enforces this.

**I18.4:** Deepgram WebSocket must send KeepAlive messages to prevent idle timeout. When no audio is flowing (e.g., bot auto-joined before user arrived), a 5-second keepalive timer prevents disconnection.

**I18.5:** Voice receive handler must not double-lock shared state. `queue_and_wake` for voice messages must not acquire a lock that `process_message_queue` also acquires internally.

## Open Questions

1. **Multi-user voice.** The current design tracks a single authorized user's SSRC. Should the system support multiple simultaneous speakers, and if so, how should overlapping transcripts be handled?

2. **TTS provider selection.** Three providers exist but there is no documented selection mechanism. Should provider choice be per-agent config, per-guild config, or dynamic?

3. **Audio quality tradeoffs.** The downsampling algorithm uses simple decimation (pick middle sample). Would a proper low-pass filter before decimation measurably improve STT accuracy?

## Implementation Notes

**axi-rs:** Voice I/O lives in the `axi-voice` crate. Songbird handles the Discord voice gateway connection and audio track management. `VoiceReceiveShared` is an `Arc<Mutex<...>>` shared between the event handler and the session, containing the SSRC-to-user mapping and the audio sender channel. Resampling functions are in `resample.rs` — they operate on raw `&[i16]` / `&[f32]` slices with no external DSP dependencies. The `TrackEndNotifier` implements Songbird's `EventHandler` trait and signals completion via a oneshot channel.
