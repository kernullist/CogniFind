use std::sync::Mutex;
use tauri::{
    Manager,
    image::Image,
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
};
use tauri_plugin_global_shortcut::ShortcutState;
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

struct AppState {
    backend_child: Mutex<Option<CommandChild>>,
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

fn start_backend_local_python(_state: &AppState) {
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
        }
        Err(e) => {
            log::error!("Failed to start Python backend: {}", e);
        }
    }
}

fn stop_backend_sidecar(state: &AppState) {
    let mut guard = state.backend_child.lock().unwrap();
    if let Some(child) = guard.take() {
        log::info!("Stopping Python backend sidecar");
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
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .manage(AppState {
            backend_child: Mutex::new(None),
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

            let app_handle2 = app.handle().clone();
            app.handle().plugin(
                tauri_plugin_global_shortcut::Builder::new()
                    .with_shortcuts(["Alt+Super+F"])
                    .unwrap()
                    .with_handler(move |_app, shortcut, event| {
                        if event.state == ShortcutState::Pressed {
                            let shortcut_str = format!("{:?}", shortcut);
                            if shortcut_str.contains("'F'") || shortcut_str.contains("KeyF") {
                                if let Some(window) = app_handle2.get_webview_window("main") {
                                    toggle_window(&window);
                                }
                            }
                        }
                    })
                    .build(),
            )?;

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
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                window.hide().unwrap();
            }
            if let tauri::WindowEvent::Focused(false) = event {
                window.hide().unwrap();
            }
        })
        .invoke_handler(tauri::generate_handler![toggle_search_window])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
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
