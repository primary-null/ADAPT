use crate::adapt_trace::{
    fail, idr_request_due, settle, tag, OrFail, ReasonCode, RejectLog, Rejection, Scope, SessionEnd, Stage,
};
use crate::{
    advertise::Advertisement,
    media::NativeVideo,
    protocol::{self, RtpReorder, RtspFramer, VideoMode},
    runtime::{AppState, Phase, VIDEO_STARTED, VIDEO_STOPPED},
    settings::Quality,
    transport_stream::TransportStream,
};
use anyhow::{Context, Result};
use socket2::{Domain, Protocol, Socket, Type};
use std::{
    io::{Read, Write},
    net::{IpAddr, Shutdown, SocketAddr, TcpListener, TcpStream, UdpSocket},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    thread,
    time::{Duration, Instant},
};
use windows::{
    Devices::WiFiDirect::*,
    Foundation::{AsyncStatus, TypedEventHandler},
    Win32::{
        Foundation::HWND,
        System::WinRT::{RoInitialize, RoUninitialize, RO_INIT_MULTITHREADED},
    },
};

struct Apartment;
impl Apartment {
    fn new() -> Result<Self> {
        unsafe {
            RoInitialize(RO_INIT_MULTITHREADED)?;
        }
        Ok(Self)
    }
}
impl Drop for Apartment {
    fn drop(&mut self) {
        unsafe {
            RoUninitialize();
        }
    }
}

pub fn start(state: Arc<AppState>) -> thread::JoinHandle<()> {
    // The window checks for stop.request once a second; a file left by an
    // earlier run would close this one at once. This runs before the first check.
    if crate::settings::data_dir().is_ok_and(|directory| crate::settings::clear_stale_stop_request(&directory)) {
        state.log("Removed a stale stop.request left by an earlier run");
    }
    thread::spawn(move || {
        let result = run(&state);
        state.advertising.store(false, Ordering::Release);
        if let Err(error) = result {
            state.log(format!("Receiver stopped: {error:#}"));
            if state.running.load(Ordering::Acquire) {
                state.record_error(Scope::Receiver, &error);
                state.set_phase(Phase::Error(error.to_string()));
            }
        }
        state.write_status();
    })
}

fn run(state: &Arc<AppState>) -> Result<()> {
    // Admission is open by default: any phone may connect. A name or peer ID
    // restricts it only when the operator sets one.
    let policy = AdmissionPolicy::from_env()?;
    state.set_admission(policy.name.as_deref(), policy.id.as_deref());
    let _apartment = Apartment::new().or_fail(ReasonCode::WifiDirectInitFailed)?;
    let guard=TcpListener::bind(("0.0.0.0",7236)).map_err(bind_failure)?;
    guard.set_nonblocking(true)?;
    let (sender, requests) = mpsc::sync_channel(1);
    let mut advertisement = Advertisement::new(state.clone())?;
    let callback_state = state.clone();
    let token = advertisement
        .listener
        .ConnectionRequested(&TypedEventHandler::new(
            move |_: &Option<WiFiDirectConnectionListener>,
                  args: &Option<WiFiDirectConnectionRequestedEventArgs>| {
                let Some(args) = args else {
                    callback_state.log("Windows delivered an empty Wi-Fi Direct request");
                    return Ok(());
                };
                if !callback_state.claim_session() {
                    callback_state.log(
                        "Ignored another connection request while a phone is already connected",
                    );
                    return Ok(());
                }
                match args.GetConnectionRequest() {
                    Ok(request) => {
                        callback_state.begin_attempt();
                        callback_state.disconnect.store(false, Ordering::Release);
                        if sender.try_send(request).is_err() {
                            callback_state
                                .session_active
                                .store(false, Ordering::Release);
                            callback_state.log("Could not queue the Wi-Fi Direct request");
                        }
                    }
                    Err(error) => {
                        callback_state
                            .session_active
                            .store(false, Ordering::Release);
                        callback_state
                            .log(format!("Windows rejected the connection request: {error}"));
                    }
                }
                Ok(())
            },
        )).or_fail(ReasonCode::WifiDirectInitFailed)?;
    advertisement.start()?;
    state.advertising.store(true, Ordering::Release);
    state.stage(Stage::Advertising);
    state.set_phase(Phase::Ready);
    while state.running.load(Ordering::Acquire) {
        let request = match requests.recv_timeout(Duration::from_millis(150)) {
            Ok(value) => value,
            Err(mpsc::RecvTimeoutError::Timeout) => continue,
            Err(error) => return Err(error.into()),
        };
        let result = connect_phone(state, &guard, &policy, request);
        record_outcome(state, &result);
        state.adapt_trace.lock().unwrap().end_attempt();
        state.session_active.store(false, Ordering::Release);
        state.live.store(false, Ordering::Release);
        *state.input.lock().unwrap() = None;
        *state.mode.lock().unwrap() = None;
        state.frames.store(0, Ordering::Release);
        state.notify(VIDEO_STOPPED);
        if !state.running.load(Ordering::Acquire) {
            break;
        }
        match result {
            Ok(_) => state.set_phase(Phase::Ready),
            Err(error) => {
                state.log(format!("Connection ended: {error:#}"));
                state.set_phase(Phase::Error(format!(
                    "Connection ended: {error}. You can reconnect from DeX."
                )));
            }
        }
    }
    advertisement.listener.RemoveConnectionRequested(token)?;
    Ok(())
}

