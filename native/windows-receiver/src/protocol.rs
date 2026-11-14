use anyhow::{bail, Context, Result};
use std::collections::{BTreeMap, HashMap};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct VideoMode {
    pub width: u32,
    pub height: u32,
    pub fps: u32,
}

impl VideoMode {
    pub fn from_wfd(value: &str) -> Result<Self> {
        let fields: Vec<_> = value.split_whitespace().collect();
        if fields.len() < 7 {
            bail!("The phone sent an incomplete video format");
        }
        let cea = u32::from_str_radix(fields[4], 16)?;
        let vesa = u32::from_str_radix(fields[5], 16)?;
        let hh = u32::from_str_radix(fields[6], 16)?;
        if vesa != 0 || hh != 0 {
            bail!("The phone selected an unsupported non-CEA video format");
        }
        let (width, height, fps) = match cea {
            1 => (640, 480, 60),
            0x20 => (1280, 720, 30),
            0x40 => (1280, 720, 60),
            0x80 => (1920, 1080, 30),
            0x100 => (1920, 1080, 60),
            0x400 => (1280, 720, 25),
            0x800 => (1280, 720, 50),
            0x1000 => (1920, 1080, 25),
            0x2000 => (1920, 1080, 50),
            0x8000 => (1280, 720, 24),
            0x10000 => (1920, 1080, 24),
            _ => bail!("The phone selected an ambiguous/unsupported CEA mode: {cea:08x}"),
        };
        Ok(Self { width, height, fps })
    }
}

#[derive(Debug)]
pub struct RtspMessage {
    pub start: String,
    pub headers: HashMap<String, String>,
    pub body: String,
}

impl RtspMessage {
    pub fn sequence(&self) -> Result<u32> {
        self.headers
            .get("cseq")
            .context("Missing RTSP sequence")?
            .parse()
            .context("Invalid RTSP sequence")
    }
    pub fn response(&self) -> bool {
        self.start.starts_with("RTSP/")
    }
    pub fn method(&self) -> &str {
        self.start.split_whitespace().next().unwrap_or("")
    }
}

#[derive(Default)]
pub struct RtspFramer {
    bytes: Vec<u8>,
}

impl RtspFramer {
    /// True while the buffer holds part of a message. Stray line breaks between
    /// messages are not part of one (see `next_message`).
    pub fn has_buffered_bytes(&self) -> bool {
        self.bytes.iter().any(|b| !matches!(b, b'\r' | b'\n'))
    }

    pub fn push(&mut self, data: &[u8]) -> Result<()> {
        if self.bytes.len() + data.len() > 98304 {
            bail!("RTSP message exceeded its size limit");
        }
        self.bytes.extend_from_slice(data);
        Ok(())
    }
    pub fn next_message(&mut self) -> Result<Option<RtspMessage>> {
        // RFC 2326 section 4.1: empty lines where a start line is expected are
        // ignored. Some sources end a body with a line break the Content-Length
        // does not count; left in place it would corrupt the next message.
        let blank = self.bytes.iter().take_while(|b| matches!(b, b'\r' | b'\n')).count();
        if blank > 0 {
            self.bytes.drain(..blank);
        }
        let Some(end) = self.bytes.windows(4).position(|w| w == b"\r\n\r\n") else {
            return Ok(None);
        };
        let head = std::str::from_utf8(&self.bytes[..end])?;
        let mut lines = head.split("\r\n");
        let start = lines.next().context("Empty RTSP header")?.to_owned();
        let mut headers = HashMap::new();
        for line in lines {
            let (key, value) = line.split_once(':').context("Malformed RTSP header")?;
            let key = key.trim().to_ascii_lowercase();
            let value = value.trim().to_owned();
            if let Some(previous) = headers.insert(key, value.clone()) {
                if previous != value {
                    bail!("Conflicting duplicate RTSP headers");
                }
            }
        }
        let size = headers
            .get("content-length")
            .map(|v| v.parse::<usize>())
            .transpose()?
            .unwrap_or(0);
        if size > 65536 {
            bail!("Invalid RTSP content length");
        }
        let total = end + 4 + size;
        if self.bytes.len() < total {
            return Ok(None);
        }
        let body = std::str::from_utf8(&self.bytes[end + 4..total])?.to_owned();
        self.bytes.drain(..total);
        Ok(Some(RtspMessage {
            start,
            headers,
            body,
        }))
    }
}

