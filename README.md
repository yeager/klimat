# Klimat

Klimat is a compact air-conditioner controller for Linux, macOS, and Windows. It lives in the system tray and opens a small popup for the controls used most often: power, room temperature, target temperature, operating mode, and fan speed.

Klimat is an early prototype. The adapters and desktop packages build, but operation with physical air conditioners has not yet been validated across supported models.

## Supported connections

| Provider | Connection | Status and limits |
| --- | --- | --- |
| Mitsubishi Electric ECHONET Lite | Local network | Manual adapter IP. Requires a supported MAC-568/578/588IF-E Wi-Fi interface with ECHONET Lite enabled in the Mitsubishi app. ECHONET Lite has no built-in security; use only on a trusted local network. |
| Mitsubishi Electric MELCloud Classic | Cloud | Email/password sign-in and power, setpoint, operating mode, and fan controls. |
| Mitsubishi Electric MELCloud Home | Cloud | Separate service using the same account email/password and supported air-to-air controls. |
| LG ThinQ | Cloud | Official ThinQ Connect API. Requires a Personal Access Token with air-conditioner access and the country where the LG account was created. |
| Midea and compatible Comfee/Toshiba Lifestyle units | Local network | Broadcast discovery or a manually entered IP address. Compatibility depends on the unit and Wi-Fi module. |
| Gree | Local network | Automatic discovery and pairing for supported units. |
| Daikin | Local network | Manual IP address for adapters exposing the compatible, older local HTTP API. Cloud-only and newer authenticated adapters are not covered. |

You can save up to six units. The popup offers power, target temperature, cool/heat/auto/dry/fan modes, and fan speed where the device supports them. It refreshes the selected unit while the popup is open and when it becomes visible again.

MELCloud Classic and MELCloud Home are separate services; choose the one used by your unit. For LG, create a token at [LG ThinQ Connect](https://connect-pat.lgthinq.com), grant it access to air conditioners, and select the country where the LG account was registered. Credentials and tokens are kept in the operating system keychain. Do not share them in bug reports. Check that the account works in the matching official app or website before reporting a login issue.

The Mitsubishi Electric ECHONET Lite adapter uses the manufacturer's documented TCP interface on port 3610. Some Wi-Fi interfaces support it when enabled in the Mitsubishi app; availability varies by adapter and region. The `MSZ-LN35VG2W` is also reported working over its CN105 service connector with an ESPHome-compatible adapter. That requires additional hardware connected to the indoor unit; Klimat does not control CN105 directly.

## Download

The [Build desktop installers workflow](https://github.com/yeager/klimat/actions/workflows/build.yml?query=branch%3Amain) builds separate artifacts for:

- `klimat-linux-x64`: Linux x64 `.deb` and `.rpm`
- `klimat-macos-x64`: Intel Mac `.dmg`
- `klimat-macos-arm64`: Apple Silicon `.dmg`
- `klimat-windows-x64`: Windows x64 `.msi` and NSIS `.exe`

Open a successful workflow run and download the artifact for your operating system and architecture. These are unsigned development builds; macOS packages are not notarized. Physical-device support and signed production releases remain to be verified.

## Tray behavior

Klimat starts hidden and opens a small popup from its tray icon. Clicking outside or closing the popup hides it. KDE Plasma provides StatusNotifierItem support. GNOME Shell generally needs the [AppIndicator and KStatusNotifierItem Support extension](https://extensions.gnome.org/extension/615/appindicator-support/). On Wayland, popup placement is controlled by the compositor.

## Privacy and security

- Cloud credentials, LG ThinQ tokens, and local device keys are stored in the operating system keychain.
- Device names and connection profiles are stored locally by Klimat.
- Cloud connections require the manufacturer's service. Local connections require access to the device on the same network.
- Never commit real account credentials, device tokens, or keys. Run [Gitleaks](https://github.com/gitleaks/gitleaks) before publishing changes.
- Home Assistant is used as a protocol reference only. Klimat does not connect to or require a Home Assistant server.

## Localization and icons

The interface supports Swedish (`sv`), English (`en`), German (`de`), French (`fr`), Spanish (`es`), Italian (`it`), Portuguese (`pt`), Dutch (`nl`), Danish (`da`), Norwegian Bokmål (`nb`), Finnish (`fi`), Polish (`pl`), Czech (`cs`), Slovak (`sk`), Hungarian (`hu`), Romanian (`ro`), Greek (`el`), Turkish (`tr`), Japanese (`ja`), and Simplified Chinese (`zh`). The system language is selected on first launch; users can change it in the popup.

Translations are in `src/i18n.ts`. Each locale uses the same message keys. Preserve placeholders and control meanings, and check the compact popup for text overflow when changing strings.

The repository includes platform app icons, a separate tray icon, and generated SVG icons for providers. Linux desktop integration uses `se.klimakontroll.desktop`; macOS uses an `.icns` app icon; Windows uses an `.ico` icon.

## Development

Install Node.js, pnpm, Rust, Python 3, and the [Tauri prerequisites](https://v2.tauri.app/start/prerequisites/) for your operating system. Python adapter packaging also needs the dependencies in `requirements-build.txt`.

```sh
pnpm install
pnpm setup:adapters
pnpm tauri dev
```

To build an installer locally:

```sh
python3 -m pip install -r requirements-build.txt
pnpm build:adapter
pnpm tauri build
```

Build installers on their target operating system. The CI workflow builds the Linux, macOS, and Windows targets listed above. The bundled Python adapter includes clients for MELCloud Classic, MELCloud Home, LG ThinQ Connect, and Midea-compatible units; Gree uses the `gree-climate` Rust crate, and Daikin uses its local HTTP API directly.

## License and third-party notices

This repository does not currently include a project license. Until one is added, do not assume that Klimat source code is licensed for reuse. Adapter builds collect third-party license notices in `THIRD_PARTY_LICENSES.txt`. Review the licenses of `python-melcloud`, `aiomelcloudhome`, `thinqconnect`, `midea-local`, `gree-climate`, and their transitive dependencies before redistribution. The ThinQ Connect SDK is Apache-2.0 and the MELCloud Home client is MIT-licensed. Home Assistant Core is Apache-2.0; if source code is copied from it, retain the required notices and comply with its license.

## Contributing

Issues and pull requests are welcome. Include the operating system, desktop environment, connection type, and device model in reproducible reports. Do not include credentials or device secrets. Compatibility reports should say which controls were checked on the physical unit.
