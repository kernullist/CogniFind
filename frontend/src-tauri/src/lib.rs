use std::sync::Mutex;
use tauri::{
    Manager,
    image::Image,
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
use tauri_plugin_global_shortcut::ShortcutState;
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

// Hard single-instance enforcement via a named mutex, checked at the very start
// of run() -- before any Tauri/window/tray/backend initialization -- so a second
// launch cannot create a stray tray icon or backend. A named auto-reset event
// lets the second launch ask the running instance to focus its window.
#[cfg(windows)]
mod single_instance {
    use std::iter::once;
    use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, ERROR_ALREADY_EXISTS, HANDLE};
    use windows_sys::Win32::System::Threading::{
        CreateEventW, CreateMutexW, OpenEventW, SetEvent, WaitForSingleObject, EVENT_MODIFY_STATE,
        INFINITE,
    };

    const MUTEX_NAME: &str = "CogniFind_SingleInstance_Mutex_v1";
    const EVENT_NAME: &str = "CogniFind_Activate_Event_v1";

    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(once(0)).collect()
    }

    /// Become the single instance. Returns the owned mutex handle on success
    /// (keep it alive for the process lifetime). Returns None if another instance
    /// already exists -- after signalling it to activate its window.
    pub fn acquire() -> Option<HANDLE> {
        let name = wide(MUTEX_NAME);
        let handle = unsafe { CreateMutexW(std::ptr::null(), 0, name.as_ptr()) };
        let already = unsafe { GetLastError() } == ERROR_ALREADY_EXISTS;
        if handle.is_null() {
            // Could not create the mutex; do not block a legitimate launch.
            return Some(std::ptr::null_mut());
        }
        if already {
            signal_activate();
            unsafe { CloseHandle(handle) };
            return None;
        }
        Some(handle)
    }

    fn signal_activate() {
        let name = wide(EVENT_NAME);
        let ev = unsafe { OpenEventW(EVENT_MODIFY_STATE, 0, name.as_ptr()) };
        if !ev.is_null() {
            unsafe {
                SetEvent(ev);
                CloseHandle(ev);
            }
        }
    }

    /// Spawns a thread that runs `on_activate` whenever a second launch signals.
    pub fn spawn_activation_listener<F: Fn() + Send + 'static>(on_activate: F) {
        std::thread::spawn(move || {
            let name = wide(EVENT_NAME);
            // Auto-reset, initially non-signalled.
            let ev = unsafe { CreateEventW(std::ptr::null(), 0, 0, name.as_ptr()) };
            if ev.is_null() {
                return;
            }
            loop {
                // WAIT_OBJECT_0 == 0 means signalled; anything else -> stop.
                if unsafe { WaitForSingleObject(ev, INFINITE) } != 0 {
                    break;
                }
                on_activate();
            }
        });
    }
}

struct AppState {
    // Sidecar process (production: bundled cognifind-backend.exe).
    backend_child: Mutex<Option<CommandChild>>,
    // Fallback process (dev: locally launched `python api.py`).
    backend_local: Mutex<Option<std::process::Child>>,
}

fn start_backend_sidecar(app: &tauri::AppHandle, state: &AppState) {
    let shell = app.shell();
    let child_result = shell.sidecar("cognifind-backend");

    match child_result {
        Ok(cmd) => {
            match cmd.spawn() {
                Ok((_rx, child)) => {
                    log::info!("Python backend sidecar started");
                    let mut guard = state.backend_child.lock().unwrap();
                    *guard = Some(child);
                }
                Err(e) => {
                    log::error!("Failed to spawn sidecar: {}", e);
                }
            }
        }
        Err(e) => {
            log::error!("Sidecar 'cognifind-backend' not found: {}", e);
            log::info!("Falling back to local python api.py...");
            start_backend_local_python(state);
        }
    }
}

fn start_backend_local_python(state: &AppState) {
    let project_root = std::env::current_exe()
        .unwrap()
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .to_path_buf();

    let api_script = project_root.join("api.py");

    if !api_script.exists() {
        log::error!("api.py not found at {:?}", api_script);
        return;
    }

    let child = std::process::Command::new("python")
        .arg(api_script.to_str().unwrap())
        .current_dir(&project_root)
        .stdout(std::process::Stdio::inherit())
        .stderr(std::process::Stdio::inherit())
        .spawn();

    match child {
        Ok(process) => {
            log::info!("Python backend started via local python (PID: {})", process.id());
            // Store the child so it is killed on app exit instead of leaking.
            let mut guard = state.backend_local.lock().unwrap();
            *guard = Some(process);
        }
        Err(e) => {
            log::error!("Failed to start Python backend: {}", e);
        }
    }
}

fn stop_backend_sidecar(state: &AppState) {
    if let Some(child) = state.backend_child.lock().unwrap().take() {
        log::info!("Stopping Python backend sidecar");
        let _ = child.kill();
    }
    if let Some(mut child) = state.backend_local.lock().unwrap().take() {
        log::info!("Stopping local python backend");
        let _ = child.kill();
    }
}


