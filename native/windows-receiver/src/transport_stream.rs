use anyhow::{bail, Result};

/// Reassembles the H.264 PES stream without a media subprocess.
#[derive(Default)]
pub struct TransportStream {
    pmt_pid: Option<u16>,
    video_pid: Option<u16>,
    pes: Vec<u8>,
    expected_size: Option<usize>,
    continuity: Option<u8>,
    assembling: bool,
    discontinuity: bool,
}

pub struct ElementaryFrame {
    pub bytes: Vec<u8>,
    pub discontinuity: bool,
}

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
                    if section.len() >= 16 && section[0] == 2 {
                        let info = (usize::from(section[10] & 15) << 8) | usize::from(section[11]);
                        let mut cursor = 12 + info;
                        let end = section.len().saturating_sub(4);
                        while cursor + 5 <= end {
                            let kind = section[cursor];
                            let stream_pid = (u16::from(section[cursor + 1] & 31) << 8)
                                | u16::from(section[cursor + 2]);
                            let extra = (usize::from(section[cursor + 3] & 15) << 8)
                                | usize::from(section[cursor + 4]);
                            if kind == 0x1b {
                                self.video_pid = Some(stream_pid);
                            }
                            cursor += 5 + extra;
                        }
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
            }
        }
        Ok(frames)
    }

    pub fn flush(&mut self) -> Result<Option<ElementaryFrame>> {
        self.finish(false)
    }

    pub fn discard(&mut self) {
        self.pes.clear();
        self.expected_size = None;
        self.assembling = false;
        self.discontinuity = true;
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
}