fn connect_phone(
    state: &Arc<AppState>,
    guard: &TcpListener,
    policy: &AdmissionPolicy,
    request: WiFiDirectConnectionRequest,
) -> Result<SessionEnd> {
    drain_stale_connections(state, guard);
    let connect_failed = ReasonCode::WifiDirectConnectFailed;
    let information = request.DeviceInformation().or_fail(connect_failed)?;
    let name = information.Name().or_fail(connect_failed)?.to_string();
    let id = information.Id().or_fail(connect_failed)?.to_string();
    let decision = policy.decide(&name, &id);
    state.note_peer(&name, &id, decision == Admission::Admitted);
    let peer = state.privacy.peer_label(&name, &id);
    if decision != Admission::Admitted {
        state.stage(Stage::PeerRejected);
        state.log(format!("ADAPT rejected a transmitter ({peer}) that does not match the configured phone name or peer ID"));
        return Ok(SessionEnd::Rejected(if decision == Admission::RejectedId {
            ReasonCode::PeerRejectedId
        } else {
            ReasonCode::PeerRejectedName
        }));
    }
    state.stage(Stage::PeerAdmitted);
    state.log(format!("ADAPT admitted selected transmitter: {peer}"));
    state.set_phase(Phase::Connecting(name.clone()));
    let parameters = WiFiDirectConnectionParameters::new().or_fail(connect_failed)?;
    parameters.SetGroupOwnerIntent(15).or_fail(connect_failed)?;
    let operation = WiFiDirectDevice::FromIdAsync2(&information.Id().or_fail(connect_failed)?, &parameters).or_fail(connect_failed)?;
    let deadline = Instant::now() + Duration::from_secs(20);
    while operation.Status().or_fail(connect_failed)? == AsyncStatus::Started {
        let cancelled = stopping(state);
        if cancelled || Instant::now() > deadline {
            operation.Cancel().or_fail(connect_failed)?;
            return Err(interrupted_or(
                cancelled,
                ReasonCode::WifiDirectConnectTimeout,
                "The Wi-Fi Direct connection was cancelled",
                "The Wi-Fi Direct connection timed out",
            ));
        }
        thread::sleep(Duration::from_millis(30));
    }
    let device = operation
        .GetResults()
        .context("Windows could not connect to the phone")
        .or_fail(connect_failed)?;
    let result = (|| -> Result<SessionEnd> {
        let endpoints = device.GetConnectionEndpointPairs()?;
        let mut peer = None;
        for endpoint in endpoints {
            let local = endpoint
                .LocalHostName()?
                .CanonicalName()?
                .to_string()
                .parse::<IpAddr>();
            let remote = endpoint
                .RemoteHostName()?
                .CanonicalName()?
                .to_string()
                .parse::<IpAddr>();
            if let (Ok(IpAddr::V4(local)), Ok(IpAddr::V4(remote))) = (local, remote) {
                peer = Some((IpAddr::V4(local), IpAddr::V4(remote)));
                break;
            }
        }
        let (local, remote) = peer
            .ok_or_else(|| fail(ReasonCode::NoIpv4Endpoint, "Windows did not provide IPv4 Wi-Fi Direct endpoints"))?;
        state.stage(Stage::WifiDirectGroup);
        state.log(format!("Wi-Fi Direct connected: {}", state.privacy.link_label(local, remote)));
        let stream = match tcp(local, remote, 7236) {
            Ok(stream) => stream,
            Err(error) => {
                state.log(format!(
                    "Phone RTSP dial failed ({error}); waiting for an inbound session"
                ));
                let deadline = Instant::now() + Duration::from_secs(5);
                let mut rejected = RejectLog::default();
                loop {
                    let cancelled = stopping(state);
                    if cancelled || Instant::now() > deadline {
                        return Err(interrupted_or(
                            cancelled,
                            ReasonCode::RtspConnectFailed,
                            "The Wi-Fi Direct connection was stopped before the phone started a Miracast session",
                            "The phone did not start a Miracast session",
                        ));
                    }
                    match guard.accept(){
                        Ok((stream,address)) if address.ip()==remote=>break stream,
                        Ok(_)=>match rejected.note() {
                            Rejection::Report=>state.log("Rejected an RTSP connection from outside the connected Wi-Fi Direct peer"),
                            Rejection::FirstSuppressed=>state.log("More RTSP connections from outside the Wi-Fi Direct peer were rejected; they are no longer logged one by one"),
                            Rejection::Quiet=>{}
                        },
                        Err(error) if error.kind()==std::io::ErrorKind::WouldBlock=>thread::sleep(Duration::from_millis(30)),
                        Err(error)=>return Err(tag(error.into(), ReasonCode::RtspConnectFailed)),
                    }
                }
            }
        };
        state.stage(Stage::RtspConnected);
        // An error inside the session that nothing classified is internal, not a
        // Wi-Fi Direct connection failure.
        session(state, stream, local, remote).map_err(|error| tag(error, ReasonCode::InternalError))
    })().or_fail(connect_failed);
    if let Err(error) = device.Close() {
        state.log(format!("Could not close the Wi-Fi Direct device: {error}"));
    }
    result
}

fn stopping(state: &AppState) -> bool {
    !state.running.load(Ordering::Acquire) || state.disconnect.load(Ordering::Acquire)
}

fn tcp(local: IpAddr, remote: IpAddr, port: u16) -> Result<TcpStream> {
    let socket = Socket::new(Domain::IPV4, Type::STREAM, Some(Protocol::TCP))?;
    socket.bind(&SocketAddr::new(local, 0).into())?;
    socket.connect_timeout(
        &SocketAddr::new(remote, port).into(),
        Duration::from_secs(5),
    )?;
    let stream: TcpStream = socket.into();
    configure_tcp(&stream)?;
    Ok(stream)
}

fn configure_tcp(stream: &TcpStream) -> Result<()> {
    stream.set_nonblocking(false)?;
    stream.set_nodelay(true)?;
    stream.set_read_timeout(Some(Duration::from_millis(120)))?;
    stream.set_write_timeout(Some(Duration::from_secs(2)))?;
    Ok(())
}

