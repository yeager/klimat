"""Standalone climate adapters, with Home Assistant used as a protocol reference."""

from urllib.parse import parse_qsl, unquote
import asyncio
import tempfile
from pathlib import Path


DAIKIN_MODES = {"0": "auto", "1": "auto", "2": "dry", "3": "cool", "4": "heat", "6": "fan", "7": "auto"}
DAIKIN_MODE_VALUES = {"auto": "0", "cool": "3", "heat": "4", "dry": "2", "fan": "6"}
MIDEA_BRANDS = {"midea", "comfee", "toshiba-lifestyle"}
TOSHIBA_CLOUD_MODES = {"AUTO": "auto", "COOL": "cool", "HEAT": "heat", "DRY": "dry", "FAN": "fan"}
TOSHIBA_CLOUD_FANS = {"AUTO": "auto", "QUIET": "low", "LOW": "low", "MEDIUM_LOW": "low", "MEDIUM": "medium", "MEDIUM_HIGH": "high", "HIGH": "high"}
GREE_MODES = {0: "auto", 1: "cool", 2: "dry", 3: "fan", 4: "heat"}
GREE_MODE_VALUES = {value: key for key, value in GREE_MODES.items()}
GREE_FANS = {0: "auto", 1: "low", 2: "low", 3: "medium", 4: "high", 5: "high"}
GREE_FAN_VALUES = {"auto": 0, "low": 1, "medium": 3, "high": 5}


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


def panasonic_device(info, device):
    from aio_panasonic_comfort_cloud import constants

    params = device.parameters
    modes = {
        constants.OperationMode.Auto: "auto",
        constants.OperationMode.Cool: "cool",
        constants.OperationMode.Heat: "heat",
        constants.OperationMode.Dry: "dry",
        constants.OperationMode.Fan: "fan",
    }
    fans = {
        constants.FanSpeed.Auto: "auto",
        constants.FanSpeed.Low: "low",
        constants.FanSpeed.LowMid: "low",
        constants.FanSpeed.Mid: "medium",
        constants.FanSpeed.HighMid: "high",
        constants.FanSpeed.High: "high",
    }
    mode = modes.get(params.mode, "auto")
    features = device.features
    available_modes = [
        mode_id for enabled, mode_id in (
            (features.auto_mode, "auto"),
            (features.cool_mode, "cool"),
            (features.heat_mode, "heat"),
            (features.dry_mode, "dry"),
            (features.fan_mode, "fan"),
        ) if enabled
    ]
    return {
        "id": str(info.id),
        "name": info.name or "Panasonic AC",
        "brand": "panasonic-cloud",
        "kind": "cloud",
        "state": "on" if params.power == constants.Power.On else "off",
        "target": params.target_temperature or 22,
        "temperatureStep": 0.5,
        "minTarget": 16,
        "maxTarget": 30,
        "current": params.inside_temperature or 0,
        "mode": mode,
        "availableModes": available_modes or [mode],
        "fan": fans.get(params.fan_speed, "auto"),
    }


async def panasonic_request(payload, action):
    import aiohttp
    from aio_panasonic_comfort_cloud import ApiClient, ChangeRequestBuilder, constants
    from aio_panasonic_comfort_cloud.exceptions import MFARequiredError

    profile = next((entry for entry in payload.get("profiles", []) if entry.get("id") == (payload.get("profileId") or payload.get("id"))), None)
    credentials = profile or payload
    username, password = credentials.get("username"), credentials.get("password")
    if not username or not password:
        raise ValueError("Panasonic Comfort Cloud-uppgifter saknas. Anslut kontot igen.")
    token_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="klimat-panasonic-", delete=False) as token_file:
            token_path = token_file.name
            saved_token = credentials.get("panasonicToken")
            if saved_token:
                token_file.write(str(saved_token).encode("utf-8"))
        async with aiohttp.ClientSession() as session:
            client = ApiClient(username, password, session, token_file_name=token_path, auto_power_on=False)
            try:
                await client.start_session(otp_code=payload.get("otp") or None)
            except MFARequiredError as error:
                raise ValueError("Panasonic kräver en verifieringskod. Ange koden från tvåfaktorsappen och försök igen.") from error
            infos = client.get_devices()
            selected = requested_ids(payload)
            limit = device_limit(payload)
            if action in {"discover", "restore"}:
                results = []
                for info in infos:
                    if selected is not None and str(info.id) not in selected:
                        continue
                    if action == "discover" and already_known(payload, "panasonic-cloud", info.id):
                        continue
                    if len(results) >= limit:
                        break
                    device = await client.get_device(info)
                    item = panasonic_device(info, device)
                    item["connection"] = {"profile_id": payload.get("id") or payload.get("profileId")}
                    results.append(item)
                return {"devices": results, "profile": {"panasonicToken": Path(token_path).read_text(encoding="utf-8")}}
            info = next((item for item in infos if str(item.id) == str(payload["device"]["id"])), None)
            if info is None:
                raise ValueError("Panasonic-enheten hittades inte på Comfort Cloud-kontot.")
            device = await client.get_device(info)
            if action != "status":
                builder = ChangeRequestBuilder(device)
                value = payload.get("value")
                if action == "power":
                    builder.set_power_mode(constants.Power.On if value == "on" else constants.Power.Off)
                elif action == "temperature":
                    builder.set_target_temperature(max(16, min(30, float(value))))
                elif action == "mode":
                    modes = {"auto": constants.OperationMode.Auto, "cool": constants.OperationMode.Cool, "heat": constants.OperationMode.Heat, "dry": constants.OperationMode.Dry, "fan": constants.OperationMode.Fan}
                    if str(value) not in modes:
                        raise ValueError("Panasonic-enheten stöder inte det valda driftläget.")
                    builder.set_hvac_mode(modes[str(value)])
                elif action == "fan":
                    fans = {"auto": constants.FanSpeed.Auto, "low": constants.FanSpeed.Low, "medium": constants.FanSpeed.Mid, "high": constants.FanSpeed.High}
                    if str(value) not in fans:
                        raise ValueError("Panasonic-enheten stöder inte den valda fläkthastigheten.")
                    builder.set_fan_speed(fans[str(value)])
                else:
                    raise ValueError(f"Unsupported Panasonic command: {action}")
                if builder.has_changes:
                    await client.set_device_raw(device, builder.build())
                    await client.try_update_device(device)
            result = panasonic_device(info, device)
            result["profile"] = {"panasonicToken": Path(token_path).read_text(encoding="utf-8")}
            return result
    finally:
        if token_path:
            Path(token_path).unlink(missing_ok=True)


