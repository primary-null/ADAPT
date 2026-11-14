use anyhow::{bail, Result};
use std::collections::VecDeque;

/// Reassembles the H.264 PES stream without a media subprocess.
///
/// Audio is opt-in: until `set_audio_enabled(true)` the demuxer ignores every
/// non-video PID exactly as it always has. With audio enabled it additionally
/// reassembles the Wi-Fi Display LPCM stream and queues decoded `AudioFrame`s
/// for `take_audio`; video output is never affected by audio data.
#[derive(Default)]
pub struct TransportStream {
    pmt_pid: Option<u16>,
    video_pid: Option<u16>,
    pes: Vec<u8>,
    expected_size: Option<usize>,
    continuity: Option<u8>,
    assembling: bool,
    discontinuity: bool,
    streams: Vec<StreamInfo>,
    audio: AudioState,
}

pub struct ElementaryFrame {
    pub bytes: Vec<u8>,
    pub discontinuity: bool,
}

/// One elementary stream announced by the most recent PMT.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StreamInfo {
    pub kind: u8,
    pub pid: u16,
}

/// PMT `stream_type` the receiver expects for Wi-Fi Display LPCM audio
/// (the private PCM type used by Wi-Fi Display sources).
pub const STREAM_TYPE_LPCM: u8 = 0x83;
const STREAM_TYPE_H264: u8 = 0x1b;
/// Largest audio PES kept while it is being assembled. Real LPCM PES packets
/// are a few kilobytes; this is far above that and bounds an unbounded
/// (length 0) PES. A declared length beyond it is dropped at once.
const AUDIO_PES_LIMIT: usize = 64 * 1024;
/// Decoded audio frames held for `take_audio`; the oldest are dropped beyond it.
const AUDIO_OUTPUT_LIMIT: usize = 128;
/// Leading PES bytes kept to diagnose the first rejected audio PES.
const REJECT_PREFIX: usize = 24;

impl TransportStream {
    pub fn push(&mut self, data: &[u8]) -> Result<Vec<ElementaryFrame>> {
        let mut frames = Vec::new();
        for packet in data.as_chunks::<188>().0 {
            if packet[0] != 0x47 {
                bail!("Invalid MPEG-TS synchronization");
            }
            if packet[1] & 0x80 != 0 {
                self.discard();
                continue;
            }
            let pid = (u16::from(packet[1] & 31) << 8) | u16::from(packet[2]);
            let start = packet[1] & 0x40 != 0;
            let adaptation = (packet[3] >> 4) & 3;
            if adaptation == 0 || adaptation == 2 {
                continue;
            }
            let mut offset = 4;
            if adaptation == 3 {
                offset += 1 + usize::from(packet[4]);
                if offset > 188 {
                    // A damaged audio packet must not end the video session.
                    if self.audio.enabled && Some(pid) == self.audio.pid {
                        self.audio.lose_data();
                        continue;
                    }
                    bail!("Invalid MPEG-TS adaptation length");
                }
            }
            if offset == 188 {
                continue;
            }
            let payload = &packet[offset..];
            if pid == 0 && start {
                if let Some(section) = section(payload) {
                    if section.len() >= 12 && section[0] == 0 {
                        let end = section.len().saturating_sub(4);
                        for item in section[8..end].as_chunks::<4>().0 {
                            if item[0] != 0 || item[1] != 0 {
                                self.pmt_pid =
                                    Some((u16::from(item[2] & 31) << 8) | u16::from(item[3]));
                                break;
                            }
                        }
                    }
                }
            } else if Some(pid) == self.pmt_pid && start {
                if let Some(section) = section(payload) {
                    if let Some(streams) = pmt_streams(section) {
                        self.apply_pmt(streams);
                    }
                }
            } else if Some(pid) == self.video_pid {
                let cc = packet[3] & 15;
                let broken = self.continuity.is_some_and(|last| cc != ((last + 1) & 15));
                self.continuity = Some(cc);
                if broken {
                    self.discard();
                }
                if start {
                    if self.assembling {
                        if let Some(frame) = self.finish(false)? {
                            frames.push(frame);
                        }
                    }
                    self.pes.clear();
                    self.assembling = true;
                    self.expected_size = None;
                }
                if !self.assembling {
                    continue;
                }
                self.pes.extend_from_slice(payload);
                if self.pes.len() > 4 * 1024 * 1024 {
                    self.discard();
                    bail!("Video PES exceeded its bounded buffer");
                }
                if self.pes.len() >= 6 && self.expected_size.is_none() {
                    let length = usize::from(u16::from_be_bytes([self.pes[4], self.pes[5]]));
                    if length > 0 {
                        self.expected_size = Some(length + 6);
                    }
                }
                if self
                    .expected_size
                    .is_some_and(|size| self.pes.len() >= size)
                {
                    if let Some(frame) = self.finish(broken)? {
                        frames.push(frame);
                    }
                }
            } else if self.audio.enabled && Some(pid) == self.audio.pid {
                self.audio.push(start, packet[3] & 15, payload);
            }
        }
        Ok(frames)
    }

    pub fn flush(&mut self) -> Result<Option<ElementaryFrame>> {
        self.audio.flush();
        self.finish(false)
    }

    pub fn discard(&mut self) {
        self.pes.clear();
        self.expected_size = None;
        self.assembling = false;
        self.discontinuity = true;
        self.audio.lose_data();
    }

    /// Turns audio reassembly on or off. Off (the default) ignores audio PIDs.
    pub fn set_audio_enabled(&mut self, enabled: bool) {
        self.audio.enabled = enabled;
        if !enabled {
            self.audio.reset();
            self.audio.out.clear();
        }
    }

    /// Audio frames completed since the last call, oldest first.
    pub fn take_audio(&mut self) -> Vec<AudioFrame> {
        self.audio.out.drain(..).collect()
    }

    pub fn audio_stats(&self) -> &AudioStats {
        &self.audio.stats
    }

    /// The audio PID the current PMT announced for LPCM, if any.
    pub fn audio_pid(&self) -> Option<u16> {
        self.audio.pid
    }

    /// Elementary streams of the most recent PMT, in announcement order.
    pub fn streams(&self) -> &[StreamInfo] {
        &self.streams
    }

