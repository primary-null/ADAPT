//! ADAPT observations. These stages do not certify Samsung desktop activation.
//!
//! This module also defines the machine-readable failure contract (reason
//! codes, end records, the `status.json` document) and the small pure helpers
//! the receiver uses to classify how a startup or a session ended. Everything
//! here is deterministic and touches no sockets, files or Windows APIs.
use anyhow::{Error as AnyError, Result};
use serde::Serialize;
use std::{
    collections::{hash_map::RandomState, BTreeSet},
    fmt,
    hash::{BuildHasher, Hash, Hasher},
    net::IpAddr,
    time::{Duration, Instant},
};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Stage {
    Starting,
    Advertising,
    ConnectionRequest,
    PeerRejected,
    PeerAdmitted,
    WifiDirectGroup,
    RtspConnected,
    VideoNegotiated,
    PlaybackAccepted,
    InputChannelConnected,
    FirstFrameDecoded,
}

#[derive(Clone, Debug, Serialize)]
pub struct Trace {
    pub attempt: u64,
    pub attempt_active: bool,
    pub last_stage: Stage,
    pub observed_stages: BTreeSet<Stage>,
}

impl Default for Trace {
    fn default() -> Self {
        Self { attempt: 0, attempt_active: false, last_stage: Stage::Starting,
            observed_stages: BTreeSet::new() }
    }
}

impl Trace {
    pub fn begin_attempt(&mut self) {
        self.attempt += 1;
        self.attempt_active = true;
        self.observed_stages.clear();
        self.record(Stage::ConnectionRequest);
    }

    pub fn record(&mut self, stage: Stage) {
        self.last_stage = stage;
        self.observed_stages.insert(stage);
    }

    pub fn end_attempt(&mut self) { self.attempt_active = false; }
}

/// Version of the `status.json` document. It is raised only for an
/// incompatible change; additive fields do not raise it, so readers must ignore
/// fields they do not know.
pub const STATUS_SCHEMA_VERSION: u32 = 1;

/// Whether an end concerns the receiver as a whole (it could not start, or it
/// stopped advertising) or one connection attempt.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Scope {
    Receiver,
    Session,
}

/// Stable reason codes. The snake_case spelling is the public contract: codes
/// are never renamed or reused, only added.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ReasonCode {
    // The receiver could not start or stopped advertising.
    InvalidConfiguration,
    PortInUse,
    WifiDirectInitFailed,
    MiracastProviderUnavailable,
    MiracastProviderFailed,
    WifiRadioUnavailable,
    WifiDirectResourceInUse,
    AdvertisementAborted,
    AdvertisementTimeout,
    // One connection attempt ended.
    PeerRejectedName,
    PeerRejectedId,
    WifiDirectConnectFailed,
    WifiDirectConnectTimeout,
    NoIpv4Endpoint,
    RtspConnectFailed,
    RtpPortUnavailable,
    ControlConnectionFailed,
    RtspProtocolError,
    RtspRequestRejected,
    UnsupportedVideoFormat,
    InputChannelUnavailable,
    InputChannelFailed,
    DecoderInitFailed,
    DecoderFailed,
    MediaStreamError,
    HandshakeTimeout,
    PartialMessageTimeout,
    ControlIdleTimeout,
    PeerDisconnected,
    PeerTeardown,
    UserStopped,
    // Either scope.
    InternalError,
    InternalPanic,
}