def smartthings_device(item):
    component = next((entry for entry in item.get("components", []) if entry.get("id") == "main"), None)
    if not component:
        return None
    capabilities = component.get("capabilities", [])
    ids = {entry.get("id") for entry in capabilities}
    required = {"airConditionerMode", "airConditionerFanMode", "switch", "temperatureMeasurement", "thermostatCoolingSetpoint"}
    if not required.issubset(ids):
        return None
    status = item.get("status", {}).get("components", {}).get("main", {})
    if not status:
        component_status = next((entry for entry in item.get("components", []) if entry.get("id") == "main"), {})
        status = {entry.get("id"): entry.get("status", {}) for entry in component_status.get("capabilities", [])}
    def value(capability, attribute, default=None):
        return status.get(capability, {}).get(attribute, {}).get("value", default)
    mode_values = value("airConditionerMode", "supportedAcModes", []) or []
    mode_map = {"auto": "auto", "aIComfort": "auto", "cool": "cool", "heat": "heat", "dry": "dry", "fanOnly": "fan", "fan": "fan", "wind": "fan"}
    mode = mode_map.get(value("airConditionerMode", "airConditionerMode", "auto"), "auto")
    available_modes = list(dict.fromkeys(mode_map[entry] for entry in mode_values if entry in mode_map)) or ["auto", "cool", "heat", "dry", "fan"]
    fan_values = value("airConditionerFanMode", "supportedAcFanModes", []) or []
    fan = str(value("airConditionerFanMode", "fanMode", "auto")).lower()
    normalized_fan = "auto" if "auto" in fan else "low" if fan in {"low", "quiet", "silent"} else "high" if fan in {"high", "turbo", "max"} else "medium"
    range_value = value("thermostatCoolingSetpoint", "coolingSetpointRange", {}) or {}
    available_fans = list(dict.fromkeys("auto" if str(entry).lower() == "auto" else "low" if str(entry).lower() in {"low", "quiet", "silent"} else "high" if str(entry).lower() in {"high", "turbo", "max"} else "medium" for entry in fan_values))
    return {
        "id": str(item.get("deviceId", "")), "name": item.get("label") or item.get("name") or "Samsung AC",
        "brand": "samsung-smartthings", "kind": "cloud",
        "state": "on" if value("switch", "switch", "off") == "on" else "off",
        "target": value("thermostatCoolingSetpoint", "coolingSetpoint", 22),
        "temperatureStep": range_value.get("step", 1),
        "minTarget": range_value.get("minimum", 16), "maxTarget": range_value.get("maximum", 30),
        "current": value("temperatureMeasurement", "temperature", 0), "mode": mode,
        "availableModes": available_modes, "fan": normalized_fan, "availableFans": available_fans or ["auto", "low", "medium", "high"],
    }


async def samsung_request(payload, action):
    import aiohttp

    profile = next((entry for entry in payload.get("profiles", []) if entry.get("id") == (payload.get("profileId") or payload.get("id"))), None)
    token = (profile or payload).get("token")
    if not token:
        raise ValueError("Samsung SmartThings-token saknas i systemets nyckelring. Anslut kontot igen.")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async def devices():
            async with session.get("https://api.smartthings.com/v1/devices", params={"capability": "airConditionerMode", "includeStatus": "true"}) as response:
                if response.status in {401, 403}:
                    raise ValueError("Samsung SmartThings-tokenet har upphört eller saknar behörighet. Skapa ett nytt token och anslut igen.")
                response.raise_for_status()
                return (await response.json()).get("items", [])
        items = await devices()
        selected = requested_ids(payload)
        limit = device_limit(payload)
        if action in {"discover", "restore"}:
            results = []
            for item in items:
                device = smartthings_device(item)
                if not device or selected is not None and device["id"] not in selected:
                    continue
                if action == "discover" and already_known(payload, "samsung-smartthings", device["id"]):
                    continue
                if len(results) >= limit:
                    break
                results.append(device)
            return {"devices": results, "profile": {}}
        item = next((item for item in items if str(item.get("deviceId")) == str(payload["device"]["id"])), None)
        device = smartthings_device(item) if item else None
        if not device:
            raise ValueError("Samsung-klimatet hittades inte eller exponerar inte SmartThings klimatfunktioner.")
        if action != "status":
            value = payload.get("value")
            if action == "power":
                command = {"component": "main", "capability": "switch", "command": "on" if value == "on" else "off"}
            elif action == "temperature":
                command = {"component": "main", "capability": "thermostatCoolingSetpoint", "command": "setCoolingSetpoint", "arguments": [float(value)]}
            elif action == "mode":
                modes = {"auto": "auto", "cool": "cool", "heat": "heat", "dry": "dry", "fan": "fanOnly"}
                if value not in modes:
                    raise ValueError("Samsung-enheten stöder inte det valda driftläget.")
                command = {"component": "main", "capability": "airConditionerMode", "command": "setAirConditionerMode", "arguments": [modes[value]]}
            elif action == "fan":
                fan = {"auto": "auto", "low": "low", "medium": "medium", "high": "high"}.get(str(value))
                if fan not in device["availableFans"]:
                    raise ValueError("Samsung-enheten stöder inte den valda fläkthastigheten.")
                command = {"component": "main", "capability": "airConditionerFanMode", "command": "setFanMode", "arguments": [fan]}
            else:
                raise ValueError(f"Unsupported Samsung SmartThings command: {action}")
            async with session.post(f"https://api.smartthings.com/v1/devices/{device['id']}/commands", json={"commands": [command]}) as response:
                if response.status in {401, 403}:
                    raise ValueError("Samsung SmartThings-tokenet har upphört eller saknar behörighet. Skapa ett nytt token och anslut igen.")
                response.raise_for_status()
            items = await devices()
            item = next((item for item in items if str(item.get("deviceId")) == device["id"]), item)
            device = smartthings_device(item) or device
        return device


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
    import asyncio
    import aiohttp

    try:
        token = await asyncio.wait_for(
            pymelcloud.login(username, password, session=session), timeout=10
        )
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