fn udp_pair(local: IpAddr, state: &AppState) -> Result<(UdpSocket, UdpSocket)> {
    for _ in 0..24 {
        let socket = Socket::new(Domain::IPV4, Type::DGRAM, Some(Protocol::UDP))?;
        socket.set_recv_buffer_size(4 * 1024 * 1024)?;
        socket.bind(&SocketAddr::new(local, 0).into())?;
        let first: UdpSocket = socket.into();
        let port = first.local_addr()?.port();
        if port % 2 != 0 {
            continue;
        }
        match UdpSocket::bind(SocketAddr::new(local, port + 1)) {
            Ok(second) => {
                first.set_read_timeout(Some(Duration::from_millis(20)))?;
                second.set_nonblocking(true)?;
                return Ok((first, second));
            }
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::AddrInUse | std::io::ErrorKind::PermissionDenied
                ) =>
            {
                state.log(format!(
                    "Could not reserve the paired RTCP port: {error}; retrying an OS-selected pair"
                ));
            }
            Err(error) => return Err(error.into()),
        }
    }
    Err(fail(ReasonCode::RtpPortUnavailable, "Windows could not reserve an RTP/RTCP port pair"))
}

fn capabilities(request: &str, port: u16, quality: Quality) -> String {
    let values=[
        ("wfd_video_formats",quality.formats().to_owned()),
        ("wfd_audio_codecs","LPCM 00000002 00".into()),
        ("wfd_client_rtp_ports",format!("RTP/AVP/UDP;unicast {port} 0 mode=play")),
        ("wfd_content_protection","none".into()),
        ("wfd_display_edid","none".into()),
        ("wfd_coupled_sink","none".into()),
        ("wfd_connector_type","05".into()),
        ("wfd_idr_request_capability","1".into()),
        ("wfd_uibc_capability","input_category_list=GENERIC, HIDC;generic_cap_list=Mouse, SingleTouch, MultiTouch;hidc_cap_list=Keyboard/USB;port=none".into()),
    ];
    let mut body = String::new();
    for name in request
        .lines()
        .map(|line| line.split(':').next().unwrap_or("").trim())
        .filter(|name| !name.is_empty())
    {
        let value = values
            .iter()
            .find(|(key, _)| name.eq_ignore_ascii_case(key))
            .map(|(_, value)| value.as_str())
            .unwrap_or("none");
        body.push_str(&format!("{name}: {value}\r\n"));
    }
    body
}

fn write(stream: &mut TcpStream, message: String) -> Result<()> {
    stream
        .write_all(message.as_bytes())
        .context("Miracast control connection failed")
        .or_fail(ReasonCode::ControlConnectionFailed)
}

