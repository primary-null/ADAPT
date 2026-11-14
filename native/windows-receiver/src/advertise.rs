use crate::{
    adapt_trace::{fail, tag, OrFail, ReasonCode},
    runtime::AppState,
};
use anyhow::{Context, Result};
use std::{
    sync::{
        atomic::{AtomicI32, Ordering},
        Arc,
    },
    thread,
    time::{Duration, Instant},
};
use windows::{
    core::{s, w, GUID},
    Devices::WiFiDirect::*,
    Foundation::{EventRegistrationToken, TypedEventHandler},
    Win32::{
        Foundation::{FreeLibrary, HMODULE},
        System::LibraryLoader::{GetProcAddress, LoadLibraryW},
    },
};

type Open = unsafe extern "system" fn(u32, *mut u32, *mut isize) -> u32;
type Close = unsafe extern "system" fn(isize) -> u32;
type Notify = unsafe extern "system" fn(GUID, u32, *const u8) -> u32;

// `WiFiDirectError` values arrive with the publisher StatusChanged event, not
// with the polled status, so they are kept in an atomic. These two sentinels
// are not `WiFiDirectError` values.
const NO_ABORT_ERROR: i32 = -1;
const ABORT_ERROR_UNREADABLE: i32 = -2;

/// Reason code and text for a publisher that aborted with `error`
/// (`WiFiDirectError`: 1 = RadioNotAvailable, 2 = ResourceInUse).
fn abort_failure(error: Option<i32>) -> (ReasonCode, &'static str) {
    match error {
        Some(1) => (
            ReasonCode::WifiRadioUnavailable,
            "Wi-Fi Direct could not start because the Wi-Fi radio is unavailable. Turn on Wi-Fi and check that this PC has an adapter that supports Wi-Fi Direct.",
        ),
        Some(2) => (
            ReasonCode::WifiDirectResourceInUse,
            "Wi-Fi Direct could not start because another program is using it. Close other wireless-display receivers.",
        ),
        _ => (
            ReasonCode::AdvertisementAborted,
            "Wi-Fi Direct could not start. Turn on Wi-Fi and close other wireless-display receivers.",
        ),
    }
}

pub struct Advertisement {
    pub publisher: WiFiDirectAdvertisementPublisher,
    pub listener: WiFiDirectConnectionListener,
    module: HMODULE,
    close: Close,
    notify: Notify,
    handle: isize,
    enabled: bool,
    sink: bool,
    abort_error: Arc<AtomicI32>,
    status_token: Option<EventRegistrationToken>,
    state: Arc<AppState>,
}

