#[derive(Clone, Copy, Debug)]
pub struct ContactEvent {
    pub x: u16,
    pub y: u16,
    pub down: bool,
}

#[derive(Default)]
pub struct Gesture {
    pending: i32,
    active_x: Option<u16>,
    current_y: f64,
    target_y: f64,
    height: u32,
    idle_ticks: u32,
    settled_ticks: u32,
    hold_ticks: u32,
}

impl Gesture {
    pub fn active(&self) -> bool {
        self.active_x.is_some()
    }
    pub fn holding(&self) -> bool {
        self.hold_ticks > 0
    }
    fn event(&self, down: bool) -> Option<ContactEvent> {
        self.active_x.map(|x| ContactEvent {
            x,
            y: self.current_y.round().clamp(0.0, 32767.0) as u16,
            down,
        })
    }
    pub fn cancel(&mut self) -> Vec<ContactEvent> {
        let events = self.event(false).into_iter().collect();
        *self = Self::default();
        events
    }
    pub fn hold(&mut self, x: u16, y: u16) -> Vec<ContactEvent> {
        let mut events = self.cancel();
        self.active_x = Some(x.min(32767));
        self.current_y = f64::from(y.min(32767));
        self.hold_ticks = 45;
        events.extend(self.event(true));
        events
    }
    pub fn wheel(
        &mut self,
        delta: i32,
        x: u16,
        y: u16,
        height: u32,
        speed: u32,
    ) -> Vec<ContactEvent> {
        let mut events = if self.holding() {
            self.cancel()
        } else {
            Vec::new()
        };
        self.pending += delta;
        let notches = self.pending / 120;
        self.pending -= notches * 120;
        if notches == 0 || height == 0 {
            return events;
        }
        self.height = height;
        if self.active_x.is_none() {
            self.active_x = Some(x.min(32767));
            self.current_y = f64::from(y.clamp(8192, 24575));
            self.target_y = self.current_y;
            events.extend(self.event(true));
        }
        self.target_y += f64::from(notches) * f64::from(speed) / f64::from(height) * 32767.0;
        self.idle_ticks = 0;
        events
    }
    pub fn tick(&mut self) -> Vec<ContactEvent> {
        let mut events = Vec::new();
        if !self.active() {
            return events;
        }
        if self.hold_ticks > 0 {
            self.hold_ticks -= 1;
            events.extend(self.event(self.hold_ticks > 0));
            if self.hold_ticks == 0 {
                self.active_x = None;
            }
            return events;
        }
        self.idle_ticks += 1;
        const LOW: f64 = 2048.0;
        const HIGH: f64 = 30719.0;
        if (self.current_y <= LOW + 0.5 && self.target_y < LOW)
            || (self.current_y >= HIGH - 0.5 && self.target_y > HIGH)
        {
            let remaining = self.target_y - self.current_y;
            events.extend(self.event(false));
            self.current_y = 16384.0;
            self.target_y = self.current_y + remaining;
            self.settled_ticks = 0;
            events.extend(self.event(true));
            return events;
        }
        let remaining = self.target_y.clamp(LOW, HIGH) - self.current_y;
        let maximum = (20.0 / f64::from(self.height) * 32767.0).max(remaining.abs() * 0.4);
        let step = remaining.clamp(-maximum, maximum);
        self.current_y += step;
        self.settled_ticks = if step.abs() < 0.5 {
            self.settled_ticks + 1
        } else {
            0
        };
        events.extend(self.event(true));
        if self.idle_ticks >= 6
            && self.settled_ticks >= 5
            && (self.target_y - self.current_y).abs() < 0.5
        {
            events.extend(self.event(false));
            self.active_x = None;
        }
        events
    }
}

pub fn hid_key(vk: u32, scan: u32, extended: bool) -> Option<u8> {
    Some(match vk {
        0x41..=0x5a => (vk - 0x41 + 4) as u8,
        0x31..=0x39 => (vk - 0x31 + 30) as u8,
        0x70..=0x7b => (vk - 0x70 + 58) as u8,
        0x61..=0x69 => (vk - 0x61 + 89) as u8,
        0x30 => 39,
        0x0d => {
            if extended {
                88
            } else {
                40
            }
        }
        0x1b => 41,
        0x08 => 42,
        0x09 => 43,
        0x20 => 44,
        0xbd => 45,
        0xbb => 46,
        0xdb => 47,
        0xdd => 48,
        0xdc => 49,
        0xba => 51,
        0xde => 52,
        0xc0 => 53,
        0xbc => 54,
        0xbe => 55,
        0xbf => 56,
        0x14 => 57,
        0x2c => 70,
        0x91 => 71,
        0x13 => 72,
        0x2d => 73,
        0x24 => 74,
        0x21 => 75,
        0x2e => 76,
        0x23 => 77,
        0x22 => 78,
        0x27 => 79,
        0x25 => 80,
        0x28 => 81,
        0x26 => 82,
        0x90 => 83,
        0x6f => 84,
        0x6a => 85,
        0x6d => 86,
        0x6b => 87,
        0x60 => 98,
        0x6e => 99,
        0x5d => 101,
        0x10 => {
            if scan == 0x36 {
                0xe5
            } else {
                0xe1
            }
        }
        0x11 => {
            if extended {
                0xe4
            } else {
                0xe0
            }
        }
        0x12 => {
            if extended {
                0xe6
            } else {
                0xe2
            }
        }
        0xa0 => 0xe1,
        0xa1 => 0xe5,
        0xa2 => 0xe0,
        0xa3 => 0xe4,
        0xa4 => 0xe2,
        0xa5 => 0xe6,
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn wheel_burst_accumulates_without_restarting_swipes() {
        let mut gesture = Gesture::default();
        let mut events = Vec::new();
        for _ in 0..5 {
            events.extend(gesture.wheel(120, 16000, 16000, 1080, 60));
            events.extend(gesture.tick());
            events.extend(gesture.tick());
        }
        for _ in 0..100 {
            if !gesture.active() {
                break;
            }
            events.extend(gesture.tick());
        }
        assert!(!gesture.active());
        assert_eq!(events.iter().filter(|e| !e.down).count(), 1);
        let pixels =
            (f64::from(events.last().unwrap().y) - f64::from(events[0].y)) * 1080.0 / 32767.0;
        assert!((pixels - 300.0).abs() < 1.0);
    }
    #[test]
    fn cancelling_input_releases_and_prevents_later_actions() {
        let mut gesture = Gesture::default();
        assert!(gesture.hold(10000, 12000)[0].down);
        assert!(!gesture.cancel().last().unwrap().down);
        assert!(gesture.tick().is_empty());
        assert_eq!(hid_key(0x5b, 0, false), None);
        assert_eq!(hid_key(0x11, 0, true), Some(0xe4));
    }
}
