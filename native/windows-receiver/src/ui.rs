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
        System::{LibraryLoader::GetModuleHandleW, Threading::CreateMutexW},
        UI::{
            Controls::*, HiDpi::*, Input::KeyboardAndMouse::*, Shell::ShellExecuteW,
            WindowsAndMessaging::*,
        },
    },
};

const GEAR: usize = 101;
const SAVE: usize = 102;
const CANCEL: usize = 103;
const LOG: usize = 104;
const DISCONNECT: usize = 105;
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
    smoke: bool,
    opened: Instant,
}

impl App {
    fn new(state: Arc<AppState>, smoke: bool) -> Self {
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
                let _ = EnableWindow(self.parent.get(), false);
                let _ = ShowWindow(window, SW_SHOW);
            }
            Err(error) => self.message(&format!("Could not open settings: {error}")),
        }
    }
    unsafe fn settings_controls(&self, window: HWND) {
        let settings = self.state.settings.lock().unwrap().clone();
        let control =
            |class: PCWSTR, label: &str, style: WINDOW_STYLE, x, y, width, height, id: usize| {
                let label = wide(label);
                let result = CreateWindowExW(
                    WINDOW_EX_STYLE(0),
                    class,
                    PCWSTR(label.as_ptr()),
                    WS_CHILD | WS_VISIBLE | style,
                    self.scale(x),
                    self.scale(y),
                    self.scale(width),
                    self.scale(height),
                    window,
                    HMENU(id as *mut _),
                    HINSTANCE(GetModuleHandleW(None).unwrap_or_default().0),
                    None,
                );
                match result {
                    Ok(handle) => {
                        SendMessageW(
                            handle,
                            WM_SETFONT,
                            WPARAM(self.regular.get().0 as usize),
                            LPARAM(1),
                        );
                        handle
                    }
                    Err(error) => {
                        self.state
                            .log(format!("Could not create settings control: {error}"));
                        HWND::default()
                    }
                }
            };
        control(
            w!("STATIC"),
            "Cast quality target",
            WINDOW_STYLE(0),
            24,
            24,
            390,
            24,
            0,
        );
        let combo = control(
            w!("COMBOBOX"),
            "",
            WINDOW_STYLE(CBS_DROPDOWNLIST as u32 | WS_VSCROLL.0 | WS_TABSTOP.0),
            24,
            54,
            390,
            150,
            201,
        );
        for label in [
            "1080p - target 60 FPS (recommended)",
            "1080p - target 30 FPS",
            "720p - target 60 FPS",
        ] {
            let label = wide(label);
            SendMessageW(
                combo,
                CB_ADDSTRING,
                WPARAM(0),
                LPARAM(label.as_ptr() as isize),
            );
        }
        let selected = match settings.quality {
            Quality::FullHd60 => 0,
            Quality::FullHd30 => 1,
            Quality::Hd60 => 2,
        };
        SendMessageW(combo, CB_SETCURSEL, WPARAM(selected), LPARAM(0));
        self.quality.set(combo);
        control(
            w!("STATIC"),
            "Changes apply on the next connection. The phone chooses actual FPS.",
            WINDOW_STYLE(0),
            24,
            90,
            390,
            40,
            0,
        );
        control(
            w!("STATIC"),
            "Scroll speed",
            WINDOW_STYLE(0),
            24,
            142,
            390,
            24,
            0,
        );
        let speed = control(TRACKBAR_CLASSW, "", WS_TABSTOP, 24, 169, 390, 32, 202);
        SendMessageW(speed, TBM_SETRANGE, WPARAM(1), LPARAM(30 | (120 << 16)));
        SendMessageW(
            speed,
            TBM_SETPOS,
            WPARAM(1),
            LPARAM(settings.scroll_speed as isize),
        );
        self.speed.set(speed);
        let top = control(
            w!("BUTTON"),
            "Keep window on top",
            WINDOW_STYLE(BS_AUTOCHECKBOX as u32) | WS_TABSTOP,
            24,
            211,
            390,
            28,
            203,
        );
        SendMessageW(
            top,
            BM_SETCHECK,
            WPARAM(if settings.always_on_top {
                BST_CHECKED.0 as usize
            } else {
                BST_UNCHECKED.0 as usize
            }),
            LPARAM(0),
        );
        self.top.set(top);
        let information = control(
            w!("STATIC"),
            &self.connection_info(),
            WINDOW_STYLE(0),
            24,
            250,
            390,
            50,
            0,
        );
        self.information.set(information);
        control(
            w!("BUTTON"),
            "Diagnostics",
            WS_TABSTOP,
            24,
            316,
            110,
            30,
            LOG,
        );
        control(
            w!("BUTTON"),
            "Disconnect",
            WS_TABSTOP,
            146,
            316,
            110,
            30,
            DISCONNECT,
        );
        control(w!("BUTTON"), "Cancel", WS_TABSTOP, 222, 366, 90, 32, CANCEL);
        control(
            w!("BUTTON"),
            "Save",
            WINDOW_STYLE(BS_DEFPUSHBUTTON as u32) | WS_TABSTOP,
            324,
            366,
            90,
            32,
            SAVE,
        );
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
            let _ = DestroyWindow(window);
            let _ = SetForegroundWindow(self.parent.get());
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

pub fn run(state: Arc<AppState>, preview: bool, smoke: bool) -> Result<()> {
    unsafe {
        let _ = SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
        let mutex = CreateMutexW(None, false, w!("Local\\ADAPT Wireless DeXSingleInstance"))?;
        if GetLastError() == ERROR_ALREADY_EXISTS {
            if let Ok(window) = FindWindowW(MAIN_CLASS, None) {
                let _ = ShowWindow(window, SW_RESTORE);
                let _ = SetForegroundWindow(window);
            }
            CloseHandle(mutex)?;
            return Ok(());
        }
        let common = INITCOMMONCONTROLSEX {
            dwSize: std::mem::size_of::<INITCOMMONCONTROLSEX>() as u32,
            dwICC: ICC_BAR_CLASSES,
        };
        if !InitCommonControlsEx(&common).as_bool() {
            CloseHandle(mutex)?;
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
                CloseHandle(mutex)?;
                return Err(windows::core::Error::from_win32().into());
            }
        }
        let app = Box::new(App::new(state.clone(), smoke));
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
        let _ = ShowWindow(window, SW_SHOW);
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
            if !app.dialog.get().is_invalid()
                && IsDialogMessageW(app.dialog.get(), &message).as_bool()
            {
                continue;
            }
            let _ = TranslateMessage(&message);
            DispatchMessageW(&message);
        }
        state.running.store(false, Ordering::Release);
        if let Some(worker) = app.worker.borrow_mut().take() {
            if worker.join().is_err() {
                state.log("Receiver worker stopped unexpectedly");
            }
        }
        for font in [
            app.regular.get(),
            app.title.get(),
            app.small.get(),
            app.icon.get(),
        ] {
            if !font.is_invalid() {
                let _ = DeleteObject(font);
            }
        }
        CloseHandle(mutex)?;
    }
    Ok(())
}