def melcloud_home_items(context):
    for building in context.buildings:
        for unit in building.air_to_air_units:
            yield unit


def melcloud_home_mode(value):
    return {
        "heat": "heat",
        "cool": "cool",
        "automatic": "auto",
        "dry": "dry",
        "fan": "fan",
    }.get(str(value or "cool").lower(), "cool")


def melcloud_home_fan(value):
    current = str(value or "auto").lower()
    if current in {"auto", "off"}:
        return "auto"
    speeds = ["one", "two", "three", "four", "five"]
    try:
        position = speeds.index(current) / max(1, len(speeds) - 1)
    except ValueError:
        return "auto"
    return "low" if position < 1 / 3 else "medium" if position < 2 / 3 else "high"


def melcloud_home_device(unit):
    capabilities = unit.capabilities
    modes = ["heat"]
    if not capabilities or capabilities.has_cool_operation_mode:
        modes.append("cool")
    if not capabilities or capabilities.has_auto_operation_mode:
        modes.append("auto")
    if not capabilities or capabilities.has_dry_operation_mode:
        modes.append("dry")
    if not capabilities or capabilities.has_fan_operation_mode:
        modes.append("fan")
    current_mode = melcloud_home_mode(unit.operation_mode)
    minimum = getattr(capabilities, "min_temp_cool", None) if capabilities else None
    maximum = getattr(capabilities, "max_temp_cool", None) if capabilities else None
    if current_mode == "heat" and capabilities:
        minimum = capabilities.min_temp_heat or minimum
        maximum = capabilities.max_temp_heat or maximum
    item = {
        "id": str(unit.id),
        "name": unit.name or "Mitsubishi Electric AC",
        "brand": "melcloud-home",
        "kind": "cloud",
        "state": "on" if unit.power else "off",
        "target": unit.set_temperature or 22,
        "temperatureStep": 0.5 if capabilities and capabilities.has_half_degree_increments else 1,
        "current": unit.room_temperature or 0,
        "mode": current_mode,
        "fan": melcloud_home_fan(unit.set_fan_speed),
        "availableModes": modes,
    }
    if minimum is not None:
        item["minTarget"] = minimum
    if maximum is not None:
        item["maxTarget"] = maximum
    return item


def melcloud_home_find_unit(context, unit_id):
    return next((unit for unit in melcloud_home_items(context) if str(unit.id) == str(unit_id)), None)


async def melcloud_home_connect(payload, action="discover"):
    from aiomelcloudhome import ATAOperationMode, MELCloudHome

    profile = next((entry for entry in payload.get("profiles", []) if entry.get("id") == payload.get("id")), None)
    if action != "discover" and profile is None:
        raise ValueError("MELCloud Home-uppgifter saknas i systemets nyckelring. Anslut kontot igen.")
    credentials = profile or payload
    username, password = credentials.get("username"), credentials.get("password")
    if not username or not password:
        raise ValueError("Ange e-postadress och lösenord för MELCloud Home.")
    async with MELCloudHome(username=username, password=password) as client:
        context = await client.get_context()
        selected = requested_ids(payload) if action == "restore" else None
        result = []
        limit = device_limit(payload)
        if limit <= 0:
            return {"devices": result, "profile": {}}
        for unit in melcloud_home_items(context):
            if selected is not None and str(unit.id) not in selected:
                continue
            if action == "discover" and already_known(payload, "melcloud-home", unit.id):
                continue
            if len(result) >= limit:
                break
            item = melcloud_home_device(unit)
            item["connection"] = {"profile_id": payload.get("id")}
            result.append(item)
        return {"devices": result, "profile": {}}