fn session(
    state: &Arc<AppState>,
    mut stream: TcpStream,
    local: IpAddr,
    remote: IpAddr,
) -> Result<SessionEnd> {
    configure_tcp(&stream).or_fail(ReasonCode::ControlConnectionFailed)?;
    let (rtp, rtcp) = udp_pair(local, state).or_fail(ReasonCode::RtpPortUnavailable)?;
    let rtp_port = rtp.local_addr()?.port();
    let mut udp = Some((rtp, rtcp));
    let life = Arc::new(AtomicBool::new(true));
    let recover = Arc::new(AtomicBool::new(false));
    let mut video_thread = None;
    let mut input_thread = None;
    let mut input_stream = None;
    let mut mode: Option<VideoMode> = None;
    let mut sequence = 0;
    let mut playback = PlaybackRequests::default();
    let mut session_id = String::new();
    let mut url = format!("rtsp://{remote}/wfd1.0/streamid=0");
    let mut uibc = String::new();
    let mut options = false;
    let mut framer = RtspFramer::default();
    let mut bytes = [0u8; 8192];
    let mut last_recover: Option<Instant> = None;
    let quality = state.settings.lock().unwrap().quality;
    let started = Instant::now();
    let mut first_frame = false;
    let mut last_message = started;
    let mut last_frames = state.frames.load(Ordering::Acquire);
    let mut partial_since: Option<Instant> = None;
    state.log(format!(
        "Starting Miracast; quality target: {}",
        quality.label()
    ));

    let result = (|| -> Result<SessionEnd> {
        while !stopping(state) && life.load(Ordering::Acquire) {
            let frames = state.frames.load(Ordering::Acquire);
            first_frame |= frames > 0;
            let now = Instant::now();
            if frames != last_frames {
                last_frames = frames;
                last_message = now;
            }
            check_deadlines(now, started, first_frame, last_message, partial_since)?;
            if recover.load(Ordering::Acquire)
                && idr_request_due(last_recover, now)
                && !session_id.is_empty()
            {
                recover.store(false, Ordering::Release);
                sequence += 1;
                write(
                    &mut stream,
                    protocol::rtsp(
                        &format!("SET_PARAMETER {url} RTSP/1.0"),
                        sequence,
                        "wfd_idr_request\r\n",
                        &format!("Session: {session_id}\r\n"),
                    ),
                )?;
                last_recover = Some(Instant::now());
            }
            match stream.read(&mut bytes) {
                Ok(0) => return Ok(SessionEnd::PeerDisconnected),
                Ok(count) => {
                    framer.push(&bytes[..count]).or_fail(ReasonCode::RtspProtocolError)?;
                    partial_since.get_or_insert_with(Instant::now);
                }
                Err(error)
                    if matches!(
                        error.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                    ) =>
                {
                    continue
                }
                Err(error) => return Err(tag(error.into(), ReasonCode::ControlConnectionFailed)),
            }
            while let Some(message) = framer.next_message().or_fail(ReasonCode::RtspProtocolError)? {
                last_message = Instant::now();
                partial_since = if framer.has_buffered_bytes() { Some(last_message) } else { None };
                let received = message.sequence().or_fail(ReasonCode::RtspProtocolError)?;
                if message.response() {
                    let answers_sent_request = received != 0 && received <= sequence;
                    if answers_sent_request && !successful_response(&message.start) {
                        return Err(fail(ReasonCode::RtspRequestRejected, format!("The phone rejected a control request: {}", message.start)));
                    }
                    let Some(response) = playback.consume(received) else { continue };
                    if response == PlaybackResponse::Setup {
                        session_id = message
                            .headers
                            .get("session")
                            .context("Missing Miracast session identifier")
                            .or_fail(ReasonCode::RtspProtocolError)?
                            .split(';')
                            .next()
                            .unwrap_or("")
                            .to_owned();
                        sequence += 1;
                        playback.play = Some(sequence);
                        write(
                            &mut stream,
                            protocol::rtsp(
                                &format!("PLAY {url} RTSP/1.0"),
                                sequence,
                                "",
                                &format!("Session: {session_id}\r\n"),
                            ),
                        )?;
                    } else if response == PlaybackResponse::Play {
                        if input_thread.is_some() || input_stream.is_some() {
                            return Err(fail(ReasonCode::RtspProtocolError, "The phone attempted to start a second input channel"));
                        }
                        state.stage(Stage::PlaybackAccepted);
                        let selected = mode.context("The phone did not select a video format").or_fail(ReasonCode::RtspProtocolError)?;
                        let (port, pointer, keyboard) = protocol::input_capability(&uibc);
                        if port == 0 || !pointer || !keyboard {
                            return Err(fail(ReasonCode::InputChannelUnavailable, "This phone did not allow the required mouse/keyboard input channel"));
                        }
                        sequence += 1;
                        write(
                            &mut stream,
                            protocol::rtsp(
                                &format!("SET_PARAMETER {url} RTSP/1.0"),
                                sequence,
                                "wfd_uibc_setting: enable\r\n",
                                &format!("Session: {session_id}\r\n"),
                            ),
                        )?;
                        let mut input = tcp(local, remote, port).or_fail(ReasonCode::InputChannelFailed)?;
                        input.write_all(&protocol::keyboard_descriptor()).or_fail(ReasonCode::InputChannelFailed)?;
                        state.stage(Stage::InputChannelConnected);
                        let (sender, receiver) = mpsc::sync_channel::<Vec<u8>>(512);
                        *state.input.lock().unwrap() = Some(sender);
                        input_stream = Some(input.try_clone().or_fail(ReasonCode::InputChannelFailed)?);
                        let worker_state = state.clone();
                        let worker_life = life.clone();
                        input_thread = Some(thread::spawn(move || {
                            let mut outcome: Result<()> = Ok(());
                            while worker_life.load(Ordering::Acquire) && !stopping(&worker_state) {
                                match receiver.recv_timeout(Duration::from_millis(100)) {
                                    Ok(packet) => {
                                        if let Err(error) = input.write_all(&packet) {
                                            worker_state
                                                .log(format!("Input connection failed: {error}"));
                                            worker_life.store(false, Ordering::Release);
                                            outcome = Err(fail(ReasonCode::InputChannelFailed, format!("Input connection failed: {error}")));
                                            break;
                                        }
                                    }
                                    Err(mpsc::RecvTimeoutError::Timeout) => {}
                                    Err(_) => break,
                                }
                            }
                            let _ = input.shutdown(Shutdown::Both);
                            outcome
                        }));
                        state.log(format!("Session ready: {}x{} at {} FPS, generic pointer and native HID keyboard",selected.width,selected.height,selected.fps));
                        recover.store(true, Ordering::Release);
                    }
                    continue;
                }
                match message.method() {
                    "OPTIONS" => {
                        write(&mut stream,protocol::rtsp("RTSP/1.0 200 OK",received,"","Public: org.wfa.wfd1.0, OPTIONS, GET_PARAMETER, SET_PARAMETER, SETUP, PLAY, TEARDOWN\r\n"))?;
                        if !options {
                            options = true;
                            sequence += 1;
                            write(
                                &mut stream,
                                protocol::rtsp(
                                    "OPTIONS * RTSP/1.0",
                                    sequence,
                                    "",
                                    "Require: org.wfa.wfd1.0\r\n",
                                ),
                            )?;
                        }
                    }
                    "GET_PARAMETER" => write(
                        &mut stream,
                        protocol::rtsp(
                            "RTSP/1.0 200 OK",
                            received,
                            &if message.body.is_empty() {
                                String::new()
                            } else {
                                capabilities(&message.body, rtp_port, quality)
                            },
                            "",
                        ),
                    )?,
                    "SET_PARAMETER" => {
                        let parameters = protocol::parameters(&message.body);
                        if let Some(value) = parameters.get("wfd_video_formats") {
                            let selected = VideoMode::from_wfd(value).or_fail(ReasonCode::UnsupportedVideoFormat)?;
                            if mode.is_some_and(|old| old != selected) {
                                return Err(fail(
                                    ReasonCode::UnsupportedVideoFormat,
                                    "The phone changed video mode; reconnect to apply the new mode",
                                ));
                            }
                            mode = Some(selected);
                            state.stage(Stage::VideoNegotiated);
                            *state.mode.lock().unwrap() = mode;
                            state.log(format!(
                                "Phone selected {}x{} / {} FPS",
                                selected.width, selected.height, selected.fps
                            ));
                            if let Some((rtp, rtcp)) = udp.take() {
                                video_thread = Some(video_worker(
                                    state.clone(),
                                    rtp,
                                    rtcp,
                                    remote,
                                    selected,
                                    life.clone(),
                                    recover.clone(),
                                ));
                            }
                        }
                        if let Some(value) = parameters.get("wfd_uibc_capability") {
                            uibc = value.clone();
                        }
                        if let Some(value) = parameters.get("wfd_presentation_url") {
                            url = value
                                .split_whitespace()
                                .next()
                                .context("Empty phone presentation URL")
                                .or_fail(ReasonCode::RtspProtocolError)?
                                .to_owned();
                        }
                        write(
                            &mut stream,
                            protocol::rtsp("RTSP/1.0 200 OK", received, "", ""),
                        )?;
                        match parameters.get("wfd_trigger_method").map(String::as_str) {
                            Some("SETUP") => {
                                sequence += 1;
                                playback.begin_setup(sequence).or_fail(ReasonCode::RtspProtocolError)?;
                                write(&mut stream,protocol::rtsp(&format!("SETUP {url} RTSP/1.0"),sequence,"",
                                    &format!("Transport: RTP/AVP/UDP;unicast;client_port={rtp_port}-{}\r\n",rtp_port+1)))?;
                            }
                            Some("TEARDOWN") => return Ok(SessionEnd::PeerTeardown),
                            _ => {}
                        }
                    }
                    "TEARDOWN" => {
                        write(
                            &mut stream,
                            protocol::rtsp("RTSP/1.0 200 OK", received, "", ""),
                        )?;
                        return Ok(SessionEnd::PeerTeardown);
                    }
                    _ => write(
                        &mut stream,
                        protocol::rtsp("RTSP/1.0 405 Method Not Allowed", received, "", ""),
                    )?,
                }
            }
        }
        Ok(SessionEnd::Stopped)
    })();
    life.store(false, Ordering::Release);
    *state.input.lock().unwrap() = None;
    if let Some(input) = input_stream {
        let _ = input.shutdown(Shutdown::Both);
    }
    let _ = stream.shutdown(Shutdown::Both);
    let input_outcome = input_thread.map(|worker| {
        worker.join().unwrap_or_else(|_| Err(fail(ReasonCode::InternalError, "Input worker stopped unexpectedly")))
    });
    let video_outcome = video_thread.map(|worker| {
        worker.join().unwrap_or_else(|_| Err(fail(ReasonCode::InternalError, "Windows video worker stopped unexpectedly")))
    });
    let (outcome, dropped) = settle(result, video_outcome.into_iter().chain(input_outcome).collect());
    for error in dropped {
        state.log(format!("Another error ended the session as well: {error}"));
    }
    outcome
}