impl ReasonCode {
    pub const ALL: &'static [ReasonCode] = &[
        Self::InvalidConfiguration,
        Self::PortInUse,
        Self::WifiDirectInitFailed,
        Self::MiracastProviderUnavailable,
        Self::MiracastProviderFailed,
        Self::WifiRadioUnavailable,
        Self::WifiDirectResourceInUse,
        Self::AdvertisementAborted,
        Self::AdvertisementTimeout,
        Self::PeerRejectedName,
        Self::PeerRejectedId,
        Self::WifiDirectConnectFailed,
        Self::WifiDirectConnectTimeout,
        Self::NoIpv4Endpoint,
        Self::RtspConnectFailed,
        Self::RtpPortUnavailable,
        Self::ControlConnectionFailed,
        Self::RtspProtocolError,
        Self::RtspRequestRejected,
        Self::UnsupportedVideoFormat,
        Self::InputChannelUnavailable,
        Self::InputChannelFailed,
        Self::DecoderInitFailed,
        Self::DecoderFailed,
        Self::MediaStreamError,
        Self::HandshakeTimeout,
        Self::PartialMessageTimeout,
        Self::ControlIdleTimeout,
        Self::PeerDisconnected,
        Self::PeerTeardown,
        Self::UserStopped,
        Self::InternalError,
        Self::InternalPanic,
    ];

    /// The wire spelling. A test keeps it identical to the serde spelling.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::InvalidConfiguration => "invalid_configuration",
            Self::PortInUse => "port_in_use",
            Self::WifiDirectInitFailed => "wifi_direct_init_failed",
            Self::MiracastProviderUnavailable => "miracast_provider_unavailable",
            Self::MiracastProviderFailed => "miracast_provider_failed",
            Self::WifiRadioUnavailable => "wifi_radio_unavailable",
            Self::WifiDirectResourceInUse => "wifi_direct_resource_in_use",
            Self::AdvertisementAborted => "advertisement_aborted",
            Self::AdvertisementTimeout => "advertisement_timeout",
            Self::PeerRejectedName => "peer_rejected_name",
            Self::PeerRejectedId => "peer_rejected_id",
            Self::WifiDirectConnectFailed => "wifi_direct_connect_failed",
            Self::WifiDirectConnectTimeout => "wifi_direct_connect_timeout",
            Self::NoIpv4Endpoint => "no_ipv4_endpoint",
            Self::RtspConnectFailed => "rtsp_connect_failed",
            Self::RtpPortUnavailable => "rtp_port_unavailable",
            Self::ControlConnectionFailed => "control_connection_failed",
            Self::RtspProtocolError => "rtsp_protocol_error",
            Self::RtspRequestRejected => "rtsp_request_rejected",
            Self::UnsupportedVideoFormat => "unsupported_video_format",
            Self::InputChannelUnavailable => "input_channel_unavailable",
            Self::InputChannelFailed => "input_channel_failed",
            Self::DecoderInitFailed => "decoder_init_failed",
            Self::DecoderFailed => "decoder_failed",
            Self::MediaStreamError => "media_stream_error",
            Self::HandshakeTimeout => "handshake_timeout",
            Self::PartialMessageTimeout => "partial_message_timeout",
            Self::ControlIdleTimeout => "control_idle_timeout",
            Self::PeerDisconnected => "peer_disconnected",
            Self::PeerTeardown => "peer_teardown",
            Self::UserStopped => "user_stopped",
            Self::InternalError => "internal_error",
            Self::InternalPanic => "internal_panic",
        }
    }

    /// `user_stopped` and `peer_teardown` are deliberate, orderly endings. They
    /// are recorded as the last session end but never as the last failure.
    pub const fn is_failure(self) -> bool {
        !matches!(self, Self::UserStopped | Self::PeerTeardown)
    }
}

impl fmt::Display for ReasonCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// An error carrying a reason code. Its text and cause chain are exactly those
/// of the wrapped error, so `{}` and `{:#}` print what they printed before the
/// code was attached.
#[derive(Debug)]
struct Tagged {
    code: ReasonCode,
    inner: AnyError,
}

impl fmt::Display for Tagged {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Display::fmt(&self.inner, f)
    }
}

impl std::error::Error for Tagged {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        self.inner.source()
    }
}

/// Attach `code` unless the error already carries one: the code chosen closest
/// to the cause wins.
pub fn tag(error: AnyError, code: ReasonCode) -> AnyError {
    if error.chain().any(|link| link.is::<Tagged>()) {
        error
    } else {
        AnyError::new(Tagged { code, inner: error })
    }
}

/// A new classified error with a fixed message.
pub fn fail(code: ReasonCode, message: impl fmt::Display + fmt::Debug + Send + Sync + 'static) -> AnyError {
    tag(AnyError::msg(message), code)
}

/// The code of the nearest classified cause, or `internal_error`.
pub fn classify(error: &AnyError) -> ReasonCode {
    error
        .chain()
        .find_map(|link| link.downcast_ref::<Tagged>())
        .map_or(ReasonCode::InternalError, |tagged| tagged.code)
}

