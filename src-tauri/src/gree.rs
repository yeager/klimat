use gree_climate::{
    Client, DeviceInfo, FanSpeed, Mode, commands::Commands, crypto::Cipher,
    discovery::discover_with_timeout, models::CipherKind, protocol,
};
use serde_json::{Value, json};
use std::time::Duration;

const KEYRING_SERVICE: &str = "Klimat";
const DISCOVERY_TIMEOUT: Duration = Duration::from_secs(5);
const CIPHER_PROBE_TIMEOUT: Duration = Duration::from_millis(1500);

pub async fn request(payload: &Value) -> Result<Value, String> {
    handle(payload).await
}

async fn handle(payload: &Value) -> Result<Value, String> {
    match payload.get("action").and_then(Value::as_str) {
        Some("discover") => discover(payload).await,
        Some("restore") => restore(payload).await,
        Some("status" | "power" | "temperature" | "mode" | "fan") => command(payload).await,
        Some(action) => Err(format!("Gree-kommandot stöds inte: {action}")),
        None => Err("Gree-förfrågan saknar åtgärd.".into()),
    }
}

async fn discover(payload: &Value) -> Result<Value, String> {
    let profile_id = required_string(payload, "id")?;
    let limit = payload
        .get("maxDevices")
        .and_then(Value::as_u64)
        .unwrap_or(6)
        .min(6) as usize;
    let found = discover_with_timeout(DISCOVERY_TIMEOUT)
        .await
        .map_err(|error| format!("Gree-sökningen misslyckades: {error}"))?;
    if found.is_empty() {
        return Err(
            "Hittade inga Gree-enheter. Kontrollera att datorn och AC-enheten är på samma nätverk."
                .into(),
        );
    }

    let saved = load_devices(profile_id)?;
    let mut public_devices = Vec::new();
    let mut private_devices = Vec::new();
    let mut failures = Vec::new();

    for info in found.into_iter().take(limit) {
        let cached = saved
            .iter()
            .find(|entry| entry.get("mac").and_then(Value::as_str) == Some(info.mac.as_str()));
        let connection = match connect_device(info.clone(), cached).await {
            Ok(connection) => connection,
            Err(error) => {
                failures.push(format!("{}: {error}", info.name));
                continue;
            }
        };
        let Some(key) = connection.client.device_info().key.clone() else {
            failures.push(format!(
                "{}: enheten lämnade ingen sessionsnyckel",
                info.name
            ));
            continue;
        };
        let cipher_name = match connection.cipher_kind {
            Some(CipherKind::V1) => "v1",
            Some(CipherKind::V2) => "v2",
            None => match detect_cipher(&info, &key).await {
                Ok(kind) => {
                    if kind == CipherKind::V1 {
                        "v1"
                    } else {
                        "v2"
                    }
                }
                Err(error) => {
                    failures.push(format!("{}: {error}", info.name));
                    continue;
                }
            },
        };
        let secret = json!({
            "id": info.mac,
            "ip": info.ip,
            "port": info.port,
            "mac": info.mac,
            "name": info.name,
            "key": key,
            "cipher": cipher_name,
        });
        public_devices.push(device_state(&info, connection.client.state(), profile_id));
        private_devices.push(secret);
    }

    if public_devices.is_empty() {
        return Err(if failures.is_empty() {
            "Inga Gree-enheter kunde anslutas.".into()
        } else {
            format!(
                "Kunde inte ansluta till Gree-enheterna: {}",
                failures.join("; ")
            )
        });
    }
    store_devices(profile_id, &private_devices)?;
    Ok(json!({ "devices": public_devices, "profile": {} }))
}

async fn restore(payload: &Value) -> Result<Value, String> {
    let profile_id = required_string(payload, "id")?;
    let saved = load_devices(profile_id)?;
    if saved.is_empty() {
        return Err(
            "Gree-enhetens nyckel saknas i systemets nyckelring. Anslut enheten igen.".into(),
        );
    }
    let mut devices = Vec::new();
    let mut failures = Vec::new();
    for secret in saved {
        let info = device_info(&secret)?;
        match connect_device(info.clone(), Some(&secret)).await {
            Ok(connection) => {
                devices.push(device_state(&info, connection.client.state(), profile_id))
            }
            Err(error) => failures.push(format!("{}: {error}", info.name)),
        }
    }
    if devices.is_empty() {
        return Err(format!(
            "Kunde inte återansluta till Gree-enheten: {}",
            failures.join("; ")
        ));
    }
    Ok(json!({ "devices": devices, "profile": {} }))
}

