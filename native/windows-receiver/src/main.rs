#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use anyhow::Result;
use dexdesk::{
    adapt_trace::Scope,
    runtime::{self, AppState},
    settings, ui,
};
use std::io::Write;
use windows::{
    core::PCWSTR,
    Win32::{
        System::Console::{AttachConsole, GetStdHandle, ATTACH_PARENT_PROCESS, STD_ERROR_HANDLE, STD_OUTPUT_HANDLE},
        UI::WindowsAndMessaging::*,
    },
};

const USAGE: &str = "\
Usage: dexdesk [OPTION]

Without options, start the wireless DeX receiver. Informational options print
one thing and exit without starting anything (no Wi-Fi Direct, no listeners,
no window); they cannot be combined with other options.

  -V, --version         print the version
      --print-data-dir  print the directory that holds status.json and the log
      --print-status-path
                        print the full path of status.json
  -h, --help            print this text

Interface options (no receiver is started):
      --preview           show the window and settings
      --preview-settings  preview with the settings dialog open
      --smoke-ui          preview that closes itself after about four seconds
";

/// What the command line asks for.
#[derive(Debug, Eq, PartialEq)]
enum Action {
    Run { preview: bool, smoke: bool, preview_settings: bool },
    Version,
    DataDir,
    StatusPath,
    Help,
}

/// Interpret the arguments after the program name. An unknown argument is an
/// error: a mistyped option must not start a receiver that advertises over Wi-Fi.
fn parse_args(args: &[String]) -> std::result::Result<Action, String> {
    let (mut preview, mut smoke, mut preview_settings) = (false, false, false);
    let mut informational = None;
    let mut count = 0;
    for arg in args {
        count += 1;
        match arg.as_str() {
            "-V" | "--version" => informational = Some(Action::Version),
            "--print-data-dir" => informational = Some(Action::DataDir),
            "--print-status-path" => informational = Some(Action::StatusPath),
            "-h" | "--help" => informational = Some(Action::Help),
            "--preview" => preview = true,
            "--smoke-ui" => (preview, smoke) = (true, true),
            "--preview-settings" => (preview, preview_settings) = (true, true),
            other => return Err(format!("unknown argument {other:?}; see --help")),
        }
    }
    match informational {
        Some(_) if count > 1 => Err("informational options cannot be combined with other options".into()),
        Some(action) => Ok(action),
        None => Ok(Action::Run { preview, smoke, preview_settings }),
    }
}

/// Write `text` for a person or a parent process. A release build is a GUI
/// program with no console of its own: when its output is not redirected, attach
/// to the console of the process that started it. Nothing here can panic on a
/// closed pipe.
fn emit(text: &str, to_stderr: bool) {
    let kind = if to_stderr { STD_ERROR_HANDLE } else { STD_OUTPUT_HANDLE };
    if unsafe { GetStdHandle(kind) }.is_ok() {
        let _ = if to_stderr {
            std::io::stderr().write_all(text.as_bytes())
        } else {
            let mut out = std::io::stdout();
            out.write_all(text.as_bytes()).and_then(|()| out.flush())
        };
        return;
    }
    let _ = unsafe { AttachConsole(ATTACH_PARENT_PROCESS) };
    if let Ok(mut console) = std::fs::OpenOptions::new()
        .write(true)
        .open(if to_stderr { "CONERR$" } else { "CONOUT$" })
    {
        let _ = console.write_all(text.as_bytes());
    }
}

/// Run an informational action and return the process exit code.
fn inform(action: &Action) -> i32 {
    let outcome: Result<String> = match action {
        Action::Version => Ok(format!("dexdesk {}\n", env!("CARGO_PKG_VERSION"))),
        Action::DataDir => settings::data_dir().map(|dir| format!("{}\n", dir.display())),
        Action::StatusPath => settings::data_dir().map(|dir| format!("{}\n", dir.join(settings::STATUS_FILE).display())),
        Action::Help => Ok(USAGE.to_owned()),
        Action::Run { .. } => return 0,
    };
    match outcome {
        Ok(text) => {
            emit(&text, false);
            0
        }
        Err(error) => {
            emit(&format!("dexdesk: {error:#}\n"), true);
            1
        }
    }
}

