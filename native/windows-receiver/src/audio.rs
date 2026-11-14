//! Experimental Wi-Fi Display audio playback through WASAPI (shared mode).
//!
//! OFF by default and UNVERIFIED on hardware: enable it with the `audio`
//! setting or `ADAPT_DEX_AUDIO=1`. The demuxer (`transport_stream`) turns the
//! phone's LPCM stream into `AudioFrame`s; this module plays them.
//!
//! Design:
//! * The video thread only ever calls `AudioSink::pump`, which moves frames into
//!   a bounded queue under a short lock. It never blocks on the audio device.
//! * One render thread owns every COM object. It opens the default render
//!   device lazily (when the first frame shows the format), feeds it from the
//!   queue and stops when asked.
//! * Sample-rate and channel conversion are done by the Windows audio engine:
//!   the stream is opened in the sender's own PCM format with
//!   `AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY`,
//!   so no resampler is needed here and no new crate is involved.
//! * Every failure disables audio for the session and is logged; video is never
//!   affected.
//!
//! The pure policy (queue, drift trimming, buffering and underrun decisions,
//! sample writing) is separated from the COM calls and unit tested.

use crate::transport_stream::{AudioFrame, TransportStream};
use anyhow::{bail, Result};
use std::{
    collections::VecDeque,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex, MutexGuard,
    },
    thread,
    time::Duration,
};

/// Environment variable that overrides the `audio` setting (`1`/`0`).
pub const ENV_VAR: &str = "ADAPT_DEX_AUDIO";

/// Queued audio above this is trimmed (oldest dropped) so delay cannot grow.
const QUEUE_MAX_MS: u64 = 120;
/// A trim drops down to this much, so one glitch buys a long calm stretch.
const QUEUE_TRIM_MS: u64 = 60;
/// Audio the queue must hold before playback (re)starts.
const PREFILL_MS: u64 = 40;
/// Most audio kept inside the device buffer; the rest waits in the queue.
const DEVICE_TARGET_MS: u64 = 60;
/// Device buffer requested from WASAPI.
const DEVICE_BUFFER_MS: i64 = 120;
/// Render thread polling period.
const TICK: Duration = Duration::from_millis(8);

