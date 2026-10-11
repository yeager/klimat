import { useEffect, useMemo, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { detectLanguage, languageNames, languages, messages, type Language, type MessageKey } from "./i18n";
import "./App.css";

type ConnectionKind = "direct" | "cloud";
type Brand = { id: string; name: string; kind: ConnectionKind; description: string; status: "ready" | "planned"; tint: string };
type Device = { id: string; name: string; brand: string; kind: ConnectionKind; state: string; target: number; current: number; mode: string; fan: string; online?: boolean; profileId?: string; connection?: Record<string, unknown> };
type Profile = { id: string; kind: ConnectionKind; brand: string; label: string; endpoint: string; region: string };
type ConnectionDraft = { username?: string; password?: string; endpoint?: string; region?: string; token?: string; key?: string };
const brands: Brand[] = [
  { id: "melcloud", name: "Mitsubishi Electric · MELCloud", kind: "cloud", description: "Styrning via MELCloud-kontot. Passar MELCloud-anslutna luftvärmepumpar.", status: "ready", tint: "#e6f0e8" },
  { id: "midea", name: "Midea och kompatibla", kind: "direct", description: "Lokal styrning via Midea-protokollet.", status: "ready", tint: "#fff0e7" },
  { id: "comfee", name: "Comfee", kind: "direct", description: "Comfee-enheter som använder Midea-protokollet.", status: "ready", tint: "#eaf2fb" },
  { id: "toshiba-lifestyle", name: "Toshiba Lifestyle", kind: "direct", description: "Toshiba Lifestyle-enheter som använder Midea-protokollet.", status: "ready", tint: "#f8ebeb" },
  { id: "daikin", name: "Daikin", kind: "direct", description: "Lokal styrning av Daikin-enheter med äldre kompatibla Wi-Fi-adaptrar.", status: "ready", tint: "#e9effb" },
  { id: "gree", name: "Gree", kind: "direct", description: "Lokal styrning av Gree-enheter via det lokala nätverket.", status: "ready", tint: "#e8f3ee" },
  { id: "melcloud-home", name: "Mitsubishi Electric · MELCloud Home", kind: "cloud", description: "Separat molntjänst för enheter som använder MELCloud Home.", status: "planned", tint: "#e6f0e8" },
  { id: "lg-thinq", name: "LG ThinQ", kind: "cloud", description: "Styrning via LG ThinQ-kontot.", status: "planned", tint: "#f8e9ef" },
  { id: "ir", name: "IR-sändare", kind: "direct", description: "Skicka fjärrkontrollskommandon via en nätverksansluten IR-sändare.", status: "planned", tint: "#f1eee5" },
];
const MAX_DEVICES = 6;
const deviceKey = (device: Pick<Device, "brand" | "id">) => `${device.brand}:${device.id}`;
const modes = [{ id: "cool", label: "Kyla", icon: "❄" }, { id: "heat", label: "Värme", icon: "☀" }, { id: "auto", label: "Auto", icon: "⟳" }, { id: "dry", label: "Avfuktning", icon: "◌" }, { id: "fan", label: "Fläkt", icon: "≋" }];

function App() {
  const [profiles, setProfiles] = useState<Profile[]>(() => JSON.parse(localStorage.getItem("klimat-profiles") || "[]"));
  const [devices, setDevices] = useState<Device[]>(() => (JSON.parse(localStorage.getItem("klimat-devices") || "[]") as Device[]).map((device) => ({ ...device, online: undefined })));
  const [selected, setSelected] = useState("");
  const [showAdd, setShowAdd] = useState(false);
  const [category, setCategory] = useState<ConnectionKind>("direct");
  const [brandId, setBrandId] = useState("midea");
  const [profile, setProfile] = useState<ConnectionDraft>({ region: "eu" });
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [restored, setRestored] = useState(false);
  const [language, setLanguage] = useState<Language>(detectLanguage);
  const t = (key: MessageKey) => messages[language][key];
  const active = useMemo(() => devices.find((device) => deviceKey(device) === selected) || devices[0], [devices, selected]);
  const chosenBrand = brands.find((brand) => brand.id === brandId) || brands[0];
  const deviceCount = new Set(devices.map(deviceKey)).size;
  const temperatureStep = active?.brand === "gree" ? 1 : 0.5;

  useEffect(() => { localStorage.setItem("klimat-profiles", JSON.stringify(profiles)); }, [profiles]);
  useEffect(() => { localStorage.setItem("klimat-devices", JSON.stringify(devices)); }, [devices]);
  useEffect(() => { localStorage.setItem("klimat-language", language); }, [language]);
  useEffect(() => {
    if (!message || showAdd) return;
    const timeout = window.setTimeout(() => setMessage(""), 3500);
    return () => window.clearTimeout(timeout);
  }, [message, showAdd]);
  useEffect(() => {
    if (restored || !profiles.length) { setRestored(true); return; }
    let cancelled = false;
    (async () => {
      const recovered: Device[] = [];
      const savedDevices = JSON.parse(localStorage.getItem("klimat-devices") || "[]") as Device[];
      const hasSavedDevices = savedDevices.length > 0;
      let restoreAttempts = 0;
      for (const saved of profiles) {
        if (recovered.length >= MAX_DEVICES) break;
        const remaining = MAX_DEVICES - (hasSavedDevices ? restoreAttempts : recovered.length);
        if (remaining <= 0) break;
        const profileDevices = savedDevices.filter((device) => device.profileId === saved.id);
        const deviceIds = profileDevices.map((device) => String(device.id)).slice(0, remaining);
        if (hasSavedDevices && !profileDevices.length) continue;
        // Profiles without a locally cached device list can still recover up to the global limit.
        if (savedDevices.some((device) => device.profileId === saved.id) && !deviceIds.length) continue;
        if (hasSavedDevices) restoreAttempts += deviceIds.length;
        try {
          const response = await invoke<{ devices: Device[] }>("climate_request", { payload: { ...saved, id: saved.id, profiles, ...(profileDevices.length ? { deviceIds } : {}), maxDevices: remaining, action: "restore" } });
          const known = new Set(recovered.map(deviceKey));
          recovered.push(...response.devices.map((device) => ({ ...device, online: true, brand: saved.brand, profileId: saved.id, connection: { ...device.connection, profileId: saved.id } })).filter((device) => !known.has(deviceKey(device))).slice(0, remaining));
        } catch { /* Offline devices stay saved and can be retried from settings. */ }
      }
      if (!cancelled) {
        const restoredKeys = new Set(recovered.map(deviceKey));
        const offline = savedDevices.filter((device) => !restoredKeys.has(deviceKey(device)));
        // Keep saved devices first so offline ones cannot be crowded out by cloud results.
        const unique = [...new Map([...offline.map((device) => ({ ...device, online: false })), ...recovered].map((device) => [deviceKey(device), device])).values()].slice(0, MAX_DEVICES);
        setDevices(unique);
        if (unique.length) setSelected((current) => unique.some((device) => deviceKey(device) === current) ? current : deviceKey(unique[0]));
      }
      if (!cancelled) setRestored(true);
    })();
    return () => { cancelled = true; };
  }, [profiles, restored]);

  useEffect(() => {
    if (!restored || !active) return;
    let cancelled = false;
    let refreshing = false;
    const refresh = async () => {
      if (cancelled || refreshing || document.visibilityState !== "visible") return;
      refreshing = true;
      try {
        const updated: Partial<Device> = await invoke("climate_request", { payload: { kind: active.kind, brand: active.brand, action: "status", device: active, profileId: active.profileId, profiles } });
        if (!cancelled) setDevices((current) => current.map((device) => deviceKey(device) === deviceKey(active) ? { ...device, ...updated, online: true } : device));
      } catch { if (!cancelled) setDevices((current) => current.map((device) => deviceKey(device) === deviceKey(active) ? { ...device, online: false } : device)); }
      finally { refreshing = false; }
    };
    const pollInterval = active.brand === "melcloud" ? 15 * 60_000 : 30_000;
    const timer = window.setInterval(() => void refresh(), pollInterval);
    document.addEventListener("visibilitychange", refresh);
    void refresh();
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [active?.brand, active?.id, active?.kind, active?.profileId, profiles, restored]);

  function beginAdd(kind: ConnectionKind) { if (deviceCount >= MAX_DEVICES) { setMessage(t("deviceLimit")); return; } setCategory(kind); setBrandId(kind === "cloud" ? "melcloud" : "midea"); setProfile({ region: "eu" }); setMessage(""); setShowAdd(true); }
  async function connect() {
    if (deviceCount >= MAX_DEVICES) { setMessage(t("deviceLimit")); return; }
    setBusy(true); setMessage("");
    try {
      const next = { id: crypto.randomUUID(), kind: category, brand: brandId, label: chosenBrand.name, endpoint: profile.endpoint || "", region: profile.region || "eu", username: profile.username || "", password: profile.password || "", token: profile.token || "", key: profile.key || "" };
      const response = await invoke<{ devices: Device[]; profile?: Partial<Profile> }>("climate_request", { payload: { ...next, profiles, knownDevices: devices.map(({ id, brand }) => ({ id: String(id), brand })), kind: category, brand: brandId, maxDevices: MAX_DEVICES - deviceCount, action: "discover" } });
      const knownIds = new Set(devices.map(deviceKey));
      const newDevices = response.devices.map((device) => ({ ...device, online: true, brand: brandId, profileId: next.id, connection: { ...device.connection, profileId: next.id } })).filter((device) => !knownIds.has(deviceKey(device))).slice(0, Math.max(0, MAX_DEVICES - deviceCount));
      const savedProfile: Profile = { id: next.id, kind: category, brand: brandId, label: chosenBrand.name, endpoint: next.endpoint, region: next.region };
      if (newDevices.length || !devices.length) setProfiles((current) => [...current, savedProfile]);
      if (response.devices.length > newDevices.length && deviceCount + newDevices.length >= MAX_DEVICES) setMessage(t("deviceLimit"));
      if (newDevices.length) { setDevices((current) => [...current, ...newDevices]); setSelected(deviceKey(newDevices[0])); setShowAdd(false); setMessage(`${newDevices.length} ${t("found")}`); }
      else setMessage(t("savedDiscovery"));
    } catch (err) { setMessage(err instanceof Error ? err.message : t("connectFailed")); }
    finally { setBusy(false); }
  }

  async function command(action: string, value?: string | number) {
    if (!active) return;
    setBusy(true); setMessage("");
    try {
      const updated: Partial<Device> = await invoke("climate_request", { payload: { kind: active.kind, brand: active.brand, action, value, device: active, profileId: active.profileId, profiles } });
      setDevices((current) => current.map((device) => deviceKey(device) === deviceKey(active) ? { ...device, ...updated, online: true } : device));
      setMessage(t("sent"));
    } catch (err) { setDevices((current) => current.map((device) => deviceKey(device) === deviceKey(active) ? { ...device, online: false } : device)); setMessage(err instanceof Error ? err.message : t("commandFailed")); }
    finally { setBusy(false); }
  }

  async function forgetActiveDevice() {
    if (!active || !window.confirm(t("confirmRemoveDevice"))) return;
    const profileId = active.profileId;
    const remaining = devices.filter((device) => deviceKey(device) !== deviceKey(active));
    const profileStillUsed = Boolean(profileId && remaining.some((device) => device.profileId === profileId));
    setBusy(true); setMessage("");
    try {
      await invoke("climate_request", { payload: { action: "forget", brand: active.brand, profileId, device: active, deleteProfile: !profileStillUsed } });
      setDevices(remaining);
      setSelected(remaining[0] ? deviceKey(remaining[0]) : "");
      if (profileId && !profileStillUsed) setProfiles((current) => current.filter((profile) => profile.id !== profileId));
      setMessage(t("removedDevice"));
    } catch (err) { setMessage(err instanceof Error ? err.message : t("commandFailed")); }
    finally { setBusy(false); }
  }

  return <main className="app-shell">
    <header className="topbar"><div className="brand"><h1>Klimat</h1></div><div className="topbar-actions"><select className="language-select" aria-label="Language" value={language} onChange={(e) => setLanguage(e.target.value as Language)}>{languages.map((lang) => <option key={lang} value={lang}>{languageNames[lang]}</option>)}</select><button className="icon-button" aria-label={t("add")} title={deviceCount >= MAX_DEVICES ? t("deviceLimit") : t("add")} disabled={deviceCount >= MAX_DEVICES} onClick={() => setShowAdd((value) => !value)}>＋</button></div></header>
    {showAdd ? <section className="setup-card"><button className="back-link" onClick={() => setShowAdd(false)}>← {t("back")}</button><span className="eyebrow">{t("connection")}</span><h2>{t("addAc")}</h2><div className="kind-tabs"><button className={category === "direct" ? "active" : ""} onClick={() => { setCategory("direct"); setBrandId("midea"); setProfile({ region: "eu" }); }}>{t("local")}</button><button className={category === "cloud" ? "active" : ""} onClick={() => { setCategory("cloud"); setBrandId("melcloud"); setProfile({ region: "eu" }); }}>{t("cloud")}</button></div><div className="brand-grid" role="group" aria-label={t("manufacturer")}>{brands.filter((brand) => brand.kind === category).map((brand) => <button type="button" key={brand.id} className={`brand-option ${brandId === brand.id ? "selected" : ""}`} aria-pressed={brandId === brand.id} onClick={() => setBrandId(brand.id)}><span className="manufacturer-icon" style={{ "--manufacturer-tint": brand.tint } as React.CSSProperties}><img src={`/manufacturer-icons/${brand.id}.svg`} alt="" /></span><span className="brand-option-name">{brand.name}</span>{brand.status === "planned" && <small>{t("comingLater")}</small>}</button>)}</div>{chosenBrand.status === "ready" ? <>
      {brandId === "melcloud" && <><label>{t("email")}<input type="email" value={profile.username || ""} onChange={(e) => setProfile({ ...profile, username: e.target.value })} placeholder={t("melEmail")} /></label><label>{t("password")}<input type="password" value={profile.password || ""} onChange={(e) => setProfile({ ...profile, password: e.target.value })} placeholder={t("melPassword")} /></label><p className="hint">{t("credentialHint")}</p></>}
      {["midea", "comfee", "toshiba-lifestyle"].includes(brandId) && <><label>{t("ipAddress")}<input value={profile.endpoint || ""} onChange={(e) => setProfile({ ...profile, endpoint: e.target.value })} placeholder="192.168.1.42" /></label><label>{t("deviceToken")}<input value={profile.token || ""} onChange={(e) => setProfile({ ...profile, token: e.target.value })} placeholder={t("hexToken")} /></label><label>{t("deviceKey")}<input type="password" value={profile.key || ""} onChange={(e) => setProfile({ ...profile, key: e.target.value })} placeholder={t("hexKey")} /></label><p className="hint">{t("localHint")}</p></>}
      {brandId === "daikin" && <><label>{t("ipAddress")}<input value={profile.endpoint || ""} onChange={(e) => setProfile({ ...profile, endpoint: e.target.value })} placeholder="192.168.1.42" /></label><p className="hint">{t("daikinHint")}</p></>}
      {brandId === "gree" && <p className="hint">{t("greeHint")}</p>}
      <button className="primary-button" disabled={busy || deviceCount >= MAX_DEVICES || (brandId === "melcloud" && (!profile.username || !profile.password)) || (["midea", "comfee", "toshiba-lifestyle", "daikin"].includes(brandId) && !profile.endpoint)} onClick={() => void connect()}>{busy ? t("connecting") : t("connectDiscover")}</button>
    </> : <div className="planned-card"><b>{t("planned")}</b><p>{t("plannedHint")}</p></div>}{message && <p className="feedback">{message}</p>}</section> : <>
      {active ? <>{devices.length > 1 && <div className="device-tabs">{devices.map((device) => { const brand = brands.find((item) => item.id === device.brand); return <button key={deviceKey(device)} className={`device-tab ${deviceKey(active) === deviceKey(device) ? "active" : ""}`} onClick={() => setSelected(deviceKey(device))} title={device.name}><span className="device-brand-icon" style={{ "--manufacturer-tint": brand?.tint || "#e9efeb" } as React.CSSProperties}><img src={`/manufacturer-icons/${device.brand}.svg`} alt="" /></span>{device.name}</button>; })}</div>}<section className="control-card"><div className="card-top"><div><h3>{active.name}</h3><span className="eyebrow">{active.kind === "cloud" ? t("cloud").toUpperCase() : t("local").toUpperCase()} · {active.state === "on" ? t("on") : t("off")}</span></div><div className="card-actions"><button className="remove-device" disabled={busy} aria-label={t("removeDevice")} title={t("removeDevice")} onClick={() => void forgetActiveDevice()}>×</button><button className={`power ${active.state === "on" ? "on" : ""}`} disabled={busy} aria-label={active.state === "on" ? t("off") : t("on")} onClick={() => void command("power", active.state === "on" ? "off" : "on")}>⏻</button></div></div><div className="temperature"><span>{active.current}<small>°</small></span><div><b>{modes.find((mode) => mode.id === active.mode)?.label || t("off")}</b><small>{t("indoor")}</small></div></div><div className="target-row"><div><span className="eyebrow">{t("setpoint")}</span><strong>{active.target}<small>°C</small></strong></div><div className="stepper"><button disabled={busy} aria-label={t("lowerTemp")} onClick={() => void command("temperature", active.target - temperatureStep)}>−</button><button disabled={busy} aria-label={t("raiseTemp")} onClick={() => void command("temperature", active.target + temperatureStep)}>＋</button></div></div><div className="control-divider"/><div className="mode-grid">{modes.map((mode) => <button key={mode.id} disabled={busy} aria-label={t(mode.id as MessageKey)} onClick={() => void command("mode", mode.id)} className={`mode-button ${active.mode === mode.id ? "selected" : ""}`}><span>{mode.icon}</span>{t(mode.id as MessageKey)}</button>)}</div><label className="select-label">{t("fan")}<select aria-label={t("fan")} value={active.fan} disabled={busy} onChange={(e) => void command("fan", e.target.value)}><option value="auto">{t("auto")}</option><option value="low">{t("low")}</option><option value="medium">{t("medium")}</option><option value="high">{t("high")}</option></select></label></section></> : <section className="empty-card"><h3>{t("emptyTitle")}</h3><p>{t("emptyHint")}</p><div className="connection-options"><button onClick={() => beginAdd("direct")}><span>⌁</span><b>{t("local")}</b><small>{t("localBrand")}</small></button><button onClick={() => beginAdd("cloud")}><span>☁</span><b>{t("cloud")}</b><small>{t("cloudBrand")}</small></button></div></section>}
    </>}
    <footer><span className={`status-light ${active?.online === false ? "offline" : active?.online === undefined ? "checking" : ""}`} aria-hidden="true" />{message && !showAdd ? message : active ? `${active.brand} · ${busy ? t("sending") : active.online === false ? t("offline") : active.online === true ? t("connected") : t("checking")}` : t("localStorage")}</footer>
  </main>;
}

export default App;
