use crate::runtime::AppState;
use anyhow::{bail, Context, Result};
use std::{
    sync::Arc,
    thread,
    time::{Duration, Instant},
};
use windows::{
    core::{s, w, GUID},
    Devices::WiFiDirect::*,
    Win32::{
        Foundation::{FreeLibrary, HMODULE},
        System::LibraryLoader::{GetProcAddress, LoadLibraryW},
    },
};

type Open = unsafe extern "system" fn(u32, *mut u32, *mut isize) -> u32;
type Close = unsafe extern "system" fn(isize) -> u32;
type Notify = unsafe extern "system" fn(GUID, u32, *const u8) -> u32;

pub struct Advertisement {
    pub publisher: WiFiDirectAdvertisementPublisher,
    pub listener: WiFiDirectConnectionListener,
    module: HMODULE,
    close: Close,
    notify: Notify,
    handle: isize,
    enabled: bool,
    sink: bool,
    state: Arc<AppState>,
}

impl Advertisement {
    pub fn new(state: Arc<AppState>) -> Result<Self> {
        unsafe {
            let publisher = WiFiDirectAdvertisementPublisher::new()?;
            let listener = WiFiDirectConnectionListener::new()?;
            publisher
                .Advertisement()?
                .SetIsAutonomousGroupOwnerEnabled(true)?;
            publisher.Advertisement()?.SetListenStateDiscoverability(
                WiFiDirectAdvertisementListenStateDiscoverability::Normal,
            )?;
            let module = LoadLibraryW(w!("wlanapi.dll"))?;
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
                    return Err(e);
                }
            };
            let mut handle = 0;
            let mut version = 0;
            let result = open(1, &mut version, &mut handle);
            if result != 0 {
                let _ = FreeLibrary(module);
                bail!("Windows could not open Miracast (error {result})");
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
                state,
            })
        }
    }
    pub fn start(&mut self) -> Result<()> {
        self.publisher.Start()?;
        let deadline = Instant::now() + Duration::from_secs(8);
        loop {
            match self.publisher.Status()?{
                WiFiDirectAdvertisementPublisherStatus::Started=>break,
                WiFiDirectAdvertisementPublisherStatus::Aborted=>bail!("Wi-Fi Direct could not start. Turn on Wi-Fi and close other wireless-display receivers."),
                _=>{}
            }
            if Instant::now() > deadline {
                bail!("Windows did not start Wi-Fi Direct in time");
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
            bail!("Windows Miracast provider command {command} failed ({result})");
        }
        Ok(())
    }
}

impl Drop for Advertisement {
    fn drop(&mut self) {
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
