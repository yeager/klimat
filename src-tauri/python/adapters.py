"""Standalone climate adapters, with Home Assistant used as a protocol reference."""

from urllib.parse import parse_qsl, unquote


DAIKIN_MODES = {"0": "auto", "1": "auto", "2": "dry", "3": "cool", "4": "heat", "6": "fan", "7": "auto"}
DAIKIN_MODE_VALUES = {"auto": "0", "cool": "3", "heat": "4", "dry": "2", "fan": "6"}
MIDEA_BRANDS = {"midea", "comfee", "toshiba-lifestyle"}


def device_limit(payload):
    try:
        return max(0, min(6, int(payload.get("maxDevices", 6))))
    except (TypeError, ValueError):
        return 6


def requested_ids(payload):
    values = payload.get("deviceIds")
    return {str(value) for value in values} if isinstance(values, list) else None


def already_known(payload, brand, appliance_id):
    return any(str(item.get("id")) == str(appliance_id) and item.get("brand") == brand for item in payload.get("knownDevices", []) if isinstance(item, dict))


def temperature_step(device, attribute, default):
    try:
        step = float(getattr(device, attribute, default) or default)
        return step if step > 0 else default
    except (TypeError, ValueError):
        return default


def normalize_operation_mode(value):
    mode = str(value or "").lower()
    return {"heat_cool": "auto", "fan_only": "fan"}.get(mode, mode)


def climate_device(device, brand, kind):
    operation_mode = normalize_operation_mode(getattr(device, "operation_mode", "cool"))
    item = {
        "id": str(getattr(device, "device_id", getattr(device, "id", ""))),
        "name": getattr(device, "name", None) or f"{brand} AC",
        "brand": brand,
        "kind": kind,
        "state": "on" if getattr(device, "power", False) else "off",
        "target": getattr(device, "target_temperature", 22) or 22,
        "temperatureStep": temperature_step(device, "temperature_increment", 0.5),
        "current": getattr(device, "room_temperature", getattr(device, "current_temperature", 0)) or 0,
        "mode": operation_mode,
        "fan": normalize_melcloud_fan(device),
    }
    minimum = getattr(device, "target_temperature_min", None)
    maximum = getattr(device, "target_temperature_max", None)
    if minimum is not None:
        item["minTarget"] = minimum
    if maximum is not None:
        item["maxTarget"] = maximum
    supported = getattr(device, "operation_modes", None)
    if supported:
        item["availableModes"] = list(dict.fromkeys(
            mode for mode in (normalize_operation_mode(value) for value in supported)
            if mode in {"auto", "cool", "heat", "dry", "fan"}
        ))
    return item


def melcloud_login_error(error):
    """Turn common MELCloud authentication failures into actionable messages."""
    import aiohttp

    if isinstance(error, aiohttp.ClientResponseError):
        if error.status in {400, 401, 403}:
            return ValueError("MELCloud nekade inloggningen. Kontrollera e-postadress och lösenord i MELCloud-appen eller på webben.")
        if error.status == 429:
            return ValueError("MELCloud begränsar tillfälligt inloggningar. Vänta en stund och försök igen.")
        return ValueError(f"MELCloud svarade med HTTP {error.status}. Försök igen senare.")
    if isinstance(error, aiohttp.ClientError) or isinstance(error, TimeoutError):
        return ValueError("Kunde inte nå MELCloud. Kontrollera internetanslutningen och försök igen.")
    if isinstance(error, (AttributeError, KeyError, TypeError)):
        return ValueError("MELCloud kunde inte bekräfta inloggningen. Kontrollera att e-postadress och lösenord fungerar i MELCloud-appen eller på webben.")
    return ValueError("MELCloud-inloggningen misslyckades. Kontrollera kontot i MELCloud-appen eller på webben.")


async def melcloud_login(pymelcloud, session, username, password):
    import aiohttp

    try:
        token = await pymelcloud.login(username, password, session=session)
    except (aiohttp.ClientError, TimeoutError, AttributeError, KeyError, TypeError) as error:
        raise melcloud_login_error(error) from error
    if not isinstance(token, str) or not token.strip():
        raise ValueError("MELCloud bekräftade inte inloggningen. Kontrollera e-postadress och lösenord i MELCloud-appen eller på webben.")
    return token