pub trait OrFail<T> {
    /// `result.or_fail(code)` classifies an error that is not classified yet.
    fn or_fail(self, code: ReasonCode) -> Result<T>;
}

impl<T, E: Into<AnyError>> OrFail<T> for std::result::Result<T, E> {
    fn or_fail(self, code: ReasonCode) -> Result<T> {
        self.map_err(|error| tag(error.into(), code))
    }
}

const MESSAGE_LIMIT: usize = 240;

/// Human text for `status.json`: no control characters, bounded length.
pub fn sanitize_message(message: &str) -> String {
    message
        .chars()
        .map(|c| if c.is_control() { ' ' } else { c })
        .take(MESSAGE_LIMIT)
        .collect()
}

/// How a startup or a session ended, as written to `status.json`.
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct EndRecord {
    pub code: ReasonCode,
    pub scope: Scope,
    /// The last stage reached before the end; the end happened while the
    /// receiver was trying to reach the next one.
    pub stage: Stage,
    /// The attempt counter at that time (0 before any connection request).
    pub attempt: u64,
    pub at_epoch_ms: u64,
    pub message: String,
}

impl EndRecord {
    pub fn new(code: ReasonCode, scope: Scope, trace: &Trace, at_epoch_ms: u64, message: &str) -> Self {
        Self { code, scope, stage: trace.last_stage, attempt: trace.attempt, at_epoch_ms, message: sanitize_message(message) }
    }
}

/// What `status.json` says about the phone of the last attempt. It never holds
/// the phone's name, peer ID or address.
#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
pub struct PeerSummary {
    pub present: bool,
    pub admitted: Option<bool>,
    pub tag: Option<String>,
}

/// Which optional admission filters are active (an empty value restricts
/// nothing). The filter values themselves are never written.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize)]
pub struct AdmissionSummary {
    pub open: bool,
    pub name_filter: bool,
    pub id_filter: bool,
}

impl AdmissionSummary {
    pub fn from_filters(name: Option<&str>, id: Option<&str>) -> Self {
        let name_filter = name.is_some_and(|value| !value.is_empty());
        let id_filter = id.is_some_and(|value| !value.is_empty());
        Self { open: !name_filter && !id_filter, name_filter, id_filter }
    }
}

/// What the log may say about the phone. By default the name, peer ID and the
/// Wi-Fi Direct addresses are not written at all; a per-process random tag lets
/// two log lines be tied to the same phone without revealing who it is.
pub struct PeerPrivacy {
    reveal: bool,
    hasher: RandomState,
}

/// `ADAPT_DEX_LOG_PEER_DETAILS` opts in to raw details in the log.
pub fn reveals_peer_details(value: Option<&str>) -> bool {
    value.is_some_and(|value| {
        ["1", "true", "yes", "on"].iter().any(|yes| value.trim().eq_ignore_ascii_case(yes))
    })
}

impl PeerPrivacy {
    pub fn new(reveal: bool) -> Self {
        Self { reveal, hasher: RandomState::new() }
    }

    /// Eight hex digits of a keyed hash. The key exists only in this process,
    /// so the tag cannot be reversed or matched against another run.
    pub fn tag(&self, name: &str, id: &str) -> String {
        let mut hasher = self.hasher.build_hasher();
        name.hash(&mut hasher);
        id.hash(&mut hasher);
        format!("{:08x}", hasher.finish() as u32)
    }

    pub fn reveals(&self) -> bool {
        self.reveal
    }

    pub fn peer_label(&self, name: &str, id: &str) -> String {
        let tag = self.tag(name, id);
        if self.reveal {
            format!("peer {tag} (name={name:?} id={id:?})")
        } else {
            format!("peer {tag}")
        }
    }

    pub fn link_label(&self, local: IpAddr, remote: IpAddr) -> String {
        if self.reveal {
            format!("{local} <-> {remote}")
        } else {
            "addresses withheld".into()
        }
    }
}

/// The two records `status.json` keeps after a session is over. The last
/// failure stays until another failure replaces it; an orderly end never
/// clears it.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct EndLedger {
    pub last_failure: Option<EndRecord>,
    pub last_session_end: Option<EndRecord>,
}

