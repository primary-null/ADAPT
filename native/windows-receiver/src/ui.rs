use crate::{
    input::{self, Gesture},
    media::fit_rect,
    protocol,
    runtime::{AppState, Phase, STATE_CHANGED, VIDEO_STARTED, VIDEO_STOPPED},
    settings::{self, Quality},
};
use anyhow::Result;
use std::{
    cell::{Cell, RefCell},
    collections::BTreeSet,
    sync::{atomic::Ordering, Arc},
    thread::JoinHandle,
    time::Instant,
};
use windows::{
    core::{w, PCWSTR},
    Win32::{
        Foundation::*,
        Graphics::{
            Dwm::{DwmSetWindowAttribute, DWMWA_USE_IMMERSIVE_DARK_MODE},
            Gdi::*,
        },
        System::{ApplicationInstallationAndServicing::*, LibraryLoader::GetModuleHandleW, Threading::CreateMutexW},
        UI::{
            Controls::*, HiDpi::*, Input::KeyboardAndMouse::*, Shell::{ShellExecuteW, SetWindowSubclass, RemoveWindowSubclass, DefSubclassProc},
            WindowsAndMessaging::*,
        },
    },
};

const GEAR: usize = 101;
const SAVE: usize = 102;
const CANCEL: usize = 103;
const LOG: usize = 104;
const DISCONNECT: usize = 105;
const HELPER: usize = 204;
const SPEED_LABEL: usize = 205;
const SESSION: usize = 206;
const INFO: usize = 207;
const QUALITY_LABEL: usize = 208;
const SURFACE: COLORREF = COLORREF(0x00271f19);
const BORDER: COLORREF = COLORREF(0x00574a40);
const TICK: usize = 1;
const HEALTH: usize = 2;
const TBM_GETPOS: u32 = WM_USER;
const MAIN_CLASS: PCWSTR = w!("ADAPT Wireless DeXWindow");
const VIDEO_CLASS: PCWSTR = w!("ADAPT Wireless DeXCanvas");
const SETTINGS_CLASS: PCWSTR = w!("ADAPT Wireless DeXSettings");
const BACKGROUND: COLORREF = COLORREF(0x00100d09);
const WHITE: COLORREF = COLORREF(0x00f6f2ec);
const MUTED: COLORREF = COLORREF(0x00b8a89a);
const ACCENT: COLORREF = COLORREF(0x00f0bf74);

struct App {
    state: Arc<AppState>,
    parent: Cell<HWND>,
    video: Cell<HWND>,
    gear: Cell<HWND>,
    dialog: Cell<HWND>,
    quality: Cell<HWND>,
    speed: Cell<HWND>,
    top: Cell<HWND>,
    information: Cell<HWND>,
    regular: Cell<HFONT>,
    title: Cell<HFONT>,
    small: Cell<HFONT>,
    icon: Cell<HFONT>,
    worker: RefCell<Option<JoinHandle<()>>>,
    gesture: RefCell<Gesture>,
    keys: RefCell<BTreeSet<u8>>,
    hovered: Cell<bool>,
    down: Cell<bool>,
    previous: Cell<HWND>,
    pointer: Cell<(u16, u16)>,
    releasing: Cell<bool>,
    closing: Cell<bool>,
    animating: Cell<bool>,
    last_frames: Cell<u64>,
    last_clock: Cell<Instant>,
    fps: Cell<f64>,
    preview: bool,
    dialog_font: Cell<HFONT>,
    dialog_heading: Cell<HFONT>,
    smoke: bool,
    opened: Instant,
}

