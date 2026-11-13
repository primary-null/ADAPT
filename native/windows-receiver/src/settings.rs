use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::{fs, path::PathBuf};

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
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            quality: Quality::FullHd60,
            scroll_speed: 60,
            always_on_top: false,
            window_width: 1100,
            window_height: 760,
        }
    }
}

pub fn data_dir() -> Result<PathBuf> {
    if let Some(value) = std::env::var_os("ADAPT_DEX_DATA_DIR") {
        let path = PathBuf::from(value);
        if !path.is_absolute() { bail!("ADAPT session directory must be absolute"); }
        return Ok(path);
    }
    Ok(PathBuf::from(
        std::env::var_os("LOCALAPPDATA").context("Windows local app data is unavailable")?,
    )
    .join("DeXDesk"))
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
}