impl EndLedger {
    pub fn record(&mut self, record: EndRecord) {
        if record.scope == Scope::Session {
            self.last_session_end = Some(record.clone());
        }
        if record.code.is_failure() {
            self.last_failure = Some(record);
        }
    }
}

/// Everything `status.json` reports, gathered by the caller.
pub struct StatusView<'a> {
    pub process_id: u32,
    pub started_at_epoch_ms: u64,
    pub updated_at_epoch_ms: u64,
    pub running: bool,
    pub advertising: bool,
    pub session_active: bool,
    pub phase: &'a str,
    pub phase_code: &'a str,
    pub frames: u64,
    pub trace: &'a Trace,
    pub admission: AdmissionSummary,
    pub peer: &'a PeerSummary,
    pub ignored_connection_requests: u64,
    pub last_failure: Option<&'a EndRecord>,
    pub last_session_end: Option<&'a EndRecord>,
}

/// The `status.json` document. `process_id`, `updated_at_epoch_ms`, `trace`,
/// `phase`, `frames`, `running` and `session_active` keep their original names
/// and meaning; the other fields are additive.
pub fn build_status(view: &StatusView) -> serde_json::Value {
    serde_json::json!({
        "schema_version": STATUS_SCHEMA_VERSION,
        "receiver_version": env!("CARGO_PKG_VERSION"),
        "process_id": view.process_id,
        "started_at_epoch_ms": view.started_at_epoch_ms,
        "updated_at_epoch_ms": view.updated_at_epoch_ms,
        "trace": view.trace,
        "phase": view.phase,
        "phase_code": view.phase_code,
        "frames": view.frames,
        "running": view.running,
        "advertising": view.advertising,
        "session_active": view.session_active,
        "admission": view.admission,
        "peer": view.peer,
        "ignored_connection_requests": view.ignored_connection_requests,
        "last_failure": view.last_failure,
        "last_session_end": view.last_session_end
    })
}

/// How one connection attempt ended without an error.
#[derive(Debug, Eq, PartialEq)]
pub enum SessionEnd {
    /// The loop stopped because the user (or the receiver itself) asked it to.
    Stopped,
    /// The phone sent TEARDOWN.
    PeerTeardown,
    /// The phone closed the control connection without TEARDOWN.
    PeerDisconnected,
    /// The phone was refused before any connection was made.
    Rejected(ReasonCode),
}

impl SessionEnd {
    /// `stopped_as` is the reason recorded for a stop request (normally
    /// `user_stopped`).
    pub fn reason(&self, stopped_as: ReasonCode) -> ReasonCode {
        match self {
            Self::Stopped => stopped_as,
            Self::PeerTeardown => ReasonCode::PeerTeardown,
            Self::PeerDisconnected => ReasonCode::PeerDisconnected,
            Self::Rejected(code) => *code,
        }
    }
}

/// Combine the control loop's outcome with the workers' outcomes. A control
/// error is the cause of the session's end (the workers only follow it); next a
/// worker error beats an orderly control end, because a worker failing is what
/// stops the control loop. The errors that lose are returned as text for the log
/// instead of vanishing.
pub fn settle(control: Result<SessionEnd>, workers: Vec<Result<()>>) -> (Result<SessionEnd>, Vec<String>) {
    let mut dropped = Vec::new();
    let mut first_worker = None;
    for outcome in workers {
        if let Err(error) = outcome {
            if first_worker.is_none() {
                first_worker = Some(error);
            } else {
                dropped.push(format!("{error:#}"));
            }
        }
    }
    match (control, first_worker) {
        (Err(error), worker) => {
            dropped.extend(worker.map(|error| format!("{error:#}")));
            (Err(error), dropped)
        }
        (Ok(_), Some(error)) => (Err(error), dropped),
        (Ok(end), None) => (Ok(end), dropped),
    }
}

/// An IDR request may be sent at once the first time and then at most once a
/// second. `None` means none was sent yet; this never subtracts from an
/// `Instant`, which panics when the clock origin is too recent.
pub fn idr_request_due(last: Option<Instant>, now: Instant) -> bool {
    last.is_none_or(|sent| now.saturating_duration_since(sent) > Duration::from_secs(1))
}