async def melcloud_home_command(payload):
    from aiomelcloudhome import ATAFanSpeed, ATAOperationMode, MELCloudHome

    active = next((p for p in payload.get("profiles", []) if p.get("id") == payload.get("profileId")), None)
    if not active or not active.get("username") or not active.get("password"):
        raise ValueError("MELCloud Home-sessionen saknas. Anslut kontot igen.")
    async with MELCloudHome(username=active["username"], password=active["password"]) as client:
        context = await client.get_context()
        unit = melcloud_home_find_unit(context, payload["device"]["id"])
        if unit is None:
            raise ValueError("Enheten hittades inte i MELCloud Home-kontot.")
        action, value = payload.get("action"), payload.get("value")
        if action == "status":
            return melcloud_home_device(unit)
        controls = {}
        if action == "power":
            controls["power"] = value == "on"
        elif action == "temperature":
            target = float(value)
            capabilities = unit.capabilities
            minimum = getattr(capabilities, "min_temp_cool", None) if capabilities else None
            maximum = getattr(capabilities, "max_temp_cool", None) if capabilities else None
            if unit.operation_mode == ATAOperationMode.HEAT and capabilities:
                minimum = capabilities.min_temp_heat or minimum
                maximum = capabilities.max_temp_heat or maximum
            if minimum is not None:
                target = max(float(minimum), target)
            if maximum is not None:
                target = min(float(maximum), target)
            controls["set_temperature"] = target
        elif action == "mode":
            controls["operation_mode"] = {
                "cool": ATAOperationMode.COOL,
                "heat": ATAOperationMode.HEAT,
                "auto": ATAOperationMode.AUTOMATIC,
                "dry": ATAOperationMode.DRY,
                "fan": ATAOperationMode.FAN,
            }.get(str(value))
        elif action == "fan":
            levels = max(1, min(5, int(getattr(unit.capabilities, "number_of_fan_speeds", 5) or 5)))
            index = {"low": 0, "medium": (levels - 1) // 2, "high": levels - 1}.get(str(value))
            speed = "AUTO" if value == "auto" else ("ONE", "TWO", "THREE", "FOUR", "FIVE")[index] if index is not None else None
            controls["set_fan_speed"] = getattr(ATAFanSpeed, speed) if speed else None
        else:
            raise ValueError(f"MELCloud Home-kommandot stöds inte: {action}")
        await client.control_ata_unit(str(unit.id), **controls)
        refreshed = await client.get_context()
        unit = melcloud_home_find_unit(refreshed, unit.id) or unit
        return melcloud_home_device(unit)


def toshiba_cloud_device(device, profile_id):
    from toshiba_ac.device.properties import ToshibaAcStatus

    mode = TOSHIBA_CLOUD_MODES.get(device.ac_mode.name, "auto")
    fans = device.supported.for_ac_mode(device.ac_mode).ac_fan_mode
    available_fans = sorted({TOSHIBA_CLOUD_FANS[fan.name] for fan in fans if fan.name in TOSHIBA_CLOUD_FANS})
    result = {
        "id": str(device.ac_unique_id),
        "name": device.name or "Toshiba AC",
        "brand": "toshiba-cloud",
        "kind": "cloud",
        "state": "on" if device.ac_status == ToshibaAcStatus.ON else "off",
        "target": device.ac_temperature or 22,
        "temperatureStep": 1,
        "current": device.ac_indoor_temperature or 0,
        "mode": mode,
        "fan": TOSHIBA_CLOUD_FANS.get(device.ac_fan_mode.name, "auto"),
        "availableModes": [TOSHIBA_CLOUD_MODES[item.name] for item in device.supported.ac_mode if item.name in TOSHIBA_CLOUD_MODES],
        "availableFans": available_fans,
        "connection": {"profile_id": profile_id},
    }
    return result


async def toshiba_cloud_request(payload, action):
    from toshiba_ac.device.properties import ToshibaAcFanMode, ToshibaAcMode, ToshibaAcStatus
    from toshiba_ac.device_manager import ToshibaAcDeviceManager

    profile_id = str(payload.get("profileId") or payload.get("id") or "")
    profile = next((entry for entry in payload.get("profiles", []) if str(entry.get("id")) == profile_id), None)
    credentials = profile or payload
    username, password = credentials.get("username"), credentials.get("password")
    if not username or not password:
        raise ValueError("Ange e-postadress och lösenord för Toshiba-kontot.")
    if action in {"discover", "restore"} and device_limit(payload) <= 0:
        return {"devices": [], "profile": {}}

    manager = ToshibaAcDeviceManager(username=username, password=password, device_id=profile_id or None)
    try:
        await manager.connect()
        devices = await manager.get_devices()
        selected = requested_ids(payload) if action == "restore" else None
        if action in {"discover", "restore"}:
            result = []
            limit = device_limit(payload)
            for device in devices:
                if selected is not None and str(device.ac_unique_id) not in selected:
                    continue
                if action == "discover" and already_known(payload, "toshiba-cloud", device.ac_unique_id):
                    continue
                await device.state_reload()
                item = toshiba_cloud_device(device, profile_id)
                result.append(item)
                if len(result) >= limit:
                    break
            return {"devices": result, "profile": {}}

        device_id = str((payload.get("device") or {}).get("id") or "")
        device = next((item for item in devices if str(item.ac_unique_id) == device_id), None)
        if device is None:
            raise ValueError("Toshiba-luftkonditioneringen hittades inte på kontot.")
        action_value = payload.get("value")
        if action == "status":
            await device.state_reload()
        elif action == "power":
            await device.set_ac_status(ToshibaAcStatus.ON if action_value == "on" else ToshibaAcStatus.OFF)
        elif action == "temperature":
            await device.set_ac_temperature(round(float(action_value)))
        elif action == "mode":
            name = {"cool": "COOL", "heat": "HEAT", "auto": "AUTO", "dry": "DRY", "fan": "FAN"}.get(str(action_value))
            if not name or not hasattr(ToshibaAcMode, name):
                raise ValueError("Det valda driftläget stöds inte av Toshiba-enheten.")
            await device.set_ac_mode(getattr(ToshibaAcMode, name))
        elif action == "fan":
            name = {"auto": "AUTO", "low": "LOW", "medium": "MEDIUM", "high": "HIGH"}.get(str(action_value))
            if not name or not hasattr(ToshibaAcFanMode, name):
                raise ValueError("Den valda fläkthastigheten stöds inte av Toshiba-enheten.")
            await device.set_ac_fan_mode(getattr(ToshibaAcFanMode, name))
        else:
            raise ValueError(f"Toshiba-molnkommandot stöds inte: {action}")
        if action != "status":
            await asyncio.sleep(1)
            await device.state_reload()
        return toshiba_cloud_device(device, profile_id)
    finally:
        await manager.shutdown()


def gree_cloud_device(device, info, profile_id):
    from greeclimate_davo22.device import Mode, Props, TemperatureUnits

    fan_value = device.fan_speed
    mode_value = device.mode
    half_degree = device.get_property(Props.TEMP_HALF_ENABLED)
    target = device.target_temperature
    current = device.current_temperature
    if device.temperature_units == TemperatureUnits.F.value:
        target = (target - 32) * 5 / 9 if target is not None else None
        current = (current - 32) * 5 / 9 if current is not None else None
    return {
        "id": str(info.mac),
        "name": info.name or "Gree AC",
        "brand": "gree-cloud",
        "kind": "cloud",
        "state": "on" if device.power else "off",
        "target": round(target, 1) if target is not None else 22,
        "temperatureStep": 0.5 if half_degree == 1 else 1,
        "minTarget": 8,
        "maxTarget": 30,
        "current": round(current, 1) if current is not None else 0,
        "mode": GREE_MODES.get(int(mode_value), "auto") if mode_value is not None else "auto",
        "fan": GREE_FANS.get(int(fan_value), "auto") if fan_value is not None else "auto",
        "availableModes": [GREE_MODES[mode.value] for mode in Mode],
        "availableFans": ["low", "medium", "high"],
        "online": bool(info.online),
        "connection": {"profile_id": profile_id},
    }


async def gree_cloud_request(payload, action):
    from greeclimate_davo22.cloud_discovery import CloudDiscovery
    from greeclimate_davo22.device import FanSpeed, Mode

    profile_id = str(payload.get("profileId") or payload.get("id") or "")
    profile = next((entry for entry in payload.get("profiles", []) if str(entry.get("id")) == profile_id), None)
    credentials = profile or payload
    username, password = credentials.get("username"), credentials.get("password")
    if not username or not password:
        raise ValueError("Ange e-postadress och lösenord för Gree-kontot.")
    if action in {"discover", "restore"} and device_limit(payload) <= 0:
        return {"devices": [], "profile": {}}

    region = credentials.get("region") or "Europe"
    if region not in CloudDiscovery.list_servers():
        raise ValueError("Välj Gree-molnregion för kontot.")
    discovery = CloudDiscovery(username=username, password=password, server=region)
    bound_devices = []
    try:
        infos = await discovery.scan()
        selected = requested_ids(payload) if action == "restore" else None
        if action in {"discover", "restore"}:
            devices = []
            limit = device_limit(payload)
            for info in infos:
                if selected is not None and str(info.mac) not in selected:
                    continue
                if not info.online:
                    continue
                if action == "discover" and already_known(payload, "gree-cloud", info.mac):
                    continue
                device = await discovery.create_device(info)
                bound_devices.append(device)
                await device.bind()
                if not device.has_valid_state:
                    raise ConnectionError(f"Gree-enheten {info.name or info.mac} svarade inte på statusförfrågan.")
                devices.append(gree_cloud_device(device, info, profile_id))
                if len(devices) >= limit:
                    break
            return {"devices": devices, "profile": {}}

        item = payload.get("device") or {}
        device_id = str(item.get("id") or "")
        info = next((entry for entry in infos if str(entry.mac) == device_id), None)
        if info is None:
            raise ValueError("Gree-luftkonditioneringen hittades inte på kontot.")
        if not info.online:
            raise ConnectionError("Gree-luftkonditioneringen är offline i molntjänsten.")
        device = await discovery.create_device(info)
        bound_devices.append(device)
        await device.bind()
        if not device.has_valid_state:
            raise ConnectionError(f"Gree-enheten {info.name or info.mac} svarade inte på statusförfrågan.")

        value = payload.get("value")
        if action == "power":
            device.power = value == "on"
        elif action == "temperature":
            target = float(value)
            if device.temperature_units == 1:
                target = target * 9 / 5 + 32
            device.target_temperature = target
        elif action == "mode":
            if str(value) not in GREE_MODE_VALUES:
                raise ValueError("Det valda driftläget stöds inte av Gree-enheten.")
            device.mode = Mode(GREE_MODE_VALUES[str(value)]).value
        elif action == "fan":
            if str(value) not in GREE_FAN_VALUES:
                raise ValueError("Den valda fläkthastigheten stöds inte av Gree-enheten.")
            device.fan_speed = FanSpeed(GREE_FAN_VALUES[str(value)]).value
        else:
            raise ValueError(f"Gree-molnkommandot stöds inte: {action}")
        if action != "status":
            await device.push_state_update()
            await asyncio.sleep(1)
            await device.update_state()
        if not device.has_valid_state:
            raise ConnectionError(f"Gree-enheten {info.name or info.mac} svarade inte på statusförfrågan.")
        return gree_cloud_device(device, info, profile_id)
    finally:
        for device in bound_devices:
            await device.close()
        await discovery.close()


def thinq_mode(value):
    return {"air_dry": "dry", "dry": "dry", "fan_only": "fan", "fan": "fan", "heat_cool": "auto"}.get(str(value or "").lower(), str(value or "cool").lower())


def thinq_fan(value):
    return {"mid": "medium", "nature": "auto", "low": "low", "medium": "medium", "high": "high"}.get(str(value or "").lower(), "auto")


async def thinq_bridge(api, raw_device):
    from thinqconnect import AirConditionerDevice, DeviceType
    from thinqconnect.integration.homeassistant.api import HABridge

    info = raw_device.get("deviceInfo") or {}
    if info.get("deviceType") != DeviceType.AIR_CONDITIONER:
        return None
    device_id = str(raw_device.get("deviceId") or "")
    if not device_id:
        return None
    profile = await api.async_get_device_profile(device_id)
    constructor = {
        "thinq_api": api,
        "device_id": device_id,
        "device_type": info.get("deviceType"),
        "model_name": info.get("modelName"),
        "alias": info.get("alias"),
        "reportable": info.get("reportable"),
        "profile": profile,
        "energy_profile": None,
    }
    if info.get("groupId"):
        constructor["group_id"] = info["groupId"]
    return HABridge(AirConditionerDevice(**constructor))


async def thinq_items(api, limit, selected=None, known=None):
    from thinqconnect.integration.homeassistant.specification import ExtendedProperty

    if limit <= 0:
        return []
    raw_devices = await api.async_get_device_list() or []
    result = []
    for raw in raw_devices:
        info = raw.get("deviceInfo") or {}
        device_id = str(raw.get("deviceId") or "")
        if not device_id or (selected is not None and device_id not in selected):
            continue
        if known and already_known({"knownDevices": known}, "lg-thinq", device_id):
            continue
        bridge = await thinq_bridge(api, raw)
        if bridge is None:
            continue
        await bridge.fetch_data()
        indices = bridge.get_active_idx(ExtendedProperty.CLIMATE_AIR_CONDITIONER)
        if not indices:
            continue
        state = bridge.state_map[indices[0]]
        available_modes = list(dict.fromkeys(
            mode for mode in (thinq_mode(item) for item in state.hvac_modes)
            if mode in {"auto", "cool", "heat", "dry", "fan"}
        ))
        available_fans = list(dict.fromkeys(thinq_fan(item) for item in state.fan_modes))
        result.append({
            "id": device_id,
            "name": info.get("alias") or "LG ThinQ AC",
            "brand": "lg-thinq",
            "kind": "cloud",
            "state": "on" if state.is_on else "off",
            "target": state.target_temp or 22,
            "temperatureStep": state.step or 1,
            "minTarget": state.min,
            "maxTarget": state.max,
            "current": state.current_temp or 0,
            "mode": thinq_mode(state.hvac_mode) if state.is_on else "cool",
            "fan": thinq_fan(state.fan_mode),
            "availableModes": available_modes,
            "availableFans": available_fans,
            "_bridge": bridge,
            "_idx": indices[0],
        })
        if len(result) >= limit:
            break
    return result


def thinq_public_device(item):
    bridge, index = item.get("_bridge"), item.get("_idx")
    if bridge is not None and index in bridge.state_map:
        state = bridge.state_map[index]
        item = {
            **item,
            "state": "on" if state.is_on else "off",
            "target": state.target_temp or item.get("target") or 22,
            "temperatureStep": state.step or item.get("temperatureStep") or 1,
            "minTarget": state.min if state.min is not None else item.get("minTarget"),
            "maxTarget": state.max if state.max is not None else item.get("maxTarget"),
            "current": state.current_temp or 0,
            "mode": thinq_mode(state.hvac_mode) if state.is_on else "cool",
            "fan": thinq_fan(state.fan_mode),
        }
    if item.get("minTarget") is None:
        item.pop("minTarget", None)
    if item.get("maxTarget") is None:
        item.pop("maxTarget", None)
    return {key: value for key, value in item.items() if not key.startswith("_")}


async def thinq_request(payload, action):
    import uuid
    import aiohttp
    from thinqconnect import ThinQApi

    profile = next((entry for entry in payload.get("profiles", []) if entry.get("id") == (payload.get("id") if action in {"discover", "restore"} else payload.get("profileId"))), None)
    credentials = profile or (payload if action == "discover" else None)
    if not credentials or not credentials.get("token"):
        raise ValueError("LG ThinQ-token saknas i systemets nyckelring. Anslut kontot igen.")
    country = str(credentials.get("region") or "SE").upper()
    client_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"klimat-lg-thinq-{credentials.get('id', '')}"))
    async with aiohttp.ClientSession() as session:
        api = ThinQApi(session=session, access_token=credentials["token"], country_code=country, client_id=client_id)
        selected = requested_ids(payload) if action == "restore" else None
        items = await thinq_items(api, device_limit(payload), selected, payload.get("knownDevices") if action == "discover" else None)
        if action in {"discover", "restore"}:
            return {"devices": [thinq_public_device(item) for item in items], "profile": {}}
        device_id = str(payload.get("device", {}).get("id"))
        item = next((entry for entry in items if entry["id"] == device_id), None)
        if item is None:
            item = next((entry for entry in await thinq_items(api, 6, {device_id}) if entry["id"] == device_id), None)
        if item is None:
            raise ValueError("LG ThinQ-luftkonditioneringen hittades inte på kontot.")
        bridge, index = item["_bridge"], item["_idx"]
        action_name, value = payload.get("action"), payload.get("value")
        if action_name == "status":
            pass
        elif action_name == "power":
            await bridge.async_turn_on(index) if value == "on" else await bridge.async_turn_off(index)
        elif action_name == "temperature":
            await bridge.async_set_target_temperature(index, float(value))
        elif action_name == "mode":
            state = bridge.state_map[index]
            raw = next((option for option in state.hvac_modes if thinq_mode(option) == value), None)
            if raw is None:
                raise ValueError("Det valda driftläget stöds inte av den här LG-enheten.")
            await bridge.async_set_hvac_mode(index, raw)
        elif action_name == "fan":
            state = bridge.state_map[index]
            raw = next((option for option in state.fan_modes if thinq_fan(option) == value), None)
            if raw is None:
                raise ValueError("Den valda fläkthastigheten stöds inte av den här LG-enheten.")
            await bridge.async_set_fan_mode(index, raw)
        else:
            raise ValueError(f"LG ThinQ-kommandot stöds inte: {action_name}")
        await bridge.fetch_data()
        return thinq_public_device({**item, "_idx": index})


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


