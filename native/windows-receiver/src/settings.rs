use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::{ffi::OsString, fs, path::{Path, PathBuf}};

/// File names inside the data directory. They are part of the contract with
/// the ADAPT CLI (see the README).
pub const STATUS_FILE: &str = "status.json";
pub const STOP_REQUEST_FILE: &str = "stop.request";

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Quality {
    #[default]
    FullHd60,
    FullHd30,
    Hd60,
}

impl Quality {
    pub fn formats(self) -> &'static str {
        match self {
            Self::FullHd60 => "40 00 02 10 000001E1 00000000 00000000 00 0000 0000 00 none none",
            Self::FullHd30 => "38 00 02 10 000000A1 00000000 00000000 00 0000 0000 00 none none",
            Self::Hd60 => "30 00 02 02 00000061 00000000 00000000 00 0000 0000 00 none none",
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            Self::FullHd60 => "1080p - target 60 FPS",
            Self::FullHd30 => "1080p - target 30 FPS",
            Self::Hd60 => "720p - target 60 FPS",
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub quality: Quality,
    pub scroll_speed: u32,
    pub always_on_top: bool,
    pub window_width: i32,
    pub window_height: i32,
    /// Experimental audio playback; off until it has been tried on hardware.
    pub audio: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            quality: Quality::FullHd60,
            scroll_speed: 60,
            always_on_top: false,
            window_width: 1100,
            window_height: 760,
            audio: false,
        }
    }
}

pub fn data_dir() -> Result<PathBuf> {
    data_dir_from(std::env::var_os("ADAPT_DEX_DATA_DIR"), std::env::var_os("LOCALAPPDATA"))
}

fn data_dir_from(override_dir: Option<OsString>, local_app_data: Option<OsString>) -> Result<PathBuf> {
    if let Some(value) = override_dir {
        let path = PathBuf::from(value);
        if !path.is_absolute() { bail!("ADAPT session directory must be absolute"); }
        return Ok(path);
    }
    Ok(PathBuf::from(
        local_app_data.context("Windows local app data is unavailable")?,
    )
    .join("DeXDesk"))
}

/// Remove a `stop.request` left by an earlier run. Nothing else deletes it, so
/// without this a request that stopped one run would stop every later run
/// within a second of starting. Returns whether a file was removed.
pub fn clear_stale_stop_request(directory: &Path) -> bool {
    let request = directory.join(STOP_REQUEST_FILE);
    request.is_file() && fs::remove_file(request).is_ok()
}

pub fn load() -> Result<Settings> {
    let path = data_dir()?.join("settings.json");
    let data = match fs::read(&path) {
        Ok(data) => data,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Settings::default()),
        Err(e) => return Err(e).context("Could not read DeXDesk settings"),
    };
    let settings: Settings =
        serde_json::from_slice(&data).context("DeXDesk settings are invalid")?;
    validate(&settings)?;
    Ok(settings)
}

fn validate(settings: &Settings) -> Result<()> {
    if !(30..=120).contains(&settings.scroll_speed)
        || !(640..=8192).contains(&settings.window_width)
        || !(480..=8192).contains(&settings.window_height)
    {
        bail!("DeXDesk settings contain an out-of-range value");
    }
    Ok(())
}

pub fn save(settings: &Settings) -> Result<()> {
    validate(settings)?;
    let directory = data_dir()?;
    fs::create_dir_all(&directory)?;
    let path = directory.join("settings.json");
    let temporary = directory.join("settings.pending.json");
    fs::write(&temporary, serde_json::to_vec_pretty(settings)?)
        .context("Could not write DeXDesk settings")?;
    fs::rename(&temporary, &path).context("Could not replace DeXDesk settings")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn target_does_not_remove_compatible_lower_refresh_mode() {
        let fields: Vec<_> = Quality::FullHd60.formats().split_whitespace().collect();
        let cea = u32::from_str_radix(fields[4], 16).unwrap();
        assert_eq!(cea & 0x180, 0x180);
        assert_eq!(
            serde_json::from_str::<Settings>("{}").unwrap().scroll_speed,
            60
        );
        assert!(validate(&Settings {
            scroll_speed: 0,
            ..Settings::default()
        })
        .is_err());
    }

    #[test]
    fn the_data_directory_override_must_be_absolute_and_wins_over_local_app_data() {
        let absolute = std::env::temp_dir().join("adapt-data");
        assert_eq!(
            data_dir_from(Some(absolute.clone().into_os_string()), Some("ignored".into())).unwrap(),
            absolute
        );
        assert!(data_dir_from(Some("relative\\dir".into()), Some("ignored".into())).is_err());
        assert!(data_dir_from(Some("".into()), None).is_err());
    }

    #[test]
    fn the_default_data_directory_is_under_local_app_data() {
        let base = std::env::temp_dir();
        assert_eq!(
            data_dir_from(None, Some(base.clone().into_os_string())).unwrap(),
            base.join("DeXDesk")
        );
        assert!(data_dir_from(None, None).is_err());
    }

    #[test]
    fn a_stale_stop_request_is_removed_once_and_only_that_file() {
        let directory = std::env::temp_dir().join(format!("dexdesk-test-{}-settings-stop-request", std::process::id()));
        let _ = fs::remove_dir_all(&directory);
        fs::create_dir_all(&directory).unwrap();
        assert!(!clear_stale_stop_request(&directory), "nothing to remove");
        fs::write(directory.join(STOP_REQUEST_FILE), b"stop").unwrap();
        fs::write(directory.join(STATUS_FILE), b"{}").unwrap();
        assert!(clear_stale_stop_request(&directory));
        assert!(!directory.join(STOP_REQUEST_FILE).exists());
        assert!(directory.join(STATUS_FILE).exists(), "other files are untouched");
        assert!(!clear_stale_stop_request(&directory));
        // A directory with that name is not a request file and is left alone.
        fs::create_dir(directory.join(STOP_REQUEST_FILE)).unwrap();
        assert!(!clear_stale_stop_request(&directory));
        let _ = fs::remove_dir_all(&directory);
    }
}