    /// One-line PMT summary for logs, e.g. `0x1b@0x1011 0x83@0x1100`.
    pub fn describe_streams(&self) -> String {
        if self.streams.is_empty() {
            return "no PMT seen".to_owned();
        }
        let parts: Vec<String> = self
            .streams
            .iter()
            .map(|s| format!("{:#04x}@{:#06x}", s.kind, s.pid))
            .collect();
        parts.join(" ")
    }

    fn apply_pmt(&mut self, streams: Vec<StreamInfo>) {
        let video = streams.iter().rev().find(|s| s.kind == STREAM_TYPE_H264);
        if let Some(video) = video.map(|s| s.pid) {
            if self.video_pid != Some(video) {
                if self.video_pid.is_some() {
                    // The pictures moved to another PID: nothing assembled or
                    // counted on the old PID may leak into the new one.
                    self.discard();
                    self.continuity = None;
                }
                self.video_pid = Some(video);
            }
        }
        let audio = streams
            .iter()
            .find(|s| s.kind == STREAM_TYPE_LPCM)
            .map(|s| s.pid)
            .filter(|pid| {
                *pid != 0 && *pid != 0x1fff && Some(*pid) != self.pmt_pid && Some(*pid) != self.video_pid
            });
        self.audio.set_pid(audio);
        self.streams = streams;
    }

    fn finish(&mut self, discontinuity: bool) -> Result<Option<ElementaryFrame>> {
        self.assembling = false;
        let size = self.expected_size.take().unwrap_or(self.pes.len());
        if self.pes.len() < 9 || size > self.pes.len() || size < 9 {
            self.pes.clear();
            return Ok(None);
        }
        if self.pes[..3] != [0, 0, 1] || self.pes[3] & 0xf0 != 0xe0 {
            self.pes.clear();
            bail!("Invalid H.264 video PES header");
        }
        let start = 9 + usize::from(self.pes[8]);
        if start > size {
            self.pes.clear();
            bail!("Invalid PES extension length");
        }
        let bytes = self.pes[start..size].to_vec();
        self.pes.clear();
        if bytes.is_empty() {
            return Ok(None);
        }
        let pending_discontinuity = std::mem::take(&mut self.discontinuity);
        Ok(Some(ElementaryFrame {
            bytes,
            discontinuity: discontinuity || pending_discontinuity,
        }))
    }
}

fn section(payload: &[u8]) -> Option<&[u8]> {
    let start = 1 + usize::from(*payload.first()?);
    if start + 3 > payload.len() {
        return None;
    }
    let length =
        ((usize::from(payload[start + 1] & 15) << 8) | usize::from(payload[start + 2])) + 3;
    payload.get(start..start + length)
}

/// Lists the elementary streams of a PMT section, or `None` if it is not one.
fn pmt_streams(section: &[u8]) -> Option<Vec<StreamInfo>> {
    if section.len() < 16 || section[0] != 2 {
        return None;
    }
    let info = (usize::from(section[10] & 15) << 8) | usize::from(section[11]);
    let mut cursor = 12 + info;
    let end = section.len().saturating_sub(4);
    let mut streams = Vec::new();
    while cursor + 5 <= end {
        let pid = (u16::from(section[cursor + 1] & 31) << 8) | u16::from(section[cursor + 2]);
        let extra = (usize::from(section[cursor + 3] & 15) << 8) | usize::from(section[cursor + 4]);
        streams.push(StreamInfo {
            kind: section[cursor],
            pid,
        });
        cursor += 5 + extra;
    }
    Some(streams)
}

// ---------------------------------------------------------------------------
// Audio: Wi-Fi Display LPCM carried in MPEG-TS.
//
// EXPERIMENTAL / UNVERIFIED AGAINST A PHONE. The framing below follows the
// Wi-Fi Display LPCM convention as implemented by common open sources: PMT
// stream_type 0x83, a PES on private_stream_1 (0xBD), and a 4-byte LPCM header
// in front of big-endian 16-bit samples. `parse_lpcm_header` is the single place
// that knows the header layout; correct it there if a capture shows otherwise.
// ---------------------------------------------------------------------------

/// Length of the LPCM header at the start of every audio PES payload.
pub const LPCM_HEADER_LEN: usize = 4;
/// Sample frames in one LPCM access unit at 48 kHz (1/600 s, as in DVD LPCM).
pub const LPCM_SAMPLES_PER_ACCESS_UNIT: usize = 80;
/// The only LPCM mode the receiver advertises: `LPCM 00000002 00`, i.e. 48 kHz,
/// 16-bit, two channels. A phone must select from what the sink advertised.
pub const LPCM_SAMPLE_RATE: u32 = 48_000;
pub const LPCM_CHANNELS: u16 = 2;
/// 33-bit MPEG presentation time stamps run at 90 kHz and wrap here.
pub const PTS_MODULUS: u64 = 1 << 33;

/// A decoded run of interleaved 16-bit samples in host byte order.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AudioFrame {
    pub samples: Vec<i16>,
    pub sample_rate: u32,
    pub channels: u16,
    /// PES presentation time stamp in 90 kHz ticks (33-bit), if the PES had one.
    pub pts: Option<u64>,
    /// Audio before this frame was lost or abandoned (gap, damage, PMT change).
    pub discontinuity: bool,
}

impl AudioFrame {
    /// Sample frames (one sample per channel) in this run.
    pub fn frames(&self) -> usize {
        self.samples.len() / usize::from(self.channels.max(1))
    }

    pub fn duration_us(&self) -> u64 {
        if self.sample_rate == 0 {
            return 0;
        }
        self.frames() as u64 * 1_000_000 / u64::from(self.sample_rate)
    }
}

/// Why an audio PES was dropped.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AudioReject {
    /// Fewer bytes than the PES length announced, or too short to read.
    Truncated,
    /// Not a well-formed PES header.
    PesHeader,
    /// A PES stream_id that is not audio or private data.
    StreamId(u8),
    /// The LPCM header is missing, or announces a mode we did not advertise.
    LpcmHeader,
    /// Fewer sample bytes than the LPCM header announced.
    ShortPayload,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct AudioStats {
    /// Audio frames decoded.
    pub frames: u64,
    /// Sample frames (per channel) decoded.
    pub sample_frames: u64,
    /// PES packets abandoned: malformed, truncated, oversized or interrupted.
    pub dropped_pes: u64,
    /// Continuity-counter gaps seen on the audio PID.
    pub continuity_errors: u64,
    /// Decoded frames discarded because `take_audio` was not called in time.
    pub overflowed: u64,
    /// The first rejected PES: reason and its leading bytes, for diagnosis.
    pub first_reject: Option<(AudioReject, Vec<u8>)>,
}