pub fn rtsp(start: &str, sequence: u32, body: &str, extra: &str) -> String {
    let content = if body.is_empty() {
        String::new()
    } else {
        format!(
            "Content-Type: text/parameters\r\nContent-Length: {}\r\n",
            body.len()
        )
    };
    format!("{start}\r\nCSeq: {sequence}\r\nUser-Agent: DeXDesk/0.1\r\n{extra}{content}\r\n{body}")
}

pub fn parameters(body: &str) -> HashMap<String, String> {
    body.lines()
        .filter_map(|line| line.split_once(':'))
        .map(|(k, v)| (k.trim().to_ascii_lowercase(), v.trim().to_owned()))
        .collect()
}

fn capability_parts(value: &str) -> HashMap<String, String> {
    value
        .split(';')
        .filter_map(|part| part.split_once('='))
        .map(|(k, v)| (k.trim().to_ascii_lowercase(), v.trim().to_owned()))
        .collect()
}

pub fn input_capability(value: &str) -> (u16, bool, bool) {
    let values = capability_parts(value);
    let has = |key: &str, name: &str| {
        values
            .get(key)
            .is_some_and(|s| s.split(',').any(|p| p.trim().eq_ignore_ascii_case(name)))
    };
    let port = values.get("port").and_then(|v| v.parse().ok()).unwrap_or(0);
    let pointer = has("input_category_list", "GENERIC")
        && (has("generic_cap_list", "Mouse") || has("generic_cap_list", "SingleTouch"));
    let keyboard = has("input_category_list", "HIDC") && has("hidc_cap_list", "Keyboard/USB");
    (port, pointer, keyboard)
}

pub fn keyboard_descriptor() -> Vec<u8> {
    const DESCRIPTOR: &[u8] = &[
        0x05, 0x01, 0x09, 0x06, 0xa1, 0x01, 0x85, 0x29, 0x05, 0x07, 0x19, 0xe0, 0x29, 0xe7, 0x15,
        0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02, 0x95, 0x01, 0x75, 0x08, 0x81, 0x03,
        0x95, 0x05, 0x75, 0x01, 0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02, 0x95, 0x01, 0x75,
        0x03, 0x91, 0x03, 0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x65, 0x05, 0x07, 0x19, 0x00,
        0x29, 0x65, 0x81, 0x00, 0xc0,
    ];
    hid_packet(0, true, DESCRIPTOR)
}

pub fn hid_packet(kind: u8, descriptor: bool, value: &[u8]) -> Vec<u8> {
    let length = (9 + value.len() + 1) & !1;
    let mut result = vec![0; length];
    result[1] = 1;
    result[2..4].copy_from_slice(&(length as u16).to_be_bytes());
    result[4] = 1;
    result[5] = kind;
    result[6] = u8::from(descriptor);
    result[7..9].copy_from_slice(&(value.len() as u16).to_be_bytes());
    result[9..9 + value.len()].copy_from_slice(value);
    result
}

pub fn keyboard(keys: &std::collections::BTreeSet<u8>) -> Vec<u8> {
    let mut report = [0; 9];
    report[0] = 0x29;
    let normal: Vec<_> = keys
        .iter()
        .copied()
        .filter(|k| !(0xe0..=0xe7).contains(k))
        .collect();
    for key in keys.iter().copied().filter(|k| (0xe0..=0xe7).contains(k)) {
        report[1] |= 1 << (key - 0xe0);
    }
    if normal.len() > 6 {
        report[3..].fill(1);
    } else {
        report[3..3 + normal.len()].copy_from_slice(&normal);
    }
    hid_packet(0, false, &report)
}

pub fn pointer(action: u8, x: u16, y: u16) -> Vec<u8> {
    let mut result = vec![0; 14];
    result[2..4].copy_from_slice(&14u16.to_be_bytes());
    result[4] = action;
    result[5..7].copy_from_slice(&7u16.to_be_bytes());
    result[7] = 1;
    result[9..11].copy_from_slice(&x.to_be_bytes());
    result[11..13].copy_from_slice(&y.to_be_bytes());
    result
}

