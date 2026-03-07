/// Downsample 48 kHz stereo i16 to 16 kHz mono i16.
///
/// Input: interleaved `[L, R, L, R, ...]` at 48 kHz.
/// Output: mono at 16 kHz (every 3rd mixed pair).
///
/// A 20 ms Songbird frame has 1920 samples (960 stereo pairs).
/// After conversion: 320 mono samples at 16 kHz.
pub fn downsample_48k_stereo_to_16k_mono(input: &[i16]) -> Vec<i16> {
    // Each chunk of 6 samples = 3 stereo pairs at 48 kHz.
    // We pick the middle pair and mix L+R.
    let mut output = Vec::with_capacity(input.len() / 6);
    for chunk in input.chunks_exact(6) {
        let l = i32::from(chunk[2]);
        let r = i32::from(chunk[3]);
        output.push(((l + r) / 2) as i16);
    }
    output
}

/// Upsample 24 kHz mono s16le to 48 kHz stereo f32.
///
/// Each input sample becomes 4 output samples:
/// 2x for sample-rate doubling, 2x for mono→stereo.
/// Result is ready for Songbird's `RawReader` (48 kHz stereo f32).
pub fn upsample_24k_mono_to_48k_stereo_f32(input: &[i16]) -> Vec<f32> {
    let mut output = Vec::with_capacity(input.len() * 4);
    for &sample in input {
        let f = f32::from(sample) / 32768.0;
        // duplicate: 2x rate × 2 channels = 4 values per input sample
        output.extend_from_slice(&[f, f, f, f]);
    }
    output
}

/// Upsample 22050 Hz mono s16le to 48 kHz stereo f32.
///
/// Uses linear interpolation for the ~2.177x rate conversion.
/// Result is ready for Songbird's `RawAdapter` (48 kHz stereo f32).
pub fn upsample_22k_mono_to_48k_stereo_f32(input: &[i16]) -> Vec<f32> {
    if input.is_empty() {
        return Vec::new();
    }
    // 48000 / 22050 ≈ 2.17687...
    // Output length: ceil(input.len() * 48000 / 22050) * 2 (stereo)
    let out_mono_len = ((input.len() as u64) * 48000 + 22049) / 22050;
    let mut output = Vec::with_capacity(out_mono_len as usize * 2);

    for i in 0..out_mono_len as usize {
        // Map output sample index back to input position
        let src_pos = i as f64 * 22050.0 / 48000.0;
        let idx = src_pos as usize;
        let frac = src_pos - idx as f64;

        let s0 = f64::from(input[idx.min(input.len() - 1)]);
        let s1 = f64::from(input[(idx + 1).min(input.len() - 1)]);
        let interpolated = (s0 + frac * (s1 - s0)) / 32768.0;
        let f = interpolated as f32;
        output.push(f); // L
        output.push(f); // R
    }
    output
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn downsample_frame_size() {
        // 20 ms at 48 kHz stereo = 1920 samples
        let input = vec![0i16; 1920];
        let output = downsample_48k_stereo_to_16k_mono(&input);
        // 20 ms at 16 kHz mono = 320 samples
        assert_eq!(output.len(), 320);
    }

    #[test]
    fn downsample_mixes_lr() {
        // 6 samples = 3 stereo pairs, we pick middle pair [2],[3]
        let input: Vec<i16> = vec![0, 0, 100, 200, 0, 0];
        let output = downsample_48k_stereo_to_16k_mono(&input);
        assert_eq!(output.len(), 1);
        assert_eq!(output[0], 150); // (100 + 200) / 2
    }

    #[test]
    fn downsample_negative_values() {
        let input: Vec<i16> = vec![0, 0, -100, -200, 0, 0];
        let output = downsample_48k_stereo_to_16k_mono(&input);
        assert_eq!(output[0], -150);
    }

    #[test]
    fn downsample_empty() {
        let output = downsample_48k_stereo_to_16k_mono(&[]);
        assert!(output.is_empty());
    }

    #[test]
    fn downsample_partial_chunk_ignored() {
        // 5 samples — not a full chunk of 6
        let input = vec![1i16; 5];
        let output = downsample_48k_stereo_to_16k_mono(&input);
        assert!(output.is_empty());
    }

    #[test]
    fn upsample_output_size() {
        let input = vec![0i16; 100];
        let output = upsample_24k_mono_to_48k_stereo_f32(&input);
        assert_eq!(output.len(), 400); // 4x
    }

    #[test]
    fn upsample_value_range() {
        let input = vec![i16::MAX, i16::MIN, 0];
        let output = upsample_24k_mono_to_48k_stereo_f32(&input);

        // Max: 32767 / 32768 ≈ 0.99997
        assert!((output[0] - 0.999_969).abs() < 0.001);
        // Min: -32768 / 32768 = -1.0
        assert!((output[4] - (-1.0)).abs() < f32::EPSILON);
        // Zero
        assert!((output[8]).abs() < f32::EPSILON);
    }

    #[test]
    fn upsample_duplication_pattern() {
        let input = vec![1000i16];
        let output = upsample_24k_mono_to_48k_stereo_f32(&input);
        // All 4 output samples should be identical
        assert_eq!(output.len(), 4);
        assert!((output[0] - output[1]).abs() < f32::EPSILON);
        assert!((output[0] - output[2]).abs() < f32::EPSILON);
        assert!((output[0] - output[3]).abs() < f32::EPSILON);
    }

    #[test]
    fn upsample_empty() {
        let output = upsample_24k_mono_to_48k_stereo_f32(&[]);
        assert!(output.is_empty());
    }
}