/// The LPCM header at the start of an audio PES payload.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LpcmHeader {
    pub access_units: usize,
    pub sample_rate: u32,
    pub channels: u16,
}

impl LpcmHeader {
    /// Bytes of 16-bit sample data this header announces.
    pub fn payload_len(&self) -> usize {
        self.access_units * LPCM_SAMPLES_PER_ACCESS_UNIT * usize::from(self.channels) * 2
    }
}

/// Parses the 4-byte LPCM header. Layout (unverified against hardware):
/// byte 0 sub-stream id `0xA0..=0xA7`; byte 1 number of access units;
/// byte 2 reserved; byte 3 = quantization (2 bits, 0 = 16-bit), sampling
/// frequency (3 bits, 2 = 48 kHz), channels (3 bits, 1 = stereo).
/// Only the advertised mode is accepted.
pub fn parse_lpcm_header(bytes: &[u8]) -> Option<LpcmHeader> {
    let &[sub_stream, access_units, _reserved, format] = bytes.first_chunk::<LPCM_HEADER_LEN>()?;
    if sub_stream & 0xf8 != 0xa0 || access_units == 0 {
        return None;
    }
    if format >> 6 != 0 || (format >> 3) & 7 != 2 || format & 7 != 1 {
        return None;
    }
    Some(LpcmHeader {
        access_units: usize::from(access_units),
        sample_rate: LPCM_SAMPLE_RATE,
        channels: LPCM_CHANNELS,
    })
}

/// Converts big-endian 16-bit PCM bytes to host-order samples (an odd trailing
/// byte is ignored).
pub fn pcm_be16_to_samples(bytes: &[u8]) -> Vec<i16> {
    bytes
        .chunks_exact(2)
        .map(|pair| i16::from_be_bytes([pair[0], pair[1]]))
        .collect()
}

/// Reads a 33-bit PTS/DTS from its 5-byte PES encoding.
pub fn parse_timestamp(bytes: &[u8; 5]) -> u64 {
    (u64::from(bytes[0] >> 1) & 7) << 30
        | u64::from(bytes[1]) << 22
        | u64::from(bytes[2] >> 1) << 15
        | u64::from(bytes[3]) << 7
        | u64::from(bytes[4] >> 1)
}

/// Signed difference `later - earlier` of two 33-bit timestamps, correct across
/// a wrap, in 90 kHz ticks. Differences beyond half the range read as negative.
pub fn pts_delta(later: u64, earlier: u64) -> i64 {
    let diff = later.wrapping_sub(earlier) & (PTS_MODULUS - 1);
    if diff >= PTS_MODULUS / 2 {
        diff as i64 - PTS_MODULUS as i64
    } else {
        diff as i64
    }
}

/// A PES header's fields that matter here.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PesHeader {
    pub stream_id: u8,
    pub pts: Option<u64>,
    /// Offset of the first payload byte.
    pub payload_start: usize,
}

/// Parses the optional-header form of a PES header (`pes` must start at the
/// `00 00 01` start code).
pub fn parse_pes_header(pes: &[u8]) -> Option<PesHeader> {
    if pes.len() < 9 || pes[..3] != [0, 0, 1] || pes[6] & 0xc0 != 0x80 {
        return None;
    }
    let header_len = usize::from(pes[8]);
    let payload_start = 9 + header_len;
    if payload_start > pes.len() {
        return None;
    }
    let pts = if pes[7] & 0x80 != 0 && header_len >= 5 {
        pes.get(9..14)
            .and_then(|bytes| <&[u8; 5]>::try_from(bytes).ok())
            .map(parse_timestamp)
    } else {
        None
    };
    Some(PesHeader {
        stream_id: pes[3],
        pts,
        payload_start,
    })
}

fn audio_stream_id(id: u8) -> bool {
    id == 0xbd || (0xc0..=0xdf).contains(&id)
}

/// Decodes one complete audio PES (start code through the last byte received).
/// A non-zero PES length is honoured; zero means "to the end of `pes`".
pub fn decode_audio_pes(pes: &[u8]) -> Result<AudioFrame, AudioReject> {
    let declared = match pes.get(4..6) {
        Some(bytes) => usize::from(u16::from_be_bytes([bytes[0], bytes[1]])),
        None => return Err(AudioReject::Truncated),
    };
    let size = if declared == 0 { pes.len() } else { declared + 6 };
    let pes = pes.get(..size).ok_or(AudioReject::Truncated)?;
    let header = parse_pes_header(pes).ok_or(AudioReject::PesHeader)?;
    if !audio_stream_id(header.stream_id) {
        return Err(AudioReject::StreamId(header.stream_id));
    }
    let payload = pes.get(header.payload_start..).ok_or(AudioReject::PesHeader)?;
    let lpcm = parse_lpcm_header(payload).ok_or(AudioReject::LpcmHeader)?;
    let data = payload
        .get(LPCM_HEADER_LEN..LPCM_HEADER_LEN + lpcm.payload_len())
        .ok_or(AudioReject::ShortPayload)?;
    Ok(AudioFrame {
        samples: pcm_be16_to_samples(data),
        sample_rate: lpcm.sample_rate,
        channels: lpcm.channels,
        pts: header.pts,
        discontinuity: false,
    })
}

/// Reassembly of the audio PES plus the bounded output queue.
#[derive(Default)]
struct AudioState {
    enabled: bool,
    pid: Option<u16>,
    pes: Vec<u8>,
    expected: Option<usize>,
    continuity: Option<u8>,
    assembling: bool,
    discontinuity: bool,
    out: VecDeque<AudioFrame>,
    stats: AudioStats,
}

impl AudioState {
    fn set_pid(&mut self, pid: Option<u16>) {
        if self.pid != pid {
            // Moving away from a stream that was running is a gap; the first
            // announcement of one is not.
            if self.pid.is_some() {
                self.discontinuity = true;
            }
            self.pid = pid;
            self.reset();
            self.continuity = None;
        }
    }