def echonet_exchange(host, instance, service, properties):
    import os
    import socket
    import time

    destination = bytes((0x01, 0x30, instance))
    transaction_id = os.urandom(2)
    request = bytearray((0x10, 0x81))
    request.extend(transaction_id)
    request.extend(bytes.fromhex("05ff01"))
    request.extend(destination)
    request.extend((service, len(properties)))
    for code, value in properties:
        value = bytes(value)
        request.extend((code, len(value)))
        request.extend(value)

    try:
        expected_ip = socket.gethostbyname(host)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(3)
            # ECHONET Lite uses UDP port 3610 for requests and responses.
            # Bind the standard client port so adapters that reply to 3610 work.
            sock.bind(("", 3610))
            sock.sendto(request, (expected_ip, 3610))
            deadline = time.monotonic() + 3
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout
                sock.settimeout(remaining)
                packet, address = sock.recvfrom(4096)
                if address[0] != expected_ip or len(packet) < 12:
                    continue
                frame = bytearray(packet)
                if frame[0:2] != b"\x10\x81" or frame[2:4] != transaction_id:
                    continue
                break
    except (socket.timeout, TimeoutError) as error:
        raise ConnectionError(f"Inget svar från {host}:3610/udp. Kontrollera att ECHONET Lite är aktiverat och att adaptern finns på samma lokala nätverk.") from error
    except OSError as error:
        raise ConnectionError(f"Kunde inte använda ECHONET Lite via UDP-port 3610 mot {host}. Kontrollera att adaptern stöder protokollet och att funktionen är aktiverad i Mitsubishi-appen. En annan ECHONET-klient kan redan använda porten.") from error

    if frame[10] not in (0x71, 0x72):
        if frame[10] in (0x51, 0x52):
            raise ValueError("Mitsubishi-adaptern avvisade ECHONET Lite-förfrågan. Kontrollera att ECHONET Lite är aktiverat.")
        raise ConnectionError(f"Mitsubishi-adaptern svarade med en okänd ECHONET Lite-tjänst: 0x{frame[10]:02x}.")

    result = {}
    cursor = 12
    for _ in range(frame[11]):
        if cursor + 2 > len(frame):
            raise ConnectionError("Mitsubishi-adaptern skickade ett ofullständigt ECHONET Lite-svar.")
        code, length = frame[cursor], frame[cursor + 1]
        cursor += 2
        if cursor + length > len(frame):
            raise ConnectionError("Mitsubishi-adaptern skickade ett ofullständigt ECHONET Lite-svar.")
        result[code] = bytes(frame[cursor : cursor + length])
        cursor += length
    return result


