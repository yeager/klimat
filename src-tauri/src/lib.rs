use serde_json::Value;
use std::{
    collections::HashMap,
    io::Write,
    path::PathBuf,
    process::{Command, Stdio},
    sync::{Mutex, OnceLock},
};
use tauri::path::BaseDirectory;
use tauri::{
    Manager, PhysicalPosition, Rect, WebviewWindow,
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
};

static SECRET_PROFILES: OnceLock<Mutex<HashMap<String, Value>>> = OnceLock::new();
static ADAPTER_DIR: OnceLock<PathBuf> = OnceLock::new();
static ADAPTER_BINARY: OnceLock<Option<PathBuf>> = OnceLock::new();

#[tauri::command]
fn climate_request(payload: Value) -> Result<Value, String> {
    let profiles = payload
        .get("profiles")
        .cloned()
        .unwrap_or(Value::Array(vec![]));
    let profile_id = payload
        .get("id")
        .and_then(Value::as_str)
        .map(str::to_string);
    let mut safe_payload = payload.clone();
    if let (Some(profile_id), Some(kind)) = (
        profile_id.clone(),
        payload.get("action").and_then(Value::as_str),
    ) {
        if kind == "discover" {
            let secret = serde_json::json!({"username": payload.get("username"), "password": payload.get("password"), "token": payload.get("token"), "key": payload.get("key")});
            let mut secrets = SECRET_PROFILES
                .get_or_init(Default::default)
                .lock()
                .map_err(|e| e.to_string())?;
            secrets.insert(profile_id.clone(), secret);
        }
    }
    if let Some(object) = safe_payload.as_object_mut() {
        let mut all = profiles.as_array().cloned().unwrap_or_default();
        let active = payload
            .get("profileId")
            .and_then(Value::as_str)
            .or(profile_id.as_deref());
        if let Some(active) = active {
            let secrets = SECRET_PROFILES
                .get_or_init(Default::default)
                .lock()
                .map_err(|e| e.to_string())?;
            let stored = secrets.get(active).cloned().or_else(|| {
                keyring::Entry::new("Klimat", active)
                    .ok()?
                    .get_password()
                    .ok()
                    .and_then(|text| serde_json::from_str(&text).ok())
            });
            if let Some(secret) = stored {
                if let Some(profile) = all
                    .iter_mut()
                    .find(|item| item.get("id").and_then(Value::as_str) == Some(active))
                {
                    if let (Some(p), Some(s)) = (profile.as_object_mut(), secret.as_object()) {
                        for (k, v) in s {
                            p.insert(k.clone(), v.clone());
                        }
                    }
                }
            }
            if let Ok(entry) = keyring::Entry::new("Klimat", &format!("{active}-devices")) {
                if let Ok(text) = entry.get_password() {
                    if let Ok(value) = serde_json::from_str::<Value>(&text) {
                        if let Some(profile) = all
                            .iter_mut()
                            .find(|item| item.get("id").and_then(Value::as_str) == Some(active))
                        {
                            if let Some(map) = profile.as_object_mut() {
                                map.insert("mideaDevices".into(), value);
                            }
                        }
                    }
                }
            }
        }
        if payload.get("action").and_then(Value::as_str) == Some("discover") {
            if let Some(profile_id) = payload.get("id").and_then(Value::as_str) {
                if let Some(secret) = SECRET_PROFILES
                    .get_or_init(Default::default)
                    .lock()
                    .map_err(|e| e.to_string())?
                    .get(profile_id)
                {
                    if let Some(secret_map) = secret.as_object() {
                        for (key, value) in secret_map {
                            if let Some(value) = value.as_str() {
                                if !value.is_empty() {
                                    object.insert(key.clone(), Value::String(value.to_string()));
                                }
                            }
                        }
                    }
                }
            }
        }
        object.insert("profiles".into(), Value::Array(all));
    }
    let mut command = if let Some(binary) = ADAPTER_BINARY.get().and_then(|path| path.as_ref()) {
        Command::new(binary)
    } else {
        let python = if cfg!(target_os = "windows") {
            "python"
        } else {
            "python3"
        };
        let adapter_dir = ADAPTER_DIR
            .get()
            .cloned()
            .or_else(|| std::env::var_os("KLIMAT_ADAPTER_PATH").map(PathBuf::from))
            .or_else(|| {
                PathBuf::from("src-tauri/python")
                    .is_dir()
                    .then(|| PathBuf::from("src-tauri/python"))
            })
            .ok_or("Hittar inte Klimats adapterfiler.")?;
        let mut command = Command::new(python);
        command.arg("-c").arg("import sys,json,asyncio; sys.path.insert(0, sys.argv[1]); from adapters import handle; print(json.dumps(asyncio.run(handle(json.load(sys.stdin)))))").arg(adapter_dir);
        command
    };
    let mut child = command
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn()
        .map_err(|error| format!("Kunde inte starta adapterbryggan: {error}. Installera Python 3 och kör pnpm setup:adapters."))?;
    child
        .stdin
        .take()
        .ok_or("Kunde inte öppna adapterbryggans indata.")?
        .write_all(safe_payload.to_string().as_bytes())
        .map_err(|error| error.to_string())?;
    let output = child
        .wait_with_output()
        .map_err(|error| error.to_string())?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_string());
    }
    let mut response: Value = serde_json::from_slice(&output.stdout)
        .map_err(|error| format!("Ogiltigt svar från adapterbryggan: {error}"))?;
    if payload.get("action").and_then(Value::as_str) == Some("discover") {
        if let Some(profile_id) = payload.get("id").and_then(Value::as_str) {
            if let Some(secret) = SECRET_PROFILES
                .get_or_init(Default::default)
                .lock()
                .map_err(|e| e.to_string())?
                .get(profile_id)
            {
                keyring::Entry::new("Klimat", profile_id).and_then(|entry| entry.set_password(&secret.to_string())).map_err(|error| format!("Anslutningen lyckades men uppgifterna kunde inte sparas i systemets nyckelring: {error}"))?;
            }
            if let Some(devices) = response
                .get("profile")
                .and_then(|value| value.get("mideaDevices"))
            {
                let entry = keyring::Entry::new("Klimat", &format!("{profile_id}-devices"))
                    .map_err(|error| error.to_string())?;
                entry
                    .set_password(&devices.to_string())
                    .map_err(|error| error.to_string())?;
            }
            if let Some(items) = response.get_mut("devices").and_then(Value::as_array_mut) {
                for item in items {
                    if let Some(object) = item.as_object_mut() {
                        object.insert("profileId".into(), Value::String(profile_id.into()));
                    }
                }
            }
        }
    }
    Ok(response)
}

