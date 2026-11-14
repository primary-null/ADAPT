use crate::{
    adapt_trace::{
        build_status, classify, AdmissionSummary, EndLedger, EndRecord, PeerPrivacy, PeerSummary,
        ReasonCode, Scope, StatusView, Trace,
    },
    protocol::{self, VideoMode},
    settings::Settings,
};
use anyhow::Result;
use std::{
    collections::{BTreeSet, VecDeque},
    convert::Infallible,
    fs::{self, File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
        mpsc::SyncSender,
        Arc, Mutex, MutexGuard, PoisonError, TryLockError,
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

/// `status.json` text for a phase. It matches the Debug form used before the
/// schema was versioned, except that the phone's name is not written.
pub fn phase_text(phase: &Phase) -> String {
    match phase {
        Phase::Connecting(_) => "Connecting(\"<redacted>\")".into(),
        other => format!("{other:?}"),
    }
}

/// Stable, machine-readable phase name for `status.json`.
pub fn phase_code(phase: &Phase) -> &'static str {
    match phase {
        Phase::Starting => "starting",
        Phase::Ready => "ready",
        Phase::Connecting(_) => "connecting",
        Phase::Connected => "connected",
        Phase::Error(_) => "error",
    }
}

/// Phase text and code as written to `status.json`. Once the receiver is no
/// longer running, the last live phase would be misleading (a stopped receiver
/// kept reading `connected`), so it is reported as stopped. An error phase is
/// kept, because it says why the receiver is not running.
pub fn reported_phase(phase: &Phase, running: bool) -> (String, &'static str) {
    if !running && !matches!(phase, Phase::Error(_)) {
        return ("Stopped".into(), "stopped");
    }
    (phase_text(phase), phase_code(phase))
}

/// A mutex that ignores poisoning. Everything these locks guard is plain data
/// that one writer replaces whole (a phase, a mode, a channel handle, a log
/// line), so a thread that panicked while holding one cannot have left it in a
/// state another thread must not read. `lock()` keeps the `Result` shape so
/// existing `.lock().unwrap()` call sites compile and can no longer panic.
/// (With `panic = "abort"` in release a panic never poisons anything; this
/// matters for debug and test builds, which unwind.)
pub struct Shared<T>(Mutex<T>);

impl<T> Shared<T> {
    pub fn new(value: T) -> Self {
        Self(Mutex::new(value))
    }
    pub fn lock(&self) -> std::result::Result<MutexGuard<'_, T>, Infallible> {
        Ok(self.0.lock().unwrap_or_else(PoisonError::into_inner))
    }
    /// For the panic hook, which may run on a thread that already holds a lock.
    pub fn try_lock(&self) -> Option<MutexGuard<'_, T>> {
        match self.0.try_lock() {
            Ok(guard) => Some(guard),
            Err(TryLockError::Poisoned(poisoned)) => Some(poisoned.into_inner()),
            Err(TryLockError::WouldBlock) => None,
        }
    }
}

/// Echo a log line to stderr. `eprintln!` panics when stderr is a pipe whose
/// reader has gone away; a supervisor that closes the pipe must not abort the
/// receiver, so a failed echo is ignored. (A pipe that is open but never read
/// still fills up and blocks, so a supervisor must drain or redirect stderr.)
fn echo(text: &str) {
    let _ = writeln!(std::io::stderr(), "{text}");
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
        .unwrap_or(0)
}

/// Replace `status.json` in `directory` by writing a temporary file and renaming
/// it over the old one, so a reader never sees a half-written document.
pub fn publish_status(directory: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let temporary = directory.join("status.pending.json");
    fs::write(&temporary, bytes)?;
    fs::rename(&temporary, directory.join(crate::settings::STATUS_FILE))
}