def echonet_unit_state(host, instance=1, profile_id=None):
    values = echonet_exchange(host, instance, 0x62, [(code, b"") for code in (0x80, 0xB0, 0xB3, 0xBB, 0xA0)])
    if 0x80 not in values or not values[0x80] or 0xB0 not in values or not values[0xB0]:
        raise ValueError("Mitsubishi-adaptern returnerade inte status för luftkonditioneringen.")
    mode = {0x40: "auto", 0x41: "auto", 0x42: "cool", 0x43: "heat", 0x44: "dry", 0x45: "fan"}.get(values[0xB0][0], "auto")
    target_data = values.get(0xB3, b"")
    target = target_data[0] if target_data and target_data[0] not in (0x7E, 0x7F, 0x80, 0xFD) else 22
    room_data = values.get(0xBB, b"")
    raw_room = room_data[0] if room_data else 0
    current = raw_room - 256 if raw_room >= 0x80 else raw_room
    if raw_room in (0x7E, 0x7F, 0x80, 0xFD):
        current = 0
    fan_data = values.get(0xA0, b"")
    raw_fan = fan_data[0] if fan_data else 0x41
    fan = "auto" if raw_fan == 0x41 or not 0x31 <= raw_fan <= 0x38 else "low" if raw_fan <= 0x32 else "medium" if raw_fan <= 0x36 else "high"
    return {
        "id": f"{host}:0130{instance:02x}",
        "name": f"Mitsubishi Electric {host}",
        "brand": "mitsubishi-echonet",
        "kind": "direct",
        "state": "on" if values[0x80][0] == 0x30 else "off",
        "target": target,
        "temperatureStep": 1,
        "minTarget": 16,
        "maxTarget": 31,
        "current": current,
        "mode": mode,
        "fan": fan,
        "availableModes": ["auto", "cool", "heat", "dry", "fan"],
        "availableFans": ["low", "medium", "high"],
        "connection": {"profile_id": profile_id, "endpoint": host, "instance": instance},
    }