fn main() {
    let args: Vec<String> = std::env::args_os().skip(1).map(|arg| arg.to_string_lossy().into_owned()).collect();
    let (preview, smoke, preview_settings) = match parse_args(&args) {
        Ok(Action::Run { preview, smoke, preview_settings }) => (preview, smoke, preview_settings),
        Ok(informational) => std::process::exit(inform(&informational)),
        Err(message) => {
            emit(&format!("dexdesk: {message}\n"), true);
            std::process::exit(2);
        }
    };
    if let Err(error) = run(preview, smoke, preview_settings) {
        // A supervising program that captures stderr sees the reason even though
        // the message box below blocks until someone closes it.
        emit(&format!("dexdesk: could not start: {error:#}\n"), true);
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

fn run(preview: bool, smoke: bool, preview_settings: bool) -> Result<()> {
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
    runtime::install_panic_hook(state.clone());
    state.log(format!(
        "DeXDesk {} starting: native Rust / Windows Media Foundation / Direct3D 11",
        env!("CARGO_PKG_VERSION")
    ));
    state.log(if state.privacy.reveals() {
        "Log privacy: the phone's name, peer ID and Wi-Fi Direct addresses ARE written to this log (ADAPT_DEX_LOG_PEER_DETAILS is set)"
    } else {
        "Log privacy: the phone's name, peer ID and Wi-Fi Direct addresses are withheld from this log (ADAPT_DEX_LOG_PEER_DETAILS=1 includes them)"
    });
    // A window that cannot be created is a receiver that never started: leave
    // that in status.json for a supervisor instead of only in a message box.
    let outcome = ui::run(state.clone(), preview, smoke, preview_settings);
    if let Err(error) = &outcome {
        state.running.store(false, std::sync::atomic::Ordering::Release);
        state.record_error(Scope::Receiver, error);
    }
    outcome
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> std::result::Result<Action, String> {
        parse_args(&args.iter().map(|arg| (*arg).to_owned()).collect::<Vec<_>>())
    }

    const RUN: Action = Action::Run { preview: false, smoke: false, preview_settings: false };

    #[test]
    fn no_arguments_start_the_receiver_as_before() {
        assert_eq!(parse(&[]), Ok(RUN));
    }

    #[test]
    fn the_interface_options_keep_their_meaning() {
        assert_eq!(parse(&["--preview"]), Ok(Action::Run { preview: true, smoke: false, preview_settings: false }));
        assert_eq!(parse(&["--smoke-ui"]), Ok(Action::Run { preview: true, smoke: true, preview_settings: false }));
        assert_eq!(parse(&["--preview-settings"]), Ok(Action::Run { preview: true, smoke: false, preview_settings: true }));
        assert_eq!(
            parse(&["--smoke-ui", "--preview-settings"]),
            Ok(Action::Run { preview: true, smoke: true, preview_settings: true })
        );
    }

    #[test]
    fn informational_options_start_nothing() {
        assert_eq!(parse(&["--version"]), Ok(Action::Version));
        assert_eq!(parse(&["-V"]), Ok(Action::Version));
        assert_eq!(parse(&["--print-data-dir"]), Ok(Action::DataDir));
        assert_eq!(parse(&["--print-status-path"]), Ok(Action::StatusPath));
        assert_eq!(parse(&["--help"]), Ok(Action::Help));
        assert_eq!(parse(&["-h"]), Ok(Action::Help));
    }

    #[test]
    fn informational_options_cannot_be_combined_with_anything() {
        assert!(parse(&["--version", "--preview"]).is_err());
        assert!(parse(&["--preview", "--print-data-dir"]).is_err());
        assert!(parse(&["--version", "--version"]).is_err());
        assert!(parse(&["--print-data-dir", "--print-status-path"]).is_err());
    }

    #[test]
    fn an_unknown_argument_is_refused_instead_of_starting_a_receiver() {
        let error = parse(&["--versoin"]).unwrap_err();
        assert!(error.contains("--versoin"), "{error}");
        assert!(parse(&["--preview", "extra"]).is_err());
        assert!(parse(&[""]).is_err());
        assert!(parse(&["\u{fffd}"]).is_err(), "a lossy-converted non-Unicode argument is unknown too");
    }

    #[test]
    fn usage_lists_every_option() {
        for option in ["--version", "--print-data-dir", "--print-status-path", "--help", "--preview", "--preview-settings", "--smoke-ui"] {
            assert!(USAGE.contains(option), "{option}");
        }
    }
}