/// Resolves whether audio is on: a recognised environment value wins over the
/// persisted setting; anything else leaves the setting in charge.
pub fn enabled(setting: bool, env: Option<&str>) -> bool {
    match env.map(|value| value.trim().to_ascii_lowercase()).as_deref() {
        Some("1" | "true" | "yes" | "on") => true,
        Some("0" | "false" | "no" | "off") => false,
        _ => setting,
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct AudioFormat {
    pub sample_rate: u32,
    pub channels: u16,
}

impl AudioFormat {
    fn of(frame: &AudioFrame) -> Self {
        Self {
            sample_rate: frame.sample_rate,
            channels: frame.channels,
        }
    }

    /// Mono or stereo 16-bit PCM at a plain rate; the audio engine converts the
    /// rest. The receiver only advertises 48 kHz stereo.
    pub fn supported(self) -> bool {
        (1..=2).contains(&self.channels) && (8_000..=192_000).contains(&self.sample_rate)
    }
}

fn ms_to_frames(sample_rate: u32, ms: u64) -> usize {
    (u64::from(sample_rate) * ms / 1000) as usize
}

/// What `PcmQueue::push` did with a frame.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Push {
    /// Queued; `dropped_frames` older sample frames were trimmed to stay bounded.
    Accepted { dropped_frames: usize },
    /// The format differs from what is already queued: audio cannot continue.
    FormatChanged,
    /// Unusable (zero rate or channels).
    Rejected,
}

/// Bounded queue of interleaved 16-bit samples with a single format.
#[derive(Default)]
pub struct PcmQueue {
    format: Option<AudioFormat>,
    samples: VecDeque<i16>,
}

impl PcmQueue {
    pub fn format(&self) -> Option<AudioFormat> {
        self.format
    }

    /// Sample frames (one sample per channel) waiting.
    pub fn frames(&self) -> usize {
        match self.format {
            Some(format) => self.samples.len() / usize::from(format.channels.max(1)),
            None => 0,
        }
    }

    /// Appends whole sample frames. Past `QUEUE_MAX_MS` the oldest audio is
    /// dropped down to `QUEUE_TRIM_MS` instead of letting the delay grow.
    pub fn push(&mut self, format: AudioFormat, samples: &[i16]) -> Push {
        if format.channels == 0 || format.sample_rate == 0 {
            return Push::Rejected;
        }
        match self.format {
            Some(current) if current != format => return Push::FormatChanged,
            _ => self.format = Some(format),
        }
        let channels = usize::from(format.channels);
        let whole = samples.len() - samples.len() % channels;
        self.samples.extend(&samples[..whole]);
        let max = ms_to_frames(format.sample_rate, QUEUE_MAX_MS).max(1);
        let mut dropped_frames = 0;
        if self.frames() > max {
            let keep = ms_to_frames(format.sample_rate, QUEUE_TRIM_MS).min(max);
            dropped_frames = self.frames() - keep;
            self.samples.drain(..dropped_frames * channels);
        }
        Push::Accepted { dropped_frames }
    }

    /// Removes up to `max_frames` sample frames from the front.
    pub fn take(&mut self, max_frames: usize) -> Vec<i16> {
        let channels = self.format.map_or(1, |format| usize::from(format.channels.max(1)));
        let count = max_frames.min(self.frames()) * channels;
        self.samples.drain(..count).collect()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RenderState {
    /// Waiting for enough audio before (re)starting playback.
    Buffering,
    Playing,
}

/// Decides how much queued audio to hand the device on each tick.
pub struct RenderPlanner {
    state: RenderState,
    prefill: usize,
    target: usize,
    underruns: u64,
}

impl RenderPlanner {
    pub fn new(sample_rate: u32) -> Self {
        Self {
            state: RenderState::Buffering,
            prefill: ms_to_frames(sample_rate, PREFILL_MS).max(1),
            target: ms_to_frames(sample_rate, DEVICE_TARGET_MS).max(1),
            underruns: 0,
        }
    }

    pub fn state(&self) -> RenderState {
        self.state
    }

    pub fn underruns(&self) -> u64 {
        self.underruns
    }

    /// Sample frames to write now. `queued` is the queue length, `padding` the
    /// frames already inside the device buffer and `capacity` its total size.
    /// Playback waits for `PREFILL_MS` of audio; if the device runs dry with
    /// nothing queued it counts an underrun and waits for a fresh prefill.
    pub fn plan(&mut self, queued: usize, padding: usize, capacity: usize) -> usize {
        match self.state {
            RenderState::Buffering => {
                if queued < self.prefill {
                    return 0;
                }
                self.state = RenderState::Playing;
            }
            RenderState::Playing => {
                if padding == 0 && queued == 0 {
                    self.state = RenderState::Buffering;
                    self.underruns += 1;
                    return 0;
                }
            }
        }
        let room = self.target.min(capacity).saturating_sub(padding);
        room.min(queued)
    }
}

/// Whether the n-th occurrence of a repeating event is worth a log line: the
/// first few, then every hundredth.
pub fn should_log(count: u64) -> bool {
    count != 0 && (count <= 5 || count % 100 == 0)
}

/// Writes samples into a device buffer as little-endian 16-bit PCM and returns
/// how many samples fit.
pub fn write_pcm16_le(destination: &mut [u8], samples: &[i16]) -> usize {
    let mut written = 0;
    for (slot, sample) in destination.chunks_exact_mut(2).zip(samples) {
        slot.copy_from_slice(&sample.to_le_bytes());
        written += 1;
    }
    written
}

/// Where audio messages go (the receiver passes `AppState::log`).
pub type LogFn = Arc<dyn Fn(&str) + Send + Sync>;

struct Shared {
    queue: Mutex<PcmQueue>,
    stop: AtomicBool,
    failed: AtomicBool,
    log: LogFn,
}

impl Shared {
    fn queue(&self) -> MutexGuard<'_, PcmQueue> {
        // A poisoned lock only means another thread panicked; the data is
        // plain samples and still usable.
        self.queue.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn fail(&self, message: &str) {
        if !self.failed.swap(true, Ordering::AcqRel) {
            (self.log)(message);
        }
    }
}

/// Plays demuxed audio on a private thread. Dropping it stops playback.
pub struct AudioSink {
    shared: Arc<Shared>,
    worker: Option<thread::JoinHandle<()>>,
    announced_streams: String,
    dropped: u64,
    drop_events: u64,
}

impl AudioSink {
    /// Starts the render thread. The audio device is opened later, on that
    /// thread, once audio actually arrives.
    pub fn start(log: LogFn) -> Result<Self> {
        let shared = Arc::new(Shared {
            queue: Mutex::new(PcmQueue::default()),
            stop: AtomicBool::new(false),
            failed: AtomicBool::new(false),
            log,
        });
        let worker_shared = shared.clone();
        let worker = thread::Builder::new()
            .name("adapt-audio".into())
            .spawn(move || render_thread(&worker_shared))?;
        Ok(Self {
            shared,
            worker: Some(worker),
            announced_streams: String::new(),
            dropped: 0,
            drop_events: 0,
        })
    }

    /// Hands every audio frame the demuxer has completed to the render thread.
    /// Cheap, never blocks on the device, and safe to call every packet batch.
    pub fn pump(&mut self, demux: &mut TransportStream) {
        if self.shared.failed.load(Ordering::Acquire) {
            demux.set_audio_enabled(false);
            return;
        }
        let frames = demux.take_audio();
        if frames.is_empty() {
            return;
        }
        let streams = demux.describe_streams();
        if streams != self.announced_streams {
            (self.shared.log)(&format!("Audio: transport stream carries {streams}"));
            self.announced_streams = streams;
        }
        for frame in &frames {
            let outcome = self.shared.queue().push(AudioFormat::of(frame), &frame.samples);
            match outcome {
                Push::Accepted { dropped_frames } if dropped_frames > 0 => {
                    self.dropped += dropped_frames as u64;
                    self.drop_events += 1;
                    if should_log(self.drop_events) {
                        (self.shared.log)(&format!(
                            "Audio: trimmed {dropped_frames} sample frames to hold delay down ({} dropped so far)",
                            self.dropped
                        ));
                    }
                }
                Push::Accepted { .. } | Push::Rejected => {}
                Push::FormatChanged => {
                    self.shared.fail("Audio disabled: the phone changed the audio format mid-session");
                    demux.set_audio_enabled(false);
                    return;
                }
            }
        }
    }

    /// True once audio has been given up for this session.
    pub fn failed(&self) -> bool {
        self.shared.failed.load(Ordering::Acquire)
    }
}

impl Drop for AudioSink {
    fn drop(&mut self) {
        self.shared.stop.store(true, Ordering::Release);
        if let Some(worker) = self.worker.take() {
            if worker.join().is_err() {
                (self.shared.log)("Audio render thread stopped unexpectedly");
            }
        }
    }
}

/// Builds the audio sink for a session when the setting or `ADAPT_DEX_AUDIO`
/// asks for it, and switches audio reassembly on in `demux`. Returns `None`
/// (changing nothing) when audio is off or could not start.
#[cfg(windows)]
pub fn for_session(
    state: &Arc<crate::runtime::AppState>,
    demux: &mut TransportStream,
) -> Option<AudioSink> {
    let setting = state.settings.lock().map(|s| s.audio).unwrap_or(false);
    let env = std::env::var(ENV_VAR).ok();
    if !enabled(setting, env.as_deref()) {
        return None;
    }
    let log_state = state.clone();
    match AudioSink::start(Arc::new(move |message: &str| log_state.log(message))) {
        Ok(sink) => {
            demux.set_audio_enabled(true);
            state.log("Audio playback is on (experimental, unverified on hardware)");
            Some(sink)
        }
        Err(error) => {
            state.log(format!("Audio disabled: {error:#}"));
            None
        }
    }
}

fn render_thread(shared: &Shared) {
    // Wait for the first frame: it tells us the format to open.
    let format = loop {
        if shared.stop.load(Ordering::Acquire) || shared.failed.load(Ordering::Acquire) {
            return;
        }
        if let Some(format) = shared.queue().format() {
            break format;
        }
        thread::sleep(TICK);
    };
    if let Err(error) = device::play(shared, format) {
        shared.fail(&format!("Audio disabled: {error:#}"));
    }
}

/// The per-tick loop around a device; kept free of COM so it reads plainly.
fn render_loop(shared: &Shared, format: AudioFormat, output: &device::Output) -> Result<()> {
    let capacity = output.capacity() as usize;
    let mut planner = RenderPlanner::new(format.sample_rate);
    let mut logged_underruns = 0;
    output.start()?;
    (shared.log)(&format!(
        "Audio output open: {} Hz, {} ch, 16-bit, device buffer {capacity} frames",
        format.sample_rate, format.channels
    ));
    while !shared.stop.load(Ordering::Acquire) && !shared.failed.load(Ordering::Acquire) {
        let padding = output.padding()? as usize;
        let queued = shared.queue().frames();
        let count = planner.plan(queued, padding, capacity);
        if count > 0 {
            let samples = shared.queue().take(count);
            output.write(&samples)?;
        }
        if planner.underruns() != logged_underruns {
            logged_underruns = planner.underruns();
            if should_log(logged_underruns) {
                (shared.log)(&format!("Audio: underrun #{logged_underruns}; rebuffering"));
            }
        }
        thread::sleep(TICK);
    }
    output.stop();
    Ok(())
}

#[cfg(windows)]
mod device {
    use super::*;
    use anyhow::Context;
    use windows::Win32::{
        Media::Audio::{
            eConsole, eRender, IAudioClient, IAudioRenderClient, IMMDeviceEnumerator,
            MMDeviceEnumerator, AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM,
            AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY, WAVEFORMATEX, WAVE_FORMAT_PCM,
        },
        System::Com::{CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_ALL, COINIT_MULTITHREADED},
    };

    /// An open shared-mode render stream on the default output device.
    pub struct Output {
        client: IAudioClient,
        render: IAudioRenderClient,
        capacity: u32,
        channels: usize,
    }

    /// Opens the default device, runs `render_loop`, then releases everything
    /// before COM is torn down. Any error leaves the caller to disable audio.
    pub fn play(shared: &Shared, format: AudioFormat) -> Result<()> {
        if !format.supported() {
            bail!(
                "unsupported audio format ({} Hz, {} channels)",
                format.sample_rate,
                format.channels
            );
        }
        unsafe {
            CoInitializeEx(None, COINIT_MULTITHREADED)
                .ok()
                .context("could not initialise COM for audio")?;
        }
        let result = (|| -> Result<()> {
            let output = Output::open(format)?;
            render_loop(shared, format, &output)
        })();
        unsafe { CoUninitialize() };
        result
    }

    impl Output {
        fn open(format: AudioFormat) -> Result<Self> {
            unsafe {
                let enumerator: IMMDeviceEnumerator =
                    CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL)
                        .context("could not reach the Windows audio service")?;
                let device = enumerator
                    .GetDefaultAudioEndpoint(eRender, eConsole)
                    .context("there is no default audio output device")?;
                let client: IAudioClient = device
                    .Activate(CLSCTX_ALL, None)
                    .context("could not open the audio output device")?;
                let block = format.channels * 2;
                let wave = WAVEFORMATEX {
                    wFormatTag: WAVE_FORMAT_PCM as u16,
                    nChannels: format.channels,
                    nSamplesPerSec: format.sample_rate,
                    nAvgBytesPerSec: format.sample_rate * u32::from(block),
                    nBlockAlign: block,
                    wBitsPerSample: 16,
                    cbSize: 0,
                };
                client
                    .Initialize(
                        AUDCLNT_SHAREMODE_SHARED,
                        AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
                        DEVICE_BUFFER_MS * 10_000,
                        0,
                        &wave,
                        None,
                    )
                    .context("the audio device refused the stream format")?;
                let capacity = client.GetBufferSize().context("no audio buffer size")?;
                let render: IAudioRenderClient = client
                    .GetService()
                    .context("could not get the audio render service")?;
                Ok(Self {
                    client,
                    render,
                    capacity,
                    channels: usize::from(format.channels),
                })
            }
        }

        pub fn capacity(&self) -> u32 {
            self.capacity
        }

        pub fn start(&self) -> Result<()> {
            unsafe { self.client.Start() }.context("could not start audio playback")
        }

        pub fn stop(&self) {
            unsafe {
                let _ = self.client.Stop();
                let _ = self.client.Reset();
            }
        }

        pub fn padding(&self) -> Result<u32> {
            unsafe { self.client.GetCurrentPadding() }.context("audio device lost (padding)")
        }

        /// Writes whole sample frames into the device buffer.
        pub fn write(&self, samples: &[i16]) -> Result<()> {
            let frames = samples.len() / self.channels;
            if frames == 0 {
                return Ok(());
            }
            let count = u32::try_from(frames).context("audio write too large")?;
            unsafe {
                let buffer = self.render.GetBuffer(count).context("audio device lost (buffer)")?;
                if buffer.is_null() {
                    bail!("audio device returned no buffer");
                }
                let bytes = std::slice::from_raw_parts_mut(buffer, frames * self.channels * 2);
                write_pcm16_le(bytes, &samples[..frames * self.channels]);
                self.render
                    .ReleaseBuffer(count, 0)
                    .context("audio device lost (release)")?;
            }
            Ok(())
        }
    }
}

#[cfg(not(windows))]
mod device {
    use super::*;

    pub struct Output;

    pub fn play(_shared: &Shared, _format: AudioFormat) -> Result<()> {
        bail!("audio output is only available on Windows")
    }

    impl Output {
        pub fn capacity(&self) -> u32 {
            0
        }
        pub fn start(&self) -> Result<()> {
            Ok(())
        }
        pub fn stop(&self) {}
        pub fn padding(&self) -> Result<u32> {
            Ok(0)
        }
        pub fn write(&self, _samples: &[i16]) -> Result<()> {
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const FORMAT: AudioFormat = AudioFormat {
        sample_rate: 48_000,
        channels: 2,
    };

    fn ramp(frames: usize) -> Vec<i16> {
        (0..frames * 2).map(|i| i as i16).collect()
    }

    #[test]
    fn environment_overrides_the_setting_only_when_it_is_clear() {
        assert!(!enabled(false, None));
        assert!(enabled(true, None));
        for on in ["1", "true", "TRUE", " yes ", "on"] {
            assert!(enabled(false, Some(on)), "{on:?}");
        }
        for off in ["0", "false", "No", "OFF"] {
            assert!(!enabled(true, Some(off)), "{off:?}");
        }
        assert!(enabled(true, Some("")), "an empty value leaves the setting alone");
        assert!(!enabled(false, Some("maybe")));
        assert!(enabled(true, Some("maybe")));
    }

    #[test]
    fn format_support_is_mono_or_stereo_at_plain_rates() {
        assert!(FORMAT.supported());
        assert!(AudioFormat { sample_rate: 44_100, channels: 1 }.supported());
        assert!(!AudioFormat { sample_rate: 48_000, channels: 0 }.supported());
        assert!(!AudioFormat { sample_rate: 48_000, channels: 6 }.supported());
        assert!(!AudioFormat { sample_rate: 0, channels: 2 }.supported());
        assert!(!AudioFormat { sample_rate: 8_000_000, channels: 2 }.supported());
    }

    #[test]
    fn queue_keeps_whole_frames_in_order() {
        let mut queue = PcmQueue::default();
        assert_eq!(queue.frames(), 0);
        assert!(queue.take(10).is_empty());
        assert_eq!(queue.push(FORMAT, &[1, 2, 3, 4, 5]), Push::Accepted { dropped_frames: 0 });
        assert_eq!(queue.frames(), 2, "the odd trailing sample is not a frame");
        assert_eq!(queue.take(1), vec![1, 2]);
        assert_eq!(queue.take(5), vec![3, 4]);
        assert_eq!(queue.frames(), 0);
        assert_eq!(queue.format(), Some(FORMAT));
    }

    #[test]
    fn queue_drops_the_oldest_audio_instead_of_growing_the_delay() {
        let mut queue = PcmQueue::default();
        let max = ms_to_frames(48_000, QUEUE_MAX_MS);
        let trim = ms_to_frames(48_000, QUEUE_TRIM_MS);
        assert_eq!(queue.push(FORMAT, &ramp(max)), Push::Accepted { dropped_frames: 0 });
        assert_eq!(queue.frames(), max);
        // One more frame crosses the cap: trim down to the low-water mark.
        let outcome = queue.push(FORMAT, &[7, 7]);
        assert_eq!(outcome, Push::Accepted { dropped_frames: max + 1 - trim });
        assert_eq!(queue.frames(), trim);
        // The newest audio survives.
        let tail = queue.take(trim);
        assert_eq!(&tail[tail.len() - 2..], &[7, 7]);
        // A single enormous push is bounded as well.
        let mut queue = PcmQueue::default();
        queue.push(FORMAT, &ramp(48_000 * 10));
        assert_eq!(queue.frames(), trim);
    }

    #[test]
    fn queue_refuses_other_formats_and_unusable_ones() {
        let mut queue = PcmQueue::default();
        assert_eq!(queue.push(AudioFormat { sample_rate: 48_000, channels: 0 }, &[1]), Push::Rejected);
        assert_eq!(queue.push(AudioFormat { sample_rate: 0, channels: 2 }, &[1, 2]), Push::Rejected);
        assert_eq!(queue.format(), None, "a rejected frame does not fix the format");
        queue.push(FORMAT, &[1, 2]);
        let mono = AudioFormat { sample_rate: 48_000, channels: 1 };
        assert_eq!(queue.push(mono, &[9]), Push::FormatChanged);
        assert_eq!(queue.frames(), 1, "nothing was queued by the refused frame");
    }

    #[test]
    fn planner_waits_for_a_prefill_then_fills_to_the_target() {
        let prefill = ms_to_frames(48_000, PREFILL_MS);
        let target = ms_to_frames(48_000, DEVICE_TARGET_MS);
        let capacity = 48_000 / 1000 * 120;
        let mut planner = RenderPlanner::new(48_000);
        assert_eq!(planner.state(), RenderState::Buffering);
        assert_eq!(planner.plan(prefill - 1, 0, capacity), 0);
        assert_eq!(planner.plan(prefill, 0, capacity), prefill);
        assert_eq!(planner.state(), RenderState::Playing);
        // Never fill the device beyond the target, even with lots queued.
        assert_eq!(planner.plan(10 * target, 0, capacity), target);
        assert_eq!(planner.plan(10 * target, 100, capacity), target - 100);
        assert_eq!(planner.plan(10 * target, target, capacity), 0);
        assert_eq!(planner.plan(10 * target, target + 5, capacity), 0, "no underflow");
        // A small device buffer caps the write too.
        assert_eq!(planner.plan(10 * target, 0, 500), 500);
        assert_eq!(planner.plan(10 * target, 600, 500), 0);
    }

    #[test]
    fn planner_counts_an_underrun_and_rebuffers() {
        let prefill = ms_to_frames(48_000, PREFILL_MS);
        let mut planner = RenderPlanner::new(48_000);
        planner.plan(prefill, 0, 5760);
        // Data still in the device is not an underrun, even with an empty queue.
        assert_eq!(planner.plan(0, 300, 5760), 0);
        assert_eq!((planner.state(), planner.underruns()), (RenderState::Playing, 0));
        // Device empty and nothing queued: starved.
        assert_eq!(planner.plan(0, 0, 5760), 0);
        assert_eq!((planner.state(), planner.underruns()), (RenderState::Buffering, 1));
        // A little audio is not enough to restart.
        assert_eq!(planner.plan(prefill - 1, 0, 5760), 0);
        assert_eq!(planner.state(), RenderState::Buffering);
        assert_eq!(planner.plan(prefill, 0, 5760), prefill);
        assert_eq!(planner.underruns(), 1);
    }

    #[test]
    fn drifting_sources_settle_inside_the_latency_budget() {
        // A phone clock slightly faster than the PC's: audio arrives 1% too
        // quickly. Simulate 20 s in 10 ms steps and check the delay never
        // exceeds queue cap + device target.
        let mut queue = PcmQueue::default();
        let mut planner = RenderPlanner::new(48_000);
        let capacity = 48_000 / 1000 * 120;
        let mut device = 0usize;
        let mut worst = 0usize;
        let mut owed = 0.0f64;
        for _ in 0..2000 {
            owed += 480.0 * 1.01;
            let arriving = owed as usize;
            owed -= arriving as f64;
            queue.push(FORMAT, &ramp(arriving));
            let write = planner.plan(queue.frames(), device, capacity);
            device += queue.take(write).len() / 2;
            device = device.saturating_sub(480);
            worst = worst.max(queue.frames() + device);
        }
        let budget = ms_to_frames(48_000, QUEUE_MAX_MS + DEVICE_TARGET_MS);
        assert!(worst <= budget, "delay {worst} exceeded {budget}");
    }

    #[test]
    fn samples_are_written_little_endian_without_overrunning() {
        let mut bytes = [0u8; 6];
        assert_eq!(write_pcm16_le(&mut bytes, &[0x0102, -2, 0x7f00, 5]), 3);
        assert_eq!(bytes, [0x02, 0x01, 0xfe, 0xff, 0x00, 0x7f]);
        let mut bytes = [0u8; 9];
        assert_eq!(write_pcm16_le(&mut bytes, &[1]), 1);
        assert_eq!(&bytes[..2], &[1, 0]);
        assert_eq!(write_pcm16_le(&mut [], &[1, 2]), 0);
    }

    #[test]
    fn repeating_events_are_logged_sparingly() {
        let logged: Vec<u64> = (0..=300).filter(|n| should_log(*n)).collect();
        assert_eq!(logged, vec![1, 2, 3, 4, 5, 100, 200, 300]);
    }

    #[test]
    fn a_failure_is_reported_once_and_remembered() {
        use std::sync::atomic::AtomicUsize;
        let lines = Arc::new(AtomicUsize::new(0));
        let counter = lines.clone();
        let shared = Shared {
            queue: Mutex::new(PcmQueue::default()),
            stop: AtomicBool::new(false),
            failed: AtomicBool::new(false),
            log: Arc::new(move |_: &str| {
                counter.fetch_add(1, Ordering::SeqCst);
            }),
        };
        shared.fail("first");
        shared.fail("second");
        assert!(shared.failed.load(Ordering::SeqCst));
        assert_eq!(lines.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn starting_and_stopping_the_sink_touches_no_device() {
        // With no audio arriving the thread only idles until told to stop;
        // no COM object or device is touched.
        let mut sink = AudioSink::start(Arc::new(|_: &str| {})).unwrap();
        let mut demux = TransportStream::default();
        sink.pump(&mut demux);
        assert!(!sink.failed());
        drop(sink);
    }
}
