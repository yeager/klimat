# Klimat

**A compact, cross-platform air-conditioner controller that lives in the system tray.** Klimat is built with Tauri, Rust, React, and TypeScript. Its small popup keeps the essential controls close at hand: power, room temperature, target temperature, operating mode, and fan speed.

> Klimat is an early prototype. MELCloud and Midea-compatible adapters are implemented, but physical-device validation and Linux/Windows release validation are still outstanding.

## Features

- Tray-first popup for macOS, Windows, and Linux desktops.
- Mitsubishi Electric devices through the MELCloud cloud service.
- Midea and compatible devices over the local network.
- Power, temperature setpoint, cool/heat/auto/dry/fan modes, and fan speed.
- Credentials and device keys stored in the operating system keychain.
- Interface translated into 20 languages, including Swedish. The system language is detected on first launch; the language selector can override it.
- Home Assistant integration code is used as a protocol reference. Klimat does not connect to or require a Home Assistant server.

## Support status

| Connection | Status | Notes |
| --- | --- | --- |
| Mitsubishi Electric MELCloud | Implemented, needs device validation | Uses a standalone MELCloud client library. |
| Midea-compatible local control | Implemented, needs device validation | Device model, credentials, and network setup affect compatibility. |
| Daikin local, MELCloud Home, LG ThinQ, network IR | Planned | The adapter interface is intended to support additional providers. |

The current MELCloud adapter uses the upstream `python-melcloud` client. Local Midea control uses `midea-local`. These are independent client libraries; no Home Assistant runtime or server is bundled.

## Tray behavior

Klimat starts hidden and opens a small, borderless popup from its tray icon. Clicking outside or closing the popup hides it. KDE Plasma supports StatusNotifierItem tray icons. GNOME Shell generally needs the [AppIndicator and KStatusNotifierItem Support extension](https://extensions.gnome.org/extension/615/appindicator-support/). On Wayland, the compositor controls popup placement.

## Development setup

Install Node.js, pnpm, Rust, Python 3, and the [Tauri prerequisites](https://v2.tauri.app/start/prerequisites/) for your operating system. Adapter packaging also requires the dependencies in `requirements-build.txt`.

```sh
pnpm install
pnpm setup:adapters
pnpm tauri dev
```

The Python bridge can be packaged as an OS-native executable before building the desktop app:

```sh
python3 -m pip install -r requirements-build.txt
pnpm build:adapter
pnpm tauri build
```

Build installers on the target OS. A GitHub Actions workflow is configured for Linux x64, macOS x64/arm64, and Windows x64. A successful workflow run and a physical-device check are required before calling those platform releases verified.

## Localization

The interface supports Swedish (`sv`), English (`en`), German (`de`), French (`fr`), Spanish (`es`), Italian (`it`), Portuguese (`pt`), Dutch (`nl`), Danish (`da`), Norwegian Bokmål (`nb`), Finnish (`fi`), Polish (`pl`), Czech (`cs`), Slovak (`sk`), Hungarian (`hu`), Romanian (`ro`), Greek (`el`), Turkish (`tr`), Japanese (`ja`), and Simplified Chinese (`zh`).

Translations live in `src/i18n.ts`. Each locale uses the same ordered message keys. When contributing a translation, preserve placeholders and control meanings, and check the compact popup for text overflow. Swedish is the source locale.

## Security and privacy

- MELCloud credentials and Midea keys are passed to the OS keychain by the Rust application.
- Device names and connection profiles are stored locally by the app.
- Cloud control requires access to the relevant manufacturer's service. Local control requires network access to the device.
- Never commit real account credentials, device tokens, or keys. Run [Gitleaks](https://github.com/gitleaks/gitleaks) before publishing changes.

## License and third-party notices

Klimat is currently distributed without a project license file. Until a license is added, do not assume the source is available for reuse. Bundled adapter builds collect third-party license notices in `THIRD_PARTY_LICENSES.txt`. Review the licenses of `python-melcloud`, `midea-local`, and their transitive dependencies before redistributing a release. Home Assistant Core is Apache-2.0; if source code is copied from it, retain the required notices and license terms.

## Contributing

Issues and pull requests are welcome. Please include the operating system, desktop environment, connection type, and device model in reproducible bug reports. Do not include credentials or device secrets. Hardware compatibility reports should identify the adapter/model and the controls verified.
