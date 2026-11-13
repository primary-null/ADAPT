#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use anyhow::Result;
use dexdesk::{runtime::AppState, settings, ui};
use windows::{core::PCWSTR, Win32::UI::WindowsAndMessaging::*};

fn main() {
    if let Err(error) = run() {
        let message: Vec<u16> = format!("DeXDesk could not start.\n\n{error:#}")
            .encode_utf16()
            .chain(Some(0))
            .collect();
        unsafe {
            MessageBoxW(
                None,
                PCWSTR(message.as_ptr()),
                windows::core::w!("DeXDesk"),
                MB_OK | MB_ICONERROR,
            );
        }
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    let args: Vec<_> = std::env::args().collect();
    let settings = match settings::load() {
        Ok(value) => value,
        Err(error) => {
            let message:Vec<u16>=format!("Saved settings could not be loaded. DeXDesk will use defaults until you save settings again.\n\n{error:#}").encode_utf16().chain(Some(0)).collect();
            unsafe {
                MessageBoxW(
                    None,
                    PCWSTR(message.as_ptr()),
                    windows::core::w!("DeXDesk settings"),
                    MB_OK | MB_ICONWARNING,
                );
            }
            settings::Settings::default()
        }
    };
    let state = AppState::new(settings)?;
    state.log("DeXDesk 0.1 starting: native Rust / Windows Media Foundation / Direct3D 11");
    ui::run(
        state,
        args.iter().any(|a| a == "--preview" || a == "--smoke-ui"),
        args.iter().any(|a| a == "--smoke-ui"),
    )
}