/// What to do about one more unwanted connection.
#[derive(Debug, Eq, PartialEq)]
pub enum Rejection {
    /// Log it.
    Report,
    /// Log once that further ones will no longer be logged one by one.
    FirstSuppressed,
    /// Count it silently.
    Quiet,
}

/// Keeps a flood of unwanted connections from flooding the log: the first few
/// are reported, the rest only counted.
#[derive(Default)]
pub struct RejectLog {
    count: u32,
}

impl RejectLog {
    const REPORTED: u32 = 3;

    /// Count one rejection and say how to report it.
    pub fn note(&mut self) -> Rejection {
        self.count = self.count.saturating_add(1);
        match self.count {
            0..=Self::REPORTED => Rejection::Report,
            n if n == Self::REPORTED + 1 => Rejection::FirstSuppressed,
            _ => Rejection::Quiet,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn advertisement_is_not_a_video_or_input_observation() {
        let mut trace = Trace::default();
        trace.record(Stage::Advertising);
        assert_eq!(trace.attempt, 0);
        assert!(!trace.observed_stages.contains(&Stage::FirstFrameDecoded));
        assert!(!trace.observed_stages.contains(&Stage::InputChannelConnected));
    }
    #[test]
    fn new_attempt_does_not_inherit_previous_video_success() {
        let mut trace = Trace::default();
        trace.begin_attempt();
        trace.record(Stage::FirstFrameDecoded);
        trace.end_attempt();
        assert_eq!(trace.last_stage, Stage::FirstFrameDecoded);
        trace.begin_attempt();
        assert_eq!(trace.attempt, 2);
        assert!(!trace.observed_stages.contains(&Stage::FirstFrameDecoded));
        assert_eq!(trace.last_stage, Stage::ConnectionRequest);
    }
    #[test]
    fn decoded_frame_does_not_certify_input_or_samsung_dex() {
        let mut trace = Trace::default();
        trace.begin_attempt();
        trace.record(Stage::FirstFrameDecoded);
        assert!(!trace.observed_stages.contains(&Stage::InputChannelConnected));
        let receipt = serde_json::to_string(&trace).unwrap();
        assert!(!receipt.contains("dex_confirmed"));
        assert!(!receipt.contains("serial"));
    }
}

#[cfg(test)]
mod contract_tests {
    use super::*;
    use serde_json::json;

    fn trace_at(stage: Stage) -> Trace {
        let mut trace = Trace::default();
        trace.begin_attempt();
        trace.record(stage);
        trace
    }

    #[test]
    fn reason_codes_are_unique_snake_case_and_match_serde() {
        let mut seen = BTreeSet::new();
        for code in ReasonCode::ALL {
            let text = code.as_str();
            assert!(
                !text.is_empty()
                    && text.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
                    && !text.starts_with('_')
                    && !text.ends_with('_')
                    && !text.contains("__"),
                "{text} is not snake_case"
            );
            assert_eq!(serde_json::to_string(code).unwrap(), format!("\"{text}\""));
            assert_eq!(code.to_string(), text);
            assert!(seen.insert(text), "{text} is listed twice");
        }
        assert_eq!(seen.len(), 33, "update README.md and this count together with the code list");
    }

    #[test]
    fn only_orderly_endings_are_not_failures() {
        let orderly: Vec<_> = ReasonCode::ALL.iter().filter(|code| !code.is_failure()).collect();
        assert_eq!(orderly, [&ReasonCode::PeerTeardown, &ReasonCode::UserStopped]);
    }

    #[test]
    fn untagged_errors_classify_as_internal() {
        assert_eq!(classify(&anyhow::anyhow!("anything")), ReasonCode::InternalError);
        let io: std::io::Result<()> = Err(std::io::Error::other("reset"));
        assert_eq!(classify(&io.unwrap_err().into()), ReasonCode::InternalError);
    }

    #[test]
    fn a_code_survives_added_context_and_the_nearest_code_wins() {
        let error = fail(ReasonCode::PortInUse, "port busy").context("while starting");
        assert_eq!(classify(&error), ReasonCode::PortInUse);
        let again = tag(error, ReasonCode::DecoderFailed);
        assert_eq!(classify(&again), ReasonCode::PortInUse);
        let from_result: Result<()> = Err(std::io::Error::other("x")).or_fail(ReasonCode::MediaStreamError);
        let nested = from_result.unwrap_err().context("outer").context("outermost");
        assert_eq!(classify(&nested), ReasonCode::MediaStreamError);
    }

    #[test]
    fn tagging_leaves_every_rendering_of_the_error_unchanged() {
        let build = || AnyError::from(std::io::Error::other("socket gone")).context("Miracast control connection failed");
        let plain = build();
        let tagged = tag(build(), ReasonCode::ControlConnectionFailed);
        assert_eq!(classify(&tagged), ReasonCode::ControlConnectionFailed);
        assert_eq!(tagged.to_string(), plain.to_string());
        assert_eq!(format!("{tagged:#}"), format!("{plain:#}"));
        assert_eq!(format!("{tagged:?}"), format!("{plain:?}"));
    }

    #[test]
    fn end_record_serialises_the_documented_fields() {
        let trace = trace_at(Stage::RtspConnected);
        let record = EndRecord::new(ReasonCode::HandshakeTimeout, Scope::Session, &trace, 1_700_000_000_123, "timed out\tbefore\nthe first frame");
        assert_eq!(
            serde_json::to_value(&record).unwrap(),
            json!({
                "code": "handshake_timeout", "scope": "session", "stage": "rtsp_connected",
                "attempt": 1, "at_epoch_ms": 1_700_000_000_123u64,
                "message": "timed out before the first frame"
            })
        );
    }

    #[test]
    fn messages_are_bounded_and_free_of_control_characters() {
        assert_eq!(sanitize_message("a\r\nb\u{7}c"), "a  b c");
        let long = "\u{e9}".repeat(1000);
        let cut = sanitize_message(&long);
        assert_eq!(cut.chars().count(), MESSAGE_LIMIT);
        assert!(cut.chars().all(|c| c == '\u{e9}'));
    }

    #[test]
    fn the_last_failure_outlives_an_orderly_session_end() {
        let trace = trace_at(Stage::FirstFrameDecoded);
        let make = |code, scope| EndRecord::new(code, scope, &trace, 1, "m");
        let mut ledger = EndLedger::default();
        ledger.record(make(ReasonCode::ControlIdleTimeout, Scope::Session));
        ledger.record(make(ReasonCode::UserStopped, Scope::Session));
        assert_eq!(ledger.last_failure.as_ref().unwrap().code, ReasonCode::ControlIdleTimeout);
        assert_eq!(ledger.last_session_end.as_ref().unwrap().code, ReasonCode::UserStopped);
        ledger.record(make(ReasonCode::PortInUse, Scope::Receiver));
        assert_eq!(ledger.last_failure.as_ref().unwrap().code, ReasonCode::PortInUse);
        assert_eq!(ledger.last_session_end.as_ref().unwrap().code, ReasonCode::UserStopped, "a receiver failure is not a session end");
        ledger.record(make(ReasonCode::PeerTeardown, Scope::Session));
        assert_eq!(ledger.last_failure.as_ref().unwrap().code, ReasonCode::PortInUse);
    }

    fn view<'a>(trace: &'a Trace, peer: &'a PeerSummary, failure: Option<&'a EndRecord>) -> StatusView<'a> {
        StatusView {
            process_id: 4242,
            started_at_epoch_ms: 1000,
            updated_at_epoch_ms: 2000,
            running: true,
            advertising: true,
            session_active: false,
            phase: "Ready",
            phase_code: "ready",
            frames: 7,
            trace,
            admission: AdmissionSummary::from_filters(None, None),
            peer,
            ignored_connection_requests: 2,
            last_failure: failure,
            last_session_end: failure,
        }
    }