def mitsubishi_endpoint(payload):
    device = payload.get("device") or {}
    connection = device.get("connection") or {}
    profile_id = payload.get("profileId") or payload.get("id")
    profile = next((item for item in payload.get("profiles", []) if item.get("id") == profile_id), {})
    endpoint = connection.get("endpoint") or profile.get("endpoint") or payload.get("endpoint")
    if not endpoint:
        raise ValueError("Ange Mitsubishi-adapterns IP-adress.")
    return str(endpoint).strip().strip("[]")


def mitsubishi_echonet_connect(payload, action="discover"):
    host = mitsubishi_endpoint(payload)
    profile_id = payload.get("id") or payload.get("profileId")
    connection = (payload.get("device") or {}).get("connection") or {}
    instance = int(connection.get("instance") or 1)
    device = echonet_unit_state(host, instance, profile_id)
    if action == "discover" and already_known(payload, "mitsubishi-echonet", device["id"]):
        return {"devices": [], "profile": {}}
    if action in {"discover", "restore"}:
        return {"devices": [device][: device_limit(payload)], "profile": {}}
    return device


def mitsubishi_echonet_command(payload):
    host = mitsubishi_endpoint(payload)
    connection = (payload.get("device") or {}).get("connection") or {}
    instance = int(connection.get("instance") or 1)
    action, value = payload.get("action"), payload.get("value")
    properties = []
    if action == "power":
        properties = [(0x80, b"\x30" if value == "on" else b"\x31")]
    elif action == "temperature":
        target = max(16, min(31, round(float(value))))
        properties = [(0xB3, bytes((target,)))]
    elif action == "mode":
        modes = {"auto": 0x41, "cool": 0x42, "heat": 0x43, "dry": 0x44, "fan": 0x45}
        if str(value) not in modes:
            raise ValueError("Det valda driftläget stöds inte av ECHONET Lite.")
        properties = [(0x80, b"\x30"), (0xB0, bytes((modes[str(value)],)))]
    elif action == "fan":
        fans = {"auto": 0x41, "low": 0x31, "medium": 0x35, "high": 0x38}
        if str(value) not in fans:
            raise ValueError("Den valda fläkthastigheten stöds inte av ECHONET Lite.")
        properties = [(0xA0, bytes((fans[str(value)],)))]
    elif action != "status":
        raise ValueError(f"Mitsubishi ECHONET Lite-kommandot stöds inte: {action}")
    if properties:
        echonet_exchange(host, instance, 0x61, properties)
    return echonet_unit_state(host, instance, payload.get("profileId"))


def mitsubishi_local_state(controller, host, profile_id):
    state = controller.fetch_status()
    general = state.general
    if general is None:
        raise ValueError("Mitsubishi-adaptern returnerade ingen status för luftkonditioneringen.")
    mode = {"AUTO": "auto", "HEATER": "heat", "DEHUM": "dry", "COOLER": "cool", "FAN": "fan"}.get(
        getattr(general.drive_mode, "name", ""), "auto"
    )
    fan_name = getattr(general.wind_speed, "name", "AUTO")
    fan = {"AUTO": "auto", "S1": "low", "S2": "low", "S3": "medium", "S4": "high", "FULL": "high"}.get(fan_name, "auto")
    vertical = getattr(getattr(general, "vertical_wind_direction", None), "name", None)
    horizontal = getattr(getattr(general, "horizontal_wind_direction", None), "name", None)
    return {
        "id": host,
        "name": f"Mitsubishi Electric {host}",
        "brand": "mitsubishi-local",
        "kind": "direct",
        "state": "on" if getattr(general.power_on_off, "name", "OFF") == "ON" else "off",
        "target": general.temperature,
        "temperatureStep": 0.5,
        "minTarget": 16,
        "maxTarget": 31,
        "current": state.sensors.room_temperature if state.sensors else 0,
        "mode": mode,
        "fan": fan,
        "verticalVane": vertical,
        "horizontalVane": horizontal,
        "availableModes": ["auto", "cool", "heat", "dry", "fan"],
        "availableFans": ["low", "medium", "high"],
        "connection": {"profile_id": profile_id, "endpoint": host},
    }