    fn reset(&mut self) {
        self.pes.clear();
        self.expected = None;
        self.assembling = false;
    }

    /// Abandons a partly assembled PES and flags the next frame as following a gap.
    fn lose_data(&mut self) {
        if self.assembling {
            self.stats.dropped_pes += 1;
        }
        self.reset();
        self.discontinuity = true;
    }

    fn reject(&mut self, reason: AudioReject) {
        self.stats.dropped_pes += 1;
        self.discontinuity = true;
        if self.stats.first_reject.is_none() {
            let prefix = self.pes.iter().copied().take(REJECT_PREFIX).collect();
            self.stats.first_reject = Some((reason, prefix));
        }
    }

    fn push(&mut self, start: bool, cc: u8, payload: &[u8]) {
        let broken = self.continuity.is_some_and(|last| cc != ((last + 1) & 15));
        self.continuity = Some(cc);
        if broken {
            self.stats.continuity_errors += 1;
            self.lose_data();
        }
        if start {
            if self.assembling {
                self.complete();
            }
            self.reset();
            self.assembling = true;
        }
        if !self.assembling {
            return;
        }
        if self.pes.len() + payload.len() > AUDIO_PES_LIMIT {
            self.lose_data();
            return;
        }
        self.pes.extend_from_slice(payload);
        if self.expected.is_none() && self.pes.len() >= 6 {
            let length = usize::from(u16::from_be_bytes([self.pes[4], self.pes[5]]));
            if length > 0 {
                if length + 6 > AUDIO_PES_LIMIT {
                    self.lose_data();
                    return;
                }
                self.expected = Some(length + 6);
            }
        }
        if self.expected.is_some_and(|size| self.pes.len() >= size) {
            self.complete();
        }
    }

    /// Finishes whatever PES is being assembled (a zero-length PES ends here).
    fn flush(&mut self) {
        if self.assembling {
            self.complete();
        }
    }