impl App {
    fn new(state: Arc<AppState>, smoke: bool, preview: bool) -> Self {
        Self {
            state,
            parent: Cell::new(HWND::default()),
            video: Cell::new(HWND::default()),
            gear: Cell::new(HWND::default()),
            dialog: Cell::new(HWND::default()),
            quality: Cell::new(HWND::default()),
            speed: Cell::new(HWND::default()),
            top: Cell::new(HWND::default()),
            information: Cell::new(HWND::default()),
            regular: Cell::new(HFONT::default()),
            title: Cell::new(HFONT::default()),
            small: Cell::new(HFONT::default()),
            icon: Cell::new(HFONT::default()),
            worker: RefCell::new(None),
            gesture: RefCell::new(Gesture::default()),
            keys: RefCell::new(BTreeSet::new()),
            hovered: Cell::new(false),
            down: Cell::new(false),
            previous: Cell::new(HWND::default()),
            pointer: Cell::new((0, 0)),
            releasing: Cell::new(false),
            closing: Cell::new(false),
            animating: Cell::new(false),
            last_frames: Cell::new(0),
            last_clock: Cell::new(Instant::now()),
            fps: Cell::new(0.0),
            preview,
            dialog_font: Cell::new(HFONT::default()),
            dialog_heading: Cell::new(HFONT::default()),
            smoke,
            opened: Instant::now(),
        }
    }
    fn scale(&self, value: i32) -> i32 {
        unsafe { value * GetDpiForWindow(self.parent.get()).max(96) as i32 / 96 }
    }
    fn contacts(&self, events: Vec<input::ContactEvent>) {
        for event in events {
            let mut down = self.down.get();
            self.state
                .send_pointer(event.x, event.y, event.down, &mut down);
            self.down.set(down);
        }
        let animate = self.gesture.borrow().active() || self.closing.get();
        if self.animating.replace(animate) != animate {
            unsafe {
                if animate {
                    SetTimer(self.parent.get(), TICK, 16, None);
                } else {
                    let _ = KillTimer(self.parent.get(), TICK);
                }
            }
        }
    }
    fn cancel_gesture(&self) {
        let events = self.gesture.borrow_mut().cancel();
        self.contacts(events);
    }
    fn release(&self, restore: bool) {
        if self.releasing.replace(true) {
            return;
        }
        let hovered = self.hovered.replace(false);
        self.cancel_gesture();
        let held = !self.keys.borrow().is_empty();
        self.keys.borrow_mut().clear();
        if hovered || held {
            self.state.release_keys();
        }
        if self.down.get() {
            let (x, y) = self.pointer.get();
            self.contacts(vec![input::ContactEvent { x, y, down: false }]);
        }
        unsafe {
            let previous = self.previous.replace(HWND::default());
            if restore
                && !previous.is_invalid()
                && previous != self.parent.get()
                && GetForegroundWindow() == self.parent.get()
            {
                let _ = SetForegroundWindow(previous);
            }
        }
        self.releasing.set(false);
    }
    fn mode(&self) -> Option<crate::protocol::VideoMode> {
        *self.state.mode.lock().unwrap()
    }
    fn point(&self, x: i32, y: i32) -> Option<(u16, u16)> {
        if !self.state.live.load(Ordering::Acquire) || !self.state.input.lock().unwrap().is_some() {
            return None;
        }
        let mode = self.mode()?;
        let mut size = RECT::default();
        unsafe {
            GetClientRect(self.video.get(), &mut size).ok()?;
        }
        let rect = fit_rect(size.right, size.bottom, mode);
        if x < rect.left || y < rect.top || x >= rect.right || y >= rect.bottom {
            return None;
        }
        Some((
            ((x - rect.left) as i64 * 32767 / (rect.right - rect.left).max(1) as i64) as u16,
            ((y - rect.top) as i64 * 32767 / (rect.bottom - rect.top).max(1) as i64) as u16,
        ))
    }
    fn hover(&self, x: i32, y: i32) {
        let Some(point) = self.point(x, y) else {
            self.release(true);
            return;
        };
        self.pointer.set(point);
        if !self.hovered.replace(true) {
            unsafe {
                self.previous.set(GetForegroundWindow());
                let _ = SetForegroundWindow(self.parent.get());
                let _ = SetFocus(self.video.get());
                let mut tracking = TRACKMOUSEEVENT {
                    cbSize: std::mem::size_of::<TRACKMOUSEEVENT>() as u32,
                    dwFlags: TME_LEAVE,
                    hwndTrack: self.video.get(),
                    dwHoverTime: 0,
                };
                let _ = TrackMouseEvent(&mut tracking);
            }
        }
        if !self.gesture.borrow().active() {
            self.contacts(vec![input::ContactEvent {
                x: point.0,
                y: point.1,
                down: self.down.get(),
            }]);
        }
    }
    fn mouse_button(&self, right: bool, down: bool) {
        if !self.hovered.get() {
            return;
        }
        let (x, y) = self.pointer.get();
        if right {
            if down {
                let events = self.gesture.borrow_mut().hold(x, y);
                self.contacts(events);
            }
        } else {
            self.cancel_gesture();
            self.contacts(vec![input::ContactEvent { x, y, down }]);
        }
    }
    fn wheel(&self, delta: i32) {
        if !self.hovered.get() || self.down.get() && !self.gesture.borrow().active() {
            return;
        }
        let Some(mode) = self.mode() else {
            return;
        };
        let (x, y) = self.pointer.get();
        let speed = self.state.settings.lock().unwrap().scroll_speed;
        let events = self
            .gesture
            .borrow_mut()
            .wheel(delta, x, y, mode.height, speed);
        self.contacts(events);
    }
    fn key(&self, vk: u32, lparam: LPARAM, down: bool) -> bool {
        if !self.hovered.get() {
            return false;
        }
        unsafe {
            if vk == VK_TAB.0 as u32 && GetKeyState(VK_MENU.0 as i32) < 0 {
                self.release(false);
                return false;
            }
            if vk == VK_ESCAPE.0 as u32
                && GetKeyState(VK_CONTROL.0 as i32) < 0
                && GetKeyState(VK_MENU.0 as i32) < 0
            {
                self.release(true);
                return true;
            }
        }
        if vk == VK_ESCAPE.0 as u32 {
            self.cancel_gesture();
        }
        let scan = ((lparam.0 as u64 >> 16) & 255) as u32;
        let extended = lparam.0 as u64 & (1 << 24) != 0;
        let Some(usage) = input::hid_key(vk, scan, extended) else {
            return false;
        };
        let mut keys = self.keys.borrow_mut();
        if down {
            keys.insert(usage);
        } else {
            keys.remove(&usage);
        }
        unsafe {
            for (modifier, usage) in [
                (VK_LCONTROL, 0xe0),
                (VK_LSHIFT, 0xe1),
                (VK_LMENU, 0xe2),
                (VK_RCONTROL, 0xe4),
                (VK_RSHIFT, 0xe5),
                (VK_RMENU, 0xe6),
            ] {
                if GetKeyState(modifier.0 as i32) < 0 {
                    keys.insert(usage);
                } else {
                    keys.remove(&usage);
                }
            }
        }
        self.state.send(protocol::keyboard(&keys));
        true
    }
    unsafe fn fonts(&self) {
        for font in [
            self.regular.get(),
            self.title.get(),
            self.small.get(),
            self.icon.get(),
        ] {
            if !font.is_invalid() {
                let _ = DeleteObject(font);
            }
        }
        self.regular.set(font(self.scale(16), 400, w!("Segoe UI")));
        self.title.set(font(self.scale(32), 600, w!("Segoe UI")));
        self.small.set(font(self.scale(13), 400, w!("Segoe UI")));
        self.icon
            .set(font(self.scale(21), 400, w!("Segoe MDL2 Assets")));
    }
    unsafe fn layout(&self) {
        let mut rect = RECT::default();
        if GetClientRect(self.parent.get(), &mut rect).is_err() {
            return;
        }
        let _ = MoveWindow(self.video.get(), 0, 0, rect.right, rect.bottom, true);
        let _ = MoveWindow(
            self.gear.get(),
            rect.right - self.scale(52),
            self.scale(12),
            self.scale(36),
            self.scale(36),
            true,
        );
        let _ = BringWindowToTop(self.gear.get());
    }
    unsafe fn welcome(&self, window: HWND) {
        let mut paint = PAINTSTRUCT::default();
        let dc = BeginPaint(window, &mut paint);
        if !self.state.live.load(Ordering::Acquire) {
            let mut rect = RECT::default();
            let _ = GetClientRect(window, &mut rect);
            let brush = GetStockObject(DC_BRUSH);
            SetDCBrushColor(dc, BACKGROUND);
            FillRect(dc, &rect, HBRUSH(brush.0));
            SetBkMode(dc, TRANSPARENT);
            let x = self.scale(56);
            let width = (rect.right - x * 2).max(self.scale(260));
            let y = (rect.bottom / 2 - self.scale(150)).max(self.scale(58));
            let phase = self.state.phase.lock().unwrap().clone();
            let error = matches!(phase, Phase::Error(_));
            let (heading, status) = match phase {
                Phase::Starting => (
                    "Getting ready",
                    "Starting the wireless receiver...".to_owned(),
                ),
                Phase::Ready => (
                    "Your Galaxy, on your laptop",
                    "Ready for wireless DeX".to_owned(),
                ),
                Phase::Connecting(name) => ("Connecting", format!("Connecting to {name}...")),
                Phase::Connected => (
                    "Almost there",
                    "Waiting for the first picture...".to_owned(),
                ),
                Phase::Error(message) => ("Let's get connected", message),
            };
            text(
                dc,
                self.title.get(),
                WHITE,
                heading,
                RECT {
                    left: x,
                    top: y,
                    right: x + width,
                    bottom: y + self.scale(52),
                },
                DT_LEFT | DT_SINGLELINE,
            );
            text(
                dc,
                self.regular.get(),
                if error { COLORREF(0x0096a6f5) } else { ACCENT },
                &status,
                RECT {
                    left: x,
                    top: y + self.scale(60),
                    right: x + width,
                    bottom: y + self.scale(128),
                },
                DT_LEFT | DT_WORDBREAK,
            );
            let computer = std::env::var("COMPUTERNAME").unwrap_or_else(|_| "this PC".to_owned());
            let instructions=format!("1   Open Quick settings on your Galaxy\n2   Tap DeX, then TV or monitor\n3   Choose {computer}");
            text(
                dc,
                self.regular.get(),
                WHITE,
                &instructions,
                RECT {
                    left: x,
                    top: y + self.scale(145),
                    right: x + width,
                    bottom: y + self.scale(255),
                },
                DT_LEFT | DT_WORDBREAK,
            );
            text(dc,self.small.get(),MUTED,"Keep Wi-Fi on, even if your laptop uses Ethernet.\nNo cable. No phone app. No USB debugging.",
                RECT{left:x,top:y+self.scale(275),right:x+width,bottom:y+self.scale(335)},DT_LEFT|DT_WORDBREAK);
        }
        let _ = EndPaint(window, &paint);
    }
    unsafe fn open_settings(&self) {
        self.release(false);
        if !self.dialog.get().is_invalid() {
            let _ = SetForegroundWindow(self.dialog.get());
            return;
        }
        let instance = HINSTANCE(GetModuleHandleW(None).unwrap_or_default().0);
        let mut parent = RECT::default();
        let _ = GetWindowRect(self.parent.get(), &mut parent);
        let width = self.scale(470);
        let height = self.scale(460);
        let result = CreateWindowExW(
            WS_EX_DLGMODALFRAME,
            SETTINGS_CLASS,
            w!("ADAPT Wireless DeX settings"),
            WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU,
            parent.left + (parent.right - parent.left - width) / 2,
            parent.top + (parent.bottom - parent.top - height) / 2,
            width,
            height,
            self.parent.get(),
            None,
            instance,
            Some(self as *const Self as *const _),
        );
        match result {
            Ok(window) => {
                self.dialog.set(window);
                // CreateWindow can apply its original size after WM_CREATE. Finalise
                // measured bounds only after it returns, before showing the window.
                self.settings_layout(window, self.settings_dpi(window));
                let mut bounds = RECT::default();
                let _ = GetWindowRect(window, &mut bounds);
                let _ = SetWindowPos(window, None,
                    parent.left + (parent.right-parent.left-(bounds.right-bounds.left))/2,
                    parent.top + (parent.bottom-parent.top-(bounds.bottom-bounds.top))/2,
                    0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
                let _ = EnableWindow(self.parent.get(), false);
                let _ = ShowWindow(window, if self.preview { SW_SHOWNOACTIVATE } else { SW_SHOW });
                // Refresh the nonclient frame after the first show as well; the
                // inactive preview must receive the same dark caption treatment.
                let dark = BOOL(1);
                let _ = DwmSetWindowAttribute(window, DWMWA_USE_IMMERSIVE_DARK_MODE,
                    &dark as *const _ as *const _, std::mem::size_of::<BOOL>() as u32);
                let _ = SetWindowPos(window, None, 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED);
            }
            Err(error) => self.message(&format!("Could not open settings: {error}")),
        }
    }
    unsafe fn settings_controls(&self, window: HWND) {
        let dark = BOOL(1);
        let _ = DwmSetWindowAttribute(window, DWMWA_USE_IMMERSIVE_DARK_MODE,
            &dark as *const _ as *const _, std::mem::size_of::<BOOL>() as u32);
        let settings = self.state.settings.lock().unwrap().clone();
        let control = |class: PCWSTR, label: &str, style: u32, id: usize| {
            let label = wide(label);
            let handle = CreateWindowExW(WINDOW_EX_STYLE(0), class, PCWSTR(label.as_ptr()),
                WS_CHILD | WS_VISIBLE | WINDOW_STYLE(style), 0, 0, 1, 1, window,
                HMENU(id as *mut _), HINSTANCE(GetModuleHandleW(None).unwrap_or_default().0), None)
                .unwrap_or_default();
            handle
        };
        control(w!("STATIC"), "Cast &quality target", 0, QUALITY_LABEL);
        let combo = control(w!("COMBOBOX"), "", CBS_DROPDOWNLIST as u32 | CBS_OWNERDRAWFIXED as u32 |
            CBS_HASSTRINGS as u32 | WS_VSCROLL.0 | WS_TABSTOP.0, 201);
        // Only the documented SetWindowTheme API is used. This optional theme name
        // supplies a dark arrow/scrollbar on Windows 11; owner drawing guarantees
        // readable item colours even if that theme is absent.
        let _ = SetWindowTheme(combo, w!("DarkMode_CFD"), None);
        for label in ["1080p - target 60 FPS (recommended)", "1080p - target 30 FPS", "720p - target 60 FPS"] {
            let label = wide(label);
            SendMessageW(combo, CB_ADDSTRING, WPARAM(0), LPARAM(label.as_ptr() as isize));
        }
        SendMessageW(combo, CB_SETCURSEL, WPARAM(match settings.quality {
            Quality::FullHd60 => 0, Quality::FullHd30 => 1, Quality::Hd60 => 2,
        }), LPARAM(0));
        let _ = SetWindowSubclass(combo, Some(settings_button_proc), 1, 0);
        self.quality.set(combo);
        control(w!("STATIC"), "Changes apply on the next connection. The phone chooses actual FPS.", 0, HELPER);
        control(w!("STATIC"), "Scroll s&peed", 0, SPEED_LABEL);
        let speed = control(TRACKBAR_CLASSW, "", WS_TABSTOP.0 | TBS_NOTICKS | TBS_FIXEDLENGTH, 202);
        SendMessageW(speed, TBM_SETRANGE, WPARAM(1), LPARAM(30 | (120 << 16)));
        SendMessageW(speed, TBM_SETPOS, WPARAM(1), LPARAM(settings.scroll_speed as isize));
        self.speed.set(speed);
        let top = control(w!("BUTTON"), "&Keep window on top", BS_AUTOCHECKBOX as u32 | WS_TABSTOP.0, 203);
        // Classic checkbox honours WM_CTLCOLORBTN, unlike the light themed label.
        let _ = SetWindowTheme(top, w!(""), w!(""));
        SendMessageW(top, BM_SETCHECK, WPARAM(settings.always_on_top as usize), LPARAM(0));
        let _ = SetWindowSubclass(top, Some(settings_button_proc), 1, 0);
        self.top.set(top);
        control(w!("STATIC"), "Session", 0, SESSION);
        self.information.set(control(w!("STATIC"), &self.connection_info(), 0, INFO));
        for (id, label) in [(LOG, "&Diagnostics"), (DISCONNECT, "D&isconnect"), (CANCEL, "&Cancel"), (SAVE, "&Save")] {
            let button = control(w!("BUTTON"), label, BS_OWNERDRAW as u32 | WS_TABSTOP.0, id);
            let _ = SetWindowSubclass(button, Some(settings_button_proc), 1, 0);
        }
        self.settings_layout(window, self.settings_dpi(window));
        SetTimer(window, 3, 500, None);
    }
    unsafe fn settings_dpi(&self, window: HWND) -> u32 {
        if cfg!(debug_assertions) && self.preview {
            std::env::var("ADAPT_DEX_PREVIEW_DPI").ok().and_then(|s| s.parse::<u32>().ok())
                .filter(|dpi| [96, 120, 144, 192].contains(dpi)).unwrap_or(GetDpiForWindow(window))
        } else { GetDpiForWindow(window) }
    }
    unsafe fn settings_layout(&self, window: HWND, dpi: u32) {
        let scale = |n: i32| (n * dpi as i32 + 48) / 96;
        let regular = font(scale(16), 400, w!("Segoe UI"));
        let heading = font(scale(16), 600, w!("Segoe UI"));
        let old = [self.dialog_font.replace(regular), self.dialog_heading.replace(heading)];
        let dc = GetDC(window);
        let unit = scale(8);
        let width = measured(dc, regular, "1080p - target 60 FPS (recommended)", 10000).right
            .max(scale(440)) + unit * 4;
        let content = width - unit * 6;
        let line = measured(dc, regular, "Ag", content).bottom;
        let row = line + unit * 2;
        let mut y = unit * 3;
        let place = |id: usize, x, y, w, h, bold| {
            if let Ok(child) = GetDlgItem(window, id as i32) {
                SendMessageW(child, WM_SETFONT, WPARAM(if bold { heading } else { regular }.0 as usize), LPARAM(1));
                let _ = MoveWindow(child, x, y, w, h, true);
            }
        };
        place(QUALITY_LABEL, unit*3, y, content, line, true); y += line + unit;
        place(201, unit*3, y, content, row * 5, false);
        SendMessageW(self.quality.get(), CB_SETITEMHEIGHT, WPARAM(usize::MAX), LPARAM(row as isize));
        SendMessageW(self.quality.get(), CB_SETITEMHEIGHT, WPARAM(0), LPARAM(row as isize));
        y += row + unit;
        let helper = measured(dc, regular, "Changes apply on the next connection. The phone chooses actual FPS.", content).bottom;
        place(HELPER, unit*3, y, content, helper, false); y += helper + unit*3;
        place(SPEED_LABEL, unit*3, y, content, line, true); y += line + unit;
        SendMessageW(self.speed.get(), TBM_SETTHUMBLENGTH, WPARAM((row-unit) as usize), LPARAM(0));
        place(202, unit*3, y, content, row, false); y += row + unit;
        place(203, unit*3, y, content, row, false); y += row + unit*3;
        place(SESSION, unit*3, y, content, line, true); y += line + unit;
        // Reserve for the longer connected status, even while disconnected.
        let info = measured(dc, regular, "Connected: 1920 x 1080, 60 FPS selected\nLive: 120.0 FPS decoded - Windows native video", content).bottom
            .max(measured(dc, regular, &self.connection_info(), content).bottom);
        place(INFO, unit*4, y+unit, content-unit*2, info, false); y += info + unit*4;
        let button_width = |label: &str| measured(dc, regular, label, 10000).right + unit*4;
        let diag = button_width("Diagnostics");
        let disconnect = button_width("Disconnect");
        place(LOG, unit*3, y, diag, row, false);
        place(DISCONNECT, unit*4+diag, y, disconnect, row, false); y += row + unit*4;
        let save = button_width("Save").max(unit*12);
        let cancel = button_width("Cancel").max(unit*12);
        place(CANCEL, width-unit*4-save-cancel, y, cancel, row, false);
        place(SAVE, width-unit*3-save, y, save, row, true); y += row + unit*3;
        let _ = ReleaseDC(window, dc);
        for value in old { if !value.is_invalid() { let _ = DeleteObject(value); } }
        let mut rect = RECT { left: 0, top: 0, right: width, bottom: y };
        let _ = AdjustWindowRectExForDpi(&mut rect, WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU,
            false, WS_EX_DLGMODALFRAME, dpi);
        let _ = SetWindowPos(window, None, 0, 0, rect.right-rect.left, rect.bottom-rect.top,
            SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE);
        let _ = InvalidateRect(window, None, true);
        self.settings_status(window);
    }
    unsafe fn settings_status(&self, window: HWND) {
        let value = SendMessageW(self.speed.get(), TBM_GETPOS, WPARAM(0), LPARAM(0)).0;
        let label = wide(&format!("Scroll s&peed   {value}"));
        if let Ok(child) = GetDlgItem(window, SPEED_LABEL as i32) {
            let _ = SetWindowTextW(child, PCWSTR(label.as_ptr()));
        }
        if let Ok(child) = GetDlgItem(window, DISCONNECT as i32) {
            let _ = EnableWindow(child, self.state.session_active.load(Ordering::Acquire));
        }
    }
    fn connection_info(&self) -> String {
        if let Some(mode) = self.mode() {
            format!("Connected: {} x {}, {} FPS selected\nLive: {:.1} FPS decoded - Windows native video",mode.width,mode.height,mode.fps,self.fps.get())
        } else {
            "Not connected. Right-click uses long-press; wheel uses smooth scrolling.".into()
        }
    }
    unsafe fn apply_settings(&self) -> Result<()> {
        let index = SendMessageW(self.quality.get(), CB_GETCURSEL, WPARAM(0), LPARAM(0)).0;
        let quality = match index {
            0 => Quality::FullHd60,
            1 => Quality::FullHd30,
            2 => Quality::Hd60,
            _ => anyhow::bail!("Select a quality target"),
        };
        let mut settings = self.state.settings.lock().unwrap().clone();
        settings.quality = quality;
        settings.scroll_speed =
            SendMessageW(self.speed.get(), TBM_GETPOS, WPARAM(0), LPARAM(0)).0 as u32;
        settings.always_on_top = SendMessageW(self.top.get(), BM_GETCHECK, WPARAM(0), LPARAM(0)).0
            == BST_CHECKED.0 as isize;
        settings::save(&settings)?;
        *self.state.settings.lock().unwrap() = settings.clone();
        SetWindowPos(
            self.parent.get(),
            if settings.always_on_top {
                HWND_TOPMOST
            } else {
                HWND_NOTOPMOST
            },
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )?;
        Ok(())
    }
    unsafe fn close_settings(&self) {
        let window = self.dialog.replace(HWND::default());
        if !window.is_invalid() {
            let _ = EnableWindow(self.parent.get(), true);
            SetWindowLongPtrW(window, GWLP_USERDATA, 0);
            let _ = DestroyWindow(window);
            if !self.preview { let _ = SetForegroundWindow(self.parent.get()); }
        }
    }
    unsafe fn message(&self, text: &str) {
        let text = wide(text);
        MessageBoxW(
            if self.dialog.get().is_invalid() {
                self.parent.get()
            } else {
                self.dialog.get()
            },
            PCWSTR(text.as_ptr()),
            w!("ADAPT Wireless DeX"),
            MB_OK | MB_ICONWARNING,
        );
    }
}

// Box<App> owns every window borrowing its address. Drop also runs on early
// errors and Rust unwinding, while the box allocation is still alive.
impl Drop for App {
    fn drop(&mut self) {
        self.state.running.store(false, Ordering::Release);
        self.state.disconnect.store(true, Ordering::Release);
        if let Some(worker) = self.worker.get_mut().take() {
            // Avoid logging (and potentially poisoned locks) during unwinding.
            let _ = worker.join();
        }
        unsafe {
            self.state.hwnd.store(0, Ordering::Release);
            self.state.video_hwnd.store(0, Ordering::Release);
            let windows = [
                self.dialog.replace(HWND::default()),
                self.video.replace(HWND::default()),
                self.parent.replace(HWND::default()),
            ];
            // Detach every borrower before destruction can send messages to
            // another window. A failed DestroyWindow cannot leave a stale App.
            for window in windows {
                if !window.is_invalid() {
                    SetWindowLongPtrW(window, GWLP_USERDATA, 0);
                }
            }
            for window in windows {
                if !window.is_invalid() {
                    let _ = DestroyWindow(window);
                }
            }
            for font in [&self.regular, &self.title, &self.small, &self.icon, &self.dialog_font, &self.dialog_heading] {
                let font = font.replace(HFONT::default());
                if !font.is_invalid() {
                    let _ = DeleteObject(font);
                }
            }
        }
    }
}

struct InstanceMutex(HANDLE);
impl Drop for InstanceMutex {
    fn drop(&mut self) {
        unsafe { let _ = CloseHandle(self.0); }
    }
}

pub fn run(state: Arc<AppState>, preview: bool, smoke: bool, preview_settings: bool) -> Result<()> {
    unsafe {
        let _ = SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
        let _styles = VisualStyles::activate()?;
        let _mutex = if preview {
            None
        } else {
            let mutex = CreateMutexW(None, false, w!("Local\\ADAPT Wireless DeXSingleInstance"))?;
            let guard = InstanceMutex(mutex);
            if GetLastError() == ERROR_ALREADY_EXISTS {
                if let Ok(window) = FindWindowW(MAIN_CLASS, None) {
                    let _ = ShowWindow(window, SW_RESTORE);
                    let _ = SetForegroundWindow(window);
                }
                return Ok(());
            }
            Some(guard)
        };
        let common = INITCOMMONCONTROLSEX {
            dwSize: std::mem::size_of::<INITCOMMONCONTROLSEX>() as u32,
            dwICC: ICC_BAR_CLASSES,
        };
        if !InitCommonControlsEx(&common).as_bool() {
            anyhow::bail!("Windows could not initialize native controls");
        }
        let instance = HINSTANCE(GetModuleHandleW(None)?.0);
        let classes: [(PCWSTR, WNDPROC); 3] = [
            (MAIN_CLASS, Some(main_proc)),
            (VIDEO_CLASS, Some(video_proc)),
            (SETTINGS_CLASS, Some(settings_proc)),
        ];
        for (name, procedure) in classes {
            let class = WNDCLASSW {
                lpfnWndProc: procedure,
                hInstance: instance,
                lpszClassName: name,
                hIcon: LoadIconW(instance, PCWSTR(std::ptr::without_provenance(1)))
                    .unwrap_or_default(),
                hCursor: LoadCursorW(None, IDC_ARROW)?,
                ..Default::default()
            };
            if RegisterClassW(&class) == 0 {
                return Err(windows::core::Error::from_win32().into());
            }
        }
        let app = Box::new(App::new(state.clone(), smoke, preview));
        let settings = state.settings.lock().unwrap().clone();
        let window = CreateWindowExW(
            WINDOW_EX_STYLE(0),
            MAIN_CLASS,
            w!("ADAPT Wireless DeX"),
            WS_OVERLAPPEDWINDOW | WS_CLIPCHILDREN,
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            settings.window_width,
            settings.window_height,
            None,
            None,
            instance,
            Some(app.as_ref() as *const App as *const _),
        )?;
        if settings.always_on_top {
            SetWindowPos(
                window,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )?;
        }
        let dark = BOOL(1);
        let _ = DwmSetWindowAttribute(
            window,
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            &dark as *const _ as *const _,
            std::mem::size_of::<BOOL>() as u32,
        );
        let _ = ShowWindow(window, if preview { SW_SHOWNOACTIVATE } else { SW_SHOW });
        if preview_settings { app.open_settings(); }
        if preview {
            state.set_phase(Phase::Error(
                "UI preview - wireless receiver is not started".into(),
            ));
        } else {
            *app.worker.borrow_mut() = Some(crate::receiver::start(state.clone()));
        }
        let mut message = MSG::default();
        loop {
            let result = GetMessageW(&mut message, None, 0, 0);
            if result.0 == 0 {
                break;
            }
            if result.0 < 0 {
                return Err(windows::core::Error::from_win32().into());
            }
            if !app.dialog.get().is_invalid() && message.message == WM_KEYDOWN {
                if (message.wParam.0 == VK_ESCAPE.0 as usize || message.wParam.0 == VK_RETURN.0 as usize)
                    && SendMessageW(app.quality.get(), CB_GETDROPPEDSTATE, WPARAM(0), LPARAM(0)).0 == 0 {
                    let id = if message.wParam.0 == VK_ESCAPE.0 as usize { CANCEL } else { SAVE };
                    SendMessageW(app.dialog.get(), WM_COMMAND, WPARAM(id), LPARAM(0));
                    continue;
                }
            }
            if !app.dialog.get().is_invalid()
                && IsDialogMessageW(app.dialog.get(), &message).as_bool()
            {
                continue;
            }
            let _ = TranslateMessage(&message);
            DispatchMessageW(&message);
        }
    }
    Ok(())
}

unsafe fn app_pointer(window: HWND, message: u32, lparam: LPARAM) -> *const App {
    if message == WM_NCCREATE {
        let create = &*(lparam.0 as *const CREATESTRUCTW);
        SetWindowLongPtrW(window, GWLP_USERDATA, create.lpCreateParams as isize);
    }
    let pointer = GetWindowLongPtrW(window, GWLP_USERDATA) as *const App;
    if !pointer.is_null() {
        let app = &*pointer;
        if message == WM_NCDESTROY {
            SetWindowLongPtrW(window, GWLP_USERDATA, 0);
            for cell in [&app.dialog, &app.video, &app.parent] {
                if cell.get() == window {
                    cell.set(HWND::default());
                }
            }
        }
    }
    pointer
}

unsafe extern "system" fn main_proc(
    window: HWND,
    message: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    let pointer = app_pointer(window, message, lparam);
    if pointer.is_null() {
        return DefWindowProcW(window, message, wparam, lparam);
    }
    let app = &*pointer;
    if message == WM_NCCREATE {
        app.parent.set(window);
    }
    match message {
        WM_CREATE => {
            app.parent.set(window);
            app.state.hwnd.store(window.0 as usize, Ordering::Release);
            app.fonts();
            let instance = HINSTANCE(GetModuleHandleW(None).unwrap_or_default().0);
            match CreateWindowExW(
                WINDOW_EX_STYLE(0),
                VIDEO_CLASS,
                None,
                WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS,
                0,
                0,
                1,
                1,
                window,
                None,
                instance,
                Some(pointer.cast()),
            ) {
                Ok(video) => {
                    app.video.set(video);
                    app.state
                        .video_hwnd
                        .store(video.0 as usize, Ordering::Release);
                }
                Err(error) => {
                    app.state
                        .log(format!("Could not create the video view: {error}"));
                    return LRESULT(-1);
                }
            }
            match CreateWindowExW(
                WINDOW_EX_STYLE(0),
                w!("BUTTON"),
                w!("Settings"),
                WS_CHILD | WS_VISIBLE | WS_TABSTOP | WINDOW_STYLE(BS_OWNERDRAW as u32),
                0,
                0,
                36,
                36,
                window,
                HMENU(GEAR as *mut _),
                instance,
                None,
            ) {
                Ok(gear) => app.gear.set(gear),
                Err(error) => {
                    app.state
                        .log(format!("Could not create settings button: {error}"));
                    return LRESULT(-1);
                }
            }
            app.layout();
            SetTimer(window, HEALTH, 1000, None);
            LRESULT(0)
        }
        WM_SIZE => {
            app.layout();
            LRESULT(0)
        }
        WM_DPICHANGED => {
            app.fonts();
            let rect = &*(lparam.0 as *const RECT);
            let _ = SetWindowPos(
                window,
                None,
                rect.left,
                rect.top,
                rect.right - rect.left,
                rect.bottom - rect.top,
                SWP_NOZORDER | SWP_NOACTIVATE,
            );
            app.layout();
            LRESULT(0)
        }
        WM_GETMINMAXINFO => {
            let info = &mut *(lparam.0 as *mut MINMAXINFO);
            info.ptMinTrackSize = POINT {
                x: app.scale(640),
                y: app.scale(480),
            };
            LRESULT(0)
        }
        WM_COMMAND if wparam.0 & 0xffff == GEAR => {
            app.open_settings();
            LRESULT(0)
        }
        WM_DRAWITEM => {
            let draw = &*(lparam.0 as *const DRAWITEMSTRUCT);
            SetDCBrushColor(draw.hDC, BACKGROUND);
            FillRect(draw.hDC, &draw.rcItem, HBRUSH(GetStockObject(DC_BRUSH).0));
            SetBkMode(draw.hDC, TRANSPARENT);
            text(
                draw.hDC,
                app.icon.get(),
                WHITE,
                "\u{e713}",
                draw.rcItem,
                DT_CENTER | DT_VCENTER | DT_SINGLELINE,
            );
            LRESULT(1)
        }
        WM_TIMER => {
            if wparam.0 == TICK {
                if app.hovered.get() && app.state.live.load(Ordering::Acquire) {
                    let events = app.gesture.borrow_mut().tick();
                    app.contacts(events);
                }
                if app.closing.get() {
                    let done = app
                        .worker
                        .borrow()
                        .as_ref()
                        .is_none_or(|worker| worker.is_finished());
                    if done {
                        if let Some(worker) = app.worker.borrow_mut().take() {
                            if worker.join().is_err() {
                                app.state.log("Receiver shutdown failed");
                            }
                        }
                        let _ = DestroyWindow(window);
                    }
                }
            } else if wparam.0 == HEALTH {
                app.state.write_status();
                if !app.closing.get() && settings::data_dir().is_ok_and(|p| p.join("stop.request").is_file()) {
                    let _ = PostMessageW(window, WM_CLOSE, WPARAM(0), LPARAM(0));
                    return LRESULT(0);
                }
                if app.smoke && app.opened.elapsed().as_secs() >= 4 {
                    let _ = PostMessageW(window, WM_CLOSE, WPARAM(0), LPARAM(0));
                }
                let count = app.state.frames.load(Ordering::Acquire);
                let elapsed = app
                    .last_clock
                    .replace(Instant::now())
                    .elapsed()
                    .as_secs_f64()
                    .max(0.001);
                let previous = app.last_frames.replace(count);
                app.fps.set(count.saturating_sub(previous) as f64 / elapsed);
                if !app.dialog.get().is_invalid() {
                    let value = wide(&app.connection_info());
                    let _ = SetWindowTextW(app.information.get(), PCWSTR(value.as_ptr()));
                }
            }
            LRESULT(0)
        }
        STATE_CHANGED | VIDEO_STARTED | VIDEO_STOPPED => {
            if message == VIDEO_STOPPED {
                app.release(false);
                app.last_frames.set(0);
                app.fps.set(0.0);
            }
            let _ = InvalidateRect(app.video.get(), None, false);
            LRESULT(0)
        }
        WM_KEYDOWN | WM_SYSKEYDOWN | WM_KEYUP | WM_SYSKEYUP => {
            if app.key(wparam.0 as u32, lparam, message == WM_KEYDOWN || message == WM_SYSKEYDOWN) {
                LRESULT(0)
            } else {
                DefWindowProcW(window, message, wparam, lparam)
            }
        }
        WM_ACTIVATE if wparam.0 & 0xffff == WA_INACTIVE as usize => {
            app.release(false);
            LRESULT(0)
        }
        WM_CLOSE => {
            if !app.closing.replace(true) {
                app.release(false);
                app.close_settings();
                if !app.smoke {
                    let mut placement = WINDOWPLACEMENT {
                        length: std::mem::size_of::<WINDOWPLACEMENT>() as u32,
                        ..Default::default()
                    };
                    if GetWindowPlacement(window, &mut placement).is_ok() {
                        let width =
                            placement.rcNormalPosition.right - placement.rcNormalPosition.left;
                        let height =
                            placement.rcNormalPosition.bottom - placement.rcNormalPosition.top;
                        let mut settings = app.state.settings.lock().unwrap().clone();
                        settings.window_width = width.clamp(640, 8192);
                        settings.window_height = height.clamp(480, 8192);
                        if let Err(error) = settings::save(&settings) {
                            app.state
                                .log(format!("Could not remember window size: {error:#}"));
                        }
                    }
                }
                app.state.running.store(false, Ordering::Release);
                app.state.disconnect.store(true, Ordering::Release);
                let _ = EnableWindow(app.gear.get(), false);
            }
            LRESULT(0)
        }
        WM_DESTROY => {
            PostQuitMessage(0);
            LRESULT(0)
        }
        _ => DefWindowProcW(window, message, wparam, lparam),
    }
}

unsafe extern "system" fn video_proc(
    window: HWND,
    message: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    let pointer = app_pointer(window, message, lparam);
    if pointer.is_null() {
        return DefWindowProcW(window, message, wparam, lparam);
    }
    let app = &*pointer;
    if message == WM_NCCREATE {
        app.video.set(window);
    }
    let x = (lparam.0 & 0xffff) as i16 as i32;
    let y = ((lparam.0 >> 16) & 0xffff) as i16 as i32;
    match message {
        WM_PAINT => {
            app.welcome(window);
            LRESULT(0)
        }
        WM_ERASEBKGND => LRESULT(1),
        WM_MOUSEMOVE => {
            app.hover(x, y);
            LRESULT(0)
        }
        WM_MOUSELEAVE => {
            app.release(true);
            LRESULT(0)
        }
        WM_LBUTTONDOWN => {
            app.hover(x, y);
            app.mouse_button(false, true);
            LRESULT(0)
        }
        WM_LBUTTONUP => {
            app.mouse_button(false, false);
            LRESULT(0)
        }
        WM_RBUTTONDOWN => {
            app.hover(x, y);
            app.mouse_button(true, true);
            LRESULT(0)
        }
        WM_RBUTTONUP | WM_CONTEXTMENU => LRESULT(0),
        WM_MOUSEWHEEL => {
            let mut point = POINT { x, y };
            let _ = ScreenToClient(window, &mut point);
            app.hover(point.x, point.y);
            app.wheel(((wparam.0 >> 16) & 0xffff) as i16 as i32);
            LRESULT(0)
        }
        WM_KEYDOWN | WM_SYSKEYDOWN | WM_KEYUP | WM_SYSKEYUP => {
            if app.key(
                wparam.0 as u32,
                lparam,
                message == WM_KEYDOWN || message == WM_SYSKEYDOWN,
            ) {
                LRESULT(0)
            } else {
                DefWindowProcW(window, message, wparam, lparam)
            }
        }
        _ => DefWindowProcW(window, message, wparam, lparam),
    }
}

unsafe extern "system" fn settings_proc(
    window: HWND,
    message: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    let pointer = app_pointer(window, message, lparam);
    if pointer.is_null() {
        return DefWindowProcW(window, message, wparam, lparam);
    }
    let app = &*pointer;
    if message == WM_NCCREATE {
        app.dialog.set(window);
    }
    match message {
        WM_CREATE => {
            app.settings_controls(window);
            LRESULT(0)
        }
        WM_DPICHANGED => {
            let rect = &*(lparam.0 as *const RECT);
            let _ = SetWindowPos(window, None, rect.left, rect.top, 0, 0,
                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
            app.settings_layout(window, (wparam.0 & 0xffff) as u32);
            LRESULT(0)
        }
        WM_TIMER | WM_HSCROLL => { app.settings_status(window); LRESULT(0) }
        WM_ERASEBKGND => {
            let mut rect = RECT::default();
            let _ = GetClientRect(window, &mut rect);
            let dc = HDC(wparam.0 as *mut _);
            fill(dc, &rect, BACKGROUND);
            let mut card = RECT::default();
            if GetWindowRect(app.information.get(), &mut card).is_ok() {
                let mut origin = POINT { x: card.left, y: card.top };
                let _ = ScreenToClient(window, &mut origin);
                let _ = OffsetRect(&mut card, origin.x-card.left, origin.y-card.top);
                let pad = (GetDpiForWindow(window) as i32 * 8 / 96).max(1);
                let _ = InflateRect(&mut card, pad, pad);
                fill(dc, &card, SURFACE);
            }
            LRESULT(1)
        }
        WM_CTLCOLORSTATIC | WM_CTLCOLORBTN | WM_CTLCOLORLISTBOX => {
            let dc = HDC(wparam.0 as *mut _);
            let id = GetDlgCtrlID(HWND(lparam.0 as *mut _)) as usize;
            let bg = if message == WM_CTLCOLORLISTBOX || id == INFO { SURFACE } else { BACKGROUND };
            SetBkColor(dc, bg);
            SetDCBrushColor(dc, bg);
            SetTextColor(dc, if id == HELPER || id == INFO { MUTED } else { WHITE });
            LRESULT(GetStockObject(DC_BRUSH).0 as isize)
        }
        WM_MEASUREITEM => {
            let item = &mut *(lparam.0 as *mut MEASUREITEMSTRUCT);
            item.itemHeight = (GetDpiForWindow(window) * 38 / 96).max(24);
            LRESULT(1)
        }
        WM_DRAWITEM => {
            settings_draw(app, &*(lparam.0 as *const DRAWITEMSTRUCT));
            LRESULT(1)
        }
        WM_NOTIFY => {
            let header = &*(lparam.0 as *const NMHDR);
            if header.hwndFrom == app.speed.get() && header.code == NM_CUSTOMDRAW {
                let draw = &*(lparam.0 as *const NMCUSTOMDRAW);
                return slider_draw(draw);
            }
            LRESULT(0)
        }
        WM_NEXTDLGCTL => {
            let next = if lparam.0 != 0 { HWND(wparam.0 as *mut _) } else {
                GetNextDlgTabItem(window, GetFocus(), wparam.0 != 0).unwrap_or_default()
            };
            let _ = SetFocus(next);
            LRESULT(0)
        }
        DM_GETDEFID => LRESULT(((DC_HASDEFID as usize) << 16 | SAVE) as isize),
        WM_COMMAND => {
            match wparam.0 & 0xffff {
                SAVE => match app.apply_settings() {
                    Ok(()) => app.close_settings(),
                    Err(error) => app.message(&format!("Could not save settings: {error:#}")),
                },
                1 => match app.apply_settings() {
                    Ok(()) => app.close_settings(),
                    Err(error) => app.message(&format!("Could not save settings: {error:#}")),
                },
                2 => app.close_settings(),
                CANCEL => app.close_settings(),
                LOG => {
                    if let Ok(directory) = settings::data_dir() {
                        let path = wide(&directory.join("DeXDesk.log").to_string_lossy());
                        ShellExecuteW(
                            window,
                            w!("open"),
                            PCWSTR(path.as_ptr()),
                            None,
                            None,
                            SW_SHOWNORMAL,
                        );
                    }
                }
                DISCONNECT => {
                    app.state.disconnect.store(true, Ordering::Release);
                    app.close_settings();
                }
                _ => {}
            }
            LRESULT(0)
        }
        WM_CLOSE => {
            app.close_settings();
            LRESULT(0)
        }
        _ => DefWindowProcW(window, message, wparam, lparam),
    }
}

unsafe fn font(height: i32, weight: i32, name: PCWSTR) -> HFONT {
    CreateFontW(
        -height,
        0,
        0,
        0,
        weight,
        0,
        0,
        0,
        DEFAULT_CHARSET.0 as u32,
        OUT_DEFAULT_PRECIS.0 as u32,
        CLIP_DEFAULT_PRECIS.0 as u32,
        CLEARTYPE_QUALITY.0 as u32,
        DEFAULT_PITCH.0 as u32,
        name,
    )
}

unsafe fn text(
    dc: HDC,
    font: HFONT,
    color: COLORREF,
    value: &str,
    mut rect: RECT,
    flags: DRAW_TEXT_FORMAT,
) {
    let previous = SelectObject(dc, font);
    SetTextColor(dc, color);
    let mut value: Vec<u16> = value.encode_utf16().collect();
    DrawTextW(dc, &mut value, &mut rect, flags);
    SelectObject(dc, previous);
}

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(Some(0)).collect()
}

// All drawing uses stock DC objects; no per-paint GDI allocations survive a call.
unsafe fn fill(dc: HDC, rect: &RECT, color: COLORREF) {
    SetDCBrushColor(dc, color);
    FillRect(dc, rect, HBRUSH(GetStockObject(DC_BRUSH).0));
}
unsafe fn measured(dc: HDC, font: HFONT, value: &str, width: i32) -> RECT {
    let previous = SelectObject(dc, font);
    let mut rect = RECT { right: width, ..Default::default() };
    let mut value: Vec<u16> = value.encode_utf16().collect();
    DrawTextW(dc, &mut value, &mut rect, DT_CALCRECT | DT_WORDBREAK);
    SelectObject(dc, previous);
    rect
}
unsafe extern "system" fn settings_button_proc(window: HWND, message: u32,
    wparam: WPARAM, lparam: LPARAM, _id: usize, _data: usize) -> LRESULT {
    match message {
        WM_PAINT if GetDlgCtrlID(window) == 201 => {
            let mut paint = PAINTSTRUCT::default();
            let dc = BeginPaint(window, &mut paint);
            let mut rect = RECT::default();
            let _ = GetClientRect(window, &mut rect);
            let selected_font = HFONT(SendMessageW(window, WM_GETFONT, WPARAM(0), LPARAM(0)).0 as *mut _);
            let old = SelectObject(dc, selected_font);
            let mut metrics = TEXTMETRICW::default();
            let _ = GetTextMetricsW(dc, &mut metrics);
            let pad = (metrics.tmHeight/2).max(1);
            let focused = GetFocus() == window;
            let open = SendMessageW(window, CB_GETDROPPEDSTATE, WPARAM(0), LPARAM(0)).0 != 0;
            let disabled = !IsWindowEnabled(window).as_bool();
            let mut cursor = POINT::default();
            let _ = GetCursorPos(&mut cursor);
            let _ = ScreenToClient(window, &mut cursor);
            let hot = cursor.x >= 0 && cursor.x < rect.right && cursor.y >= 0 && cursor.y < rect.bottom;
            fill(dc, &rect, if focused || open { ACCENT } else if hot { MUTED } else { BORDER });
            let _ = InflateRect(&mut rect, -1, -1);
            fill(dc, &rect, if open { BORDER } else { SURFACE });
            rect.left += pad;
            rect.right -= pad*3;
            let mut label = [0u16; 256];
            let index = SendMessageW(window, CB_GETCURSEL, WPARAM(0), LPARAM(0)).0;
            let count = if index >= 0 {
                SendMessageW(window, CB_GETLBTEXT, WPARAM(index as usize), LPARAM(label.as_mut_ptr() as isize)).0.max(0) as usize
            } else { 0 };
            SetBkMode(dc, TRANSPARENT);
            text(dc, selected_font, if disabled { MUTED } else { WHITE },
                &String::from_utf16_lossy(&label[..count.min(255)]), rect, DT_LEFT | DT_SINGLELINE | DT_VCENTER);
            rect.left = rect.right;
            rect.right += pad*3;
            text(dc, selected_font, if disabled { MUTED } else { WHITE }, "\u{2304}", rect, DT_CENTER | DT_SINGLELINE | DT_VCENTER);
            SelectObject(dc, old);
            let _ = EndPaint(window, &paint);
            return LRESULT(0);
        }
        WM_PAINT if GetDlgCtrlID(window) == 203 => {
            let mut paint = PAINTSTRUCT::default();
            let dc = BeginPaint(window, &mut paint);
            let mut rect = RECT::default();
            let _ = GetClientRect(window, &mut rect);
            fill(dc, &rect, BACKGROUND);
            let selected_font = HFONT(SendMessageW(window, WM_GETFONT, WPARAM(0), LPARAM(0)).0 as *mut _);
            let old = SelectObject(dc, selected_font);
            let mut metrics = TEXTMETRICW::default();
            let _ = GetTextMetricsW(dc, &mut metrics);
            let size = metrics.tmHeight.max(12);
            let pad = (size / 2).max(1);
            let state = SendMessageW(window, BM_GETSTATE, WPARAM(0), LPARAM(0)).0 as u32;
            let checked = SendMessageW(window, BM_GETCHECK, WPARAM(0), LPARAM(0)).0 == 1;
            let disabled = !IsWindowEnabled(window).as_bool();
            let box_rect = RECT { left: 1, top: (rect.bottom-size)/2, right: size+1, bottom: (rect.bottom+size)/2 };
            let mut cursor = POINT::default();
            let _ = GetCursorPos(&mut cursor);
            let _ = ScreenToClient(window, &mut cursor);
            let hot = cursor.x >= 0 && cursor.x < rect.right && cursor.y >= 0 && cursor.y < rect.bottom;
            fill(dc, &box_rect, if disabled { MUTED } else if state & BST_PUSHED != 0 { WHITE }
                else if checked || hot { ACCENT } else { BORDER });
            let mut inside = box_rect;
            let _ = InflateRect(&mut inside, -1, -1);
            if !checked { fill(dc, &inside, SURFACE); }
            SetBkMode(dc, TRANSPARENT);
            if checked { text(dc, selected_font, BACKGROUND, "\u{2713}", box_rect, DT_CENTER | DT_VCENTER | DT_SINGLELINE); }
            let mut label = [0u16; 128];
            let count = GetWindowTextW(window, &mut label) as usize;
            rect.left = size + pad;
            text(dc, selected_font, if disabled { MUTED } else if state & BST_PUSHED != 0 { ACCENT } else { WHITE },
                &String::from_utf16_lossy(&label[..count]), rect, DT_LEFT | DT_VCENTER | DT_SINGLELINE);
            if GetFocus() == window { let _ = DrawFocusRect(dc, &rect); }
            SelectObject(dc, old);
            let _ = EndPaint(window, &paint);
            return LRESULT(0);
        }
        WM_ERASEBKGND if [201, 203].contains(&GetDlgCtrlID(window)) => return LRESULT(1),
        WM_MOUSEMOVE => {
            let mut tracking = TRACKMOUSEEVENT { cbSize: std::mem::size_of::<TRACKMOUSEEVENT>() as u32,
                dwFlags: TME_LEAVE, hwndTrack: window, ..Default::default() };
            let _ = TrackMouseEvent(&mut tracking);
            let _ = InvalidateRect(window, None, false);
        }
        WM_MOUSELEAVE | WM_SETFOCUS | WM_KILLFOCUS | WM_ENABLE => { let _ = InvalidateRect(window, None, false); }
        WM_NCDESTROY => { let _ = RemoveWindowSubclass(window, Some(settings_button_proc), 1); }
        // Dialog navigation may try to change an owner-drawn button's type.
        BM_SETSTYLE => return LRESULT(0),
        _ => {}
    }
    DefSubclassProc(window, message, wparam, lparam)
}
unsafe fn settings_draw(app: &App, draw: &DRAWITEMSTRUCT) {
    let dc = draw.hDC;
    let rect = draw.rcItem;
    let selected = draw.itemState.0 & ODS_SELECTED.0 != 0;
    let disabled = draw.itemState.0 & ODS_DISABLED.0 != 0;
    let focus = draw.itemState.0 & ODS_FOCUS.0 != 0;
    let mut cursor = POINT::default();
    let _ = GetCursorPos(&mut cursor);
    let _ = ScreenToClient(draw.hwndItem, &mut cursor);
    let hover = cursor.x >= rect.left && cursor.x < rect.right && cursor.y >= rect.top && cursor.y < rect.bottom;
    let primary = draw.CtlID as usize == SAVE;
    let combo = draw.CtlID == 201;
    let bg = if disabled { SURFACE } else if combo {
        if selected { BORDER } else { SURFACE }
    } else if selected { if primary { COLORREF(0x00d0a060) } else { BORDER } }
    else if primary { if hover { COLORREF(0x00ffd098) } else { ACCENT } }
    else if hover { COLORREF(0x003e3229) } else { SURFACE };
    if combo { fill(dc, &rect, bg); } else {
        fill(dc, &rect, BACKGROUND);
        let brush = SelectObject(dc, GetStockObject(DC_BRUSH));
        let pen = SelectObject(dc, GetStockObject(DC_PEN));
        SetDCBrushColor(dc, bg);
        SetDCPenColor(dc, if primary { ACCENT } else { BORDER });
        let _ = RoundRect(dc, rect.left, rect.top, rect.right, rect.bottom, (GetDpiForWindow(draw.hwndItem)*8/96) as i32, (GetDpiForWindow(draw.hwndItem)*8/96) as i32);
        SelectObject(dc, pen);
        SelectObject(dc, brush);
    }
    let mut inner = rect;
    let pad = (GetDpiForWindow(draw.hwndItem) as i32 * 8 / 96).max(1);
    inner.left += pad;
    inner.right -= pad;
    let mut label = [0u16; 256];
    let count = if combo {
        if draw.itemID == u32::MAX { 0 } else {
            SendMessageW(draw.hwndItem, CB_GETLBTEXT, WPARAM(draw.itemID as usize), LPARAM(label.as_mut_ptr() as isize)).0.max(0) as usize
        }
    } else { GetWindowTextW(draw.hwndItem, &mut label) as usize };
    let color = if disabled { MUTED } else if primary { BACKGROUND }
        else if draw.CtlID as usize == DISCONNECT { COLORREF(0x00a9b8ff) } else { WHITE };
    SetBkMode(dc, TRANSPARENT);
    text(dc, app.dialog_font.get(), color, &String::from_utf16_lossy(&label[..count.min(255)]), inner,
        DT_SINGLELINE | DT_VCENTER | if combo { DT_LEFT } else { DT_CENTER });
    if focus {
        let mut focus_rect = rect;
        let _ = InflateRect(&mut focus_rect, -((pad/2).max(2)), -((pad/2).max(2)));
        SetTextColor(dc, WHITE);
        SetBkColor(dc, bg);
        let _ = DrawFocusRect(dc, &focus_rect);
    }
}
unsafe fn slider_draw(draw: &NMCUSTOMDRAW) -> LRESULT {
    if draw.dwDrawStage == CDDS_PREPAINT { return LRESULT(CDRF_NOTIFYITEMDRAW as isize); }
    if draw.dwDrawStage == CDDS_ITEMPREPAINT {
        match draw.dwItemSpec as u32 {
            TBCD_CHANNEL => { // channel
                fill(draw.hdc, &draw.rc, BORDER);
                return LRESULT(CDRF_SKIPDEFAULT as isize);
            }
            TBCD_THUMB => { // thumb; the native control retains pointer and keyboard behaviour
                let color = if draw.uItemState.0 & CDIS_DISABLED.0 != 0 { MUTED }
                    else if draw.uItemState.0 & (CDIS_SELECTED.0 | CDIS_HOT.0) != 0 { WHITE } else { ACCENT };
                let old_brush = SelectObject(draw.hdc, GetStockObject(DC_BRUSH));
                let old_pen = SelectObject(draw.hdc, GetStockObject(DC_PEN));
                SetDCBrushColor(draw.hdc, color);
                SetDCPenColor(draw.hdc, color);
                let radius = draw.rc.right-draw.rc.left;
                let _ = RoundRect(draw.hdc, draw.rc.left, draw.rc.top, draw.rc.right, draw.rc.bottom, radius, radius);
                SelectObject(draw.hdc, old_pen);
                SelectObject(draw.hdc, old_brush);
                return LRESULT(CDRF_SKIPDEFAULT as isize);
            }
            _ => {}
        }
    }
    LRESULT(CDRF_DODEFAULT as isize)
}

struct VisualStyles(HANDLE, usize);
impl VisualStyles {
    unsafe fn activate() -> Result<Self> {
        // The exact same manifest used by resource_stamp is available in ordinary
        // builds too. Explicit activation also works when a manifest is embedded.
        let path = settings::data_dir()?.join("visual-styles.manifest");
        std::fs::write(&path, include_bytes!("../app.manifest"))?;
        let source = wide(&path.to_string_lossy());
        let handle = CreateActCtxW(&ACTCTXW {
            cbSize: std::mem::size_of::<ACTCTXW>() as u32,
            dwFlags: 0, lpSource: PCWSTR(source.as_ptr()), ..Default::default()
        })?;
        let mut cookie = 0;
        if let Err(error) = ActivateActCtx(handle, &mut cookie) {
            ReleaseActCtx(handle);
            return Err(error.into());
        }
        Ok(Self(handle, cookie))
    }
}
impl Drop for VisualStyles {
    fn drop(&mut self) {
        unsafe {
            let _ = DeactivateActCtx(0, self.1);
            ReleaseActCtx(self.0);
        }
    }
}
