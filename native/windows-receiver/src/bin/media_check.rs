use anyhow::{Context, Result};
use dexdesk::{media::NativeVideo, protocol::VideoMode, transport_stream::TransportStream};
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

fn run(window: HWND, path: &str, mode: VideoMode) -> Result<()> {
    let data = std::fs::read(path)?;
    let mut demux = TransportStream::default();
    let mut video = NativeVideo::new(window, mode)?;
    let mut pictures = 0;
    for batch in data.chunks(188 * 7) {
        for frame in demux.push(batch)? {
            video.push(&frame.bytes, frame.discontinuity)?;
            pictures += 1;
        }
    }
    if let Some(frame) = demux.flush()? {
        video.push(&frame.bytes, frame.discontinuity)?;
        pictures += 1;
    }
    let decoded = video.finish()?;
    println!(
        "PES pictures={pictures}, decoded={decoded}, native={}x{}, D3D-aware={}",
        mode.width, mode.height, video.hardware
    );
    anyhow::ensure!(
        pictures >= 20 && decoded >= 20,
        "Native media path did not decode enough complete pictures"
    );
    Ok(())
}
