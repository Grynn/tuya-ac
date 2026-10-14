# tuya-ac

Control a Daikin200 air conditioner over the LAN through a compatible Tuya/Smart Life Wi-Fi infrared hub. macOS CLI with local keys stored in Keychain.

```sh
brew install grynn/tap/tuya-ac
ac on          # cooling at 24°C
ac temp 28     # cooling/on at 28°C
ac off
```

Set up the hub and key first using the instructions below. The formula is named `tuya-ac`; the installed command is `ac`. This is an unofficial project, not affiliated with Tuya or Home Assistant.

## Supported hardware and behavior

Tested with one Tuya 3.5 Wi-Fi IR hub using DPS 201/202 and a Daikin200 remote. This is **not a universal AC driver**. Other Daikin variants, other AC brands, RF remotes, and hubs with different data points may not work.

The bundled Daikin200 template uses unit address `0x48`, high fan and no swing. Temperature commands switch cooling on and accept 16–30°C. `on` always chooses 24°C; it does not restore a remembered temperature. These are full-state commands, not toggle commands. A different remote's settings/address require a matching learned template.

The hub authenticated and acknowledged commands; original-capture replay and generated 24°C produced an AC beep confirmed by the operator. Off is encoded and tested offline, but has not yet been physically confirmed. There is no AC state/setpoint readback: an ACK means the hub accepted the command, not that the AC received it. The handheld remote's display does not update when this CLI sends IR.

## 1. Pair the hub in Smart Life

1. Install **Smart Life**, sign in, and add your Wi-Fi IR hub using its pairing instructions (usually a 2.4 GHz Wi-Fi network).
2. Add your Daikin AC remote inside the hub and verify that Smart Life can control it.
3. Put your Mac on the same LAN as the hub; guest-network/client isolation must not block communication.
4. In Smart Life, open **Me → Settings → Account and Security → User Code**. This is your account's User Code, not the device's Virtual ID and not a local key. Menu labels can vary with app version.

## 2. Retrieve local keys using Smart Life QR login

```sh
ac login
```

Enter the User Code when prompted. A QR image opens on your Mac. In **Smart Life → Home → + → Scan**, scan that QR and approve authorization. The image is temporary and removed when login finishes or expires (four-minute timeout).

The command uses Tuya's sharing SDK with the public Home Assistant client registration. It lists device names/IDs and whether a local key was saved. Local keys go directly to **macOS Keychain**, under server `tuya-local-device`, account equal to the physical device ID. Keys and cloud tokens are not printed. Cloud tokens are not persisted. Device metadata is saved locally in `~/.config/tuya-ac/inventory.json`.

Choose the **physical Wi-Fi IR hub** (often named Smart IR, category `wnykq`), not the virtual AC remote. The virtual remote does not provide the hub's LAN connection. QR login requires internet access; normal on/off/temperature control does not need the cloud API or a developer subscription. Tuya can change or restrict this sharing endpoint; if no local key is returned, use the alternative below.

References: [Home Assistant's Tuya account/QR instructions](https://www.home-assistant.io/integrations/tuya/), [Tuya sharing SDK](https://github.com/tuya/tuya-device-sharing-sdk).

### Alternative: retrieve a key through a Tuya developer project

If you already have the hub's 16-byte local key, skip straight to `ac key` after configuration.

Otherwise follow [TinyTuya's local-key wizard guide](https://github.com/jasonacox/tinytuya#get-the-tuya-device-local-key): create a Smart Home project in Tuya's developer platform, choose the correct data center for your app account, authorize the required APIs, and link the Smart Life account by scanning the platform's QR in the app. Run the wizard in a private working directory:

```sh
mkdir -m 700 ~/tuya-key-setup
cd ~/tuya-key-setup
uv run --with tinytuya python -m tinytuya wizard
```

Supply the project's API Access ID, Access Secret, region and a linked device ID when prompted. The wizard writes `devices.json` containing device local keys and may write API credentials in other JSON files. These files are secrets: do not commit or share them. Find the physical hub's key, configure its ID/IP as below, then run `ac key` and paste the key at the hidden prompt. Securely manage or remove the wizard files afterward. Developer API permissions and IoT Core subscriptions can expire; this route is not guaranteed to remain free or available.

## 3. Find and configure the hub

```sh
ac scan
ac configure --device-id YOUR_PHYSICAL_HUB_ID --ip 192.168.1.123
ac check
```

`ac scan` listens for Tuya discovery broadcasts for about 12 seconds and reports IDs, LAN IPs and protocol versions. The current CLI uses protocol **3.5**. If discovery finds nothing, check your router's DHCP leases; the public IP shown in Smart Life is not the LAN IP you need. Reserve the hub's DHCP address to prevent it changing.

If you obtained the key manually rather than through `ac login`:

```sh
ac key   # hidden prompt; stores key in Keychain
ac check
```

`ac check` authenticates to the hub and closes the connection without sending IR. Keychain may ask you to allow access. Configuration is `~/.config/tuya-ac/config.json`; captures are in the same directory. Set `AC_CONFIG_DIR` to use another configuration directory. No configuration or credentials are stored in Homebrew's Cellar, so upgrades preserve them.

Re-pairing or resetting a hub may rotate its key: repeat `ac login` or `ac key` and update the configured device ID if needed.

## 4. Control and optional learning

```sh
ac on
ac temp 28
ac off
ac temp 24 --dry-run  # inspect payload without connecting or transmitting
ac list
```

Commands send once and do not automatically retry uncertain transmissions. If no ACK arrives, the command may still have been transmitted. Observe the AC before retrying.

For another matching Daikin200 remote, put it in cooling mode with your desired fan/swing settings, then:

```sh
ac learn capture-a --seconds 120
```

Point the remote at the hub and press the intended button. This local capture overrides the bundled template. The decoder verifies frame length, signatures, bit timings, cooling mode and checksums before generating temperatures. A learned `on`, `off`, or `cool-N` overrides generation for that command; use those names only after verifying the remote display. Full-state captures work without needing to interpret a different protocol, but automatic temperature generation requires Daikin200.

## Implementation and development

TinyTuya provides the generic authenticated Tuya 3.5 AES-GCM TCP transport on port 6668. No TinyTuya IR helper or cloud IR library is used. `daikin.py` builds the 25-byte Daikin200 state, updates temperature/power/checksums, and preserves measured pulse timings. DPS 201 carries a `send_ir` JSON command, with `key1` equal to `1` followed by base64 little-endian uint16 pulse durations. Learning uses `study`/`study_exit` and DPS 202.

Protocol references: [IRremoteESP8266 Daikin implementation](https://github.com/crankyoldgit/IRremoteESP8266/blob/master/src/ir_Daikin.cpp), [Daikin200 generator](https://github.com/AnilDaoud/daikin200-ir-codegen). This repository contains an independent Python implementation.

```sh
git clone https://github.com/Grynn/tuya-ac.git
cd tuya-ac
uv sync --frozen
./ac --help
```

Homebrew formula: [Grynn/homebrew-tap](https://github.com/Grynn/homebrew-tap/blob/main/tuya-ac.rb). Dependencies are pinned in `uv.lock`.

## Uninstall

`brew uninstall tuya-ac` removes the CLI. Your configuration/captures and Keychain keys remain. Remove the configuration directory and the corresponding `tuya-local-device` internet-password entries separately if you want to erase setup.