fn video_worker(
    state: Arc<AppState>,
    rtp: UdpSocket,
    rtcp: UdpSocket,
    remote: IpAddr,
    mode: VideoMode,
    life: Arc<AtomicBool>,
    recover: Arc<AtomicBool>,
) -> thread::JoinHandle<Result<()>> {
    thread::spawn(move || {
        let result = (|| -> Result<()> {
            let _apartment = Apartment::new().or_fail(ReasonCode::DecoderInitFailed)?;
            let window = HWND(state.video_hwnd.load(Ordering::Acquire) as *mut _);
            let mut video = NativeVideo::new(window, mode).or_fail(ReasonCode::DecoderInitFailed)?;
            *state.backend.lock().unwrap() = if video.hardware {
                "Windows H.264 / DXGI / Direct3D 11"
            } else {
                "Windows H.264 / CPU upload / Direct3D 11"
            }
            .into();
            let mut demux = TransportStream::default();
            let mut audio = crate::audio::for_session(&state, &mut demux); // AUDIO HOOK 1/2 (None unless enabled)
            let mut reorder = RtpReorder::default();
            let mut packet = [0u8; 65536];
            let started = Instant::now();
            while life.load(Ordering::Acquire) && !stopping(&state) {
                let batch = match rtp.recv_from(&mut packet) {
                    Ok((length, peer)) if peer.ip() == remote => {
                        let Some((source, sequence, payload)) =
                            protocol::rtp_payload(&packet[..length])
                        else {
                            continue;
                        };
                        reorder.push(
                            source,
                            sequence,
                            payload,
                            started.elapsed().as_millis() as u64,
                        )
                    }
                    Ok(_) => continue,
                    Err(error)
                        if matches!(
                            error.kind(),
                            std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                        ) =>
                    {
                        reorder.flush(started.elapsed().as_millis() as u64)
                    }
                    Err(error) => return Err(tag(error.into(), ReasonCode::MediaStreamError)),
                };
                if batch.1 > 0 {
                    state.log(format!(
                        "Video recovery: {} missing RTP sequence numbers",
                        batch.1
                    ));
                    demux.discard();
                    recover.store(true, Ordering::Release);
                }
                for payload in batch.0 {
                    for picture in demux.push(&payload).or_fail(ReasonCode::MediaStreamError)? {
                        let count = video.push(&picture.bytes, picture.discontinuity).or_fail(ReasonCode::DecoderFailed)?;
                        state.frames.store(count, Ordering::Release);
                        if count > 0 && !state.live.swap(true, Ordering::AcqRel) {
                            state.stage(Stage::FirstFrameDecoded);
                            state.set_phase(Phase::Connected);
                            state.notify(VIDEO_STARTED);
                        }
                    }
                }
                if let Some(audio) = audio.as_mut() { audio.pump(&mut demux); } // AUDIO HOOK 2/2
                let mut control = [0u8; 1500];
                // Count every datagram, including foreign traffic, against the budget.
                for _ in 0..32 {
                    if !life.load(Ordering::Acquire) || stopping(&state) {
                        break;
                    }
                    match rtcp.recv_from(&mut control) {
                        Ok((_, peer)) if peer.ip() == remote => {}
                        Ok(_) => continue,
                        Err(_) => break,
                    }
                }
            }
            Ok(())
        })();
        if let Err(error) = &result {
            state.log(format!("Native video failed: {error:#}"));
            life.store(false, Ordering::Release);
        }
        result
    })
}