    #[test]
    fn status_keeps_the_original_fields_and_adds_the_contract() {
        let trace = trace_at(Stage::PeerAdmitted);
        let peer = PeerSummary::default();
        let status = build_status(&view(&trace, &peer, None));
        // Original fields: names, types and meaning.
        assert_eq!(status["process_id"], 4242);
        assert_eq!(status["updated_at_epoch_ms"], 2000);
        assert_eq!(status["phase"], "Ready");
        assert_eq!(status["frames"], 7);
        assert_eq!(status["running"], true);
        assert_eq!(status["session_active"], false);
        assert_eq!(status["trace"]["attempt"], 1);
        assert_eq!(status["trace"]["attempt_active"], true);
        assert_eq!(status["trace"]["last_stage"], "peer_admitted");
        assert_eq!(status["trace"]["observed_stages"], json!(["connection_request", "peer_admitted"]));
        // Additive fields.
        assert_eq!(status["schema_version"], 1);
        assert_eq!(status["receiver_version"], env!("CARGO_PKG_VERSION"));
        assert_eq!(status["started_at_epoch_ms"], 1000);
        assert_eq!(status["phase_code"], "ready");
        assert_eq!(status["advertising"], true);
        assert_eq!(status["admission"], json!({"open": true, "name_filter": false, "id_filter": false}));
        assert_eq!(status["peer"], json!({"present": false, "admitted": null, "tag": null}));
        assert_eq!(status["ignored_connection_requests"], 2);
        assert!(status["last_failure"].is_null() && status["last_session_end"].is_null());
    }