async fn command(payload: &Value) -> Result<Value, String> {
    let profile_id = required_string(payload, "profileId")?;
    let device = payload
        .get("device")
        .ok_or("Gree-enheten saknas i förfrågan.")?;
    let id = required_string(device, "id")?;
    let saved = load_devices(profile_id)?;
    let secret = saved
        .iter()
        .find(|entry| entry.get("id").and_then(Value::as_str) == Some(id))
        .ok_or("Gree-enhetens nyckel saknas i systemets nyckelring. Anslut enheten igen.")?;
    let info = device_info(secret)?;
    let mut connection = connect_device(info.clone(), Some(secret)).await?;
    if payload.get("action").and_then(Value::as_str) == Some("status") {
        connection
            .client
            .refresh()
            .await
            .map_err(|error| error.to_string())?;
        return Ok(device_state(&info, connection.client.state(), profile_id));
    }
    let value = payload.get("value").ok_or("Gree-kommandot saknar värde.")?;
    match payload.get("action").and_then(Value::as_str) {
        Some("power") => connection
            .client
            .set_power(value.as_str() == Some("on"))
            .await
            .map_err(|error| error.to_string())?,
        Some("temperature") => {
            let celsius = value
                .as_f64()
                .ok_or("Ogiltigt temperaturvärde för Gree-enheten.")?;
            let unit_value = if connection.client.state().temperature_unit
                == gree_climate::TemperatureUnit::Celsius
            {
                celsius.round().clamp(16.0, 30.0)
            } else {
                (celsius * 9.0 / 5.0 + 32.0).round().clamp(61.0, 86.0)
            } as u8;
            connection
                .client
                .set_temperature(unit_value)
                .await
                .map_err(|error| error.to_string())?;
        }
        Some("mode") => {
            let mode = match value.as_str() {
                Some("cool") => Mode::Cool,
                Some("heat") => Mode::Heat,
                Some("dry") => Mode::Dry,
                Some("fan") => Mode::Fan,
                Some("auto") => Mode::Auto,
                _ => return Err("Ogiltigt driftläge för Gree-enheten.".into()),
            };
            connection
                .client
                .set_mode(mode)
                .await
                .map_err(|error| error.to_string())?;
        }
        Some("fan") => {
            let fan = match value.as_str() {
                Some("low") => FanSpeed::Low,
                Some("medium") => FanSpeed::Medium,
                Some("high") => FanSpeed::High,
                _ => FanSpeed::Auto,
            };
            connection
                .client
                .set_fan_speed(fan)
                .await
                .map_err(|error| error.to_string())?;
        }
        _ => return Err("Gree-kommandot stöds inte.".into()),
    }
    connection
        .client
        .refresh()
        .await
        .map_err(|error| error.to_string())?;
    Ok(device_state(&info, connection.client.state(), profile_id))
}

struct DeviceConnection {
    client: Client,
    cipher_kind: Option<CipherKind>,
}

async fn connect_device(
    info: DeviceInfo,
    saved: Option<&Value>,
) -> Result<DeviceConnection, String> {
    let mut client = Client::connect(info.clone())
        .await
        .map_err(|error| error.to_string())?;
    let key = saved
        .and_then(|entry| entry.get("key"))
        .and_then(Value::as_str);
    let cipher = saved
        .and_then(|entry| entry.get("cipher"))
        .and_then(Value::as_str);
    if let Some(key) = key {
        let kinds: &[CipherKind] = match cipher {
            Some("v1") => &[CipherKind::V1],
            Some("v2") => &[CipherKind::V2],
            _ => &[CipherKind::V1, CipherKind::V2],
        };
        let mut last_error = String::new();
        for kind in kinds {
            if client.bind_with_key(Some(key), Some(*kind)).await.is_ok() {
                return Ok(DeviceConnection {
                    client,
                    cipher_kind: Some(*kind),
                });
            }
            last_error = format!("Gree-nyckeln fungerade inte med {kind:?}.");
            client = Client::connect(info.clone())
                .await
                .map_err(|error| error.to_string())?;
        }
        return Err(last_error);
    }
    client.bind().await.map_err(|error| error.to_string())?;
    Ok(DeviceConnection {
        client,
        cipher_kind: None,
    })
}

