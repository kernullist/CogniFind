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

const TRAY_ICON: Image<'_> = Image::new(
    include_bytes!("../icons/icon.png"),
    256,
    256,
);

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
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

            std::thread::sleep(std::time::Duration::from_secs(2));

            // Register the global shortcut exactly once, non-fatally: if the
            // hotkey is already taken (e.g. by another app or a leftover
            // instance), log and continue instead of aborting setup, which
            // would close the app.
            let shortcut_plugin = tauri_plugin_global_shortcut::Builder::new()
                .with_shortcuts(["Alt+Super+F"])
                .unwrap()
                .with_handler(|app, shortcut, event| {
                    if event.state == ShortcutState::Pressed {
                        let shortcut_str = format!("{:?}", shortcut);
                        if shortcut_str.contains("'F'") || shortcut_str.contains("KeyF") {
                            if let Some(window) = app.get_webview_window("main") {
                                toggle_window(&window);
                            }
                        }
                    }
                })
                .build();
            if let Err(e) = app.handle().plugin(shortcut_plugin) {
                log::error!("Failed to register global shortcut Alt+Super+F: {}", e);
            }

            let quit_item = MenuItem::with_id(app, "quit", "Exit", true, None::<&str>)?;
            let scan_item = MenuItem::with_id(app, "scan", "Re-index Now", true, None::<&str>)?;
            let search_item = MenuItem::with_id(app, "search", "Search Documents", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&search_item, &scan_item, &quit_item])?;

            let _tray = TrayIconBuilder::with_id("tray")
                .icon(TRAY_ICON)
                .tooltip("ContextFinder - Local Semantic Search")
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
                        let client = reqwest::blocking::Client::new();
                        let _ = client.post("http://127.0.0.1:8765/api/index/scan").send();
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
        .invoke_handler(tauri::generate_handler![toggle_search_window])
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

fn toggle_window(window: &tauri::WebviewWindow) {
    if window.is_visible().unwrap_or(false) {
        let _ = window.hide();
    } else {
        let _ = window.center();
        let _ = window.show();
        let _ = window.set_focus();
    }
}
