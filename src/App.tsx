import { useEffect, useMemo, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { openUrl } from "@tauri-apps/plugin-opener";
import { detectLanguage, languageNames, languages, messages, type Language, type MessageKey } from "./i18n";
import "./App.css";

type ConnectionKind = "direct" | "cloud";
type Brand = { id: string; name: string; kind: ConnectionKind; description: string; status: "ready" | "planned"; tint: string };
type Device = { id: string; name: string; brand: string; kind: ConnectionKind; state: string; target: number; current: number; mode: string; fan: string; verticalVane?: string | null; horizontalVane?: string | null; temperatureStep?: number; minTarget?: number; maxTarget?: number; availableModes?: string[]; availableFans?: string[]; online?: boolean; profileId?: string; connection?: Record<string, unknown> };
type Profile = { id: string; kind: ConnectionKind; brand: string; label: string; endpoint: string; region: string };
type ConnectionDraft = { username?: string; password?: string; endpoint?: string; region?: string; token?: string; key?: string; otp?: string };
const brands: Brand[] = [
  { id: "mitsubishi-local", name: "Mitsubishi Electric (lokalt)", kind: "direct", description: "Lokal styrning via Wi-Fi-adaptern.", status: "ready", tint: "#e6f0e8" },
  { id: "melcloud", name: "MELCloud Classic", kind: "cloud", description: "Mitsubishi Electric · det klassiska MELCloud-kontot.", status: "ready", tint: "#e6f0e8" },
  { id: "midea", name: "Midea och kompatibla", kind: "direct", description: "Lokal styrning via Midea-protokollet.", status: "ready", tint: "#fff0e7" },
  { id: "comfee", name: "Comfee", kind: "direct", description: "Comfee-enheter som använder Midea-protokollet.", status: "ready", tint: "#eaf2fb" },
  { id: "toshiba-lifestyle", name: "Toshiba Lifestyle", kind: "direct", description: "Toshiba Lifestyle-enheter som använder Midea-protokollet.", status: "ready", tint: "#f8ebeb" },
  { id: "daikin", name: "Daikin", kind: "direct", description: "Lokal styrning av Daikin-enheter med äldre kompatibla Wi-Fi-adaptrar.", status: "ready", tint: "#e9effb" },
  { id: "gree", name: "Gree", kind: "direct", description: "Lokal styrning av Gree-enheter via det lokala nätverket.", status: "ready", tint: "#e8f3ee" },
  { id: "melcloud-home", name: "MELCloud Home", kind: "cloud", description: "Mitsubishi Electric · den nyare, separata MELCloud Home-tjänsten.", status: "ready", tint: "#e6f0e8" },
  { id: "lg-thinq", name: "LG ThinQ", kind: "cloud", description: "Styrning via LG ThinQ Connect.", status: "ready", tint: "#f8e9ef" },
  { id: "panasonic-cloud", name: "Panasonic Comfort Cloud", kind: "cloud", description: "Styrning via Panasonic Comfort Cloud.", status: "ready", tint: "#e8edf8" },
  { id: "samsung-smartthings", name: "Samsung SmartThings", kind: "cloud", description: "Styrning via SmartThings API.", status: "ready", tint: "#e8f0fb" },
  { id: "toshiba-cloud", name: "Toshiba Lifestyle Cloud", kind: "cloud", description: "Styrning via Toshibas molntjänst.", status: "ready", tint: "#f8ebeb" },
  { id: "gree-cloud", name: "Gree Cloud", kind: "cloud", description: "Styrning via Gree-kontot och Gree Cloud.", status: "ready", tint: "#e8f3ee" },
  { id: "ir", name: "IR-sändare", kind: "direct", description: "Skicka fjärrkontrollskommandon via en nätverksansluten IR-sändare.", status: "planned", tint: "#f1eee5" },
];
const manufacturerIcon = (brand: string) => brand === "toshiba-cloud" ? "toshiba-lifestyle" : brand === "gree-cloud" ? "gree" : brand;
const MAX_DEVICES = 6;
const deviceKey = (device: Pick<Device, "brand" | "id">) => `${device.brand}:${device.id}`;
const modes = [{ id: "cool", icon: "❄" }, { id: "heat", icon: "☀" }, { id: "auto", icon: "⟳" }, { id: "dry", icon: "◌" }, { id: "fan", icon: "≋" }];

function App() {
  const [profiles, setProfiles] = useState<Profile[]>(() => JSON.parse(localStorage.getItem("klimat-profiles") || "[]"));
  const [devices, setDevices] = useState<Device[]>(() => (JSON.parse(localStorage.getItem("klimat-devices") || "[]") as Device[]).map((device) => ({ ...device, online: undefined })));
  const [selected, setSelected] = useState("");
  const [showAdd, setShowAdd] = useState(false);
  const [showAbout, setShowAbout] = useState(false);
  const [aboutTab, setAboutTab] = useState<"overview" | "project" | "third-party">("overview");
  const [aboutInfo, setAboutInfo] = useState<{ version: string; projectLicense: string; thirdPartyLicenses: string } | null>(null);
  const [aboutError, setAboutError] = useState(false);
  const [category, setCategory] = useState<ConnectionKind>("direct");
  const [brandId, setBrandId] = useState("midea");
  const [profile, setProfile] = useState<ConnectionDraft>({ region: "eu" });
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [restored, setRestored] = useState(false);
  const [language, setLanguage] = useState<Language>(detectLanguage);
  const t = (key: MessageKey) => messages[language][key];
  const aboutLoadFailed = t("aboutLoadFailed");
  const active = useMemo(() => devices.find((device) => deviceKey(device) === selected) || devices[0], [devices, selected]);
  const chosenBrand = brands.find((brand) => brand.id === brandId) || brands[0];
  const deviceCount = new Set(devices.map(deviceKey)).size;
  const temperatureStep = active?.temperatureStep || (active?.brand === "gree" || active?.brand === "daikin" ? 1 : 0.5);
  const supportedModes = active?.availableModes?.length ? modes.filter((mode) => active.availableModes?.includes(mode.id)) : modes;
  const supportedFans = active?.availableFans?.length ? [...new Set(["auto", ...active.availableFans])] : ["auto", "low", "medium", "high"];

  useEffect(() => { localStorage.setItem("klimat-profiles", JSON.stringify(profiles)); }, [profiles]);
  useEffect(() => { localStorage.setItem("klimat-devices", JSON.stringify(devices)); }, [devices]);
  useEffect(() => { localStorage.setItem("klimat-language", language); }, [language]);
  useEffect(() => {
    let cancelled = false;
    const unlisten: Array<() => void> = [];
    void Promise.all([
      listen("show-about", () => {
        setShowAdd(false);
        setAboutTab("overview");
        setShowAbout(true);
        setAboutError(false);
        void invoke<typeof aboutInfo>("about_info").then((info) => { if (!cancelled) setAboutInfo(info); }).catch(() => { if (!cancelled) setAboutError(true); });
      }),
      listen("show-controls", () => setShowAbout(false)),
    ]).then((listeners) => {
      if (cancelled) listeners.forEach((stop) => stop());
      else unlisten.push(...listeners);
    });
    return () => { cancelled = true; unlisten.forEach((stop) => stop()); };
  }, []);
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
    const pollInterval = active.kind === "cloud" ? 15 * 60_000 : 30_000;
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
      const next = { id: crypto.randomUUID(), kind: category, brand: brandId, label: chosenBrand.name, endpoint: profile.endpoint || "", region: profile.region || "eu", username: profile.username || "", password: profile.password || "", token: profile.token || "", key: profile.key || "", otp: profile.otp || "" };
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
    <header className="topbar"><div className="brand"><h1>Klimat</h1></div><div className="topbar-actions"><select className="language-select" aria-label="Language" value={language} onChange={(e) => setLanguage(e.target.value as Language)}>{languages.map((lang) => <option key={lang} value={lang}>{languageNames[lang]}</option>)}</select>{!showAbout && <button className="icon-button" aria-label={t("add")} title={deviceCount >= MAX_DEVICES ? t("deviceLimit") : t("add")} disabled={deviceCount >= MAX_DEVICES} onClick={() => setShowAdd((value) => !value)}>＋</button>}</div></header>
    {showAbout ? <section className="about-card">{aboutTab === "overview" ? <><button className="back-link" onClick={() => setShowAbout(false)}>← {t("back")}</button><div className="about-brand"><img src="/klimat.svg" alt="" /><div><h2>Klimat</h2><span>{t("version")} {aboutInfo?.version || "…"}</span></div></div><div className="about-meta"><span>{t("author")}</span><strong>Daniel Nylander</strong></div><div className="about-meta"><span>{t("projectLicense")}</span><strong>GPL-3.0-only</strong></div><button className="about-action" onClick={() => setAboutTab("project")}>{t("readProjectLicense")}</button><button className="about-action" onClick={() => setAboutTab("third-party")}>{t("thirdPartyLicenses")}</button><button className="about-github" onClick={() => void openUrl("https://github.com/yeager/klimat")}>GitHub ↗</button>{aboutError && <p className="feedback">{aboutLoadFailed}</p>}</> : <><button className="back-link" onClick={() => setAboutTab("overview")}>← {t("back")}</button><h2>{aboutTab === "project" ? t("projectLicense") : t("thirdPartyLicenses")}</h2><pre className="license-text">{aboutTab === "project" ? aboutInfo?.projectLicense : aboutInfo?.thirdPartyLicenses}</pre></>}</section> : showAdd ? <section className="setup-card"><button className="back-link" onClick={() => setShowAdd(false)}>← {t("back")}</button><span className="eyebrow">{t("connection")}</span><h2>{t("addAc")}</h2><div className="kind-tabs"><button className={category === "direct" ? "active" : ""} onClick={() => { setCategory("direct"); setBrandId("midea"); setProfile({ region: "eu" }); }}>{t("local")}</button><button className={category === "cloud" ? "active" : ""} onClick={() => { setCategory("cloud"); setBrandId("melcloud"); setProfile({ region: "eu" }); }}>{t("cloud")}</button></div><div className="brand-grid" role="group" aria-label={t("manufacturer")}>{brands.filter((brand) => brand.kind === category).map((brand) => <button type="button" key={brand.id} className={`brand-option ${brandId === brand.id ? "selected" : ""}`} aria-pressed={brandId === brand.id} onClick={() => { setBrandId(brand.id); if (brand.id === "lg-thinq" && (!profile.region || profile.region === "eu")) setProfile({ ...profile, region: "SE" }); if (brand.id === "gree-cloud") setProfile({ ...profile, region: "Europe" }); }}><span className="manufacturer-icon" style={{ "--manufacturer-tint": brand.tint } as React.CSSProperties}><img src={`/manufacturer-icons/${manufacturerIcon(brand.id)}.svg`} alt="" /></span><span className="brand-option-name">{brand.name}</span>{brand.status === "planned" && <small>{t("comingLater")}</small>}</button>)}</div>{chosenBrand.status === "ready" ? <>
      {["melcloud", "melcloud-home", "toshiba-cloud", "panasonic-cloud"].includes(brandId) && <><label>{t("email")}<input type="email" value={profile.username || ""} onChange={(e) => setProfile({ ...profile, username: e.target.value })} placeholder={brandId === "melcloud" ? t("melEmail") : t("email")} /></label><label>{t("password")}<input type="password" value={profile.password || ""} onChange={(e) => setProfile({ ...profile, password: e.target.value })} placeholder={brandId === "melcloud" ? t("melPassword") : t("password")} /></label><p className="hint">{t("credentialHint")}</p>{brandId === "panasonic-cloud" && <><label>{t("verificationCode")}<input inputMode="numeric" autoComplete="one-time-code" value={profile.otp || ""} onChange={(e) => setProfile({ ...profile, otp: e.target.value })} /></label><p className="hint">{t("panasonicHint")}</p></>}</>}
      {brandId === "lg-thinq" && <><label>{t("lgTokenLabel")}<input type="password" autoComplete="off" value={profile.token || ""} onChange={(e) => setProfile({ ...profile, token: e.target.value })} placeholder="Personal Access Token" /></label><label>{t("accountCountry")}<select value={profile.region || "SE"} onChange={(e) => setProfile({ ...profile, region: e.target.value })}>{["SE", "NO", "DK", "FI", "GB", "DE", "FR", "ES", "IT", "NL", "BE", "PL", "US", "CA", "AU", "JP", "KR", "CN"].map((country) => <option key={country} value={country}>{country}</option>)}</select></label><p className="hint">{t("lgHint")}</p><p className="hint">{t("credentialHint")}</p></>}
      {brandId === "samsung-smartthings" && <><label>{t("lgTokenLabel")}<input type="password" autoComplete="off" value={profile.token || ""} onChange={(e) => setProfile({ ...profile, token: e.target.value })} placeholder="SmartThings token" /></label><p className="hint">{t("samsungHint")}</p><p className="hint">{t("credentialHint")}</p></>}
      {brandId === "gree-cloud" && <><label>{t("email")}<input type="email" value={profile.username || ""} onChange={(e) => setProfile({ ...profile, username: e.target.value })} /></label><label>{t("password")}<input type="password" value={profile.password || ""} onChange={(e) => setProfile({ ...profile, password: e.target.value })} /></label><label>{t("greeCloudRegion")}<select value={profile.region || "Europe"} onChange={(e) => setProfile({ ...profile, region: e.target.value })}>{["Australia", "China Mainland", "East South Asia", "Europe", "India", "Latin American", "Middle East", "North American", "Russia", "South American"].map((region) => <option key={region} value={region}>{region}</option>)}</select></label><p className="hint">{t("credentialHint")}</p></>}
      {brandId === "mitsubishi-local" && <><label>{t("ipAddress")}<input value={profile.endpoint || ""} onChange={(e) => setProfile({ ...profile, endpoint: e.target.value })} placeholder="192.168.1.42" /></label><p className="hint">{t("mitsubishiLocalHint")}</p></>}
      {["midea", "comfee", "toshiba-lifestyle"].includes(brandId) && <><label>{t("ipAddress")}<input value={profile.endpoint || ""} onChange={(e) => setProfile({ ...profile, endpoint: e.target.value })} placeholder="192.168.1.42" /></label><p className="hint">{t("mideaScanHint")}</p><label>{t("deviceToken")}<input value={profile.token || ""} onChange={(e) => setProfile({ ...profile, token: e.target.value })} placeholder={t("hexToken")} /></label><label>{t("deviceKey")}<input type="password" value={profile.key || ""} onChange={(e) => setProfile({ ...profile, key: e.target.value })} placeholder={t("hexKey")} /></label><p className="hint">{t("localHint")}</p></>}
      {brandId === "daikin" && <><label>{t("ipAddress")}<input value={profile.endpoint || ""} onChange={(e) => setProfile({ ...profile, endpoint: e.target.value })} placeholder="192.168.1.42" /></label><p className="hint">{t("daikinHint")}</p></>}
      {brandId === "gree" && <p className="hint">{t("greeHint")}</p>}
      <button className="primary-button" disabled={busy || deviceCount >= MAX_DEVICES || (["melcloud", "melcloud-home", "toshiba-cloud", "gree-cloud", "panasonic-cloud"].includes(brandId) && (!profile.username || !profile.password)) || (["lg-thinq", "samsung-smartthings"].includes(brandId) && !profile.token) || (["daikin", "mitsubishi-local"].includes(brandId) && !profile.endpoint)} onClick={() => void connect()}>{busy ? t("connecting") : t("connectDiscover")}</button>
    </> : <div className="planned-card"><b>{t("planned")}</b><p>{t("plannedHint")}</p></div>}{message && <p className="feedback">{message}</p>}</section> : <>
      {active ? <>{devices.length > 1 && <div className="device-tabs">{devices.map((device) => { const brand = brands.find((item) => item.id === device.brand); return <button key={deviceKey(device)} className={`device-tab ${deviceKey(active) === deviceKey(device) ? "active" : ""}`} onClick={() => setSelected(deviceKey(device))} title={device.name}><span className="device-brand-icon" style={{ "--manufacturer-tint": brand?.tint || "#e9efeb" } as React.CSSProperties}><img src={`/manufacturer-icons/${manufacturerIcon(device.brand)}.svg`} alt="" /></span>{device.name}</button>; })}</div>}<section className="control-card"><div className="card-top"><div className="active-device-title"><span className="device-brand-icon active-brand-icon" style={{ "--manufacturer-tint": brands.find((item) => item.id === active.brand)?.tint || "#e9efeb" } as React.CSSProperties}><img src={`/manufacturer-icons/${manufacturerIcon(active.brand)}.svg`} alt="" /></span><div><h3>{active.name}</h3><span className="eyebrow">{active.kind === "cloud" ? t("cloud").toUpperCase() : t("local").toUpperCase()} · {active.state === "on" ? t("on") : t("off")}</span></div></div><div className="card-actions"><button className="remove-device" disabled={busy} aria-label={t("removeDevice")} title={t("removeDevice")} onClick={() => void forgetActiveDevice()}>×</button><button className={`power ${active.state === "on" ? "on" : ""}`} disabled={busy} aria-label={active.state === "on" ? t("off") : t("on")} onClick={() => void command("power", active.state === "on" ? "off" : "on")}>⏻</button></div></div><div className="temperature"><span>{active.current}<small>°</small></span><div><b>{t((modes.find((mode) => mode.id === active.mode)?.id || "off") as MessageKey)}</b><small>{t("indoor")}</small></div></div><div className="target-row"><div><span className="eyebrow">{t("setpoint")}</span><strong>{active.target}<small>°C</small></strong></div><div className="stepper"><button disabled={busy || (active.minTarget !== undefined && active.target - temperatureStep < active.minTarget - 0.001)} aria-label={t("lowerTemp")} onClick={() => void command("temperature", active.target - temperatureStep)}>−</button><button disabled={busy || (active.maxTarget !== undefined && active.target + temperatureStep > active.maxTarget + 0.001)} aria-label={t("raiseTemp")} onClick={() => void command("temperature", active.target + temperatureStep)}>＋</button></div></div><div className="control-divider"/><div className="mode-grid">{supportedModes.map((mode) => <button key={mode.id} disabled={busy} aria-label={t(mode.id as MessageKey)} onClick={() => void command("mode", mode.id)} className={`mode-button ${active.mode === mode.id ? "selected" : ""}`}><span>{mode.icon}</span>{t(mode.id as MessageKey)}</button>)}</div><label className="select-label">{t("fan")}<select aria-label={t("fan")} value={active.fan} disabled={busy} onChange={(e) => void command("fan", e.target.value)}>{supportedFans.map((fan) => <option key={fan} value={fan}>{t(fan as MessageKey)}</option>)}</select></label>{active.brand === "mitsubishi-local" && active.verticalVane && active.horizontalVane && <div className="vane-controls"><label>{t("verticalAirflow")}<select aria-label={t("verticalAirflow")} value={active.verticalVane} disabled={busy} onChange={(e) => void command("verticalVane", e.target.value)}>{["AUTO", "V1", "V2", "V3", "V4", "V5", "SWING"].map((value) => <option key={value} value={value}>{value === "AUTO" ? t("auto") : value === "SWING" ? t("swing") : value.slice(1)}</option>)}</select></label><label>{t("horizontalAirflow")}<select aria-label={t("horizontalAirflow")} value={active.horizontalVane} disabled={busy} onChange={(e) => void command("horizontalVane", e.target.value)}>{["AUTO", "FAR_LEFT", "LEFT", "CENTER", "RIGHT", "FAR_RIGHT", "LEFT_CENTER", "CENTER_RIGHT", "LEFT_RIGHT", "LEFT_CENTER_RIGHT", "SWING"].map((value) => <option key={value} value={value}>{value === "AUTO" ? t("auto") : value === "SWING" ? t("swing") : ({ FAR_LEFT: "⇤", LEFT: "←", CENTER: "•", RIGHT: "→", FAR_RIGHT: "⇥", LEFT_CENTER: "⇤←", CENTER_RIGHT: "→⇥", LEFT_RIGHT: "↔", LEFT_CENTER_RIGHT: "⇤•⇥" } as Record<string, string>)[value]}</option>)}</select></label></div>}</section></> : <section className="empty-card"><h3>{t("emptyTitle")}</h3><p>{t("emptyHint")}</p><div className="connection-options"><button onClick={() => beginAdd("direct")}><span>⌁</span><b>{t("local")}</b><small>{t("localBrand")}</small></button><button onClick={() => beginAdd("cloud")}><span>☁</span><b>{t("cloud")}</b><small>{t("cloudBrand")}</small></button></div></section>}
    </>}
    <footer><span className={`status-light ${active?.online === false ? "offline" : active?.online === undefined ? "checking" : ""}`} aria-hidden="true" />{message && !showAdd ? message : active ? `${active.brand} · ${busy ? t("sending") : active.online === false ? t("offline") : active.online === true ? t("connected") : t("checking")}` : t("localStorage")}</footer>
  </main>;
}

export default App;
