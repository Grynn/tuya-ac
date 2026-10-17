"""Local-only Tuya IR CLI. Credentials are retrieved from macOS Keychain."""
import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time

import tinytuya

import daikin

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get('AC_CONFIG_DIR', str(Path.home() / '.config' / 'tuya-ac')))
CODES = CONFIG_DIR / 'ir-codes.json'
CONFIG = CONFIG_DIR / 'config.json'


def config():
    if not CONFIG.exists():
        raise ValueError('Missing configuration. Run ac setup')
    data = json.loads(CONFIG.read_text())
    if not isinstance(data, dict) or not isinstance(data.get('device_id'), str) or not data['device_id']:
        raise ValueError('Configuration requires device_id and ip')
    if not isinstance(data.get('ip'), str):
        raise ValueError('Configuration requires an IPv4 address')
    ipaddress.IPv4Address(data['ip'])
    return data


class MissingKey(RuntimeError):
    pass


def local_key(device_id):
    result = subprocess.run(['security', 'find-internet-password', '-s',
                             'tuya-local-device', '-a', device_id, '-w'],
                            capture_output=True, text=True)
    if result.returncode == 44:  # errSecItemNotFound (-25300), as a shell exit status
        raise MissingKey('Device local key missing from macOS Keychain. Run ac login or see '
                         'README key alternatives, then rerun ac setup.')
    if result.returncode:
        raise RuntimeError('Could not read macOS Keychain; allow access and retry ac setup')
    key = result.stdout.rstrip('\n')
    if len(key.encode()) != 16:
        raise RuntimeError('Invalid local key in Keychain; update it with ac login or ac key')
    return key


def connect(settings=None, key=None):
    settings = config() if settings is None else settings
    key = local_key(settings['device_id']) if key is None else key
    dev = tinytuya.Device(settings['device_id'], settings['ip'], key,
                         version=3.5, connection_timeout=2, persist=True)
    try:
        dev.disabledetect = True
        dev.set_socketRetryLimit(1)
        if dev._get_socket(False) is not True:
            raise RuntimeError('LAN connection/session authentication failed')
    except BaseException:
        dev.close()
        raise
    return dev


def check_connection(settings=None, key=None):
    dev = connect(settings, key)
    dev.close()


def save_config(settings):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Replace atomically so an interruption never leaves a half-written config.
    with tempfile.NamedTemporaryFile(mode='w', dir=CONFIG_DIR, delete=False) as stream:
        path = Path(stream.name)
        try:
            stream.write(json.dumps(settings, indent=2) + '\n')
            stream.close()
            path.replace(CONFIG)
        finally:
            path.unlink(missing_ok=True)


def discover():
    # No DPS polling, force scanning, or keys loaded from a working-directory
    # devices.json. TinyTuya treats an empty list as a request to load that file.
    devices = tinytuya.deviceScan(False, 12, poll=False, forcescan=False,
                                 show_timer=False, tuyadevices=[{}])
    found = []
    for ip, device in devices.items():
        device_id = device.get('gwId') or device.get('id')
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        if isinstance(device_id, str) and device_id:
            found.append({'device_id': device_id, 'ip': ip, 'version': device.get('version')})
    return sorted(found, key=lambda item: (item['device_id'], item['ip']))


def inventory():
    try:
        data = json.loads((CONFIG_DIR / 'inventory.json').read_text())
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError('Invalid inventory')
        return {item['id']: item for item in data if isinstance(item.get('id'), str)}
    except (FileNotFoundError, ValueError):
        return {}