// Open by default. A configured name and a configured peer ID are each an
// optional restriction; an unset or empty value restricts nothing. Neither is
// cryptographic identity proof.
fn peer_matches(expected: Option<&str>, expected_id: Option<&str>, name: &str, id: &str) -> bool {
    let allows = |wanted: Option<&str>, actual: &str| wanted.is_none_or(|value| value.is_empty() || value == actual);
    allows(expected, name) && allows(expected_id, id)
}

#[derive(Debug, Eq, PartialEq)]
enum Admission {
    Admitted,
    RejectedName,
    RejectedId,
}

// A phone that fails both filters is reported as failing the name filter.
fn admission(expected: Option<&str>, expected_id: Option<&str>, name: &str, id: &str) -> Admission {
    if !peer_matches(expected, None, name, id) {
        Admission::RejectedName
    } else if !peer_matches(None, expected_id, name, id) {
        Admission::RejectedId
    } else {
        Admission::Admitted
    }
}

// The optional filters, read and checked once at startup. A filter that is set
// but cannot be used is a startup error: silently ignoring it would turn a
// restricted receiver into an open one.
struct AdmissionPolicy {
    name: Option<String>,
    id: Option<String>,
}

impl AdmissionPolicy {
    fn from_env() -> Result<Self> {
        Self::from_values(std::env::var_os("ADAPT_DEX_EXPECTED_NAME"), std::env::var_os("ADAPT_DEX_EXPECTED_ID"))
    }
    fn from_values(name: Option<std::ffi::OsString>, id: Option<std::ffi::OsString>) -> Result<Self> {
        Ok(Self {
            name: filter_value("ADAPT_DEX_EXPECTED_NAME", name, 256)?,
            id: filter_value("ADAPT_DEX_EXPECTED_ID", id, 1024)?,
        })
    }
    fn decide(&self, name: &str, id: &str) -> Admission {
        admission(self.name.as_deref(), self.id.as_deref(), name, id)
    }
}

fn filter_value(variable: &str, value: Option<std::ffi::OsString>, limit: usize) -> Result<Option<String>> {
    let Some(value) = value else { return Ok(None) };
    let Ok(value) = value.into_string() else {
        return Err(fail(
            ReasonCode::InvalidConfiguration,
            format!("{variable} is not valid Unicode, so it cannot restrict admission; fix or unset it"),
        ));
    };
    if value.len() > limit || value.chars().any(char::is_control) {
        return Err(fail(
            ReasonCode::InvalidConfiguration,
            format!("{variable} must be at most {limit} characters with no control characters"),
        ));
    }
    Ok(Some(value))
}

fn control_port_failure(kind: std::io::ErrorKind) -> ReasonCode {
    match kind {
        std::io::ErrorKind::AddrInUse | std::io::ErrorKind::PermissionDenied => ReasonCode::PortInUse,
        _ => ReasonCode::InternalError,
    }
}

fn bind_failure(error: std::io::Error) -> anyhow::Error {
    let code = control_port_failure(error.kind());
    let message = if code == ReasonCode::PortInUse {
        "Another wireless-display receiver is using this PC. Close Windows Wireless Display or the earlier test app, then reopen DeXDesk."
    } else {
        "Windows could not listen on TCP port 7236 for the Miracast session"
    };
    tag(anyhow::Error::from(error).context(message), code)
}

// A stop request wins over the failure it interrupted: cancelling is not an error.
fn interrupted_or(cancelled: bool, code: ReasonCode, cancelled_text: &'static str, failed_text: &'static str) -> anyhow::Error {
    if cancelled {
        fail(ReasonCode::UserStopped, cancelled_text)
    } else {
        fail(code, failed_text)
    }
}

// The guard listener is only polled while a session is being set up. A connection
// that reached it earlier (a phone that dialled in as well as being dialled, or
// any host that can reach port 7236) is still queued by the OS and would
// otherwise be taken for the next session's control connection. Called before
// the Wi-Fi Direct group is formed, so no connection of the new session can be
// in the queue yet.
fn drain_stale_connections(state: &AppState, guard: &TcpListener) {
    let mut closed = 0u32;
    while closed < 256 && guard.accept().is_ok() {
        closed += 1;
    }
    if closed > 0 {
        state.log(format!("Closed {closed} unclaimed connection(s) that were queued on TCP 7236 before this attempt"));
    }
}

fn end_text(code: ReasonCode) -> &'static str {
    match code {
        ReasonCode::UserStopped => "The session was stopped from this PC",
        ReasonCode::InputChannelFailed => "The mouse/keyboard channel failed and the session was ended",
        ReasonCode::PeerTeardown => "The phone ended the session with TEARDOWN",
        ReasonCode::PeerDisconnected => "The phone closed the control connection without TEARDOWN",
        ReasonCode::PeerRejectedName => "The phone's name does not match ADAPT_DEX_EXPECTED_NAME",
        ReasonCode::PeerRejectedId => "The phone's peer ID does not match ADAPT_DEX_EXPECTED_ID",
        other => other.as_str(),
    }
}

// Write how the attempt ended to the log and to status.json. It runs before the
// attempt is closed so the recorded stage and attempt number are its own.
fn record_outcome(state: &AppState, result: &Result<SessionEnd>) {
    match result {
        Ok(end) => {
            let code = end.reason(state.take_stop_reason());
            state.record_end(code, Scope::Session, end_text(code));
        }
        Err(error) => state.record_error(Scope::Session, error),
    }
}

// Allow slow group/decoder startup, a bounded partial message, and at least
// four typical WFD keep-alive intervals (GET_PARAMETER is about every 30s).
const HANDSHAKE_LIMIT: Duration = Duration::from_secs(90);
const PARTIAL_MESSAGE_LIMIT: Duration = Duration::from_secs(10);
const CONTROL_IDLE_LIMIT: Duration = Duration::from_secs(150);

