import { useEffect, useMemo, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { detectLanguage, languageNames, languages, messages, type Language, type MessageKey } from "./i18n";
import "./App.css";

type ConnectionKind = "direct" | "cloud";
type Brand = { id: string; name: string; kind: ConnectionKind; description: string; status: "ready" | "planned" };
type Device = { id: string; name: string; brand: string; kind: ConnectionKind; state: string; target: number; current: number; mode: string; fan: string; profileId?: string; connection?: Record<string, unknown> };
type Profile = { id: string; kind: ConnectionKind; brand: string; label: string; endpoint: string; region: string };
type ConnectionDraft = { username?: string; password?: string; endpoint?: string; region?: string; token?: string; key?: string };
const brands: Brand[] = [
  { id: "melcloud", name: "Mitsubishi Electric · MELCloud", kind: "cloud", description: "Styrning via MELCloud-kontot. Passar MELCloud-anslutna luftvärmepumpar.", status: "ready" },
  { id: "midea", name: "Midea och kompatibla", kind: "direct", description: "Lokal styrning över nätverket för Midea-protokollet och kompatibla märken.", status: "ready" },
  { id: "daikin", name: "Daikin", kind: "direct", description: "Lokal nätverksstyrning för modeller med kompatibel nätverksadapter.", status: "planned" },
  { id: "melcloud-home", name: "Mitsubishi Electric · MELCloud Home", kind: "cloud", description: "Separat molntjänst för enheter som använder MELCloud Home.", status: "planned" },
  { id: "lg-thinq", name: "LG ThinQ", kind: "cloud", description: "Styrning via LG ThinQ-kontot.", status: "planned" },
  { id: "ir", name: "IR-sändare", kind: "direct", description: "Skicka fjärrkontrollskommandon via en nätverksansluten IR-sändare.", status: "planned" },
];
const modes = [{ id: "cool", label: "Kyla", icon: "❄" }, { id: "heat", label: "Värme", icon: "☀" }, { id: "auto", label: "Auto", icon: "⟳" }, { id: "dry", label: "Avfuktning", icon: "◌" }, { id: "fan", label: "Fläkt", icon: "≋" }];

function App() {
  const [profiles, setProfiles] = useState<Profile[]>(() => JSON.parse(localStorage.getItem("klimat-profiles") || "[]"));
  const [devices, setDevices] = useState<Device[]>(() => JSON.parse(localStorage.getItem("klimat-devices") || "[]"));
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
  const active = useMemo(() => devices.find((device) => device.id === selected) || devices[0], [devices, selected]);
  const chosenBrand = brands.find((brand) => brand.id === brandId) || brands[0];

  useEffect(() => { localStorage.setItem("klimat-profiles", JSON.stringify(profiles)); }, [profiles]);
  useEffect(() => { localStorage.setItem("klimat-devices", JSON.stringify(devices)); }, [devices]);
  useEffect(() => { localStorage.setItem("klimat-language", language); }, [language]);
  useEffect(() => {
    if (restored || !profiles.length) { setRestored(true); return; }
    let cancelled = false;
    (async () => {
      const recovered: Device[] = [];
      for (const saved of profiles) {
        try {
          const response = await invoke<{ devices: Device[] }>("climate_request", { payload: { ...saved, id: saved.id, profiles, action: "restore" } });
          recovered.push(...response.devices.map((device) => ({ ...device, brand: saved.brand, profileId: saved.id, connection: { ...device.connection, profileId: saved.id } })));
        } catch { /* Offline devices stay saved and can be retried from settings. */ }
      }
      if (!cancelled && recovered.length) { setDevices(recovered); setSelected(recovered[0].id); }
      if (!cancelled) setRestored(true);
    })();
    return () => { cancelled = true; };
  }, [profiles, restored]);

  function beginAdd(kind: ConnectionKind) { setCategory(kind); setBrandId(kind === "cloud" ? "melcloud" : "midea"); setProfile({ region: "eu" }); setMessage(""); setShowAdd(true); }
  async function connect() {
    setBusy(true); setMessage("");
    try {
      const next = { id: crypto.randomUUID(), kind: category, brand: brandId, label: chosenBrand.name, endpoint: profile.endpoint || "", region: profile.region || "eu", username: profile.username || "", password: profile.password || "", token: profile.token || "", key: profile.key || "" };
      const response = await invoke<{ devices: Device[]; profile?: Partial<Profile> }>("climate_request", { payload: { ...next, profiles, kind: category, brand: brandId, action: "discover" } });
      const newDevices = response.devices.map((device) => ({ ...device, brand: brandId, profileId: next.id, connection: { ...device.connection, profileId: next.id } }));
      const savedProfile: Profile = { id: next.id, kind: category, brand: brandId, label: chosenBrand.name, endpoint: next.endpoint, region: next.region };
      setProfiles((current) => [...current, savedProfile]);
      if (newDevices.length) { setDevices((current) => [...current, ...newDevices]); setSelected(newDevices[0].id); setShowAdd(false); setMessage(`${newDevices.length} ${t("found")}`); }
      else setMessage(t("savedDiscovery"));
    } catch (err) { setMessage(err instanceof Error ? err.message : t("connectFailed")); }
    finally { setBusy(false); }
  }

  async function command(action: string, value?: string | number) {
    if (!active) return;
    setBusy(true); setMessage("");
    try {
      const updated: Partial<Device> = await invoke("climate_request", { payload: { kind: active.kind, brand: active.brand, action, value, device: active, profileId: active.profileId, profiles } });
      setDevices((current) => current.map((device) => device.id === active.id ? { ...device, ...updated } : device));
      setMessage(t("sent"));
    } catch (err) { setMessage(err instanceof Error ? err.message : t("commandFailed")); }
    finally { setBusy(false); }
  }

  return <main className="app-shell">
    <header className="topbar"><div className="brand"><h1>Klimat</h1></div><div className="topbar-actions"><select className="language-select" aria-label="Language" value={language} onChange={(e) => setLanguage(e.target.value as Language)}>{languages.map((lang) => <option key={lang} value={lang}>{languageNames[lang]}</option>)}</select><button className="icon-button" aria-label={t("add")} title={t("add")} onClick={() => setShowAdd((value) => !value)}>＋</button></div></header>
    {showAdd ? <section className="setup-card"><button className="back-link" onClick={() => setShowAdd(false)}>← {t("back")}</button><span className="eyebrow">{t("connection")}</span><h2>{t("addAc")}</h2><div className="kind-tabs"><button className={category === "direct" ? "active" : ""} onClick={() => beginAdd("direct")}>{t("local")}</button><button className={category === "cloud" ? "active" : ""} onClick={() => beginAdd("cloud")}>{t("cloud")}</button></div><label>{t("manufacturer")}<select value={brandId} onChange={(e) => setBrandId(e.target.value)}>{brands.filter((brand) => brand.kind === category).map((brand) => <option key={brand.id} value={brand.id}>{brand.name}{brand.status === "planned" ? ` · ${t("comingLater")}` : ""}</option>)}</select></label>{chosenBrand.status === "ready" ? <>
      {brandId === "melcloud" && <><label>{t("email")}<input type="email" value={profile.username || ""} onChange={(e) => setProfile({ ...profile, username: e.target.value })} placeholder={t("melEmail")} /></label><label>{t("password")}<input type="password" value={profile.password || ""} onChange={(e) => setProfile({ ...profile, password: e.target.value })} placeholder={t("melPassword")} /></label><p className="hint">{t("credentialHint")}</p></>}
      {brandId === "midea" && <><label>{t("ipAddress")}<input value={profile.endpoint || ""} onChange={(e) => setProfile({ ...profile, endpoint: e.target.value })} placeholder="192.168.1.42" /></label><label>{t("deviceToken")}<input value={profile.token || ""} onChange={(e) => setProfile({ ...profile, token: e.target.value })} placeholder={t("hexToken")} /></label><label>{t("deviceKey")}<input type="password" value={profile.key || ""} onChange={(e) => setProfile({ ...profile, key: e.target.value })} placeholder={t("hexKey")} /></label><p className="hint">{t("localHint")}</p></>}
      <button className="primary-button" disabled={busy || (brandId === "melcloud" && (!profile.username || !profile.password)) || (brandId === "midea" && !profile.endpoint)} onClick={() => void connect()}>{busy ? t("connecting") : t("connectDiscover")}</button>
    </> : <div className="planned-card"><b>{t("planned")}</b><p>{t("plannedHint")}</p></div>}{message && <p className="feedback">{message}</p>}</section> : <>
      {active ? <>{devices.length > 1 && <div className="device-tabs">{devices.map((device) => <button key={device.id} className={`device-tab ${active.id === device.id ? "active" : ""}`} onClick={() => setSelected(device.id)}>{device.name}</button>)}</div>}<section className="control-card"><div className="card-top"><div><h3>{active.name}</h3><span className="eyebrow">{active.kind === "cloud" ? t("cloud").toUpperCase() : t("local").toUpperCase()} · {active.state === "on" ? t("on") : t("off")}</span></div><button className={`power ${active.state === "on" ? "on" : ""}`} disabled={busy} aria-label={active.state === "on" ? t("off") : t("on")} onClick={() => void command("power", active.state === "on" ? "off" : "on")}>⏻</button></div><div className="temperature"><span>{active.current}<small>°</small></span><div><b>{modes.find((mode) => mode.id === active.mode)?.label || t("off")}</b><small>{t("indoor")}</small></div></div><div className="target-row"><div><span className="eyebrow">{t("setpoint")}</span><strong>{active.target}<small>°C</small></strong></div><div className="stepper"><button disabled={busy} aria-label={t("lowerTemp")} onClick={() => void command("temperature", active.target - 0.5)}>−</button><button disabled={busy} aria-label={t("raiseTemp")} onClick={() => void command("temperature", active.target + 0.5)}>＋</button></div></div><div className="control-divider"/><div className="mode-grid">{modes.map((mode) => <button key={mode.id} disabled={busy} aria-label={t(mode.id as MessageKey)} onClick={() => void command("mode", mode.id)} className={`mode-button ${active.mode === mode.id ? "selected" : ""}`}><span>{mode.icon}</span>{t(mode.id as MessageKey)}</button>)}</div><label className="select-label">{t("fan")}<select aria-label={t("fan")} value={active.fan} disabled={busy} onChange={(e) => void command("fan", e.target.value)}><option value="auto">{t("auto")}</option><option value="low">{t("low")}</option><option value="medium">{t("medium")}</option><option value="high">{t("high")}</option></select></label></section></> : <section className="empty-card"><h3>{t("emptyTitle")}</h3><p>{t("emptyHint")}</p><div className="connection-options"><button onClick={() => beginAdd("direct")}><span>⌁</span><b>{t("local")}</b><small>{t("localBrand")}</small></button><button onClick={() => beginAdd("cloud")}><span>☁</span><b>{t("cloud")}</b><small>{t("cloudBrand")}</small></button></div></section>}
    </>}
    <footer><span className="status-light" />{message && !showAdd ? message : active ? `${active.brand} · ${busy ? t("sending") : t("connected")}` : t("localStorage")}</footer>
  </main>;
}

export default App;
