use crate::{
    protocol::{self, VideoMode},
    settings::Settings,
};
use anyhow::Result;
use std::{
    collections::{BTreeSet, VecDeque},
    fs::{self, File, OpenOptions},
    io::Write,
    sync::{
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
        mpsc::SyncSender,
        Arc, Mutex,
    },
    time::{SystemTime, UNIX_EPOCH},
};
use windows::Win32::{
    Foundation::{HWND, LPARAM, WPARAM},
    UI::WindowsAndMessaging::{PostMessageW, WM_APP},
};

pub const STATE_CHANGED: u32 = WM_APP + 1;
pub const VIDEO_STARTED: u32 = WM_APP + 2;
pub const VIDEO_STOPPED: u32 = WM_APP + 3;

#[derive(Clone, Debug)]
pub enum Phase {
    Starting,
    Ready,
    Connecting(String),
    Connected,
    Error(String),
}

pub struct AppState {
    pub adapt_trace: Mutex<crate::adapt_trace::Trace>,
    status_writer: Mutex<()>,
    pub running: AtomicBool,
    pub session_active: AtomicBool,
    pub live: AtomicBool,
    pub disconnect: AtomicBool,
    pub hwnd: AtomicUsize,
    pub video_hwnd: AtomicUsize,
    pub frames: AtomicU64,
    pub phase: Mutex<Phase>,
    pub mode: Mutex<Option<VideoMode>>,
    pub settings: Mutex<Settings>,
    pub backend: Mutex<String>,
    pub input: Mutex<Option<SyncSender<Vec<u8>>>>,
    logs: Mutex<VecDeque<String>>,
    file: Mutex<File>,
}

impl AppState {
    pub fn new(settings: Settings) -> Result<Arc<Self>> {
        let directory = crate::settings::data_dir()?;
        fs::create_dir_all(&directory)?;
        let log = directory.join("DeXDesk.log");
        if fs::metadata(&log).is_ok_and(|m| m.len() > 512 * 1024) {
            let previous = directory.join("DeXDesk.previous.log");
            if previous.exists() {
                fs::remove_file(&previous)?;
            }
            fs::rename(&log, &previous)?;
        }
        let file = OpenOptions::new().create(true).append(true).open(log)?;
        Ok(Arc::new(Self {
            adapt_trace: Mutex::new(crate::adapt_trace::Trace::default()),
            status_writer: Mutex::new(()),
            running: AtomicBool::new(true),
            session_active: AtomicBool::new(false),
            live: AtomicBool::new(false),
            disconnect: AtomicBool::new(false),
            hwnd: AtomicUsize::new(0),
            video_hwnd: AtomicUsize::new(0),
            frames: AtomicU64::new(0),
            phase: Mutex::new(Phase::Starting),
            mode: Mutex::new(None),
            settings: Mutex::new(settings),
            backend: Mutex::new("Windows native H.264 / Direct3D 11".into()),
            input: Mutex::new(None),
            logs: Mutex::new(VecDeque::new()),
            file: Mutex::new(file),
        }))
    }
    pub fn log(&self, message: impl AsRef<str>) {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let line = format!("[{stamp}] {}", message.as_ref());
        eprintln!("{line}");
        let mut logs = self.logs.lock().unwrap();
        if logs.len() >= 160 {
            logs.pop_front();
        }
        logs.push_back(line.clone());
        let mut file = self.file.lock().unwrap();
        if file
            .metadata()
            .is_ok_and(|metadata| metadata.len() > 512 * 1024)
        {
            if let Err(error) = file.set_len(0) {
                eprintln!("Could not rotate DeXDesk log: {error}");
            }
        }
        if let Err(error) = writeln!(file, "{line}") {
            eprintln!("Could not write DeXDesk log: {error}");
        }
    }
    pub fn set_phase(&self, phase: Phase) {
        *self.phase.lock().unwrap() = phase;
        self.write_status();
        self.notify(STATE_CHANGED);
    }
    pub fn write_status(&self) {
        let _writer = self.status_writer.lock().unwrap();
        if let Ok(directory) = crate::settings::data_dir() {
            let status = serde_json::json!({
                "process_id": std::process::id(),
                "updated_at_epoch_ms": SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0),
                "trace": &*self.adapt_trace.lock().unwrap(),
                "phase": format!("{:?}", *self.phase.lock().unwrap()),
                "frames": self.frames.load(Ordering::Acquire),
                "running": self.running.load(Ordering::Acquire),
                "session_active": self.session_active.load(Ordering::Acquire)
            });
            let temporary = directory.join("status.pending.json");
            if let Ok(bytes) = serde_json::to_vec(&status) {
                if fs::write(&temporary, bytes).is_ok() {
                    let _ = fs::rename(&temporary, directory.join("status.json"));
                }
            }
        }
    }
    pub fn stage(&self, stage: crate::adapt_trace::Stage) {
        self.adapt_trace.lock().unwrap().record(stage);
        self.log(format!("ADAPT stage: {stage:?}"));
        self.write_status();
    }
    pub fn begin_attempt(&self) {
        self.adapt_trace.lock().unwrap().begin_attempt();
        self.log("ADAPT stage: ConnectionRequest");
        self.write_status();
    }
    pub fn notify(&self, message: u32) {
        let window = self.hwnd.load(Ordering::Acquire);
        if window != 0 {
            unsafe {
                let _ = PostMessageW(HWND(window as *mut _), message, WPARAM(0), LPARAM(0));
            }
        }
    }
    pub fn send(&self, packet: Vec<u8>) {
        let mut input = self.input.lock().unwrap();
        if let Some(sender) = input.as_ref() {
            if let Err(error) = sender.try_send(packet) {
                self.log(format!(
                    "Input channel failed: {error}. Reconnect the phone."
                ));
                *input = None;
                self.disconnect.store(true, Ordering::Release);
            }
        }
    }
    pub fn send_pointer(&self, x: u16, y: u16, down: bool, previous: &mut bool) {
        let Some(mode) = *self.mode.lock().unwrap() else {
            return;
        };
        let action = if down != *previous {
            if down {
                0
            } else {
                1
            }
        } else {
            2
        };
        let px = ((u32::from(x) * mode.width) / 32768).min(mode.width - 1) as u16;
        let py = ((u32::from(y) * mode.height) / 32768).min(mode.height - 1) as u16;
        self.send(protocol::pointer(action, px, py));
        *previous = down;
    }
    pub fn release_keys(&self) {
        self.send(protocol::keyboard(&BTreeSet::new()));
    }
    pub fn diagnostics(&self) -> String {
        self.logs
            .lock()
            .unwrap()
            .iter()
            .cloned()
            .collect::<Vec<_>>()
            .join("\r\n")
    }
}