async def melcloud_connect(payload):
    import aiohttp
    import pymelcloud

    async with aiohttp.ClientSession() as session:
        token = await melcloud_login(pymelcloud, session, payload["username"], payload["password"])
        devices = await pymelcloud.get_devices(token, session=session)
        result = []
        limit = device_limit(payload)
        for group in devices.values():
            for device in group:
                if already_known(payload, "melcloud", device.device_id):
                    continue
                if len(result) >= limit:
                    break
                await device.update()
                item = climate_device(device, "melcloud", "cloud")
                item["id"] = str(device.device_id)
                item["connection"] = {"profile_id": payload["id"], "serial": str(device.serial)}
                result.append(item)
            if len(result) >= limit:
                break
        return {"devices": result, "profile": {}}


def midea_connect(payload):
    from midealocal.const import ProtocolVersion
    from midealocal.discover import discover
    from midealocal.devices import device_selector

    host = payload.get("endpoint") or None
    found = discover(ip_address=[host] if host else None)
    saved_profile = next((profile for profile in payload.get("profiles", []) if profile.get("id") == payload.get("id")), {})
    saved_connections = {str(entry.get("id")): entry.get("connection", {}) for entry in saved_profile.get("mideaDevices", [])}
    for appliance_id, details in found.items():
        details.update(saved_connections.get(str(appliance_id), {}))
    result = []
    limit = device_limit(payload)
    for appliance_id, details in found.items():
        if requested_ids(payload) is not None and str(appliance_id) not in requested_ids(payload):
            continue
        if already_known(payload, payload.get("brand", "midea"), appliance_id):
            continue
        if len(result) >= limit:
            break
        details["token"] = payload.get("token") or details.get("token")
        details["key"] = payload.get("key") or details.get("key")
        device = device_selector(details.get("name", "Midea AC"), appliance_id, details["type"], details["ip_address"], details["port"], details.get("token", ""), details.get("key", ""), ProtocolVersion(details["protocol"]), details["model"], details.get("subtype", 0), "", details.get("mac"), details.get("sn"))
        if device is None or not device.connect(check_protocol=True):
            continue
        try:
            result.append({
                "id": str(appliance_id),
                "name": details.get("name") or "Midea AC",
                "brand": "midea",
                "kind": "direct",
                "state": "on" if device.get_attribute("power") else "off",
                "target": device.get_attribute("target_temperature") or 22,
                "temperatureStep": temperature_step(device, "temperature_step", 0.5),
                "minTarget": device.min_temperature(),
                "maxTarget": device.max_temperature(),
                "current": device.get_attribute("indoor_temperature") or 0,
                "mode": {"heat": "heat", "cool": "cool", "auto": "auto", "dry": "dry", "fan_only": "fan"}.get(device.raw_hvac_mode() or "cool", "cool"),
                "availableModes": list(dict.fromkeys(
                    mode for mode in (normalize_operation_mode(value) for value in (device.raw_hvac_modes or []))
                    if mode in {"auto", "cool", "heat", "dry", "fan"}
                )),
                "fan": normalize_midea_fan(device.raw_fan_mode()),
                "connection": {key: value for key, value in details.items() if key not in {"token", "key"}} | {"profile_id": payload["id"]},
            })
        finally:
            device.close_socket()
    private_devices = list(saved_profile.get("mideaDevices", []))
    for item in result:
        details = found[int(item["id"])]
        private_devices = [entry for entry in private_devices if str(entry.get("id")) != item["id"]]
        private_devices.append({"id": item["id"], "connection": {**item["connection"], "token": details.get("token"), "key": details.get("key")}})
    return {"devices": result, "profile": {"mideaDevices": private_devices}}