pub fn rtp_payload(packet: &[u8]) -> Option<(u32, u16, &[u8])> {
    if packet.len() < 12 || packet[0] >> 6 != 2 || packet[1] & 127 != 33 {
        return None;
    }
    let mut start = 12 + usize::from(packet[0] & 15) * 4;
    if start > packet.len() {
        return None;
    }
    if packet[0] & 16 != 0 {
        if start + 4 > packet.len() {
            return None;
        }
        let words = u16::from_be_bytes([packet[start + 2], packet[start + 3]]);
        start += 4 + usize::from(words) * 4;
    }
    let mut end = packet.len();
    if packet[0] & 32 != 0 {
        let padding = usize::from(*packet.last()?);
        if padding == 0 || padding > end.saturating_sub(start) {
            return None;
        }
        end -= padding;
    }
    if start >= end || !(end - start).is_multiple_of(188) || packet[start] != 0x47 {
        return None;
    }
    Some((
        u32::from_be_bytes(packet[8..12].try_into().ok()?),
        u16::from_be_bytes(packet[2..4].try_into().ok()?),
        &packet[start..end],
    ))
}

#[derive(Default)]
pub struct RtpReorder {
    source: Option<u32>,
    next: u16,
    pending: BTreeMap<u16, Vec<u8>>,
    gap_since: Option<u64>,
}

