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
    Emitter, Manager, PhysicalPosition, Rect, WebviewWindow,
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
};

mod gree;

static SECRET_PROFILES: OnceLock<Mutex<HashMap<String, Value>>> = OnceLock::new();
static ADAPTER_DIR: OnceLock<PathBuf> = OnceLock::new();
static ADAPTER_BINARY: OnceLock<Option<PathBuf>> = OnceLock::new();

#[cfg(target_os = "macos")]
fn set_macos_tray_activation_policy() {
    use objc2::MainThreadMarker;
    use objc2_app_kit::{NSApplication, NSApplicationActivationPolicy};

    if let Some(main_thread) = MainThreadMarker::new() {
        let _ = NSApplication::sharedApplication(main_thread)
            .setActivationPolicy(NSApplicationActivationPolicy::Accessory);
    }
}

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct AboutInfo {
    version: String,
    project_license: String,
    third_party_licenses: String,
}

#[tauri::command]
fn about_info(app: tauri::AppHandle) -> Result<AboutInfo, String> {
    let licenses_path = app
        .path()
        .resolve(
            "python/dist/THIRD_PARTY_LICENSES.txt",
            BaseDirectory::Resource,
        )
        .map_err(|error| error.to_string())?;
    let third_party_licenses = std::fs::read_to_string(licenses_path)
        .map_err(|error| format!("Kunde inte läsa tredjepartslicenserna: {error}"))?;
    Ok(AboutInfo {
        version: env!("CARGO_PKG_VERSION").to_owned(),
        project_license: include_str!("../../LICENSE").to_owned(),
        third_party_licenses,
    })
}

#[tauri::command]
async fn climate_request(payload: Value) -> Result<Value, String> {
    if payload.get("action").and_then(Value::as_str) == Some("forget") {
        return tauri::async_runtime::spawn_blocking(move || forget_device_blocking(payload))
            .await
            .map_err(|error| error.to_string())?;
    }
    if payload.get("brand").and_then(Value::as_str) == Some("gree") {
        return gree::request(&payload).await;
    }
    tauri::async_runtime::spawn_blocking(move || climate_request_blocking(payload))
        .await
        .map_err(|error| error.to_string())?
}

fn forget_device_blocking(payload: Value) -> Result<Value, String> {
    let profile_id = payload
        .get("profileId")
        .and_then(Value::as_str)
        .ok_or("Anslutningsprofilen saknas.")?;
    let device_id = payload
        .pointer("/device/id")
        .map(|value| {
            value
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| value.to_string())
        })
        .ok_or("Enheten saknas.")?;
    let brand = payload
        .get("brand")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let storage_id = if brand == "gree" {
        format!("{profile_id}-gree-devices")
    } else {
        format!("{profile_id}-devices")
    };
    let entry = keyring::Entry::new("Klimat", &storage_id).map_err(|error| error.to_string())?;
    match entry.get_password() {
        Ok(text) => {
            let mut saved: Vec<Value> = serde_json::from_str(&text)
                .map_err(|error| format!("Kunde inte läsa enhetens sparade anslutning: {error}"))?;
            saved.retain(|item| {
                item.get("id")
                    .map(|value| {
                        value
                            .as_str()
                            .map(str::to_owned)
                            .unwrap_or_else(|| value.to_string())
                    })
                    .as_deref()
                    != Some(device_id.as_str())
            });
            let text = serde_json::to_string(&saved).map_err(|error| error.to_string())?;
            entry.set_password(&text).map_err(|error| {
                format!("Kunde inte uppdatera enhetens data i nyckelringen: {error}")
            })?;
        }
        Err(keyring::Error::NoEntry) => {}
        Err(error) => return Err(format!("Kunde inte läsa systemets nyckelring: {error}")),
    }

    if payload
        .get("deleteProfile")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        SECRET_PROFILES
            .get_or_init(Default::default)
            .lock()
            .map_err(|error| error.to_string())?
            .remove(profile_id);
        for key in [
            profile_id.to_owned(),
            format!("{profile_id}-devices"),
            format!("{profile_id}-gree-devices"),
        ] {
            let entry = keyring::Entry::new("Klimat", &key).map_err(|error| error.to_string())?;
            match entry.delete_credential() {
                Ok(()) | Err(keyring::Error::NoEntry) => {}
                Err(error) => {
                    return Err(format!("Kunde inte rensa systemets nyckelring: {error}"));
                }
            }
        }
    }
    Ok(Value::Null)
}

