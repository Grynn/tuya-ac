"""Local-only Tuya IR CLI. Credentials are retrieved from macOS Keychain."""
import argparse
import base64
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import time

import tinytuya

import daikin

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get('AC_CONFIG_DIR', str(Path.home() / '.config' / 'tuya-ac')))
CODES = CONFIG_DIR / 'ir-codes.json'
CONFIG = CONFIG_DIR / 'config.json'


def config():
    if not CONFIG.exists():
        raise ValueError('Missing configuration. Run ac configure --device-id ID --ip LAN_IP')
    data = json.loads(CONFIG.read_text())
    if not data.get('device_id') or not data.get('ip'):
        raise ValueError('Configuration requires device_id and ip')
    return data


def connect():
    settings = config()
    result = subprocess.run(['security', 'find-internet-password', '-s',
                             'tuya-local-device', '-a', settings['device_id'], '-w'],
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Device local key missing from macOS Keychain')
    dev = tinytuya.Device(settings['device_id'], settings['ip'], result.stdout.rstrip('\n'),
                         version=3.5, connection_timeout=2, persist=True)
    dev.disabledetect = True
    dev.set_socketRetryLimit(1)
    if dev._get_socket(False) is not True:
        dev.close()
        raise RuntimeError('LAN connection/session authentication failed')
    return dev


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
        temperature = int(name[5:]) if name.startswith('cool-') else 24
        template = validate_code(codes['capture-a']['base64'])
        codes[name] = {'base64': daikin.encode(template, temperature, power=name != 'off')}
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
    parser.add_argument('--version', action='version', version='%(prog)s 0.1.0')
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
    sub.add_parser('login', help='Retrieve local keys with Smart Life QR login')
    sub.add_parser('key', help='Save an existing local key via a hidden prompt')
    sub.add_parser('check', help='Authenticate to the hub without sending IR')
    sub.add_parser('scan', help='Discover Tuya LAN devices')
    setup = sub.add_parser('configure', help='Save hub ID and LAN IP, not secrets')
    setup.add_argument('--device-id', required=True)
    setup.add_argument('--ip', required=True)
    args = parser.parse_args()
    if args.action == 'configure':
        import ipaddress
        ipaddress.IPv4Address(args.ip)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        CONFIG.write_text(json.dumps({'device_id': args.device_id, 'ip': args.ip}, indent=2) + '\n')
        print(f'Saved configuration to {CONFIG}')
    elif args.action == 'login':
        import setup_keys
        setup_keys.login(CONFIG_DIR)
    elif args.action == 'key':
        import setup_keys
        setup_keys.manual_key(config()['device_id'])
    elif args.action == 'check':
        dev = connect()
        dev.close()
        print('Authenticated LAN connection OK; no IR sent.')
    elif args.action == 'scan':
        devices = tinytuya.deviceScan(False, 12)
        print(json.dumps([{'ip': ip, 'device_id': d.get('gwId', d.get('id')), 'version': d.get('version')} for ip, d in devices.items()], indent=2))
    elif args.action == 'learn':
        learn(args.name, args.seconds)
    elif args.action == 'list':
        print(json.dumps({'captures': list(load_codes()),
                          'generated': ['on (cooling 24C)', 'off', 'temp 16..30']}, indent=2))
    else:
        name = f'cool-{args.temperature}' if args.action == 'temp' else args.action
        send(name, args.dry_run)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from None