    fn complete(&mut self) {
        self.assembling = false;
        self.expected = None;
        match decode_audio_pes(&self.pes) {
            Ok(mut frame) => {
                frame.discontinuity = std::mem::take(&mut self.discontinuity);
                self.stats.frames += 1;
                self.stats.sample_frames += frame.frames() as u64;
                if self.out.len() >= AUDIO_OUTPUT_LIMIT {
                    self.out.pop_front();
                    self.stats.overflowed += 1;
                }
                self.out.push_back(frame);
            }
            Err(reason) => self.reject(reason),
        }
        self.pes.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn packet(pid: u16, start: bool, cc: u8, data: &[u8]) -> [u8; 188] {
        let mut p = [0xff; 188];
        p[0] = 0x47;
        p[1] = ((pid >> 8) as u8 & 31) | if start { 0x40 } else { 0 };
        p[2] = pid as u8;
        let padding = 184 - data.len();
        if padding > 0 {
            p[3] = 0x30 | cc;
            p[4] = (padding - 1) as u8;
            p[5..4 + padding].fill(0);
            p[4 + padding..].copy_from_slice(data);
        } else {
            p[3] = 0x10 | cc;
            p[4..].copy_from_slice(data);
        }
        p
    }
    #[test]
    fn complete_video_pes_is_not_delayed_until_another_frame() {
        let mut ts = TransportStream {
            video_pid: Some(0x1011),
            ..Default::default()
        };
        let pes = [0, 0, 1, 0xe0, 0, 9, 0x80, 0, 0, 0, 0, 1, 0x65, 0x88, 0x80];
        let result = ts.push(&packet(0x1011, true, 0, &pes)).unwrap();
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].bytes, &pes[9..]);
        assert!(ts.push(&packet(0x1011, true, 2, &pes)).unwrap()[0].discontinuity);
        assert!(!ts.push(&packet(0x1011, true, 3, &pes)).unwrap()[0].discontinuity);
    }
    #[test]
    fn continuity_loss_does_not_inject_a_partial_picture() {
        let mut ts = TransportStream {
            video_pid: Some(0x1011),
            ..Default::default()
        };
        ts.push(&packet(
            0x1011,
            true,
            0,
            &[0, 0, 1, 0xe0, 0, 0, 0x80, 0, 0, 0, 0, 1],
        ))
        .unwrap();
        assert!(ts
            .push(&packet(0x1011, false, 2, &[0x65, 1, 2]))
            .unwrap()
            .is_empty());
        assert!(ts.flush().unwrap().is_none());
    }
    #[test]
    fn unbounded_pes_keeps_discontinuity_until_the_next_complete_picture() {
        let mut ts = TransportStream {
            video_pid: Some(0x1011),
            ..Default::default()
        };
        let pes = [0, 0, 1, 0xe0, 0, 0, 0x80, 0, 0, 0, 0, 1, 0x65];
        assert!(ts.push(&packet(0x1011, true, 0, &pes)).unwrap().is_empty());
        assert!(ts.push(&packet(0x1011, true, 2, &pes)).unwrap().is_empty());
        let frames = ts.push(&packet(0x1011, true, 3, &pes)).unwrap();
        assert_eq!(frames.len(), 1);
        assert!(frames[0].discontinuity);
        assert!(!ts.flush().unwrap().unwrap().discontinuity);
    }

    // ----- builders for the audio and PMT tests -----

    const PMT_PID: u16 = 0x100;
    const VIDEO: u16 = 0x1011;
    const AUDIO: u16 = 0x1100;

    fn encode_pts(pts: u64) -> [u8; 5] {
        [
            0x21 | ((((pts >> 30) & 7) as u8) << 1),
            (pts >> 22) as u8,
            ((((pts >> 15) & 0x7f) as u8) << 1) | 1,
            (pts >> 7) as u8,
            (((pts & 0x7f) as u8) << 1) | 1,
        ]
    }

    /// A PES with the optional header; `bounded` fills the 16-bit length.
    fn pes(stream_id: u8, pts: Option<u64>, payload: &[u8], bounded: bool) -> Vec<u8> {
        let mut out = vec![0, 0, 1, stream_id, 0, 0, 0x80, if pts.is_some() { 0x80 } else { 0 }];
        match pts {
            Some(value) => {
                out.push(5);
                out.extend_from_slice(&encode_pts(value));
            }
            None => out.push(0),
        }
        out.extend_from_slice(payload);
        if bounded {
            let length = (out.len() - 6) as u16;
            out[4..6].copy_from_slice(&length.to_be_bytes());
        }
        out
    }

    fn lpcm_payload(access_units: u8, first: i16) -> Vec<u8> {
        let mut out = vec![0xa0, access_units, 0, 0x11];
        for i in 0..usize::from(access_units) * 160 {
            out.extend_from_slice(&first.wrapping_add(i as i16).to_be_bytes());
        }
        out
    }

    fn lpcm_pes(pts: Option<u64>, access_units: u8, first: i16) -> Vec<u8> {
        pes(0xbd, pts, &lpcm_payload(access_units, first), true)
    }

    fn ts_packets(pid: u16, cc: u8, pes: &[u8]) -> Vec<[u8; 188]> {
        pes.chunks(184)
            .enumerate()
            .map(|(i, chunk)| packet(pid, i == 0, ((usize::from(cc) + i) & 15) as u8, chunk))
            .collect()
    }

    fn psi(pid: u16, table_id: u8, body: &[u8]) -> [u8; 188] {
        let length = body.len() + 4;
        let mut data = vec![0, table_id, 0xb0 | (length >> 8) as u8, length as u8];
        data.extend_from_slice(body);
        data.extend_from_slice(&[0; 4]);
        packet(pid, true, 0, &data)
    }

    fn pat() -> [u8; 188] {
        psi(0, 0, &[0, 1, 0xc1, 0, 0, 0, 1, 0xe0 | (PMT_PID >> 8) as u8, PMT_PID as u8])
    }

    fn pmt(streams: &[(u8, u16)]) -> [u8; 188] {
        let mut body = vec![0, 1, 0xc1, 0, 0, 0xe0 | (VIDEO >> 8) as u8, VIDEO as u8, 0xf0, 0];
        for &(kind, pid) in streams {
            body.extend_from_slice(&[kind, 0xe0 | (pid >> 8) as u8, pid as u8, 0xf0, 0]);
        }
        psi(PMT_PID, 2, &body)
    }

    fn tuned(streams: &[(u8, u16)]) -> TransportStream {
        let mut ts = TransportStream::default();
        ts.set_audio_enabled(true);
        ts.push(&pat()).unwrap();
        ts.push(&pmt(streams)).unwrap();
        ts
    }

    const BOTH: [(u8, u16); 2] = [(0x1b, VIDEO), (0x83, AUDIO)];
    const VIDEO_PES: [u8; 15] = [0, 0, 1, 0xe0, 0, 9, 0x80, 0, 0, 0, 0, 1, 0x65, 0x88, 0x80];

    fn samples(first: i16, count: usize) -> Vec<i16> {
        (0..count).map(|i| first.wrapping_add(i as i16)).collect()
    }

    // ----- audio -----

    #[test]
    fn lpcm_header_layout_is_one_small_function() {
        let ok = parse_lpcm_header(&[0xa0, 3, 0, 0x11]).unwrap();
        assert_eq!((ok.access_units, ok.sample_rate, ok.channels), (3, 48_000, 2));
        assert_eq!(ok.payload_len(), 3 * 80 * 2 * 2);
        assert!(parse_lpcm_header(&[0xa7, 1, 0xff, 0x11]).is_some());
        assert!(parse_lpcm_header(&[0xa0, 1, 0]).is_none(), "truncated");
        assert!(parse_lpcm_header(&[]).is_none());
        assert!(parse_lpcm_header(&[0xb0, 1, 0, 0x11]).is_none(), "wrong sub-stream");
        assert!(parse_lpcm_header(&[0xa0, 0, 0, 0x11]).is_none(), "no access units");
        assert!(parse_lpcm_header(&[0xa0, 1, 0, 0x51]).is_none(), "not 16-bit");
        assert!(parse_lpcm_header(&[0xa0, 1, 0, 0x09]).is_none(), "44.1 kHz was not advertised");
        assert!(parse_lpcm_header(&[0xa0, 1, 0, 0x12]).is_none(), "not stereo");
    }

    #[test]
    fn big_endian_samples_become_host_order() {
        assert_eq!(
            pcm_be16_to_samples(&[0x12, 0x34, 0xff, 0xfe, 0x80, 0x00, 0x7f, 0xff, 0x55]),
            vec![0x1234, -2, i16::MIN, i16::MAX]
        );
        assert!(pcm_be16_to_samples(&[1]).is_empty());
    }

    #[test]
    fn pes_header_and_timestamps() {
        let max = PTS_MODULUS - 1;
        assert_eq!(parse_timestamp(&encode_pts(max)), max);
        assert_eq!(parse_timestamp(&encode_pts(0)), 0);
        assert_eq!(parse_timestamp(&encode_pts(0x1_2345_6789)), 0x1_2345_6789);
        let header = parse_pes_header(&pes(0xbd, Some(90_000), &[1, 2, 3], true)).unwrap();
        assert_eq!((header.stream_id, header.pts, header.payload_start), (0xbd, Some(90_000), 14));
        let bare = parse_pes_header(&pes(0xbd, None, &[9], true)).unwrap();
        assert_eq!((bare.pts, bare.payload_start), (None, 9));
        assert!(parse_pes_header(&[0, 0, 1, 0xbd, 0, 3, 0x80, 0, 0]).is_some());
        assert!(parse_pes_header(&[0, 0, 1, 0xbd, 0, 3, 0x80, 0, 1]).is_none(), "header past the end");
        assert!(parse_pes_header(&[0, 0, 1, 0xbd, 0, 3, 0x40, 0, 0]).is_none(), "marker bits");
        assert!(parse_pes_header(&[0, 0, 2, 0xbd, 0, 3, 0x80, 0, 0]).is_none(), "start code");
        assert!(parse_pes_header(&[0, 0, 1, 0xbd]).is_none());
    }

    #[test]
    fn timestamp_differences_survive_the_33_bit_wrap() {
        assert_eq!(pts_delta(100, 40), 60);
        assert_eq!(pts_delta(40, 100), -60);
        assert_eq!(pts_delta(5, PTS_MODULUS - 5), 10);
        assert_eq!(pts_delta(PTS_MODULUS - 5, 5), -10);
        assert_eq!(pts_delta(7, 7), 0);
    }

    #[test]
    fn interleaved_video_and_audio_yield_both() {
        let mut ts = tuned(&BOTH);
        let first = lpcm_pes(Some(9000), 2, 100);
        let second = lpcm_pes(Some(9300), 1, -50);
        let mut a = ts_packets(AUDIO, 0, &first);
        a.extend(ts_packets(AUDIO, a.len() as u8, &second));
        let mut stream = Vec::new();
        let mut video_cc = 0;
        for (i, audio_packet) in a.iter().enumerate() {
            stream.extend_from_slice(audio_packet);
            if i % 2 == 0 {
                stream.extend_from_slice(&packet(VIDEO, true, video_cc, &VIDEO_PES));
                video_cc = (video_cc + 1) & 15;
            }
        }
        let pictures = ts.push(&stream).unwrap();
        assert_eq!(pictures.len(), (a.len() + 1) / 2);
        assert!(pictures.iter().all(|p| p.bytes == VIDEO_PES[9..]));
        let audio = ts.take_audio();
        assert_eq!(audio.len(), 2);
        assert_eq!(audio[0].samples, samples(100, 320));
        assert_eq!((audio[0].sample_rate, audio[0].channels, audio[0].pts), (48_000, 2, Some(9000)));
        assert_eq!(audio[0].frames(), 160);
        assert_eq!(audio[0].duration_us(), 160 * 1_000_000 / 48_000);
        assert!(!audio[0].discontinuity, "the start of a stream is not a gap");
        assert_eq!(audio[1].samples, samples(-50, 160));
        assert_eq!(audio[1].pts, Some(9300));
        assert!(ts.take_audio().is_empty());
        let stats = ts.audio_stats();
        assert_eq!((stats.frames, stats.sample_frames, stats.dropped_pes), (2, 240, 0));
        assert_eq!(ts.describe_streams(), "0x1b@0x1011 0x83@0x1100");
    }

    #[test]
    fn audio_alone_without_video() {
        let mut ts = tuned(&[(0x83, AUDIO)]);
        let stream = ts_packets(AUDIO, 0, &lpcm_pes(None, 1, 7)).concat();
        assert!(ts.push(&stream).unwrap().is_empty());
        let audio = ts.take_audio();
        assert_eq!(audio.len(), 1);
        assert_eq!(audio[0].pts, None);
        assert_eq!(audio[0].samples, samples(7, 160));
    }

    #[test]
    fn audio_is_ignored_unless_enabled() {
        let mut ts = TransportStream::default();
        ts.push(&pat()).unwrap();
        ts.push(&pmt(&BOTH)).unwrap();
        let mut stream = ts_packets(AUDIO, 0, &lpcm_pes(Some(1), 1, 0)).concat();
        stream.extend_from_slice(&packet(VIDEO, true, 0, &VIDEO_PES));
        assert_eq!(ts.push(&stream).unwrap().len(), 1);
        assert!(ts.take_audio().is_empty());
        assert_eq!(ts.audio_stats(), &AudioStats::default());
        // Even a damaged audio packet still ends the session, as it always did.
        let mut bad = packet(AUDIO, false, 5, &[1, 2, 3]);
        bad[3] = 0x30 | 5;
        bad[4] = 200;
        assert!(ts.push(&bad).is_err());
    }

    #[test]
    fn truncated_audio_header_is_dropped_and_flags_a_gap() {
        let mut ts = tuned(&BOTH);
        let short = pes(0xbd, Some(1), &[0xa0, 1, 0], true);
        let mut stream = ts_packets(AUDIO, 0, &short).concat();
        stream.extend_from_slice(&packet(VIDEO, true, 0, &VIDEO_PES));
        assert_eq!(ts.push(&stream).unwrap().len(), 1, "video is undisturbed");
        assert!(ts.take_audio().is_empty());
        let stats = ts.audio_stats();
        assert_eq!(stats.dropped_pes, 1);
        let (reason, prefix) = stats.first_reject.clone().unwrap();
        assert_eq!(reason, AudioReject::LpcmHeader);
        assert_eq!(&prefix[..4], &[0, 0, 1, 0xbd]);
        ts.push(&ts_packets(AUDIO, 1, &lpcm_pes(Some(2), 1, 1)).concat()).unwrap();
        let audio = ts.take_audio();
        assert_eq!(audio.len(), 1);
        assert!(audio[0].discontinuity);
    }

    #[test]
    fn short_payload_excess_payload_and_foreign_stream_ids() {
        let mut ts = tuned(&BOTH);
        let mut cc = 0;
        let mut send = |ts: &mut TransportStream, bytes: &[u8]| {
            let packets = ts_packets(AUDIO, cc, bytes);
            cc = (cc + packets.len() as u8) & 15;
            ts.push(&packets.concat()).unwrap();
        };
        // One access unit announced, but only part of it present.
        let mut payload = lpcm_payload(1, 0);
        payload.truncate(4 + 100);
        send(&mut ts, &pes(0xbd, None, &payload, true));
        assert_eq!(ts.audio_stats().first_reject.as_ref().map(|r| r.0), Some(AudioReject::ShortPayload));
        // Trailing bytes beyond the announced access units are ignored.
        let mut long = lpcm_payload(1, 5);
        long.extend_from_slice(&[0xee; 64]);
        send(&mut ts, &pes(0xbd, None, &long, true));
        // A video stream id on the audio PID is not audio.
        send(&mut ts, &pes(0xe0, None, &lpcm_payload(1, 0), true));
        // Generic audio stream ids are accepted.
        send(&mut ts, &pes(0xc0, None, &lpcm_payload(1, 9), true));
        let audio = ts.take_audio();
        assert_eq!(audio.len(), 2);
        assert_eq!(audio[0].samples, samples(5, 160));
        assert_eq!(audio[1].samples, samples(9, 160));
        assert_eq!(ts.audio_stats().dropped_pes, 2);
        assert!(decode_audio_pes(&[0, 0, 1]).is_err());
    }

    #[test]
    fn non_lpcm_stream_types_are_ignored() {
        for kind in [0x0f, 0x06, 0x81, 0x03, 0x04] {
            let mut ts = tuned(&[(0x1b, VIDEO), (kind, AUDIO)]);
            assert_eq!(ts.audio_pid(), None, "type {kind:#x}");
            let mut stream = ts_packets(AUDIO, 0, &lpcm_pes(Some(1), 1, 0)).concat();
            stream.extend_from_slice(&packet(VIDEO, true, 0, &VIDEO_PES));
            assert_eq!(ts.push(&stream).unwrap().len(), 1);
            assert!(ts.take_audio().is_empty());
            assert_eq!(ts.audio_stats(), &AudioStats::default());
        }
    }

    #[test]
    fn audio_pid_may_not_collide_with_other_pids() {
        for pid in [0, 0x1fff, PMT_PID, VIDEO] {
            assert_eq!(tuned(&[(0x1b, VIDEO), (0x83, pid)]).audio_pid(), None, "pid {pid:#x}");
        }
    }

    #[test]
    fn oversized_audio_pes_is_dropped_without_growing() {
        let mut ts = tuned(&BOTH);
        // A declared length beyond the limit is refused at once.
        let mut huge = pes(0xbd, None, &lpcm_payload(1, 0), false);
        huge[4..6].copy_from_slice(&0xffffu16.to_be_bytes());
        let packets = ts_packets(AUDIO, 0, &huge);
        ts.push(&packets.concat()).unwrap();
        assert!(ts.audio.pes.is_empty() && !ts.audio.assembling);
        // An unbounded PES that never ends cannot grow without bound either.
        let endless = ts_packets(AUDIO, 2, &pes(0xbd, None, &vec![0x11; 184 * 400], false));
        for chunk in &endless {
            ts.push(chunk).unwrap();
            assert!(ts.audio.pes.len() <= AUDIO_PES_LIMIT);
        }
        assert!(ts.take_audio().is_empty());
        assert_eq!(ts.audio_stats().dropped_pes, 2, "one declared, one endless");
        assert_eq!(ts.audio_stats().continuity_errors, 0);
        // The very next well-formed PES still plays, flagged as following a gap.
        let cc = ((2 + endless.len()) & 15) as u8;
        ts.push(&ts_packets(AUDIO, cc, &lpcm_pes(None, 1, 3)).concat()).unwrap();
        let audio = ts.take_audio();
        assert_eq!(audio.len(), 1);
        assert!(audio[0].discontinuity);
    }

    #[test]
    fn audio_continuity_gap_drops_the_partial_pes_only() {
        let mut ts = tuned(&BOTH);
        let first = ts_packets(AUDIO, 0, &lpcm_pes(Some(1), 2, 0));
        assert!(first.len() >= 3);
        let mut stream = first[0].to_vec();
        stream.extend_from_slice(&packet(VIDEO, true, 0, &VIDEO_PES));
        stream.extend_from_slice(&first[2]); // packet 1 went missing
        stream.extend_from_slice(&packet(VIDEO, true, 1, &VIDEO_PES));
        assert_eq!(ts.push(&stream).unwrap().len(), 2, "video continues");
        assert!(ts.take_audio().is_empty());
        assert_eq!(ts.audio_stats().continuity_errors, 1);
        assert_eq!(ts.audio_stats().dropped_pes, 1);
        let resume = ts_packets(AUDIO, 3, &lpcm_pes(Some(2), 1, 8));
        ts.push(&resume.concat()).unwrap();
        let audio = ts.take_audio();
        assert_eq!(audio.len(), 1);
        assert!(audio[0].discontinuity);
        // The flag is consumed by exactly one frame.
        ts.push(&ts_packets(AUDIO, 3 + resume.len() as u8, &lpcm_pes(Some(3), 1, 8)).concat()).unwrap();
        assert!(!ts.take_audio()[0].discontinuity);
    }

    #[test]
    fn damaged_audio_packet_does_not_end_the_session() {
        let mut ts = tuned(&BOTH);
        let mut bad = packet(AUDIO, false, 0, &[1, 2, 3]);
        bad[3] = 0x30;
        bad[4] = 200; // adaptation field longer than the packet
        let mut stream = bad.to_vec();
        stream.extend_from_slice(&packet(VIDEO, true, 0, &VIDEO_PES));
        assert_eq!(ts.push(&stream).unwrap().len(), 1);
        // A damaged video packet is still an error, as before.
        let mut bad_video = packet(VIDEO, false, 1, &[1, 2, 3]);
        bad_video[3] = 0x31;
        bad_video[4] = 200;
        assert!(ts.push(&bad_video).is_err());
    }

    #[test]
    fn transport_errors_and_rtp_gaps_abandon_audio_too() {
        let mut ts = tuned(&BOTH);
        let packets = ts_packets(AUDIO, 0, &lpcm_pes(Some(1), 2, 0));
        assert_eq!(packets.len(), 4);
        // The receiver calls discard() when RTP packets went missing.
        ts.push(&packets[0]).unwrap();
        ts.discard();
        for p in &packets[1..] {
            ts.push(p).unwrap();
        }
        assert!(ts.take_audio().is_empty(), "the half PES must not be completed");
        ts.push(&ts_packets(AUDIO, 4, &lpcm_pes(Some(2), 1, 0)).concat()).unwrap();
        assert!(ts.take_audio()[0].discontinuity);
        ts.push(&ts_packets(AUDIO, 6, &lpcm_pes(Some(3), 1, 0)).concat()).unwrap();
        assert!(!ts.take_audio()[0].discontinuity);
        // A packet flagged in error is untrustworthy, whatever PID it names.
        let mut flagged = packet(0x0555, true, 0, &[0; 10]);
        flagged[1] |= 0x80;
        ts.push(&flagged).unwrap();
        ts.push(&ts_packets(AUDIO, 8, &lpcm_pes(Some(4), 1, 0)).concat()).unwrap();
        assert!(ts.take_audio()[0].discontinuity);
    }

    #[test]
    fn pmt_changes_move_or_remove_the_audio_stream() {
        let mut ts = tuned(&BOTH);
        assert_eq!(ts.audio_pid(), Some(AUDIO));
        ts.push(&pmt(&[(0x1b, VIDEO), (0x83, 0x1101)])).unwrap();
        assert_eq!(ts.audio_pid(), Some(0x1101));
        // The old PID is no longer audio.
        ts.push(&ts_packets(AUDIO, 0, &lpcm_pes(Some(1), 1, 0)).concat()).unwrap();
        assert!(ts.take_audio().is_empty());
        ts.push(&ts_packets(0x1101, 0, &lpcm_pes(Some(1), 1, 0)).concat()).unwrap();
        let audio = ts.take_audio();
        assert_eq!(audio.len(), 1);
        assert!(audio[0].discontinuity, "the stream moved");
        // An identical repeat of the PMT changes nothing.
        ts.push(&pmt(&[(0x1b, VIDEO), (0x83, 0x1101)])).unwrap();
        ts.push(&ts_packets(0x1101, 2, &lpcm_pes(Some(2), 1, 0)).concat()).unwrap();
        assert!(!ts.take_audio()[0].discontinuity);
        ts.push(&pmt(&[(0x1b, VIDEO)])).unwrap();
        assert_eq!(ts.audio_pid(), None);
        assert_eq!(ts.video_pid, Some(VIDEO));
        ts.push(&ts_packets(0x1101, 4, &lpcm_pes(Some(3), 1, 0)).concat()).unwrap();
        assert!(ts.take_audio().is_empty());
    }

    #[test]
    fn zero_length_audio_pes_ends_at_the_next_start_or_flush() {
        let mut ts = tuned(&BOTH);
        let unbounded = |first| pes(0xbd, Some(1), &lpcm_payload(1, first), false);
        assert!(ts.push(&ts_packets(AUDIO, 0, &unbounded(1)).concat()).unwrap().is_empty());
        assert!(ts.take_audio().is_empty(), "not complete until something ends it");
        let next = ts_packets(AUDIO, 2, &unbounded(2));
        ts.push(&next.concat()).unwrap();
        let audio = ts.take_audio();
        assert_eq!(audio.len(), 1);
        assert_eq!(audio[0].samples, samples(1, 160));
        assert!(ts.flush().unwrap().is_none());
        let audio = ts.take_audio();
        assert_eq!(audio.len(), 1, "flush completes the last one");
        assert_eq!(audio[0].samples, samples(2, 160));
    }

    #[test]
    fn audio_output_is_bounded_when_nobody_takes_it() {
        let mut ts = tuned(&BOTH);
        let mut cc = 0u8;
        for i in 0..(AUDIO_OUTPUT_LIMIT + 20) {
            let packets = ts_packets(AUDIO, cc, &lpcm_pes(None, 1, i as i16));
            cc = (cc + packets.len() as u8) & 15;
            ts.push(&packets.concat()).unwrap();
        }
        assert_eq!(ts.audio.out.len(), AUDIO_OUTPUT_LIMIT);
        assert_eq!(ts.audio_stats().overflowed, 20);
        let audio = ts.take_audio();
        assert_eq!(audio[0].samples[0], 20, "the oldest frames were dropped");
    }

    #[test]
    fn disabling_audio_clears_what_was_queued() {
        let mut ts = tuned(&BOTH);
        ts.push(&ts_packets(AUDIO, 0, &lpcm_pes(None, 1, 0)).concat()).unwrap();
        ts.set_audio_enabled(false);
        assert!(ts.take_audio().is_empty());
    }

    // ----- PMT handling -----

    #[test]
    fn video_pid_change_does_not_continue_the_old_picture() {
        let mut ts = tuned(&BOTH);
        // 20 bytes of PES (length field 14), only the first 12 sent on the old PID.
        let mut whole = vec![0, 0, 1, 0xe0, 0, 14, 0x80, 0, 0];
        whole.extend_from_slice(&[0, 0, 0, 1, 0x65, 1, 2, 3, 4, 5, 6]);
        assert!(ts.push(&packet(VIDEO, true, 4, &whole[..12])).unwrap().is_empty());
        ts.push(&pmt(&[(0x1b, 0x1012), (0x83, AUDIO)])).unwrap();
        // The remainder arrives on the new PID with the "next" counter value.
        let result = ts.push(&packet(0x1012, false, 5, &whole[12..])).unwrap();
        assert!(result.is_empty(), "bytes of two PIDs must not be glued together");
        // A fresh picture on the new PID decodes and is flagged as a discontinuity.
        let result = ts.push(&packet(0x1012, true, 9, &VIDEO_PES)).unwrap();
        assert_eq!(result.len(), 1);
        assert!(result[0].discontinuity);
        assert!(!ts.push(&packet(0x1012, true, 10, &VIDEO_PES)).unwrap()[0].discontinuity);
    }

    #[test]
    fn unchanged_pmt_repeats_do_not_disturb_video() {
        let mut ts = tuned(&BOTH);
        let start = [0, 0, 1, 0xe0, 0, 14, 0x80, 0, 0, 0, 0, 0, 1, 0x65];
        assert!(ts.push(&packet(VIDEO, true, 0, &start)).unwrap().is_empty());
        ts.push(&pmt(&BOTH)).unwrap();
        let rest = ts.push(&packet(VIDEO, false, 1, &[1, 2, 3, 4, 5, 6])).unwrap();
        assert_eq!(rest.len(), 1);
        assert!(!rest[0].discontinuity);
    }

    #[test]
    fn hostile_psi_never_panics() {
        let mut ts = TransportStream::default();
        ts.set_audio_enabled(true);
        for fill in [0u8, 0xff, 0x47, 0x55] {
            for pid in [0u16, PMT_PID, VIDEO, AUDIO, 0x1fff] {
                for control in 0..4u8 {
                    let mut p = [fill; 188];
                    p[0] = 0x47;
                    p[1] = 0x40 | (pid >> 8) as u8;
                    p[2] = pid as u8;
                    p[3] = control << 4;
                    // Errors are fine; panics are not.
                    let _ = ts.push(&p);
                }
            }
        }
        ts.push(&pat()).unwrap();
        for len in 0..40usize {
            let mut body = vec![0, 1, 0xc1, 0, 0, 0xff, 0xff, 0xff, 0xff];
            body.extend(std::iter::repeat(0x83).take(len));
            let _ = ts.push(&psi(PMT_PID, 2, &body));
        }
        let _ = ts.flush();
        let _ = ts.take_audio();
    }
}