fn climate_request_blocking(payload: Value) -> Result<Value, String> {
    let profiles = payload
        .get("profiles")
        .cloned()
        .unwrap_or(Value::Array(vec![]));
    let profile_id = payload
        .get("id")
        .and_then(Value::as_str)
        .map(str::to_string);
    let mut safe_payload = payload.clone();
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
    let mut response: Value = serde_json::from_slice(&output.stdout).map_err(|error| {
        let stderr = String::from_utf8_lossy(&output.stderr);
        if !output.status.success() {
            format!(
                "Adapterbryggan avslutades med {}: {}",
                output.status,
                stderr.trim()
            )
        } else {
            format!(
                "Ogiltigt svar från adapterbryggan: {error}. {}",
                stderr.trim()
            )
        }
    })?;
    if !output.status.success() {
        return Err(response
            .get("error")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .unwrap_or_else(|| String::from_utf8_lossy(&output.stderr).trim().to_string()));
    }
    let has_discovered_devices = response
        .get("devices")
        .and_then(Value::as_array)
        .is_some_and(|devices| !devices.is_empty());
    let is_discovery = payload.get("action").and_then(Value::as_str) == Some("discover");
    let active_profile = payload
        .get("profileId")
        .and_then(Value::as_str)
        .or_else(|| payload.get("id").and_then(Value::as_str));
    let panasonic_token = response
        .get("profile")
        .and_then(|value| value.get("panasonicToken"))
        .and_then(Value::as_str)
        .map(str::to_string);
    let should_store_panasonic_token = !is_discovery || has_discovered_devices;
    if let (Some(profile_id), Some(token)) = (
        active_profile.filter(|_| should_store_panasonic_token),
        panasonic_token,
    ) {
        let mut secrets = SECRET_PROFILES
            .get_or_init(Default::default)
            .lock()
            .map_err(|e| e.to_string())?;
        let mut secret = secrets
            .get(profile_id)
            .cloned()
            .or_else(|| {
                keyring::Entry::new("Klimat", profile_id)
                    .ok()?
                    .get_password()
                    .ok()
                    .and_then(|text| serde_json::from_str(&text).ok())
            })
            .unwrap_or_else(|| {
                if is_discovery && has_discovered_devices {
                    serde_json::json!({
                        "username": payload.get("username"),
                        "password": payload.get("password"),
                        "token": payload.get("token"),
                        "key": payload.get("key"),
                    })
                } else {
                    serde_json::json!({})
                }
            });
        if let Some(object) = secret.as_object_mut() {
            object.insert("panasonicToken".into(), Value::String(token));
        }
        keyring::Entry::new("Klimat", profile_id)
            .and_then(|entry| entry.set_password(&secret.to_string()))
            .map_err(|error| {
                format!("Kunde inte spara Panasonic-sessionen i systemets nyckelring: {error}")
            })?;
        secrets.insert(profile_id.to_string(), secret);
    }
    if let Some(profile) = response.get_mut("profile").and_then(Value::as_object_mut) {
        profile.remove("panasonicToken");
    }
    if is_discovery && has_discovered_devices {
        if let Some(profile_id) = payload.get("id").and_then(Value::as_str) {
            let secret = SECRET_PROFILES
                .get_or_init(Default::default)
                .lock()
                .map_err(|e| e.to_string())?
                .get(profile_id)
                .cloned()
                .unwrap_or_else(|| {
                    serde_json::json!({
                        "username": payload.get("username"),
                        "password": payload.get("password"),
                        "token": payload.get("token"),
                        "key": payload.get("key"),
                    })
                });
            keyring::Entry::new("Klimat", profile_id)
                .and_then(|entry| entry.set_password(&secret.to_string()))
                .map_err(|error| format!("Anslutningen lyckades men uppgifterna kunde inte sparas i systemets nyckelring: {error}"))?;
            SECRET_PROFILES
                .get_or_init(Default::default)
                .lock()
                .map_err(|error| error.to_string())?
                .insert(profile_id.to_owned(), secret);
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
    } else if is_discovery {
        if let Some(profile_id) = profile_id {
            SECRET_PROFILES
                .get_or_init(Default::default)
                .lock()
                .map_err(|error| error.to_string())?
                .remove(&profile_id);
        }
    }
    if let Some(profile) = response.get_mut("profile").and_then(Value::as_object_mut) {
        profile.remove("mideaDevices");
    }
    Ok(response)
}

#[tauri::command]
fn hide_on_close(window: WebviewWindow) {
    let _ = window.hide();
}

fn tray_menu_labels(language: &str) -> (&'static str, &'static str, &'static str) {
    match language.split(['-', '_']).next().unwrap_or("en") {
        "sv" => ("Visa Klimat", "Om Klimat", "Avsluta"),
        "de" => ("Klimat anzeigen", "Über Klimat", "Beenden"),
        "fr" => ("Afficher Klimat", "À propos de Klimat", "Quitter"),
        "es" => ("Mostrar Klimat", "Acerca de Klimat", "Salir"),
        "it" => ("Mostra Klimat", "Informazioni su Klimat", "Esci"),
        "pt" => ("Mostrar Klimat", "Sobre Klimat", "Sair"),
        "nl" => ("Klimat tonen", "Over Klimat", "Afsluiten"),
        "da" => ("Vis Klimat", "Om Klimat", "Afslut"),
        "nb" => ("Vis Klimat", "Om Klimat", "Avslutt"),
        "fi" => ("Näytä Klimat", "Tietoja Klimatista", "Lopeta"),
        "pl" => ("Pokaż Klimat", "O Klimat", "Zakończ"),
        "cs" => ("Zobrazit Klimat", "O aplikaci Klimat", "Ukončit"),
        "sk" => ("Zobraziť Klimat", "O aplikácii Klimat", "Ukončiť"),
        "hu" => ("Klimat megjelenítése", "Névjegy: Klimat", "Kilépés"),
        "ro" => ("Afișează Klimat", "Despre Klimat", "Ieșire"),
        "el" => ("Εμφάνιση Klimat", "Σχετικά με το Klimat", "Έξοδος"),
        "tr" => ("Klimat’ı göster", "Klimat hakkında", "Çıkış"),
        "ja" => ("Klimatを表示", "Klimatについて", "終了"),
        "zh" => ("显示 Klimat", "关于 Klimat", "退出"),
        _ => ("Show Klimat", "About Klimat", "Quit"),
    }
}

fn build_tray_menu(app: &tauri::AppHandle, language: &str) -> tauri::Result<Menu<tauri::Wry>> {
    let (open_label, about_label, quit_label) = tray_menu_labels(language);
    let open = MenuItem::with_id(app, "open", open_label, true, None::<&str>)?;
    let about = MenuItem::with_id(app, "about", about_label, true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", quit_label, true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    Menu::with_items(app, &[&open, &about, &separator, &quit])
}

#[tauri::command]
fn set_tray_language(app: tauri::AppHandle, language: String) -> Result<(), String> {
    let menu = build_tray_menu(&app, &language).map_err(|error| error.to_string())?;
    let tray = app.tray_by_id("main").ok_or("Klimats trayikon saknas.")?;
    tray.set_menu(Some(menu))
        .map_err(|error| error.to_string())?;
    Ok(())
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
        .invoke_handler(tauri::generate_handler![
            hide_on_close,
            climate_request,
            about_info,
            set_tray_language
        ])
        .setup(|app| {
            #[cfg(target_os = "macos")]
            set_macos_tray_activation_policy();

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
            let menu = build_tray_menu(app.handle(), "sv")?;
            let tray_icon = tauri::image::Image::from_bytes(include_bytes!("../icons/tray.png"))?;
            TrayIconBuilder::with_id("main")
                .icon(tray_icon)
                .icon_as_template(cfg!(target_os = "macos"))
                .menu(&menu)
                .tooltip("Klimat")
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "open" => {
                        if let Some(window) = app.get_webview_window("main") {
                            show_popup(&window, None);
                            let _ = app.emit("show-controls", ());
                        }
                    }
                    "about" => {
                        if let Some(window) = app.get_webview_window("main") {
                            show_popup(&window, None);
                            let _ = app.emit("show-about", ());
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