def mitsubishi_local_request(payload):
    import time

    from pymitsubishi import MitsubishiController
    from pymitsubishi.mitsubishi_parser import DriveMode, HorizontalWindDirection, VerticalWindDirection, WindSpeed

    host = mitsubishi_endpoint(payload)
    profile_id = payload.get("profileId") or payload.get("id")
    controller = MitsubishiController.create(host)
    try:
        action, value = payload.get("action"), payload.get("value")
        if action in {"power", "temperature", "mode", "fan", "verticalVane", "horizontalVane"}:
            controller.fetch_status()
            if action == "power":
                controller.set_power(value == "on")
            elif action == "temperature":
                controller.set_temperature(max(16, min(31, float(value))))
            elif action == "mode":
                modes = {"auto": DriveMode.AUTO, "heat": DriveMode.HEATER, "dry": DriveMode.DEHUM, "cool": DriveMode.COOLER, "fan": DriveMode.FAN}
                if str(value) not in modes:
                    raise ValueError("Det valda driftläget stöds inte av Mitsubishi-adaptern.")
                controller.set_mode(modes[str(value)])
            elif action == "fan":
                fans = {"auto": WindSpeed.AUTO, "low": WindSpeed.S2, "medium": WindSpeed.S3, "high": WindSpeed.FULL}
                if str(value) not in fans:
                    raise ValueError("Den valda fläkthastigheten stöds inte av Mitsubishi-adaptern.")
                controller.set_fan_speed(fans[str(value)])
            elif action == "verticalVane":
                if str(value) not in VerticalWindDirection.__members__:
                    raise ValueError("Det valda vertikala lamelläget stöds inte av Mitsubishi-adaptern.")
                controller.set_vertical_vane(VerticalWindDirection[str(value)])
            elif action == "horizontalVane":
                if str(value) not in HorizontalWindDirection.__members__:
                    raise ValueError("Det valda horisontella lamelläget stöds inte av Mitsubishi-adaptern.")
                controller.set_horizontal_vane(HorizontalWindDirection[str(value)])
            time.sleep(controller.wait_time_after_command)
        device = mitsubishi_local_state(controller, host, profile_id)
        if action == "discover" and already_known(payload, "mitsubishi-local", device["id"]):
            return {"devices": [], "profile": {}}
        if action in {"discover", "restore"}:
            return {"devices": [device][: device_limit(payload)], "profile": {}}
        return device
    finally:
        controller.api.close()


def mitsubishi_discover(payload):
    """Discover Mitsubishi Wi-Fi adapters using their local adapter protocol."""
    return mitsubishi_local_request(payload)


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
    if brand == "mitsubishi-local" and action == "discover":
        return mitsubishi_discover(payload)
    if brand == "mitsubishi-local" and action in {"status", "restore", "power", "temperature", "mode", "fan", "verticalVane", "horizontalVane"}:
        return mitsubishi_local_request(payload)
    if brand == "mitsubishi-echonet" and action == "status":
        return mitsubishi_echonet_connect(payload, "status")
    if brand == "mitsubishi-echonet" and action in {"power", "temperature", "mode", "fan"}:
        return mitsubishi_echonet_command(payload)
    if action == "status" and brand == "melcloud":
        return await melcloud_status(payload)
    if action == "status" and brand == "melcloud-home":
        return await melcloud_home_command({**payload, "action": "status"})
    if action == "status" and brand == "toshiba-cloud":
        return await toshiba_cloud_request(payload, action)
    if action == "status" and brand == "gree-cloud":
        return await gree_cloud_request(payload, action)
    if action == "status" and brand == "lg-thinq":
        return await thinq_request(payload, action)
    if action == "status" and brand == "panasonic-cloud":
        return await panasonic_request(payload, action)
    if action == "status" and brand == "samsung-smartthings":
        return await samsung_request(payload, action)
    if action == "status" and brand == "daikin":
        return await daikin_status(payload)
    if action == "discover":
        if brand == "mitsubishi-echonet":
            return mitsubishi_echonet_connect(payload)
        if brand == "melcloud":
            return await melcloud_connect(payload)
        if brand == "melcloud-home":
            return await melcloud_home_connect(payload)
        if brand == "toshiba-cloud":
            return await toshiba_cloud_request(payload, action)
        if brand == "gree-cloud":
            return await gree_cloud_request(payload, action)
        if brand == "lg-thinq":
            return await thinq_request(payload, action)
        if brand == "panasonic-cloud":
            return await panasonic_request(payload, action)
        if brand == "samsung-smartthings":
            return await samsung_request(payload, action)
        if brand == "daikin":
            return await daikin_connect(payload)
        if brand in MIDEA_BRANDS:
            return midea_connect(payload)
        raise ValueError(f"Integrationen {brand} stöds inte ännu.")
    if action == "restore":
        if brand == "mitsubishi-echonet":
            return mitsubishi_echonet_connect(payload, action)
        if brand == "melcloud":
            return await melcloud_restore(payload)
        if brand == "melcloud-home":
            return await melcloud_home_connect(payload, action)
        if brand == "toshiba-cloud":
            return await toshiba_cloud_request(payload, action)
        if brand == "gree-cloud":
            return await gree_cloud_request(payload, action)
        if brand == "lg-thinq":
            return await thinq_request(payload, action)
        if brand == "panasonic-cloud":
            return await panasonic_request(payload, action)
        if brand == "samsung-smartthings":
            return await samsung_request(payload, action)
        if brand == "daikin":
            return await daikin_restore(payload)
        if brand in MIDEA_BRANDS:
            return midea_connect(payload)
        raise ValueError(f"Integrationen {brand} stöds inte ännu.")
    if brand == "melcloud":
        return await melcloud_command(payload)
    if brand == "melcloud-home":
        return await melcloud_home_command(payload)
    if brand == "toshiba-cloud":
        return await toshiba_cloud_request(payload, action)
    if brand == "gree-cloud":
        return await gree_cloud_request(payload, action)
    if brand == "lg-thinq":
        return await thinq_request(payload, action)
    if brand == "panasonic-cloud":
        return await panasonic_request(payload, action)
    if brand == "samsung-smartthings":
        return await samsung_request(payload, action)
    if brand == "daikin":
        return await daikin_command(payload)
    if brand in MIDEA_BRANDS:
        return midea_command(payload)
    raise ValueError(f"Integrationen {brand} stöds inte ännu.")
