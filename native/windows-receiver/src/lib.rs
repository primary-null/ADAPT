#[cfg(windows)]
pub mod advertise;
pub mod audio;
pub mod input;
#[cfg(windows)]
pub mod media;
pub mod protocol;
#[cfg(windows)]
pub mod receiver;
#[cfg(windows)]
pub mod runtime;
pub mod settings;
pub mod transport_stream;
#[cfg(windows)]
pub mod ui;
pub mod adapt_trace;
