use anyhow::{Context, Result};
use dexdesk::{
    media::NativeVideo,
    protocol::VideoMode,
    transport_stream::{pts_delta, AudioFrame, TransportStream},
};
use windows::{
    core::w,
    Win32::{
        Foundation::{HINSTANCE, HWND},
        System::Com::{CoInitializeEx, CoUninitialize, COINIT_MULTITHREADED},
        UI::WindowsAndMessaging::*,
    },
};

fn main() -> Result<()> {
    let args: Vec<_> = std::env::args().collect();
    let path = args
        .get(1)
        .context("Usage: media_check FILE.ts [WIDTH HEIGHT FPS]")?;
    let mode = VideoMode {
        width: args.get(2).map(|s| s.parse()).transpose()?.unwrap_or(1920),
        height: args.get(3).map(|s| s.parse()).transpose()?.unwrap_or(1080),
        fps: args.get(4).map(|s| s.parse()).transpose()?.unwrap_or(30),
    };
    unsafe {
        CoInitializeEx(None, COINIT_MULTITHREADED).ok()?;
        let window = CreateWindowExW(
            WINDOW_EX_STYLE(0),
            w!("STATIC"),
            w!("DeXDesk media check"),
            WS_OVERLAPPEDWINDOW,
            0,
            0,
            640,
            400,
            None,
            None,
            HINSTANCE::default(),
            None,
        )?;
        let result = run(window, path, mode);
        DestroyWindow(window)?;
        CoUninitialize();
        result
    }
}

/// What the demuxer found for audio; reporting only, nothing is played.
#[derive(Default)]
struct AudioReport {
    frames: u64,
    sample_frames: u64,
    format: Option<(u32, u16)>,
    mixed_formats: bool,
    discontinuities: u64,
    first_pts: Option<u64>,
    last_pts: Option<u64>,
}

impl AudioReport {
    fn add(&mut self, frames: Vec<AudioFrame>) {
        for frame in frames {
            self.frames += 1;
            self.sample_frames += frame.frames() as u64;
            let format = (frame.sample_rate, frame.channels);
            match self.format {
                None => self.format = Some(format),
                Some(first) if first != format => self.mixed_formats = true,
                Some(_) => {}
            }
            self.discontinuities += u64::from(frame.discontinuity);
            if let Some(pts) = frame.pts {
                self.first_pts.get_or_insert(pts);
                self.last_pts = Some(pts);
            }
        }
    }

    fn print(&self, demux: &TransportStream) {
        let stats = demux.audio_stats();
        println!("audio: PMT streams: {}", demux.describe_streams());
        match self.format {
            Some((rate, channels)) if rate > 0 => {
                let seconds = self.sample_frames as f64 / f64::from(rate);
                let span = match (self.first_pts, self.last_pts) {
                    (Some(first), Some(last)) => {
                        format!("{:.3}s", pts_delta(last, first) as f64 / 90_000.0)
                    }
                    _ => "n/a".to_owned(),
                };
                println!(
                    "audio: frames={}, format={rate} Hz x {channels} ch 16-bit, duration={seconds:.3}s, \
                     pts span={span}, discontinuities={}, dropped PES={}, continuity errors={}{}",
                    self.frames,
                    self.discontinuities,
                    stats.dropped_pes,
                    stats.continuity_errors,
                    if self.mixed_formats { ", FORMAT CHANGED mid-stream" } else { "" }
                );
            }
            _ => println!(
                "audio: none decoded (LPCM PID {:?}, dropped PES={}, continuity errors={})",
                demux.audio_pid(),
                stats.dropped_pes,
                stats.continuity_errors
            ),
        }
        if let Some((reason, prefix)) = &stats.first_reject {
            println!("audio: first rejected PES ({reason:?}), leading bytes: {prefix:02x?}");
        }
    }
}

fn run(window: HWND, path: &str, mode: VideoMode) -> Result<()> {
    let data = std::fs::read(path)?;
    let mut demux = TransportStream::default();
    demux.set_audio_enabled(true);
    let mut audio = AudioReport::default();
    let mut video = NativeVideo::new(window, mode)?;
    let mut pictures = 0;
    for batch in data.chunks(188 * 7) {
        for frame in demux.push(batch)? {
            video.push(&frame.bytes, frame.discontinuity)?;
            pictures += 1;
        }
        audio.add(demux.take_audio());
    }
    if let Some(frame) = demux.flush()? {
        video.push(&frame.bytes, frame.discontinuity)?;
        pictures += 1;
    }
    audio.add(demux.take_audio());
    let decoded = video.finish()?;
    println!(
        "PES pictures={pictures}, decoded={decoded}, native={}x{}, D3D-aware={}",
        mode.width, mode.height, video.hardware
    );
    audio.print(&demux);
    anyhow::ensure!(
        pictures >= 20 && decoded >= 20,
        "Native media path did not decode enough complete pictures"
    );
    Ok(())
}