pub struct AppState {
    pub adapt_trace: Shared<crate::adapt_trace::Trace>,
    status_writer: Shared<()>,
    pub running: AtomicBool,
    pub session_active: AtomicBool,
    pub live: AtomicBool,
    pub disconnect: AtomicBool,
    pub hwnd: AtomicUsize,
    pub video_hwnd: AtomicUsize,
    pub frames: AtomicU64,
    pub phase: Shared<Phase>,
    pub mode: Shared<Option<VideoMode>>,
    pub settings: Shared<Settings>,
    pub backend: Shared<String>,
    pub input: Shared<Option<SyncSender<Vec<u8>>>>,
    /// True while the Wi-Fi Direct advertisement is up and a phone can connect.
    pub advertising: AtomicBool,
    /// Connection requests ignored because a session was already active.
    pub ignored_requests: AtomicU64,
    /// What the log may reveal about the phone, and the per-run peer tag.
    pub privacy: PeerPrivacy,
    directory: PathBuf,
    started_at_epoch_ms: u64,
    ends: Shared<EndLedger>,
    peer: Shared<PeerSummary>,
    admission: Shared<AdmissionSummary>,
    stop_reason: Shared<Option<ReasonCode>>,
    logs: Shared<VecDeque<String>>,
    file: Shared<File>,
}