def setup(device_id=None, non_interactive=False):
    """Discover and authenticate a physical hub before saving any configuration."""
    interactive = not non_interactive and sys.stdin.isatty() and sys.stdout.isatty()
    logged_in = False

    def authorize():
        nonlocal logged_in
        if logged_in:
            raise RuntimeError('Login did not provide this hub\'s key. See README key alternatives; '
                               'then rerun ac setup. Configuration was not changed.')
        if not interactive:
            raise RuntimeError('Smart Life login is required. Run ac setup in a terminal, '
                               'or run ac login first. Configuration was not changed.')
        print('Smart Life QR authorization is needed to identify hubs or retrieve a missing key.\n'
              'This uses the public Home Assistant sharing registration and saves returned device\n'
              'keys in Keychain. Review the access requested in Smart Life before approving.')
        if input('Open Smart Life QR login? [y/N] ').strip().lower() not in ('y', 'yes'):
            raise RuntimeError('Setup cancelled; configuration was not changed.')
        import setup_keys
        try:
            setup_keys.login(CONFIG_DIR)
        except (RuntimeError, ValueError, OSError):
            # SDK errors can include credentials; do not echo their payloads.
            raise RuntimeError('Smart Life login failed or expired. Rerun ac setup to retry; '
                               'configuration was not changed.') from None
        logged_in = True

    try:
        previous = config() if CONFIG.exists() else None
    except ValueError:
        # Preserve malformed files until the user explicitly selects a replacement.
        if not device_id:
            raise RuntimeError('Invalid configuration. Rerun ac setup --device-id PHYSICAL_HUB_ID '
                               'to replace it after validation, or repair config.json.') from None
        previous = None

    target = device_id or (previous['device_id'] if previous else None)
    if previous and target == previous['device_id']:
        # Check Keychain separately: access denial must never trigger QR login.
        try:
            key = local_key(target)
        except MissingKey:
            authorize()
            key = local_key(target)
        try:
            check_connection(previous, key)
        except (RuntimeError, OSError):
            print('Existing hub did not authenticate. Looking for its current LAN address…')
        else:
            print('Setup ready: existing hub authenticated; no IR sent. Use ac --help for controls.')
            return

    print('Discovering Tuya LAN devices (about 12 seconds)…', flush=True)
    devices = discover()
    if not devices:
        raise RuntimeError('No Tuya LAN devices found. Check hub power, the same Wi-Fi/LAN, and '
                           'guest/client isolation, then rerun ac setup. For a hub that does not '
                           'broadcast, use ac configure with its router DHCP address, then ac check.')
    known = inventory()
    if not target and not known:
        authorize()
        known = inventory()
    candidates = [item for item in devices if item['device_id'] == target] if target else [
        item for item in devices if known.get(item['device_id'], {}).get('category') == 'wnykq']
    if not candidates:
        raise RuntimeError('Configured/requested hub was not discovered. Check power and LAN access, '
                           'then rerun ac setup. To select a replacement, use --device-id.' if target else
                           'No known physical IR hubs were discovered. Check Smart Life pairing '
                           'and LAN access; run ac login to refresh inventory. For an unrecognized '
                           'hub category, use ac setup --device-id PHYSICAL_HUB_ID.')
    if len(candidates) > 1:
        print('Choose the physical Wi-Fi IR hub (not a virtual AC remote):')
        for number, item in enumerate(candidates, 1):
            name = known.get(item['device_id'], {}).get('name', 'IR hub')
            # repr prevents names from injecting terminal control sequences.
            print(f"  {number}. {name!r} — {item['device_id']!r} at {item['ip']} "
                  f"(protocol {item['version']!r})")
        if target:
            raise RuntimeError('Multiple addresses advertised for this hub; resolve the LAN '
                               'conflict or use ac configure with its verified address.')
        if not interactive:
            raise RuntimeError('Multiple IR hubs found. Rerun ac setup --device-id PHYSICAL_HUB_ID '
                               'to select one. Configuration was not changed.')
        answer = input('Hub number (Enter to cancel): ').strip()
        if not answer.isdecimal() or not 1 <= int(answer) <= len(candidates):
            raise RuntimeError('Setup cancelled: no valid hub selected; configuration was not changed.')
        chosen = candidates[int(answer) - 1]
    else:
        chosen = candidates[0]
    if str(chosen['version']) != '3.5':
        raise RuntimeError('This CLI requires Tuya protocol 3.5; the discovered hub advertises '
                           f"{chosen['version']!r}. Configuration was not changed.")
    print(f"Using physical hub {chosen['device_id']!r} at {chosen['ip']}.")
    try:
        key = local_key(chosen['device_id'])
    except MissingKey:
        authorize()
        key = local_key(chosen['device_id'])
    settings = dict(previous or {}, device_id=chosen['device_id'], ip=chosen['ip'])
    try:
        check_connection(settings, key)
    except (RuntimeError, OSError):
        raise RuntimeError('Hub authentication failed; configuration was not changed. Check LAN '
                           'access and Keychain permission. If the hub was re-paired, refresh its '
                           'key with ac login or ac key, then rerun ac setup.') from None
    save_config(settings)
    print('Setup complete: authenticated LAN connection OK; no IR sent. '
          'Use ac --help for controls; ac check repeats this safe connectivity check.')