unsafe fn app_pointer(window: HWND, message: u32, lparam: LPARAM) -> *const App {
    if message == WM_NCCREATE {
        let create = &*(lparam.0 as *const CREATESTRUCTW);
        SetWindowLongPtrW(window, GWLP_USERDATA, create.lpCreateParams as isize);
    }
    GetWindowLongPtrW(window, GWLP_USERDATA) as *const App
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
    match message {
        WM_CREATE => {
            app.settings_controls(window);
            LRESULT(0)
        }
        WM_ERASEBKGND => {
            let mut rect = RECT::default();
            let _ = GetClientRect(window, &mut rect);
            FillRect(
                HDC(wparam.0 as *mut _),
                &rect,
                GetSysColorBrush(COLOR_WINDOW),
            );
            LRESULT(1)
        }
        WM_CTLCOLORSTATIC => {
            let dc = HDC(wparam.0 as *mut _);
            SetBkColor(dc, COLORREF(GetSysColor(COLOR_WINDOW)));
            SetTextColor(dc, COLORREF(GetSysColor(COLOR_WINDOWTEXT)));
            LRESULT(GetSysColorBrush(COLOR_WINDOW).0 as isize)
        }
        WM_COMMAND => {
            match wparam.0 & 0xffff {
                SAVE => match app.apply_settings() {
                    Ok(()) => app.close_settings(),
                    Err(error) => app.message(&format!("Could not save settings: {error:#}")),
                },
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