impl Advertisement {
    pub fn new(state: Arc<AppState>) -> Result<Self> {
        unsafe {
            let init = ReasonCode::WifiDirectInitFailed;
            let publisher = WiFiDirectAdvertisementPublisher::new().or_fail(init)?;
            let listener = WiFiDirectConnectionListener::new().or_fail(init)?;
            publisher
                .Advertisement()
                .or_fail(init)?
                .SetIsAutonomousGroupOwnerEnabled(true)
                .or_fail(init)?;
            publisher
                .Advertisement()
                .or_fail(init)?
                .SetListenStateDiscoverability(WiFiDirectAdvertisementListenStateDiscoverability::Normal)
                .or_fail(init)?;
            let module = LoadLibraryW(w!("wlanapi.dll")).or_fail(ReasonCode::MiracastProviderUnavailable)?;
            let functions = (|| -> Result<(Open, Close, Notify)> {
                let open = GetProcAddress(module, s!("WFDOpenHandle"))
                    .context("Windows does not expose WFDOpenHandle")?;
                let close = GetProcAddress(module, s!("WFDCloseHandle"))
                    .context("Windows does not expose WFDCloseHandle")?;
                let notify = GetProcAddress(module, s!("WlanNotifyVsIeProviderInt")).context(
                    "This Windows version does not expose the Miracast advertisement provider",
                )?;
                Ok((
                    std::mem::transmute::<unsafe extern "system" fn() -> isize, Open>(open),
                    std::mem::transmute::<unsafe extern "system" fn() -> isize, Close>(close),
                    std::mem::transmute::<unsafe extern "system" fn() -> isize, Notify>(notify),
                ))
            })();
            let (open, close, notify) = match functions {
                Ok(v) => v,
                Err(e) => {
                    let _ = FreeLibrary(module);
                    return Err(tag(e, ReasonCode::MiracastProviderUnavailable));
                }
            };
            let mut handle = 0;
            let mut version = 0;
            let result = open(1, &mut version, &mut handle);
            if result != 0 {
                let _ = FreeLibrary(module);
                return Err(fail(
                    ReasonCode::MiracastProviderUnavailable,
                    format!("Windows could not open Miracast (error {result})"),
                ));
            }
            Ok(Self {
                publisher,
                listener,
                module,
                close,
                notify,
                handle,
                enabled: false,
                sink: false,
                abort_error: Arc::new(AtomicI32::new(NO_ABORT_ERROR)),
                status_token: None,
                state,
            })
        }
    }
    // The reason an aborted publisher reports, if its event has arrived. The
    // event can trail the polled status slightly, so wait a moment for it.
    fn abort_reason(&self) -> Option<i32> {
        let until = Instant::now() + Duration::from_millis(500);
        loop {
            let value = self.abort_error.load(Ordering::Acquire);
            if value != NO_ABORT_ERROR || Instant::now() > until {
                return (value != NO_ABORT_ERROR).then_some(value);
            }
            thread::sleep(Duration::from_millis(20));
        }
    }
    pub fn start(&mut self) -> Result<()> {
        // Diagnostics only: if the handler cannot be registered, an abort is
        // still detected by polling, just without its reason.
        let abort = self.abort_error.clone();
        let lost = self.state.clone();
        self.status_token = self
            .publisher
            .StatusChanged(&TypedEventHandler::new(
                move |_: &Option<WiFiDirectAdvertisementPublisher>,
                      args: &Option<WiFiDirectAdvertisementPublisherStatusChangedEventArgs>| {
                    if let Some(args) = args {
                        if args.Status().ok() == Some(WiFiDirectAdvertisementPublisherStatus::Aborted) {
                            let error = args.Error().map_or(ABORT_ERROR_UNREADABLE, |error| error.0);
                            abort.store(error, Ordering::Release);
                            let (code, message) = abort_failure(Some(error));
                            // Only acts once advertising has started; an abort while
                            // starting is reported by the polling loop below.
                            lost.advertisement_lost(code, &format!("The advertisement stopped while running: {message}"));
                        }
                    }
                    Ok(())
                },
            ))
            .ok();
        self.publisher.Start().or_fail(ReasonCode::WifiDirectInitFailed)?;
        let deadline = Instant::now() + Duration::from_secs(8);
        loop {
            match self.publisher.Status().or_fail(ReasonCode::WifiDirectInitFailed)?{
                WiFiDirectAdvertisementPublisherStatus::Started=>break,
                WiFiDirectAdvertisementPublisherStatus::Aborted=>{
                    let (code, message) = abort_failure(self.abort_reason());
                    return Err(fail(code, message));
                }
                _=>{}
            }
            if Instant::now() > deadline {
                return Err(fail(ReasonCode::AdvertisementTimeout, "Windows did not start Wi-Fi Direct in time"));
            }
            thread::sleep(Duration::from_millis(40));
        }
        self.command(0, 0, 7236)?;
        self.enabled = true;
        self.command(3, 1, 0)?;
        self.sink = true;
        self.state
            .log("Windows-native Miracast advertisement is active");
        Ok(())
    }
    fn command(&self, command: u32, role: u32, port: u32) -> Result<()> {
        let mut message = [0u8; 20];
        message[..4].copy_from_slice(&command.to_le_bytes());
        message[4..8].copy_from_slice(&role.to_le_bytes());
        message[16..].copy_from_slice(&port.to_le_bytes());
        let result = unsafe {
            (self.notify)(
                GUID::from_u128(0xb7d94b4d_5db1_4e70_b5c3_dd003eeebe66),
                20,
                message.as_ptr(),
            )
        };
        if result != 0 {
            return Err(fail(
                ReasonCode::MiracastProviderFailed,
                format!("Windows Miracast provider command {command} failed ({result})"),
            ));
        }
        Ok(())
    }
}

impl Drop for Advertisement {
    fn drop(&mut self) {
        if let Some(token) = self.status_token.take() {
            let _ = self.publisher.RemoveStatusChanged(token);
        }
        if self.sink {
            if let Err(e) = self.command(3, 0, 0) {
                self.state.log(format!("Could not clear sink mode: {e}"));
            }
        }
        if self.enabled {
            if let Err(e) = self.command(1, 0, 0) {
                self.state
                    .log(format!("Could not stop Miracast provider: {e}"));
            }
        }
        if self.publisher.Status().ok() == Some(WiFiDirectAdvertisementPublisherStatus::Started) {
            if let Err(e) = self.publisher.Stop() {
                self.state.log(format!("Could not stop Wi-Fi Direct: {e}"));
            }
        }
        unsafe {
            let result = (self.close)(self.handle);
            if result != 0 {
                self.state
                    .log(format!("Could not close WFD handle ({result})"));
            }
            let _ = FreeLibrary(self.module);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_aborted_publisher_is_classified_by_its_reported_error() {
        assert_eq!(abort_failure(Some(1)).0, ReasonCode::WifiRadioUnavailable);
        assert_eq!(abort_failure(Some(2)).0, ReasonCode::WifiDirectResourceInUse);
        // Success (0), an unknown value, an unreadable error and no event at all
        // are all just "aborted".
        for other in [Some(0), Some(3), Some(ABORT_ERROR_UNREADABLE), Some(NO_ABORT_ERROR), None] {
            assert_eq!(abort_failure(other).0, ReasonCode::AdvertisementAborted, "{other:?}");
        }
        for error in [Some(1), Some(2), None] {
            assert!(!abort_failure(error).1.is_empty());
        }
    }

    #[test]
    fn provider_errors_carry_their_codes_and_texts() {
        let failure = fail(ReasonCode::MiracastProviderFailed, format!("Windows Miracast provider command {} failed ({})", 3, 5));
        assert_eq!(crate::adapt_trace::classify(&failure), ReasonCode::MiracastProviderFailed);
        assert_eq!(failure.to_string(), "Windows Miracast provider command 3 failed (5)");
    }
}