#[tauri::command]
fn hide_on_close(window: WebviewWindow) {
    let _ = window.hide();
}

fn show_popup(window: &WebviewWindow, anchor: Option<(PhysicalPosition<f64>, Rect)>) {
    let monitor = match anchor {
        Some((position, _)) => window
            .monitor_from_point(position.x, position.y)
            .ok()
            .flatten(),
        None => None,
    }
    .or_else(|| window.primary_monitor().ok().flatten());
    if let Some(monitor) = monitor {
        let area = monitor.work_area();
        let size = window
            .outer_size()
            .unwrap_or(tauri::PhysicalSize::new(360, 470));
        let left = area.position.x as f64;
        let top = area.position.y as f64;
        let right = left + area.size.width as f64;
        let bottom = top + area.size.height as f64;
        let (x, y) = if let Some((position, rect)) = anchor {
            let rect_position = rect.position.to_physical::<f64>(monitor.scale_factor());
            let rect_size = rect.size.to_physical::<f64>(monitor.scale_factor());
            let icon_width = rect_size.width;
            let icon_height = rect_size.height;
            let anchor_x = if icon_width > 0.0 {
                rect_position.x + icon_width / 2.0
            } else {
                position.x
            };
            let anchor_y = if icon_height > 0.0 {
                rect_position.y + icon_height / 2.0
            } else {
                position.y
            };
            let x = (anchor_x - size.width as f64 / 2.0)
                .clamp(left, (right - size.width as f64).max(left));
            let y = if anchor_y > top + area.size.height as f64 / 2.0 {
                anchor_y - size.height as f64 - 8.0
            } else {
                anchor_y + icon_height / 2.0 + 8.0
            }
            .clamp(top, (bottom - size.height as f64).max(top));
            (x, y)
        } else {
            (
                right - size.width as f64 - 12.0,
                bottom - size.height as f64 - 12.0,
            )
        };
        let _ = window.set_position(PhysicalPosition::new(x.round() as i32, y.round() as i32));
    }
    let _ = window.show();
    let _ = window.set_focus();
}

fn toggle_popup(window: &WebviewWindow, position: PhysicalPosition<f64>, rect: Rect) {
    if window.is_visible().unwrap_or(false) {
        let _ = window.hide();
    } else {
        show_popup(window, Some((position, rect)));
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![hide_on_close, climate_request])
        .setup(|app| {
            let resource_dir = app.path().resolve("python", BaseDirectory::Resource)?;
            let adapter_file = resource_dir.join("adapters.py");
            let resolved_adapter_dir = if adapter_file.is_file() {
                resource_dir
            } else {
                PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("python")
            };
            let _ = ADAPTER_DIR.set(resolved_adapter_dir);
            let sidecar_name = if cfg!(target_os = "windows") {
                "python/dist/klimat-adapter.exe"
            } else {
                "python/dist/klimat-adapter"
            };
            let sidecar = app.path().resolve(sidecar_name, BaseDirectory::Resource)?;
            let _ = ADAPTER_BINARY.set(sidecar.is_file().then_some(sidecar));
            let open = MenuItem::with_id(app, "open", "Visa klimatkontroller", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Avsluta", true, None::<&str>)?;
            let separator = PredefinedMenuItem::separator(app)?;
            let menu = Menu::with_items(app, &[&open, &separator, &quit])?;
            let tray_icon = tauri::image::Image::from_bytes(include_bytes!("../icons/tray.png"))?;
            TrayIconBuilder::new()
                .icon(tray_icon)
                .icon_as_template(cfg!(target_os = "macos"))
                .menu(&menu)
                .tooltip("Klimat")
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "open" => {
                        if let Some(window) = app.get_webview_window("main") {
                            show_popup(&window, None);
                        }
                    }
                    "quit" => app.exit(0),
                    _ => (),
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        position,
                        rect,
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        if let Some(window) = tray.app_handle().get_webview_window("main") {
                            toggle_popup(&window, position, rect);
                        }
                    }
                })
                .build(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            } else if let tauri::WindowEvent::Focused(false) = event {
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