impl RtpReorder {
    pub fn push(
        &mut self,
        source: u32,
        sequence: u16,
        payload: &[u8],
        now: u64,
    ) -> (Vec<Vec<u8>>, u16) {
        if self.source != Some(source) {
            self.source = Some(source);
            self.next = sequence;
            self.pending.clear();
            self.gap_since = None;
        }
        if sequence.wrapping_sub(self.next) as i16 >= 0 {
            self.pending
                .entry(sequence)
                .or_insert_with(|| payload.to_vec());
        }
        self.flush(now)
    }
    pub fn flush(&mut self, now: u64) -> (Vec<Vec<u8>>, u16) {
        let mut ready = Vec::new();
        self.drain(&mut ready);
        let mut skipped = 0;
        if self.pending.is_empty() {
            self.gap_since = None;
        } else {
            let since = *self.gap_since.get_or_insert(now);
            if self.pending.len() >= 64 || now.saturating_sub(since) >= 20 {
                if let Some(nearest) = self
                    .pending
                    .keys()
                    .copied()
                    .min_by_key(|s| s.wrapping_sub(self.next))
                {
                    skipped = nearest.wrapping_sub(self.next);
                    self.next = nearest;
                    self.gap_since = None;
                    self.drain(&mut ready);
                    if !self.pending.is_empty() {
                        self.gap_since = Some(now);
                    }
                }
            }
        }
        (ready, skipped)
    }
    fn drain(&mut self, ready: &mut Vec<Vec<u8>>) {
        while let Some(value) = self.pending.remove(&self.next) {
            ready.push(value);
            self.next = self.next.wrapping_add(1);
            self.gap_since = None;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rtsp_fragments_and_duplicates() {
        let text = rtsp(
            "GET_PARAMETER rtsp://localhost/wfd1.0 RTSP/1.0",
            3,
            "wfd_video_formats\r\n",
            "",
        );
        let mut parser = RtspFramer::default();
        assert!(!parser.has_buffered_bytes());
        for fragment in text.as_bytes().chunks(3) {
            parser.push(fragment).unwrap();
            assert!(parser.has_buffered_bytes());
        }
        let message = parser.next_message().unwrap().unwrap();
        assert!(!parser.has_buffered_bytes());
        assert_eq!(message.sequence().unwrap(), 3);
        assert_eq!(message.body, "wfd_video_formats\r\n");
        assert!(parser.next_message().unwrap().is_none());
        parser
            .push(b"RTSP/1.0 200 OK\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\nxx")
            .unwrap();
        assert!(parser.next_message().is_err());
    }
    #[test]
    fn input_matches_verified_wire_format() {
        assert_eq!(
            keyboard(&Default::default()),
            vec![0, 1, 0, 18, 1, 0, 0, 0, 9, 0x29, 0, 0, 0, 0, 0, 0, 0, 0]
        );
        assert_eq!(
            pointer(0, 960, 540),
            vec![0, 0, 0, 14, 0, 0, 7, 1, 0, 3, 192, 2, 28, 0]
        );
        let packet = keyboard_descriptor();
        assert_eq!(&packet[..9], &[0, 1, 0, 74, 1, 0, 1, 0, 65]);
        let caps = input_capability("input_category_list=GENERIC, HIDC;generic_cap_list=Mouse, SingleTouch;hidc_cap_list=Keyboard/USB;port=34137");
        assert_eq!(caps, (34137, true, true));
    }
    #[test]
    fn actual_fps_not_requested_native_byte() {
        assert_eq!(
            VideoMode::from_wfd("40 00 02 04 00000080 00000000 00000000")
                .unwrap()
                .fps,
            30
        );
        assert_eq!(
            VideoMode::from_wfd("00 00 02 10 00000100 00000000 00000000")
                .unwrap()
                .fps,
            60
        );
    }
    #[test]
    fn bounded_reorder_wrap_and_source_reset() {
        let mut reorder = RtpReorder::default();
        assert_eq!(reorder.push(1, 10, &[10], 0).0, vec![vec![10]]);
        assert!(reorder.push(1, 12, &[12], 1).0.is_empty());
        assert_eq!(reorder.push(1, 11, &[11], 2).0, vec![vec![11], vec![12]]);
        assert!(reorder.push(1, 11, &[11], 3).0.is_empty());
        reorder.push(1, 14, &[14], 4);
        assert_eq!(reorder.flush(24), (vec![vec![14]], 1));
        reorder.push(2, u16::MAX, &[1], 30);
        assert_eq!(reorder.push(2, 0, &[2], 31).0, vec![vec![2]]);
    }

    fn rtp(sequence: u16, ssrc: u32, payload: &[u8]) -> Vec<u8> {
        let mut packet = vec![0x80, 33];
        packet.extend_from_slice(&sequence.to_be_bytes());
        packet.extend_from_slice(&[0; 4]);
        packet.extend_from_slice(&ssrc.to_be_bytes());
        packet.extend_from_slice(payload);
        packet
    }

    fn ts_payload(fill: u8) -> Vec<u8> {
        let mut packet = vec![fill; 188];
        packet[0] = 0x47;
        packet
    }

    #[test]
    fn rtp_header_options_are_skipped_exactly() {
        let ts = ts_payload(1);
        let plain = rtp(7, 0xdead_beef, &ts);
        let (ssrc, sequence, payload) = rtp_payload(&plain).unwrap();
        assert_eq!((ssrc, sequence, payload), (0xdead_beef, 7, &ts[..]));
        // Two CSRCs, a one-word extension and two bytes of padding together.
        let mut packet = rtp(9, 5, &[]);
        packet[0] = 0x80 | 0x20 | 0x10 | 2;
        packet.extend_from_slice(&[0; 8]);
        packet.extend_from_slice(&[0xbe, 0xde, 0, 1, 9, 9, 9, 9]);
        packet.extend_from_slice(&ts_payload(2));
        packet.extend_from_slice(&[0, 2]);
        let (_, _, payload) = rtp_payload(&packet).unwrap();
        assert_eq!(payload, &ts_payload(2)[..]);
        let double = [ts_payload(3), ts_payload(4)].concat();
        assert_eq!(rtp_payload(&rtp(1, 1, &double)).unwrap().2, &double[..]);
    }

    #[test]
    fn hostile_rtp_packets_are_rejected_without_panicking() {
        let ts = ts_payload(1);
        let good = rtp(1, 1, &ts);
        for cut in 0..good.len() {
            // Any strict prefix is either too short or not whole TS packets.
            assert!(rtp_payload(&good[..cut]).is_none(), "prefix {cut}");
        }
        let mut bad = good.clone();
        bad[0] = 0x40; // version 1
        assert!(rtp_payload(&bad).is_none());
        let mut bad = good.clone();
        bad[1] = 96; // dynamic payload type, not MP2T
        assert!(rtp_payload(&bad).is_none());
        let mut bad = good.clone();
        bad[0] = 0x8f; // 15 CSRCs that are not there
        assert!(rtp_payload(&bad[..40]).is_none());
        let mut bad = good[..12].to_vec();
        bad[0] = 0x90; // extension announcing 65535 words
        bad.extend_from_slice(&[0, 0, 0xff, 0xff]);
        bad.extend_from_slice(&ts);
        assert!(rtp_payload(&bad).is_none());
        let mut bad = good.clone();
        bad[0] |= 0x10; // extension header cut off
        assert!(rtp_payload(&bad[..14]).is_none());
        let mut padded = rtp(1, 1, &ts);
        padded[0] |= 0x20;
        padded.push(0); // zero padding length is invalid
        assert!(rtp_payload(&padded).is_none());
        *padded.last_mut().unwrap() = 255; // more padding than payload
        assert!(rtp_payload(&padded).is_none());
        assert!(rtp_payload(&rtp(1, 1, &[])).is_none(), "no payload");
        assert!(rtp_payload(&rtp(1, 1, &ts[..187])).is_none(), "not a whole packet");
        let mut unsynced = ts_payload(1);
        unsynced[0] = 0x46;
        assert!(rtp_payload(&rtp(1, 1, &unsynced)).is_none());
    }

    #[test]
    fn reorder_keeps_order_across_the_sequence_wrap() {
        let mut reorder = RtpReorder::default();
        assert_eq!(reorder.push(9, 65534, &[1], 0).0, vec![vec![1]]);
        assert!(reorder.push(9, 0, &[3], 1).0.is_empty());
        assert!(reorder.push(9, 1, &[4], 2).0.is_empty());
        assert_eq!(reorder.push(9, 65535, &[2], 3).0, vec![vec![2], vec![3], vec![4]]);
        assert_eq!(reorder.push(9, 2, &[5], 4).0, vec![vec![5]]);
    }

    #[test]
    fn duplicates_and_late_packets_are_never_replayed() {
        let mut reorder = RtpReorder::default();
        reorder.push(1, 10, &[10], 0);
        reorder.push(1, 12, &[12], 1);
        reorder.push(1, 12, &[99], 2); // duplicate of a held packet keeps the first copy
        assert_eq!(reorder.push(1, 11, &[11], 3).0, vec![vec![11], vec![12]]);
        for late in [12u16, 11, 10, 9, 1, 65000, 40000] {
            assert!(reorder.push(1, late, &[0], 4).0.is_empty(), "late {late}");
        }
        assert!(reorder.pending.is_empty());
        assert_eq!(reorder.push(1, 13, &[13], 5).0, vec![vec![13]]);
    }

    #[test]
    fn skipping_a_gap_across_the_wrap_reports_its_size() {
        let mut reorder = RtpReorder::default();
        reorder.push(1, 65530, &[0], 0);
        assert!(reorder.push(1, 3, &[3], 1).0.is_empty());
        assert_eq!(reorder.flush(30), (vec![vec![3]], 8));
        assert_eq!(reorder.push(1, 4, &[4], 31).0, vec![vec![4]]);
        // A late packet from inside the skipped gap stays dropped.
        assert!(reorder.push(1, 65535, &[9], 32).0.is_empty());
    }

    #[test]
    fn reorder_holds_a_bounded_number_of_packets() {
        let mut reorder = RtpReorder::default();
        reorder.push(1, 0, &[0], 0);
        for i in 0..2000u16 {
            reorder.push(1, 2 + i * 2, &[0; 1400], u64::from(i));
            assert!(reorder.pending.len() <= 64);
        }
    }

    #[test]
    fn stray_line_breaks_between_rtsp_messages_are_ignored() {
        let mut framer = RtspFramer::default();
        // A body followed by a line break that Content-Length does not count.
        framer
            .push(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: 2\r\n\r\nab\r\n")
            .unwrap();
        assert_eq!(framer.next_message().unwrap().unwrap().body, "ab");
        assert!(!framer.has_buffered_bytes(), "a lone line break is not a partial message");
        assert!(framer.next_message().unwrap().is_none());
        // Blank lines in front of the next start line are skipped, even split up.
        framer.push(b"\r\n\r").unwrap();
        assert!(!framer.has_buffered_bytes());
        framer.push(b"\n\r\nGET_PARAMETER rtsp://x/wfd1.0 RTSP/1.0\r\nCSeq: 2\r\n\r\n").unwrap();
        assert!(framer.has_buffered_bytes());
        let message = framer.next_message().unwrap().unwrap();
        assert_eq!((message.method(), message.sequence().unwrap()), ("GET_PARAMETER", 2));
        assert!(!framer.has_buffered_bytes());
    }

    #[test]
    fn rtsp_framer_rejects_oversized_and_malformed_messages() {
        let mut framer = RtspFramer::default();
        framer.push(&vec![b'a'; 98304]).unwrap();
        assert!(framer.push(b"a").is_err(), "an endless header must hit the size limit");
        assert!(framer.next_message().unwrap().is_none());
        for head in [
            "RTSP/1.0 200 OK\r\nContent-Length: 65537\r\n\r\n",
            "RTSP/1.0 200 OK\r\nContent-Length: -1\r\n\r\n",
            "RTSP/1.0 200 OK\r\nContent-Length: ten\r\n\r\n",
            "RTSP/1.0 200 OK\r\nContent-Length: 99999999999999999999999\r\n\r\n",
            "RTSP/1.0 200 OK\r\nno colon here\r\n\r\n",
        ] {
            let mut framer = RtspFramer::default();
            framer.push(head.as_bytes()).unwrap();
            assert!(framer.next_message().is_err(), "{head:?}");
        }
        let mut framer = RtspFramer::default();
        framer.push(b"RTSP/1.0 200 OK\r\nCSeq: \xff\r\n\r\n").unwrap();
        assert!(framer.next_message().is_err(), "header bytes must be UTF-8");
        let mut framer = RtspFramer::default();
        framer.push(b"RTSP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n\xff\xfe").unwrap();
        assert!(framer.next_message().is_err(), "body bytes must be UTF-8");
        // The largest allowed body is waited for, not rejected.
        let mut framer = RtspFramer::default();
        framer.push(b"RTSP/1.0 200 OK\r\nContent-Length: 65536\r\n\r\nxy").unwrap();
        assert!(framer.next_message().unwrap().is_none());
        assert!(framer.has_buffered_bytes());
    }

    #[test]
    fn rtsp_messages_split_across_reads_and_pipelined() {
        let one = rtsp("OPTIONS * RTSP/1.0", 1, "", "");
        let two = rtsp("SET_PARAMETER rtsp://x RTSP/1.0", 2, "a: b\r\n", "");
        let mut framer = RtspFramer::default();
        framer.push(format!("{one}{two}").as_bytes()).unwrap();
        assert_eq!(framer.next_message().unwrap().unwrap().sequence().unwrap(), 1);
        let second = framer.next_message().unwrap().unwrap();
        assert_eq!((second.sequence().unwrap(), second.body.as_str()), (2, "a: b\r\n"));
        assert!(framer.next_message().unwrap().is_none());
    }

    #[test]
    fn only_cea_formats_are_advertised_and_accepted() {
        use crate::settings::Quality;
        for quality in [Quality::FullHd60, Quality::FullHd30, Quality::Hd60] {
            let fields: Vec<_> = quality.formats().split_whitespace().collect();
            let mask = |index: usize| u32::from_str_radix(fields[index], 16).unwrap();
            assert_ne!(mask(4), 0, "{quality:?} offers CEA modes");
            assert_eq!((mask(5), mask(6)), (0, 0), "{quality:?} offers no VESA or handheld modes");
        }
        assert!(VideoMode::from_wfd("00 00 02 10 00000000 00000001 00000000").is_err());
        assert!(VideoMode::from_wfd("00 00 02 10 00000100 00000000 00000001").is_err());
        assert!(VideoMode::from_wfd("00 00 02 10 00000180 00000000 00000000").is_err());
        assert!(VideoMode::from_wfd("00 00 02 10 00000100").is_err());
    }
}