fn check_deadlines(now: Instant, started: Instant, first_frame: bool, last_message: Instant, partial_since: Option<Instant>) -> Result<()> {
    if !first_frame && now.duration_since(started) >= HANDSHAKE_LIMIT {
        return Err(fail(ReasonCode::HandshakeTimeout, "Miracast handshake timed out before the first decoded frame"));
    }
    if partial_since.is_some_and(|since| now.duration_since(since) >= PARTIAL_MESSAGE_LIMIT) {
        return Err(fail(ReasonCode::PartialMessageTimeout, "Timed out completing a partial RTSP message"));
    }
    if first_frame && now.duration_since(last_message) >= CONTROL_IDLE_LIMIT {
        return Err(fail(ReasonCode::ControlIdleTimeout, "Miracast control connection idle timeout"));
    }
    Ok(())
}

#[derive(Default)]
struct PlaybackRequests {
    setup_started: bool,
    setup: Option<u32>,
    play: Option<u32>,
}

#[derive(Debug, PartialEq)]
enum PlaybackResponse { Setup, Play }

impl PlaybackRequests {
    fn begin_setup(&mut self, sequence: u32) -> Result<()> {
        if self.setup_started {
            return Err(fail(ReasonCode::RtspProtocolError, "The phone requested SETUP more than once"));
        }
        self.setup_started = true;
        self.setup = Some(sequence);
        Ok(())
    }

    fn consume(&mut self, sequence: u32) -> Option<PlaybackResponse> {
        if self.setup == Some(sequence) {
            self.setup.take();
            Some(PlaybackResponse::Setup)
        } else if self.play == Some(sequence) {
            self.play.take();
            Some(PlaybackResponse::Play)
        } else {
            None
        }
    }
}

fn successful_response(start: &str) -> bool {
    start.starts_with("RTSP/1.0 200 ")
}

#[cfg(test)]
mod adapt_failure_tests {
    use super::*;
    use crate::adapt_trace::classify;
    use std::{ffi::OsString, os::windows::ffi::OsStringExt};

    fn code_of(result: Result<()>) -> ReasonCode {
        classify(&result.unwrap_err())
    }

    #[test]
    fn the_admission_decision_names_the_filter_that_refused_the_phone() {
        assert_eq!(admission(None, None, "any", "any"), Admission::Admitted);
        assert_eq!(admission(Some("a"), Some("i"), "a", "i"), Admission::Admitted);
        assert_eq!(admission(Some("a"), None, "b", "i"), Admission::RejectedName);
        assert_eq!(admission(None, Some("i"), "a", "j"), Admission::RejectedId);
        assert_eq!(admission(Some("a"), Some("i"), "a", "j"), Admission::RejectedId);
        assert_eq!(admission(Some("a"), Some("i"), "b", "j"), Admission::RejectedName, "name is checked first");
        assert_eq!(admission(Some(""), Some(""), "x", "y"), Admission::Admitted, "empty restricts nothing");
    }

    #[test]
    fn unset_filters_keep_admission_open() {
        let policy = AdmissionPolicy::from_values(None, None).unwrap();
        assert_eq!(policy.decide("any phone", "any id"), Admission::Admitted);
        let policy = AdmissionPolicy::from_values(Some("".into()), Some("".into())).unwrap();
        assert_eq!(policy.decide("any phone", "any id"), Admission::Admitted);
        let policy = AdmissionPolicy::from_values(Some("Phone".into()), Some("id-1".into())).unwrap();
        assert_eq!(policy.decide("Phone", "id-1"), Admission::Admitted);
        assert_eq!(policy.decide("Other", "id-1"), Admission::RejectedName);
        assert_eq!(policy.decide("Phone", "id-2"), Admission::RejectedId);
    }

    #[test]
    fn a_filter_that_cannot_be_read_is_an_error_not_an_open_door() {
        // A Windows environment value may hold an unpaired surrogate, which is not
        // valid Unicode. Reading it with `env::var(..).ok()` used to give None and
        // silently disable the restriction.
        let not_unicode = OsString::from_wide(&[0x0050, 0xD800, 0x0051]);
        let name = AdmissionPolicy::from_values(Some(not_unicode.clone()), None);
        assert_eq!(classify(&name.err().unwrap()), ReasonCode::InvalidConfiguration);
        let id = AdmissionPolicy::from_values(None, Some(not_unicode));
        let error = id.err().unwrap();
        assert_eq!(classify(&error), ReasonCode::InvalidConfiguration);
        assert!(error.to_string().contains("ADAPT_DEX_EXPECTED_ID"));
    }

    #[test]
    fn filter_values_are_bounded_and_free_of_control_characters() {
        assert!(filter_value("V", Some("x".repeat(256).into()), 256).is_ok());
        assert_eq!(code_of(filter_value("V", Some("x".repeat(257).into()), 256).map(drop)), ReasonCode::InvalidConfiguration);
        assert_eq!(code_of(filter_value("V", Some("a\nb".into()), 256).map(drop)), ReasonCode::InvalidConfiguration);
        assert_eq!(code_of(AdmissionPolicy::from_values(Some("a\u{7}".into()), None).map(drop)), ReasonCode::InvalidConfiguration);
        assert_eq!(code_of(AdmissionPolicy::from_values(None, Some("x".repeat(1025).into())).map(drop)), ReasonCode::InvalidConfiguration);
        assert_eq!(filter_value("V", None, 256).unwrap(), None);
        assert_eq!(filter_value("V", Some("Galaxy".into()), 256).unwrap().as_deref(), Some("Galaxy"));
    }