async fn detect_cipher(info: &DeviceInfo, key: &str) -> Result<CipherKind, String> {
    let (socket, _) = protocol::create_unicast_socket()
        .await
        .map_err(|error| error.to_string())?;
    for kind in [CipherKind::V1, CipherKind::V2] {
        let cipher = match kind {
            CipherKind::V1 => Cipher::v1_with_key(key),
            CipherKind::V2 => Cipher::v2_with_key(key),
        };
        let mut request = Commands::status_packet(&info.mac, false);
        request
            .encrypt_pack(&cipher)
            .map_err(|error| error.to_string())?;
        if let Ok(response) =
            protocol::send_and_recv(&socket, info.socket_addr(), &request, CIPHER_PROBE_TIMEOUT)
                .await
            && let Ok(pack) = response.decrypt_pack(&cipher)
            && matches!(pack.get("t").and_then(Value::as_str), Some("dat" | "res"))
        {
            return Ok(kind);
        }
    }
    Err("Kunde inte identifiera Gree-enhetens krypteringsläge.".into())
}

fn device_state(info: &DeviceInfo, state: &gree_climate::State, profile_id: &str) -> Value {
    let to_celsius = |temperature: u8| {
        if state.temperature_unit == gree_climate::TemperatureUnit::Celsius {
            f64::from(temperature)
        } else {
            ((f64::from(temperature) - 32.0) * 5.0 / 9.0).round()
        }
    };
    let fan = match state.fan_speed {
        FanSpeed::Low | FanSpeed::MediumLow => "low",
        FanSpeed::Medium => "medium",
        FanSpeed::MediumHigh | FanSpeed::High => "high",
        FanSpeed::Auto => "auto",
    };
    json!({
        "id": info.mac,
        "name": info.name,
        "brand": "gree",
        "kind": "direct",
        "state": if state.power { "on" } else { "off" },
        "target": to_celsius(state.target_temperature),
        "current": state.current_temperature.map(to_celsius).unwrap_or(0.0),
        "mode": match state.mode { Mode::Auto => "auto", Mode::Cool => "cool", Mode::Dry => "dry", Mode::Fan => "fan", Mode::Heat => "heat" },
        "fan": fan,
        "connection": {"profile_id": profile_id, "ip": info.ip, "port": info.port, "mac": info.mac}
    })
}

fn device_info(secret: &Value) -> Result<DeviceInfo, String> {
    let ip = required_string(secret, "ip")?;
    let mac = required_string(secret, "mac")?;
    let name = secret
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("Gree AC");
    let port = secret.get("port").and_then(Value::as_u64).unwrap_or(7000) as u16;
    Ok(DeviceInfo::new(ip.into(), port, mac.into(), name.into()))
}

fn load_devices(profile_id: &str) -> Result<Vec<Value>, String> {
    let entry = keyring::Entry::new(KEYRING_SERVICE, &format!("{profile_id}-gree-devices"))
        .map_err(|error| format!("Kunde inte öppna systemets nyckelring: {error}"))?;
    match entry.get_password() {
        Ok(text) => serde_json::from_str(&text).map_err(|error| {
            format!("Gree-nyckeln i systemets nyckelring kunde inte läsas: {error}")
        }),
        Err(keyring::Error::NoEntry) => Ok(Vec::new()),
        Err(error) => Err(format!(
            "Kunde inte läsa Gree-nyckeln från systemets nyckelring: {error}"
        )),
    }
}

fn store_devices(profile_id: &str, devices: &[Value]) -> Result<(), String> {
    let entry = keyring::Entry::new(KEYRING_SERVICE, &format!("{profile_id}-gree-devices"))
        .map_err(|error| format!("Kunde inte öppna systemets nyckelring: {error}"))?;
    let text = serde_json::to_string(devices).map_err(|error| error.to_string())?;
    entry
        .set_password(&text)
        .map_err(|error| format!("Kunde inte spara Gree-nyckeln i systemets nyckelring: {error}"))
}

fn required_string<'a>(value: &'a Value, key: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("Gree-förfrågan saknar {key}."))
}