#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Hard single-instance gate: if another instance already holds the named
    // mutex, this call signals it to focus and returns None -- we exit here,
    // before Tauri creates any window/tray/backend.
    #[cfg(windows)]
    let _instance_mutex = match single_instance::acquire() {
        Some(handle) => handle,
        None => return,
    };

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_os::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(AppState {
            backend_child: Mutex::new(None),
            backend_local: Mutex::new(None),
        })
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            let app_handle = app.handle().clone();
            let state = app.state::<AppState>();
            start_backend_sidecar(&app_handle, &state);

            // When a second launch is blocked by the mutex gate, it signals this
            // running instance to bring its window to the front.
            #[cfg(windows)]
            {
                let activate_handle = app.handle().clone();
                single_instance::spawn_activation_listener(move || {
                    let h = activate_handle.clone();
                    let _ = activate_handle.run_on_main_thread(move || {
                        if let Some(window) = h.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.unminimize();
                            let _ = window.set_focus();
                        }
                    });
                });
            }

            // Register the global shortcut exactly once, non-fatally: if the
            // hotkey is already taken (e.g. by another app or a leftover
            // instance), log and continue instead of aborting setup, which
            // would close the app.
            let shortcut_plugin = tauri_plugin_global_shortcut::Builder::new()
                .with_shortcuts(["Ctrl+Alt+F"])
                .unwrap()
                .with_handler(|app, _shortcut, event| {
                    // Only one shortcut is registered, so any Pressed event is ours.
                    if event.state == ShortcutState::Pressed {
                        if let Some(window) = app.get_webview_window("main") {
                            toggle_window(&window);
                        }
                    }
                })
                .build();
            if let Err(e) = app.handle().plugin(shortcut_plugin) {
                log::error!("Failed to register global shortcut Ctrl+Alt+F: {}", e);
            }

            let quit_item = MenuItem::with_id(app, "quit", "Exit", true, None::<&str>)?;
            let scan_item = MenuItem::with_id(app, "scan", "Re-index Now", true, None::<&str>)?;
            let search_item = MenuItem::with_id(app, "search", "Search Documents", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&search_item, &scan_item, &quit_item])?;

            // Decode the PNG into RGBA. (Image::new expects raw RGBA pixels, so
            // passing PNG bytes there produced a broken/garbage tray icon.)
            let tray_icon = Image::from_bytes(include_bytes!("../icons/icon.png"))?;
            let _tray = TrayIconBuilder::with_id("tray")
                .icon(tray_icon)
                .tooltip("CogniFind - Local Semantic Search")
                .menu(&menu)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "quit" => {
                        let state = app.state::<AppState>();
                        stop_backend_sidecar(&state);
                        app.exit(0);
                    }
                    "search" => {
                        if let Some(window) = app.get_webview_window("main") {
                            toggle_window(&window);
                        }
                    }
                    "scan" => {
                        // Off the UI thread: a blocking HTTP call here would
                        // freeze the menu/UI if the backend is slow.
                        std::thread::spawn(|| {
                            let client = reqwest::blocking::Client::new();
                            let _ = client.post("http://127.0.0.1:8765/api/index/scan").send();
                        });
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let tauri::tray::TrayIconEvent::Click { .. } = event {
                        if let Some(window) = tray.app_handle().get_webview_window("main") {
                            toggle_window(&window);
                        }
                    }
                })
                .build(app)?;

            Ok(())
        })
        .on_window_event(|window, event| {
            // On close (top-right X), ask whether to minimize to the tray.
            // Yes -> hide to tray, No -> stop the backend and quit.
            // Focus-out no longer auto-hides; the window behaves like a normal one.
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let win = window.clone();
                window
                    .app_handle()
                    .dialog()
                    .message("CogniFind를 트레이로 보낼까요?\n'아니오'를 누르면 완전히 종료됩니다.")
                    .title("CogniFind")
                    .buttons(MessageDialogButtons::YesNo)
                    .show(move |send_to_tray| {
                        if send_to_tray {
                            let _ = win.hide();
                        }
                        else {
                            let state = win.app_handle().state::<AppState>();
                            stop_backend_sidecar(&state);
                            win.app_handle().exit(0);
                        }
                    });
            }
        })
        .invoke_handler(tauri::generate_handler![toggle_search_window, pick_folder])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            // Ensure the Python backend is terminated on every exit path, not
            // just the tray "Exit" menu, so it is never left orphaned.
            if let tauri::RunEvent::ExitRequested { .. } = event {
                let state = app_handle.state::<AppState>();
                stop_backend_sidecar(&state);
            }
        });
}

#[tauri::command]
fn toggle_search_window(app: tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        toggle_window(&window);
    }
}

// Opens a native folder picker and returns the chosen path (or None if cancelled).
// async so it runs off the main thread, where blocking_pick_folder is safe.
#[tauri::command]
async fn pick_folder(app: tauri::AppHandle) -> Option<String> {
    app.dialog()
        .file()
        .blocking_pick_folder()
        .map(|p| p.to_string())
}

fn toggle_window(window: &tauri::WebviewWindow) {
    if window.is_visible().unwrap_or(false) {
        let _ = window.hide();
    } else {
        let _ = window.center();
        let _ = window.show();
        let _ = window.set_focus();
    }
}