async def melcloud_restore(payload):
    import aiohttp
    import pymelcloud

    profile = next((entry for entry in payload.get("profiles", []) if entry.get("id") == payload.get("id")), None)
    if not profile or not profile.get("username") or not profile.get("password"):
        raise ValueError("MELCloud-uppgifter saknas i systemets nyckelring. Anslut kontot igen.")
    async with aiohttp.ClientSession() as session:
        token = await melcloud_login(pymelcloud, session, profile["username"], profile["password"])
        devices = await pymelcloud.get_devices(token, session=session)
        result = []
        selected = requested_ids(payload)
        limit = device_limit(payload)
        for group in devices.values():
            for device in group:
                if selected is not None and str(device.device_id) not in selected:
                    continue
                if len(result) >= limit:
                    break
                await device.update()
                item = climate_device(device, "melcloud", "cloud")
                item["id"] = str(device.device_id)
                item["connection"] = {"profile_id": payload["id"], "serial": str(device.serial)}
                result.append(item)
            if len(result) >= limit:
                break
        return {"devices": result, "profile": {}}


async def melcloud_command(payload):
    import aiohttp
    import pymelcloud

    active_profile = payload.get("profileId")
    active = next((p for p in payload.get("profiles", []) if p.get("id") == active_profile), None)
    async with aiohttp.ClientSession() as session:
        if not active:
            raise ValueError("MELCloud-sessionen saknas. Anslut kontot igen.")
        token = await melcloud_login(pymelcloud, session, active["username"], active["password"])
        devices = await pymelcloud.get_devices(token, session=session)
        device = next((d for group in devices.values() for d in group if str(d.device_id) == str(payload["device"]["id"])), None)
        if device is None:
            raise ValueError("Enheten hittades inte i MELCloud-kontot.")
        action, value = payload["action"], payload.get("value")
        mode = {"cool": "cool", "heat": "heat", "auto": "heat_cool", "dry": "dry", "fan": "fan_only"}.get(str(value), value)
        fan = None
        if action == "fan":
            if value == "auto":
                fan = "auto"
            else:
                available = [str(speed) for speed in (device.fan_speeds or []) if str(speed).lower() != "auto"]
                if available:
                    index = {"low": 0, "medium": (len(available) - 1) // 2, "high": len(available) - 1}.get(str(value))
                    fan = available[index] if index is not None else str(value)
                else:
                    fan = {"low": "1", "medium": "2", "high": "3"}.get(str(value), value)
        target = float(value) if action == "temperature" else None
        if target is not None:
            minimum = getattr(device, "target_temperature_min", None)
            maximum = getattr(device, "target_temperature_max", None)
            if minimum is not None:
                target = max(float(minimum), target)
            if maximum is not None:
                target = min(float(maximum), target)
        properties = {"power": value == "on"} if action == "power" else {"target_temperature": target} if action == "temperature" else {"operation_mode": mode} if action == "mode" else {"fan_speed": fan} if action == "fan" else {}
        if not properties:
            raise ValueError(f"Unsupported MELCloud command: {action}")
        await device.set(properties)
        result = climate_device(device, "melcloud", "cloud")
        result["id"] = payload["device"]["id"]
        result["brand"] = "melcloud"
        return result


async def melcloud_status(payload):
    import aiohttp
    import pymelcloud

    active_profile = payload.get("profileId")
    active = next((p for p in payload.get("profiles", []) if p.get("id") == active_profile), None)
    if not active:
        raise ValueError("MELCloud-sessionen saknas. Anslut kontot igen.")
    async with aiohttp.ClientSession() as session:
        token = await melcloud_login(pymelcloud, session, active["username"], active["password"])
        devices = await pymelcloud.get_devices(token, session=session)
        device = next((d for group in devices.values() for d in group if str(d.device_id) == str(payload["device"]["id"])), None)
        if device is None:
            raise ValueError("Enheten hittades inte i MELCloud-kontot.")
        await device.update()
        result = climate_device(device, "melcloud", "cloud")
        result["id"] = payload["device"]["id"]
        result["brand"] = "melcloud"
        return result


async def daikin_request(base_url, path, params=None):
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.request(
                "POST" if params is not None else "GET",
                f"{base_url}/{path}",
                data=params,
            ) as response:
                response.raise_for_status()
                body = await response.text()
                values = dict(parse_qsl(body.replace(",", "&"), keep_blank_values=True))
        except (aiohttp.ClientError, TimeoutError) as error:
            raise ConnectionError(f"Kunde inte nå Daikin-enheten på {base_url}: {error}") from error
    if values.get("ret") not in (None, "OK"):
        raise ValueError(f"Daikin-enheten avvisade kommandot: {values['ret']}")
    return values


async def daikin_read(payload):
    endpoint = str(payload.get("endpoint") or "").strip().rstrip("/")
    if not endpoint:
        raise ValueError("Ange Daikin-enhetens IP-adress.")
    if "://" not in endpoint:
        endpoint = f"http://{endpoint}"
    if not endpoint.startswith(("http://", "https://")):
        raise ValueError("Daikin-adressen måste använda HTTP eller HTTPS.")
    profile_id = str(payload.get("id") or payload.get("profileId") or "")
    control = await daikin_request(endpoint, "aircon/get_control_info")
    sensor = await daikin_request(endpoint, "aircon/get_sensor_info")
    info = {}
    try:
        info = await daikin_request(endpoint, "common/basic_info")
    except (ConnectionError, ValueError):
        pass
    raw_mode = control.get("mode", "0")
    device_id = info.get("mac") or info.get("id") or endpoint
    return {
        "id": str(device_id),
        "name": unquote(info.get("name", "")).strip() or "Daikin AC",
        "brand": "daikin",
        "kind": "direct",
        "state": "on" if control.get("pow") == "1" else "off",
        "target": daikin_temperature(control.get("stemp"), 22),
        "temperatureStep": 1,
        "current": daikin_temperature(sensor.get("htemp"), 0),
        "mode": DAIKIN_MODES.get(raw_mode, "auto"),
        "fan": daikin_fan(control.get("f_rate", "A")),
        "connection": {"profile_id": profile_id, "endpoint": endpoint},
    }


def daikin_fan(value):
    if str(value).upper() == "A":
        return "auto"
    try:
        speed = int(value)
    except (TypeError, ValueError):
        return "auto"
    return "low" if speed <= 3 else "medium" if speed <= 5 else "high"


def daikin_temperature(value, fallback):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


async def daikin_connect(payload):
    return {"devices": [await daikin_read(payload)], "profile": {}}


async def daikin_restore(payload):
    profile = next((entry for entry in payload.get("profiles", []) if entry.get("id") == payload.get("id")), None)
    if not profile or not profile.get("endpoint"):
        raise ValueError("Daikin-enhetens IP-adress saknas. Anslut enheten igen.")
    return await daikin_connect({**profile, **payload})


async def daikin_command(payload):
    active = next((entry for entry in payload.get("profiles", []) if entry.get("id") == payload.get("profileId")), None)
    endpoint = (payload.get("device", {}).get("connection") or {}).get("endpoint") or (active or {}).get("endpoint")
    if not endpoint:
        raise ValueError("Daikin-enhetens IP-adress saknas. Anslut enheten igen.")
    control = await daikin_request(endpoint, "aircon/get_control_info")
    action, value = payload.get("action"), payload.get("value")
    params = {key: control.get(key, default) for key, default in (("pow", "0"), ("mode", "0"), ("stemp", "22"), ("shum", "0"), ("f_rate", "A"), ("f_dir", "0"))}
    if action == "power":
        params["pow"] = "1" if value == "on" else "0"
    elif action == "temperature":
        params["stemp"] = f"{float(value):g}"
    elif action == "mode":
        params["mode"] = DAIKIN_MODE_VALUES.get(str(value), "0")
    elif action == "fan":
        params["f_rate"] = {"auto": "A", "low": "3", "medium": "5", "high": "7"}.get(str(value), "A")
    else:
        raise ValueError(f"Daikin-kommandot stöds inte: {action}")
    await daikin_request(endpoint, "aircon/set_control_info", params)
    return await daikin_read({"endpoint": endpoint, "profileId": payload.get("profileId")})


async def daikin_status(payload):
    active = next((entry for entry in payload.get("profiles", []) if entry.get("id") == payload.get("profileId")), None)
    endpoint = (payload.get("device", {}).get("connection") or {}).get("endpoint") or (active or {}).get("endpoint")
    if not endpoint:
        raise ValueError("Daikin-enhetens IP-adress saknas. Anslut enheten igen.")
    return await daikin_read({"endpoint": endpoint, "profileId": payload.get("profileId")})


def midea_command(payload):
    from midealocal.const import ProtocolVersion
    from midealocal.devices import device_selector
    item = payload["device"]
    profile_id = payload.get("profileId")
    profile = next((entry for entry in payload.get("profiles", []) if entry.get("id") == profile_id), {})
    saved = next((entry.get("connection") for entry in profile.get("mideaDevices", []) if str(entry.get("id")) == str(item.get("id"))), {})
    data = {**saved, **(item.get("connection") or {})}
    device = device_selector(item["name"], int(item["id"]), data["type"], data["ip_address"], data["port"], data["token"], data["key"], ProtocolVersion(data["protocol"]), data["model"], data["subtype"], "", data.get("mac"), data.get("sn"))
    if device is None or not device.connect(check_protocol=True):
        raise ConnectionError("Kunde inte ansluta till Midea-enheten.")
    try:
        action, value = payload["action"], payload.get("value")
        if action == "status": pass
        elif action == "power": device.turn_on() if value == "on" else device.turn_off()
        elif action == "temperature":
            target = max(device.min_temperature(), min(device.max_temperature(), float(value)))
            device.set_raw_target_temperature(target, None)
        elif action == "mode":
            device.set_raw_hvac_mode({"cool": "cool", "heat": "heat", "auto": "auto", "dry": "dry", "fan": "fan_only"}.get(str(value), str(value)))
        elif action == "fan":
            device.set_raw_fan_mode({"auto": "auto", "low": "low", "medium": "medium", "high": "high"}.get(str(value), str(value)))
        else: raise ValueError(f"Unsupported Midea command: {action}")
        device.refresh_status()
        updated_mode = {"heat": "heat", "cool": "cool", "auto": "auto", "dry": "dry", "fan_only": "fan"}.get(device.raw_hvac_mode() or "cool", "cool")
        return {"state": "on" if device.get_attribute("power") else "off", "target": device.get_attribute("target_temperature") or 22, "current": device.get_attribute("indoor_temperature") or 0, "mode": updated_mode, "fan": normalize_midea_fan(device.raw_fan_mode()), "minTarget": device.min_temperature(), "maxTarget": device.max_temperature()}
    finally:
        device.close_socket()


def normalize_midea_fan(value):
    fan = str(value or "auto").lower()
    if fan in {"silent", "low"}:
        return "low"
    if fan in {"medium", "mid"}:
        return "medium"
    if fan in {"high", "full", "full_speed"}:
        return "high"
    return "auto"


def normalize_melcloud_fan(device):
    current = str(getattr(device, "fan_speed", "auto") or "auto").lower()
    if current == "auto":
        return "auto"
    available = [str(speed).lower() for speed in (getattr(device, "fan_speeds", None) or []) if str(speed).lower() != "auto"]
    try:
        index = available.index(current)
    except ValueError:
        return "auto"
    if len(available) <= 1:
        return "medium"
    position = index / (len(available) - 1)
    return "low" if position < 1 / 3 else "medium" if position < 2 / 3 else "high"


async def handle(payload):
    action, brand = payload.get("action"), payload.get("brand")
    if action == "status" and brand == "melcloud":
        return await melcloud_status(payload)
    if action == "status" and brand == "daikin":
        return await daikin_status(payload)
    if action == "discover":
        if brand == "melcloud":
            return await melcloud_connect(payload)
        if brand == "daikin":
            return await daikin_connect(payload)
        if brand in MIDEA_BRANDS:
            return midea_connect(payload)
        raise ValueError(f"Integrationen {brand} stöds inte ännu.")
    if action == "restore":
        if brand == "melcloud":
            return await melcloud_restore(payload)
        if brand == "daikin":
            return await daikin_restore(payload)
        if brand in MIDEA_BRANDS:
            return midea_connect(payload)
        raise ValueError(f"Integrationen {brand} stöds inte ännu.")
    if brand == "melcloud":
        return await melcloud_command(payload)
    if brand == "daikin":
        return await daikin_command(payload)
    if brand in MIDEA_BRANDS:
        return midea_command(payload)
    raise ValueError(f"Integrationen {brand} stöds inte ännu.")
