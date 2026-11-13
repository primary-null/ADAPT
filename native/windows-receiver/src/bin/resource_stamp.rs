use anyhow::{Context, Result};
use std::{env, fs, os::windows::ffi::OsStrExt, path::Path};
use windows::{core::PCWSTR, Win32::System::LibraryLoader::*};

fn main() -> Result<()> {
    let args: Vec<_> = env::args_os().collect();
    anyhow::ensure!(args.len() == 3, "Usage: resource_stamp <exe> <manifest>");
    let executable: Vec<_> = Path::new(&args[1])
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect();
    let manifest = fs::read(&args[2]).context("Could not read the Windows application manifest")?;
    let icon = icon();
    let mut group = vec![0, 0, 1, 0, 1, 0, 32, 32, 0, 0, 1, 0, 32, 0];
    group.extend((icon.len() as u32).to_le_bytes());
    group.extend(1u16.to_le_bytes());
    let notices = concat!(
        include_str!("..\\..\\LICENSE"),
        "\n\n",
        include_str!("..\\..\\THIRD-PARTY-NOTICES.txt")
    );
    let resources = [
        (24, manifest),
        (3, icon),
        (14, group),
        (16, version()),
        (10, notices.as_bytes().to_vec()),
    ];
    unsafe {
        let update = BeginUpdateResourceW(PCWSTR(executable.as_ptr()), false)?;
        for (kind, bytes) in resources {
            if let Err(error) = UpdateResourceW(
                update,
                PCWSTR(kind as *const u16),
                PCWSTR(std::ptr::without_provenance(1)),
                0,
                Some(bytes.as_ptr().cast()),
                bytes.len().try_into()?,
            ) {
                let _ = EndUpdateResourceW(update, true);
                return Err(error).context("Could not embed native application resources");
            }
        }
        EndUpdateResourceW(update, false).context("Could not finish application resources")?;
    }
    Ok(())
}

fn icon() -> Vec<u8> {
    let mut data = Vec::new();
    for value in [40u32, 32, 64] {
        data.extend(value.to_le_bytes());
    }
    data.extend(1u16.to_le_bytes());
    data.extend(32u16.to_le_bytes());
    for value in [0u32, 4096, 0, 0, 0, 0] {
        data.extend(value.to_le_bytes());
    }
    let mut mask = vec![0u8; 128];
    for y in (0i32..32).rev() {
        for x in 0i32..32 {
            let visible = (x - x.clamp(6, 25)).pow(2) + (y - y.clamp(6, 25)).pow(2) <= 36;
            let screen = (6..26).contains(&x)
                && (7..22).contains(&y)
                && !((8..24).contains(&x) && (9..19).contains(&y));
            let stand =
                (15..17).contains(&x) && (22..26).contains(&y) || (11..21).contains(&x) && y == 25;
            data.extend(if !visible {
                [0, 0, 0, 0]
            } else if screen || stand {
                [240, 191, 116, 255]
            } else {
                [16, 13, 9, 255]
            });
            if !visible {
                mask[((31 - y) * 4 + x / 8) as usize] |= 1 << (7 - x % 8);
            }
        }
    }
    data.extend(mask);
    data
}

fn version() -> Vec<u8> {
    let mut fixed = Vec::new();
    for value in [
        0xfeef04bdu32,
        0x10000,
        1,
        0,
        1,
        0,
        0x3f,
        0,
        0x40004,
        1,
        0,
        0,
        0,
    ] {
        fixed.extend(value.to_le_bytes());
    }
    let strings = [
        ("FileDescription", "DeXDesk wireless desktop receiver"),
        ("FileVersion", "0.1.0"),
        ("ProductName", "DeXDesk"),
        ("ProductVersion", "0.1.0"),
        ("OriginalFilename", "DeXDesk.exe"),
    ];
    let strings: Vec<_> = strings
        .into_iter()
        .map(|(key, value)| block(key, true, &utf16(value), &[]))
        .collect();
    let table = block("040904b0", true, &[], &strings);
    let info = block("StringFileInfo", true, &[], &[table]);
    let translation = block("Translation", false, &[9, 4, 0xb0, 4], &[]);
    let variables = block("VarFileInfo", true, &[], &[translation]);
    block("VS_VERSION_INFO", false, &fixed, &[info, variables])
}

fn block(key: &str, text: bool, value: &[u8], children: &[Vec<u8>]) -> Vec<u8> {
    let mut data = vec![0u8; 6];
    data.extend(utf16(key));
    while !data.len().is_multiple_of(4) {
        data.push(0);
    }
    data.extend(value);
    for child in children {
        while !data.len().is_multiple_of(4) {
            data.push(0);
        }
        data.extend(child);
    }
    let length = data.len() as u16;
    data[..2].copy_from_slice(&length.to_le_bytes());
    let count = if text { value.len() / 2 } else { value.len() } as u16;
    data[2..4].copy_from_slice(&count.to_le_bytes());
    data[4..6].copy_from_slice(&u16::from(text).to_le_bytes());
    data
}

fn utf16(value: &str) -> Vec<u8> {
    value
        .encode_utf16()
        .chain(Some(0))
        .flat_map(u16::to_le_bytes)
        .collect()
}