impl AppState {
    pub fn new(settings: Settings) -> Result<Arc<Self>> {
        let reveal = crate::adapt_trace::reveals_peer_details(
            std::env::var("ADAPT_DEX_LOG_PEER_DETAILS").ok().as_deref(),
        );
        Self::in_directory(settings, crate::settings::data_dir()?, reveal)
    }
    pub fn in_directory(settings: Settings, directory: PathBuf, reveal_peer_details: bool) -> Result<Arc<Self>> {
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
            adapt_trace: Shared::new(crate::adapt_trace::Trace::default()),
            status_writer: Shared::new(()),
            running: AtomicBool::new(true),
            session_active: AtomicBool::new(false),
            live: AtomicBool::new(false),
            disconnect: AtomicBool::new(false),
            hwnd: AtomicUsize::new(0),
            video_hwnd: AtomicUsize::new(0),
            frames: AtomicU64::new(0),
            phase: Shared::new(Phase::Starting),
            mode: Shared::new(None),
            settings: Shared::new(settings),
            backend: Shared::new("Windows native H.264 / Direct3D 11".into()),
            input: Shared::new(None),
            advertising: AtomicBool::new(false),
            ignored_requests: AtomicU64::new(0),
            privacy: PeerPrivacy::new(reveal_peer_details),
            directory,
            started_at_epoch_ms: now_ms(),
            ends: Shared::new(EndLedger::default()),
            peer: Shared::new(PeerSummary::default()),
            admission: Shared::new(AdmissionSummary { open: true, name_filter: false, id_filter: false }),
            stop_reason: Shared::new(None),
            logs: Shared::new(VecDeque::new()),
            file: Shared::new(file),
        }))
    }
    pub fn log(&self, message: impl AsRef<str>) {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let line = format!("[{stamp}] {}", message.as_ref());
        echo(&line);
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
                echo(&format!("Could not rotate DeXDesk log: {error}"));
            }
        }
        if let Err(error) = writeln!(file, "{line}") {
            echo(&format!("Could not write DeXDesk log: {error}"));
        }
    }
    pub fn set_phase(&self, phase: Phase) {
        *self.phase.lock().unwrap() = phase;
        self.write_status();
        self.notify(STATE_CHANGED);
    }
    pub fn write_status(&self) {
        let _writer = self.status_writer.lock().unwrap();
        let phase = self.phase.lock().unwrap().clone();
        let ends = self.ends.lock().unwrap().clone();
        let peer = self.peer.lock().unwrap().clone();
        let admission = *self.admission.lock().unwrap();
        let trace = self.adapt_trace.lock().unwrap().clone();
        self.persist(&trace, &phase, &ends, &peer, admission);
    }
    fn persist(&self, trace: &Trace, phase: &Phase, ends: &EndLedger, peer: &PeerSummary, admission: AdmissionSummary) {
        let running = self.running.load(Ordering::Acquire);
        let (text, code) = reported_phase(phase, running);
        let status = build_status(&StatusView {
            process_id: std::process::id(),
            started_at_epoch_ms: self.started_at_epoch_ms,
            updated_at_epoch_ms: now_ms(),
            running,
            advertising: self.advertising.load(Ordering::Acquire),
            session_active: self.session_active.load(Ordering::Acquire),
            phase: &text,
            phase_code: code,
            frames: self.frames.load(Ordering::Acquire),
            trace,
            admission,
            peer,
            ignored_connection_requests: self.ignored_requests.load(Ordering::Acquire),
            last_failure: ends.last_failure.as_ref(),
            last_session_end: ends.last_session_end.as_ref(),
        });
        if let Ok(bytes) = serde_json::to_vec(&status) {
            let _ = publish_status(&self.directory, &bytes);
        }
    }
    pub fn stage(&self, stage: crate::adapt_trace::Stage) {
        self.adapt_trace.lock().unwrap().record(stage);
        self.log(format!("ADAPT stage: {stage:?}"));
        self.write_status();
    }
    pub fn begin_attempt(&self) {
        self.adapt_trace.lock().unwrap().begin_attempt();
        *self.peer.lock().unwrap() = PeerSummary::default();
        *self.stop_reason.lock().unwrap() = None;
        self.log("ADAPT stage: ConnectionRequest");
        self.write_status();
    }
    /// Claim the single session slot for a new connection request. A request
    /// that arrives while a session is active is not queued: it is counted and
    /// the caller ignores it.
    pub fn claim_session(&self) -> bool {
        let claimed = self
            .session_active
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_ok();
        if !claimed {
            self.ignored_requests.fetch_add(1, Ordering::AcqRel);
        }
        claimed
    }
    /// Record which admission filters are active. Only their presence is kept.
    pub fn set_admission(&self, name: Option<&str>, id: Option<&str>) {
        *self.admission.lock().unwrap() = AdmissionSummary::from_filters(name, id);
    }
    /// Record the phone of the current attempt as a tag, never as a name or ID.
    pub fn note_peer(&self, name: &str, id: &str, admitted: bool) {
        *self.peer.lock().unwrap() = PeerSummary {
            present: true,
            admitted: Some(admitted),
            tag: Some(self.privacy.tag(name, id)),
        };
    }
    /// End the current session because of `reason`. The first reason wins. The
    /// window's Disconnect button and closing the window set `disconnect`
    /// directly; an end without a recorded reason is reported as `user_stopped`.
    pub fn request_stop(&self, reason: ReasonCode) {
        self.stop_reason.lock().unwrap().get_or_insert(reason);
        self.disconnect.store(true, Ordering::Release);
    }
    pub fn take_stop_reason(&self) -> ReasonCode {
        self.stop_reason.lock().unwrap().take().unwrap_or(ReasonCode::UserStopped)
    }
    /// Write an end to the log and to `status.json`.
    pub fn record_end(&self, code: ReasonCode, scope: Scope, message: &str) {
        let record = {
            let trace = self.adapt_trace.lock().unwrap();
            EndRecord::new(code, scope, &trace, now_ms(), message)
        };
        self.log(format!("ADAPT end: {code} at stage {:?} ({scope:?}): {}", record.stage, record.message));
        self.ends.lock().unwrap().record(record);
        self.write_status();
    }
    pub fn record_error(&self, scope: Scope, error: &anyhow::Error) {
        self.record_end(classify(error), scope, &format!("{error:#}"));
    }
    /// The advertisement stopped after it had started (for example Wi-Fi was
    /// turned off). Only the first report after a start is recorded.
    pub fn advertisement_lost(&self, code: ReasonCode, message: &str) {
        if self.advertising.swap(false, Ordering::AcqRel) {
            self.record_end(code, Scope::Receiver, message);
        }
    }
    /// Called from the panic hook, possibly while this thread holds one of the
    /// locks, so nothing here may wait for a lock. With `panic = "abort"` the
    /// process ends right after the hook; this is the only trace it leaves.
    fn record_panic(&self, message: &str) {
        let scope = if self.session_active.load(Ordering::Acquire) { Scope::Session } else { Scope::Receiver };
        let Some(trace) = self.adapt_trace.try_lock() else { return };
        let record = EndRecord::new(ReasonCode::InternalPanic, scope, &trace, now_ms(), message);
        let (Some(mut ends), Some(phase), Some(peer), Some(admission), Some(_writer)) = (
            self.ends.try_lock(),
            self.phase.try_lock(),
            self.peer.try_lock(),
            self.admission.try_lock(),
            self.status_writer.try_lock(),
        ) else {
            return;
        };
        ends.record(record);
        self.persist(&trace, &phase, &ends, &peer, *admission);
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
                self.request_stop(ReasonCode::InputChannelFailed);
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

/// Record a panic in `status.json` before the process dies. It records the
/// failure and then lets the previous hook run unchanged.
pub fn install_panic_hook(state: Arc<AppState>) {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let text = info
            .payload()
            .downcast_ref::<&str>()
            .map(|text| (*text).to_owned())
            .or_else(|| info.payload().downcast_ref::<String>().cloned())
            .unwrap_or_else(|| "panic".into());
        let place = info.location().map(|l| format!(" at {}:{}", l.file(), l.line())).unwrap_or_default();
        state.record_panic(&format!("panic: {text}{place}"));
        previous(info);
    }));
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::adapt_trace::Stage;

    /// A fresh state whose files live in a private temporary directory.
    struct Fixture {
        state: Arc<AppState>,
        directory: PathBuf,
    }

    impl Fixture {
        fn new(name: &str, reveal: bool) -> Self {
            let directory = std::env::temp_dir().join(format!("dexdesk-test-{}-{name}", std::process::id()));
            let _ = fs::remove_dir_all(&directory);
            let state = AppState::in_directory(Settings::default(), directory.clone(), reveal).unwrap();
            Self { state, directory }
        }
        fn status(&self) -> serde_json::Value {
            self.state.write_status();
            serde_json::from_slice(&fs::read(self.directory.join("status.json")).unwrap()).unwrap()
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.directory);
        }
    }

    #[test]
    fn a_poisoned_lock_is_recovered_instead_of_panicking() {
        let shared = Arc::new(Shared::new(41));
        let poisoner = shared.clone();
        let result = std::thread::spawn(move || {
            let mut guard = poisoner.lock().unwrap();
            *guard = 42;
            panic!("poison the lock on purpose");
        })
        .join();
        assert!(result.is_err());
        assert!(shared.0.is_poisoned(), "the fixture must really poison the mutex");
        assert_eq!(*shared.lock().unwrap(), 42);
        *shared.lock().unwrap() += 1;
        assert_eq!(*shared.try_lock().unwrap(), 43);
    }

    #[test]
    fn try_lock_does_not_wait_for_a_held_lock() {
        let shared = Shared::new(());
        let _held = shared.lock().unwrap();
        assert!(shared.try_lock().is_none());
    }

    #[test]
    fn phase_text_keeps_the_old_shape_but_not_the_phone_name() {
        assert_eq!(phase_text(&Phase::Starting), "Starting");
        assert_eq!(phase_text(&Phase::Ready), "Ready");
        assert_eq!(phase_text(&Phase::Connected), "Connected");
        assert_eq!(phase_text(&Phase::Error("it broke".into())), "Error(\"it broke\")");
        let connecting = phase_text(&Phase::Connecting("Alice's Galaxy".into()));
        assert_eq!(connecting, "Connecting(\"<redacted>\")");
        assert!(!connecting.contains("Alice"));
    }

    #[test]
    fn a_stopped_receiver_does_not_keep_reporting_its_last_live_phase() {
        assert_eq!(reported_phase(&Phase::Connected, true), ("Connected".to_string(), "connected"));
        assert_eq!(reported_phase(&Phase::Connected, false), ("Stopped".to_string(), "stopped"));
        assert_eq!(reported_phase(&Phase::Ready, false), ("Stopped".to_string(), "stopped"));
        // An error phase explains why the receiver is not running, so it is kept.
        assert_eq!(reported_phase(&Phase::Error("e".into()), false).1, "error");
    }

    #[test]
    fn phase_codes_are_stable_names() {
        let codes: Vec<_> = [Phase::Starting, Phase::Ready, Phase::Connecting("x".into()), Phase::Connected, Phase::Error("e".into())]
            .iter()
            .map(phase_code)
            .collect();
        assert_eq!(codes, ["starting", "ready", "connecting", "connected", "error"]);
    }

    #[test]
    fn status_is_replaced_whole_and_leaves_no_temporary_file() {
        let fixture = Fixture::new("publish", false);
        publish_status(&fixture.directory, b"{\"a\":1}").unwrap();
        publish_status(&fixture.directory, b"{\"a\":2}").unwrap();
        assert_eq!(fs::read(fixture.directory.join("status.json")).unwrap(), b"{\"a\":2}");
        assert!(!fixture.directory.join("status.pending.json").exists());
    }

    #[test]
    fn a_fresh_state_publishes_a_complete_idle_status() {
        let fixture = Fixture::new("idle", false);
        let status = fixture.status();
        assert_eq!(status["schema_version"], 1);
        assert_eq!(status["phase"], "Starting");
        assert_eq!(status["phase_code"], "starting");
        assert_eq!(status["running"], true);
        assert_eq!(status["advertising"], false);
        assert_eq!(status["session_active"], false);
        assert_eq!(status["process_id"], std::process::id());
        assert!(status["last_failure"].is_null());
        assert_eq!(status["trace"]["last_stage"], "starting");
    }

    #[test]
    fn a_session_failure_is_written_with_code_and_stage_and_survives_the_next_attempt() {
        let fixture = Fixture::new("failure", false);
        let state = &fixture.state;
        state.begin_attempt();
        state.adapt_trace.lock().unwrap().record(Stage::RtspConnected);
        let error = crate::adapt_trace::fail(ReasonCode::HandshakeTimeout, "Miracast handshake timed out before the first decoded frame");
        state.record_error(Scope::Session, &error);
        let status = fixture.status();
        assert_eq!(status["last_failure"]["code"], "handshake_timeout");
        assert_eq!(status["last_failure"]["stage"], "rtsp_connected");
        assert_eq!(status["last_failure"]["scope"], "session");
        assert_eq!(status["last_failure"]["attempt"], 1);
        assert_eq!(status["last_session_end"]["code"], "handshake_timeout");
        // A new attempt starts a clean trace but keeps the previous failure visible.
        state.begin_attempt();
        let status = fixture.status();
        assert_eq!(status["trace"]["attempt"], 2);
        assert_eq!(status["last_failure"]["code"], "handshake_timeout");
        assert_eq!(status["last_failure"]["attempt"], 1);
        // An orderly end replaces the last session end, not the last failure.
        state.record_end(ReasonCode::PeerTeardown, Scope::Session, "The phone ended the session");
        let status = fixture.status();
        assert_eq!(status["last_session_end"]["code"], "peer_teardown");
        assert_eq!(status["last_failure"]["code"], "handshake_timeout");
    }

    #[test]
    fn peer_identity_reaches_neither_status_nor_the_log_by_default() {
        let fixture = Fixture::new("privacy", false);
        let state = &fixture.state;
        state.begin_attempt();
        state.note_peer("Alice Galaxy S24", "WiFiDirect#aa:bb:cc", true);
        state.set_phase(Phase::Connecting("Alice Galaxy S24".into()));
        let status = fixture.status();
        let text = status.to_string();
        assert_eq!(status["peer"]["present"], true);
        assert_eq!(status["peer"]["admitted"], true);
        assert_eq!(status["peer"]["tag"].as_str().unwrap().len(), 8);
        assert_eq!(status["phase_code"], "connecting");
        for secret in ["Alice", "WiFiDirect", "aa:bb"] {
            assert!(!text.contains(secret), "status leaks {secret}");
        }
        state.log(state.privacy.peer_label("Alice Galaxy S24", "WiFiDirect#aa:bb:cc"));
        let log = fs::read_to_string(fixture.directory.join("DeXDesk.log")).unwrap();
        assert!(log.contains("peer ") && !log.contains("Alice") && !log.contains("WiFiDirect"));
        // A new attempt forgets the previous phone.
        state.begin_attempt();
        assert_eq!(fixture.status()["peer"]["present"], false);
    }

    #[test]
    fn opting_in_puts_peer_details_in_the_log_only() {
        let fixture = Fixture::new("reveal", true);
        let state = &fixture.state;
        state.note_peer("Alice Galaxy S24", "WiFiDirect#aa:bb:cc", true);
        state.log(state.privacy.peer_label("Alice Galaxy S24", "WiFiDirect#aa:bb:cc"));
        assert!(fs::read_to_string(fixture.directory.join("DeXDesk.log")).unwrap().contains("Alice Galaxy S24"));
        assert!(!fixture.status().to_string().contains("Alice"));
    }

    #[test]
    fn a_second_connection_request_during_a_session_is_ignored_and_counted() {
        let fixture = Fixture::new("claim", false);
        let state = &fixture.state;
        assert!(state.claim_session());
        assert!(state.session_active.load(Ordering::Acquire));
        assert!(!state.claim_session());
        assert!(!state.claim_session());
        let status = fixture.status();
        assert_eq!(status["ignored_connection_requests"], 2);
        assert_eq!(status["session_active"], true);
        state.session_active.store(false, Ordering::Release);
        assert!(state.claim_session(), "the slot is free again once the session has ended");
        assert_eq!(fixture.status()["ignored_connection_requests"], 2);
    }

    #[test]
    fn a_stop_without_a_reason_is_a_user_stop_and_the_first_reason_wins() {
        let fixture = Fixture::new("stop", false);
        let state = &fixture.state;
        assert_eq!(state.take_stop_reason(), ReasonCode::UserStopped);
        state.request_stop(ReasonCode::InputChannelFailed);
        state.request_stop(ReasonCode::UserStopped);
        assert!(state.disconnect.load(Ordering::Acquire));
        assert_eq!(state.take_stop_reason(), ReasonCode::InputChannelFailed);
        assert_eq!(state.take_stop_reason(), ReasonCode::UserStopped, "taking the reason consumes it");
        state.request_stop(ReasonCode::InputChannelFailed);
        state.begin_attempt();
        assert_eq!(state.take_stop_reason(), ReasonCode::UserStopped, "a new attempt forgets an old reason");
    }

    #[test]
    fn a_full_input_queue_ends_the_session_as_an_input_failure_not_a_user_stop() {
        let fixture = Fixture::new("input", false);
        let state = &fixture.state;
        let (sender, receiver) = std::sync::mpsc::sync_channel::<Vec<u8>>(1);
        *state.input.lock().unwrap() = Some(sender);
        state.send(vec![1]);
        assert!(!state.disconnect.load(Ordering::Acquire));
        state.send(vec![2]);
        assert!(state.disconnect.load(Ordering::Acquire));
        assert!(state.input.lock().unwrap().is_none());
        assert_eq!(state.take_stop_reason(), ReasonCode::InputChannelFailed);
        drop(receiver);
    }

    #[test]
    fn a_lost_advertisement_is_recorded_once_and_only_after_it_started() {
        let fixture = Fixture::new("lost", false);
        let state = &fixture.state;
        state.advertisement_lost(ReasonCode::AdvertisementAborted, "before start");
        assert!(fixture.status()["last_failure"].is_null());
        state.advertising.store(true, Ordering::Release);
        state.advertisement_lost(ReasonCode::WifiRadioUnavailable, "Wi-Fi was turned off");
        state.advertisement_lost(ReasonCode::AdvertisementAborted, "duplicate");
        let status = fixture.status();
        assert_eq!(status["advertising"], false);
        assert_eq!(status["last_failure"]["code"], "wifi_radio_unavailable");
        assert_eq!(status["last_failure"]["scope"], "receiver");
        assert_eq!(status["last_session_end"], serde_json::Value::Null);
    }

    #[test]
    fn a_panic_is_recorded_without_waiting_for_locks() {
        let fixture = Fixture::new("panic", false);
        let state = &fixture.state;
        state.record_panic("panic: boom at src/x.rs:1");
        let status = fixture.status();
        assert_eq!(status["last_failure"]["code"], "internal_panic");
        assert_eq!(status["last_failure"]["scope"], "receiver");
        // Re-entrancy: a panic on a thread that holds the trace lock must not deadlock.
        let _held = state.adapt_trace.lock().unwrap();
        state.record_panic("panic: while holding the lock");
    }
}