def control(dev, command):
    payload = dev.generate_payload(tinytuya.CONTROL, {'201': json.dumps(command)})
    dev._send_receive_quick(payload, 1)
    if not any(m.cmd == 13 and m.retcode == 0 and m.crc_good for m in dev.raw_recv):
        raise RuntimeError('No authenticated success ACK from IR hub; transmission outcome unknown')


def decode_report(payload):
    start = payload.find(b'{')
    if start < 0:
        return {}
    data = json.loads(payload[start:])
    return data.get('dps', data.get('data', {}).get('dps', {}))


def validate_code(code):
    raw = base64.b64decode(code, validate=True)
    if len(raw) < 4 or len(raw) % 2:
        raise ValueError('Expected base64 of little-endian 16-bit pulse timings')
    pulses = struct.unpack('<' + 'H' * (len(raw) // 2), raw)
    if not all(pulses):
        raise ValueError('IR code contains a zero-length pulse')
    return pulses


def load_codes():
    codes = json.loads((ROOT / 'daikin200-template.json').read_text())
    if CODES.exists():
        codes.update(json.loads(CODES.read_text()))
    return codes


def learn(name, seconds):
    dev = connect()
    try:
        control(dev, {'control': 'study_exit'})
        control(dev, {'control': 'study'})
        print(f'Learning {name}: point the Daikin remote at the hub and press the intended button now.', flush=True)
        deadline = time.monotonic() + seconds
        heartbeat_at = time.monotonic() + 4
        rearm_at = time.monotonic() + 20
        pending = []
        while time.monotonic() < deadline:
            try:
                msg = pending.pop(0) if pending else dev._receive()
                if not msg.crc_good:
                    continue
                dps = decode_report(msg.payload)
                code = dps.get('202')
                if code:
                    pulses = validate_code(code)
                    codes = load_codes()
                    codes[name] = {'base64': code, 'source': 'physical-remote', 'captured_at': time.time()}
                    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
                    CODES.write_text(json.dumps(codes, indent=2) + '\n')
                    print(f'Saved {name}: {len(pulses)} pulse durations', flush=True)
                    return
            except socket.timeout:
                pass
            if time.monotonic() >= heartbeat_at:
                dev._send_receive_quick(dev.generate_payload(tinytuya.HEART_BEAT), 1)
                pending.extend(dev.raw_recv)
                heartbeat_at = time.monotonic() + 4
            if time.monotonic() >= rearm_at and not pending:
                control(dev, {'control': 'study'})
                pending.extend(dev.raw_recv)
                rearm_at = time.monotonic() + 20
        raise RuntimeError('No IR capture received before timeout')
    finally:
        try:
            control(dev, {'control': 'study_exit'})
        finally:
            dev.close()


def send(name, dry_run=False):
    codes = load_codes()
    if name not in codes and 'capture-a' in codes and (name in ('on', 'off') or name.startswith('cool-')):
        temperature = int(name[5:]) if name.startswith('cool-') else 28
        template = validate_code(codes['capture-a']['base64'])
        codes[name] = {'base64': daikin.encode(template, temperature, power=name != 'off', fan_auto=name == 'on')}
    if name not in codes:
        raise ValueError(f'No verified code for {name}. Capture it first: ./ac learn {name}')
    code = codes[name]['base64']
    pulses = validate_code(code)
    command = {'control': 'send_ir', 'type': 0, 'head': '', 'key1': '1' + code}
    if dry_run:
        print(json.dumps({'ip': config()['ip'], 'port': 6668, 'protocol': '3.5', 'dp': 201,
                          'preset': name, 'pulse_count': len(pulses), 'command': command}, indent=2))
        return
    dev = connect()
    try:
        control(dev, command)
    finally:
        dev.close()
    print(f'LAN IR command acknowledged: {name}. Physical AC response requires observation.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version='%(prog)s 0.1.1')
    sub = parser.add_subparsers(dest='action', required=True)
    for name in ('on', 'off'):
        sub.add_parser(name).add_argument('--dry-run', action='store_true')
    temp = sub.add_parser('temp', help='Set cooling/on temperature using a Daikin200 frame')
    temp.add_argument('temperature', type=int, choices=range(16,31))
    temp.add_argument('--dry-run', action='store_true')
    capture = sub.add_parser('learn', help='Capture a full-state code from the physical remote')
    capture.add_argument('name', help='on, off, or cool-24 (etc.)')
    capture.add_argument('--seconds', type=int, default=60)
    sub.add_parser('list')
    wizard = sub.add_parser('setup', help='Discover, configure, and validate an IR hub without sending IR')
    wizard.add_argument('--device-id', help='Select a physical IR hub explicitly (also for scripts)')
    wizard.add_argument('--non-interactive', action='store_true',
                        help='Never prompt or start login; fail if authorization or a choice is needed')
    sub.add_parser('login', help='Retrieve local keys with Smart Life QR login')
    sub.add_parser('key', help='Save an existing local key via a hidden prompt')
    sub.add_parser('check', help='Authenticate to the hub without sending IR')
    sub.add_parser('scan', help='Discover Tuya LAN devices')
    manual = sub.add_parser('configure', help='Manually save hub ID and LAN IP, not secrets')
    manual.add_argument('--device-id', required=True)
    manual.add_argument('--ip', required=True)
    args = parser.parse_args()
    if args.action == 'setup':
        try:
            setup(args.device_id, args.non_interactive)
        except (EOFError, KeyboardInterrupt):
            raise RuntimeError('Setup interrupted; rerun ac setup to continue. '
                               'A completed login may already have saved keys in Keychain.') from None
    elif args.action == 'configure':
        ipaddress.IPv4Address(args.ip)
        save_config({'device_id': args.device_id, 'ip': args.ip})
        print(f'Saved configuration to {CONFIG}')
    elif args.action == 'login':
        import setup_keys
        setup_keys.login(CONFIG_DIR)
    elif args.action == 'key':
        import setup_keys
        setup_keys.manual_key(config()['device_id'])
    elif args.action == 'check':
        check_connection()
        print('Authenticated LAN connection OK; no IR sent.')
    elif args.action == 'scan':
        print(json.dumps(discover(), indent=2))
    elif args.action == 'learn':
        learn(args.name, args.seconds)
    elif args.action == 'list':
        print(json.dumps({'captures': list(load_codes()),
                          'generated': ['on (cooling 28C, auto fan)', 'off', 'temp 16..30']}, indent=2))
    else:
        name = f'cool-{args.temperature}' if args.action == 'temp' else args.action
        send(name, args.dry_run)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from None
    except (EOFError, KeyboardInterrupt):
        raise SystemExit('Cancelled.') from None
