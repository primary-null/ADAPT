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
    pub fn push(&mut self, data: &[u8]) -> Result<()> {
        if self.bytes.len() + data.len() > 98304 {
            bail!("RTSP message exceeded its size limit");
        }
        self.bytes.extend_from_slice(data);
        Ok(())
    }
    pub fn next_message(&mut self) -> Result<Option<RtspMessage>> {
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
        for fragment in text.as_bytes().chunks(3) {
            parser.push(fragment).unwrap();
        }
        let message = parser.next_message().unwrap().unwrap();
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
}