    #[test]
    fn a_busy_control_port_is_told_apart_from_other_bind_failures() {
        use std::io::ErrorKind::*;
        assert_eq!(control_port_failure(AddrInUse), ReasonCode::PortInUse);
        assert_eq!(control_port_failure(PermissionDenied), ReasonCode::PortInUse);
        assert_eq!(control_port_failure(AddrNotAvailable), ReasonCode::InternalError);
        let busy = bind_failure(std::io::Error::from(AddrInUse));
        assert_eq!(classify(&busy), ReasonCode::PortInUse);
        assert!(busy.to_string().starts_with("Another wireless-display receiver"));
        let other = bind_failure(std::io::Error::from(AddrNotAvailable));
        assert_eq!(classify(&other), ReasonCode::InternalError);
        assert!(!other.to_string().starts_with("Another wireless-display receiver"));
    }

    #[test]
    fn deadline_failures_carry_their_own_codes() {
        let start = Instant::now();
        let code = |now, first, last, partial| code_of(check_deadlines(now, start, first, last, partial));
        assert_eq!(code(start + HANDSHAKE_LIMIT, false, start + HANDSHAKE_LIMIT, None), ReasonCode::HandshakeTimeout);
        assert_eq!(code(start + PARTIAL_MESSAGE_LIMIT, false, start, Some(start)), ReasonCode::PartialMessageTimeout);
        assert_eq!(code(start + CONTROL_IDLE_LIMIT, true, start, None), ReasonCode::ControlIdleTimeout);
        // The handshake limit is reported before a partial message, and a partial
        // message before an idle control connection.
        assert_eq!(code(start + HANDSHAKE_LIMIT, false, start, Some(start)), ReasonCode::HandshakeTimeout);
        assert_eq!(code(start + CONTROL_IDLE_LIMIT, true, start, Some(start)), ReasonCode::PartialMessageTimeout);
        // A "last message" or partial start that lies after `now` does not panic or fire.
        assert!(check_deadlines(start, start, true, start + Duration::from_secs(500), Some(start + Duration::from_secs(500))).is_ok());
    }

    #[test]
    fn a_repeated_setup_request_is_a_protocol_error() {
        let mut pending = PlaybackRequests::default();
        pending.begin_setup(1).unwrap();
        assert_eq!(code_of(pending.begin_setup(2)), ReasonCode::RtspProtocolError);
    }

    #[test]
    fn a_stop_request_beats_the_timeout_it_interrupted() {
        let stopped = interrupted_or(true, ReasonCode::WifiDirectConnectTimeout, "cancelled", "timed out");
        assert_eq!(classify(&stopped), ReasonCode::UserStopped);
        assert_eq!(stopped.to_string(), "cancelled");
        let timed_out = interrupted_or(false, ReasonCode::WifiDirectConnectTimeout, "cancelled", "timed out");
        assert_eq!(classify(&timed_out), ReasonCode::WifiDirectConnectTimeout);
        assert_eq!(timed_out.to_string(), "timed out");
    }

    #[test]
    fn every_session_end_has_a_readable_text() {
        for code in ReasonCode::ALL {
            assert!(!end_text(*code).is_empty());
        }
        assert_eq!(end_text(ReasonCode::PeerTeardown), "The phone ended the session with TEARDOWN");
    }
}

#[cfg(test)]
mod adapt_admission_tests {
    use super::*;

    #[test]
    fn admission_is_open_by_default() {
        assert!(peer_matches(None, None, "any phone", "any-id"));
        assert!(peer_matches(Some(""), Some(""), "any phone", "any-id"));
        assert!(peer_matches(None, None, "", ""));
    }

    #[test]
    fn a_configured_name_restricts_admission() {
        assert!(peer_matches(Some("selected"), None, "selected", "id"));
        assert!(!peer_matches(Some("selected"), None, "another", "id"));
        assert!(!peer_matches(Some("selected"), Some(""), "Selected", "id"));
    }

    #[test]
    fn a_configured_peer_id_restricts_admission() {
        assert!(peer_matches(None, Some("expected-id"), "any phone", "expected-id"));
        assert!(!peer_matches(None, Some("expected-id"), "any phone", "other-id"));
        assert!(peer_matches(Some("selected"), Some("expected-id"), "selected", "expected-id"));
        assert!(!peer_matches(Some("selected"), Some("expected-id"), "selected", "other-id"));
        assert!(!peer_matches(Some("selected"), Some("expected-id"), "another", "expected-id"));
    }

    #[test]
    fn responses_require_outstanding_request_and_are_consumed_once() {
        let mut pending = PlaybackRequests::default();
        assert_eq!(pending.consume(0), None);
        pending.begin_setup(3).unwrap();
        assert_eq!(pending.consume(4), None);
        assert_eq!(pending.consume(3), Some(PlaybackResponse::Setup));
        assert_eq!(pending.consume(3), None);
        assert!(pending.begin_setup(5).is_err());
        assert_eq!(pending.consume(4), None);
        pending.play = Some(4);
        assert_eq!(pending.consume(4), Some(PlaybackResponse::Play));
        assert_eq!(pending.consume(4), None);
        assert!(successful_response("RTSP/1.0 200 OK"));
        assert!(!successful_response("RTSP/1.0 2000 OK"));
        assert!(!successful_response("RTSP/1.0 200"));
    }

    #[test]
    fn session_deadlines_bound_silent_and_partial_peers() {
        let start = Instant::now();
        assert!(check_deadlines(start + HANDSHAKE_LIMIT, start, false, start + HANDSHAKE_LIMIT, None).is_err());
        assert!(check_deadlines(start + PARTIAL_MESSAGE_LIMIT, start, false, start, Some(start)).is_err());
        assert!(check_deadlines(start + CONTROL_IDLE_LIMIT, start, true, start, None).is_err());
        assert!(check_deadlines(start + Duration::from_secs(70), start, true, start, None).is_ok());
        assert!(check_deadlines(start + Duration::from_secs(9), start, false, start, Some(start)).is_ok());
    }
}