    #[test]
    fn status_carries_the_failure_code_and_stage_but_no_peer_identity() {
        let trace = trace_at(Stage::VideoNegotiated);
        let privacy = PeerPrivacy::new(false);
        let (name, id) = ("Alice Galaxy S24", "WiFiDirect#aa:bb:cc:dd:ee:ff");
        let peer = PeerSummary { present: true, admitted: Some(true), tag: Some(privacy.tag(name, id)) };
        let failure = EndRecord::new(ReasonCode::DecoderInitFailed, Scope::Session, &trace, 5, "decoder is unavailable");
        let text = serde_json::to_string(&build_status(&view(&trace, &peer, Some(&failure)))).unwrap();
        let value: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(value["last_failure"]["code"], "decoder_init_failed");
        assert_eq!(value["last_failure"]["stage"], "video_negotiated");
        assert_eq!(value["last_failure"]["scope"], "session");
        assert_eq!(value["last_failure"]["attempt"], 1);
        assert_eq!(value["peer"]["admitted"], true);
        for secret in [name, id, "Alice", "aa:bb", "WiFiDirect"] {
            assert!(!text.contains(secret), "status leaks {secret}");
        }
    }

    #[test]
    fn peer_tags_are_stable_within_a_run_distinct_per_peer_and_reveal_nothing() {
        let privacy = PeerPrivacy::new(false);
        let tag = privacy.tag("Pixel", "id-1");
        assert_eq!(tag, privacy.tag("Pixel", "id-1"));
        assert_ne!(tag, privacy.tag("Pixel", "id-2"));
        assert_ne!(privacy.tag("ab", "c"), privacy.tag("a", "bc"));
        assert_eq!(tag.len(), 8);
        assert!(tag.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
        assert!(!tag.contains("Pixel"));
    }

    #[test]
    fn the_log_withholds_name_id_and_addresses_unless_opted_in() {
        let local: IpAddr = "192.168.137.1".parse().unwrap();
        let remote: IpAddr = "192.168.137.42".parse().unwrap();
        let hidden = PeerPrivacy::new(false);
        let line = format!("{} / {}", hidden.peer_label("Alice phone", "peer-id-9"), hidden.link_label(local, remote));
        for secret in ["Alice", "peer-id-9", "192.168", "137"] {
            assert!(!line.contains(secret), "default log line leaks {secret}: {line}");
        }
        assert!(line.starts_with("peer "));
        let shown = PeerPrivacy::new(true);
        let line = format!("{} / {}", shown.peer_label("Alice phone", "peer-id-9"), shown.link_label(local, remote));
        assert!(line.contains("Alice phone") && line.contains("peer-id-9"));
        assert!(line.contains("192.168.137.1 <-> 192.168.137.42"));
    }

    #[test]
    fn peer_details_logging_needs_an_explicit_yes() {
        for yes in ["1", "true", "TRUE", " yes ", "On"] {
            assert!(reveals_peer_details(Some(yes)), "{yes}");
        }
        for no in ["", "0", "false", "no", "off", "2", "enabled"] {
            assert!(!reveals_peer_details(Some(no)), "{no}");
        }
        assert!(!reveals_peer_details(None));
    }

    #[test]
    fn admission_summary_reports_filters_without_their_values() {
        assert_eq!(AdmissionSummary::from_filters(None, None), AdmissionSummary { open: true, name_filter: false, id_filter: false });
        assert!(AdmissionSummary::from_filters(Some(""), Some("")).open);
        let both = AdmissionSummary::from_filters(Some("secret-name"), Some("secret-id"));
        assert!(!both.open && both.name_filter && both.id_filter);
        assert!(!AdmissionSummary::from_filters(None, Some("i")).open);
        let text = serde_json::to_string(&both).unwrap();
        assert!(!text.contains("secret"));
    }

    fn broken(code: ReasonCode, text: &'static str) -> Result<()> {
        Err(fail(code, text))
    }

    #[test]
    fn a_control_error_is_the_cause_and_the_worker_error_is_kept_for_the_log() {
        let (outcome, dropped) = settle(
            Err(fail(ReasonCode::RtspProtocolError, "bad message")),
            vec![broken(ReasonCode::DecoderFailed, "decoder died"), Ok(())],
        );
        assert_eq!(classify(&outcome.unwrap_err()), ReasonCode::RtspProtocolError);
        assert_eq!(dropped, ["decoder died"]);
    }

    #[test]
    fn a_failed_worker_beats_an_orderly_control_end() {
        for end in [SessionEnd::Stopped, SessionEnd::PeerDisconnected, SessionEnd::PeerTeardown] {
            let (outcome, dropped) = settle(
                Ok(end),
                vec![broken(ReasonCode::DecoderInitFailed, "no decoder"), broken(ReasonCode::InputChannelFailed, "input died")],
            );
            assert_eq!(classify(&outcome.unwrap_err()), ReasonCode::DecoderInitFailed);
            assert_eq!(dropped, ["input died"]);
        }
    }

    #[test]
    fn a_clean_session_settles_to_its_own_end() {
        let (outcome, dropped) = settle(Ok(SessionEnd::PeerTeardown), vec![Ok(()), Ok(())]);
        assert_eq!(outcome.unwrap(), SessionEnd::PeerTeardown);
        assert!(dropped.is_empty());
    }

    #[test]
    fn session_end_maps_to_reason_codes() {
        assert_eq!(SessionEnd::Stopped.reason(ReasonCode::UserStopped), ReasonCode::UserStopped);
        assert_eq!(SessionEnd::Stopped.reason(ReasonCode::InputChannelFailed), ReasonCode::InputChannelFailed);
        assert_eq!(SessionEnd::PeerTeardown.reason(ReasonCode::UserStopped), ReasonCode::PeerTeardown);
        assert_eq!(SessionEnd::PeerDisconnected.reason(ReasonCode::UserStopped), ReasonCode::PeerDisconnected);
        assert_eq!(SessionEnd::Rejected(ReasonCode::PeerRejectedId).reason(ReasonCode::UserStopped), ReasonCode::PeerRejectedId);
    }

    #[test]
    fn idr_requests_are_immediate_once_then_at_most_one_per_second() {
        let now = Instant::now();
        assert!(idr_request_due(None, now));
        assert!(!idr_request_due(Some(now), now));
        assert!(!idr_request_due(Some(now), now + Duration::from_secs(1)));
        assert!(idr_request_due(Some(now), now + Duration::from_millis(1001)));
        // A "last" time later than now (clock skew between threads) must not panic.
        assert!(!idr_request_due(Some(now + Duration::from_secs(5)), now));
    }

    #[test]
    fn rejected_connection_logging_is_bounded() {
        let mut log = RejectLog::default();
        let outcomes: Vec<_> = (0..1000).map(|_| log.note()).collect();
        assert_eq!(outcomes.iter().filter(|o| **o == Rejection::Report).count(), 3);
        assert_eq!(outcomes.iter().filter(|o| **o == Rejection::FirstSuppressed).count(), 1);
        assert_eq!(outcomes[3], Rejection::FirstSuppressed);
        assert_eq!(outcomes.iter().filter(|o| **o == Rejection::Quiet).count(), 996);
        log.count = u32::MAX;
        assert_eq!(log.note(), Rejection::Quiet, "the counter saturates instead of overflowing");
    }
}
