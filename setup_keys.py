"""Smart Life sharing login; local keys stay in macOS Keychain."""
import getpass
import json
from pathlib import Path
import subprocess
import tempfile
import time

import qrcode
from tuya_sharing import LoginControl, Manager

# Public Home Assistant registration, not a client secret. This unofficial CLI
# uses Tuya's sharing SDK flow; availability depends on Tuya's service.
CLIENT_ID = 'HA_3y9q4ak7g4ephrvke'


def save_key(device_id, key):
    if len(key.encode()) != 16:
        raise ValueError('Expected a 16-byte Tuya local key')
    result = subprocess.run(
        ['security', 'add-internet-password', '-U', '-s', 'tuya-local-device',
         '-a', device_id, '-w', key, '-r', 'htps',
         str(Path.home() / 'Library/Keychains/login.keychain-db')],
        capture_output=True, text=True)
    if result.returncode:
        # Do not render the command or its arguments (they contain the key).
        raise RuntimeError('Could not save local key to macOS Keychain')


def manual_key(device_id):
    save_key(device_id, getpass.getpass('Local key (hidden): '))
    print('Local key saved to macOS Keychain.')


class TokenListener:
    def update_token(self, token_info):
        # Session is used only during this login and is not written to disk.
        pass


def login(config_dir):
    user_code = input('Smart Life → Me → Settings → Account and Security → User Code: ').strip()
    if not user_code:
        raise ValueError('User Code is required (not the device Virtual ID)')
    control = LoginControl()
    response = control.qr_code(CLIENT_ID, 'haauthorize', user_code)
    if not response.get('success'):
        raise RuntimeError('QR generation failed; check the User Code or use ac key')
    token = response['result']['qrcode']
    with tempfile.TemporaryDirectory(prefix='tuya-ac-') as directory:
        path = Path(directory) / 'smartlife-login.png'
        qrcode.make(f'smartlife--qrLogin?token={token}').save(path)
        print('Scan the QR in Smart Life (Home → + → Scan), then approve login.', flush=True)
        print(f'QR image: {path}', flush=True)
        subprocess.run(['open', str(path)], check=False)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            ok, info = control.login_result(token, CLIENT_ID, user_code)
            if ok:
                break
            time.sleep(2)
        else:
            raise RuntimeError('QR login expired. Run ac login again.')
        endpoint = info.get('endpoint') or info.get('end_point')
        if not endpoint or not info.get('terminal_id'):
            raise RuntimeError('Login response missing endpoint or terminal ID')
        tokens = {k: info[k] for k in ('t', 'uid', 'expire_time', 'access_token', 'refresh_token') if k in info}
        manager = Manager(CLIENT_ID, user_code, info['terminal_id'], endpoint, tokens, TokenListener())
        manager.update_device_cache()
        inventory = []
        for device in manager.device_map.values():
            key = getattr(device, 'local_key', '')
            if key:
                save_key(device.id, key)
            inventory.append({'id': device.id, 'name': device.name,
                              'category': getattr(device, 'category', None),
                              'local_key_saved': bool(key)})
        config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (config_dir / 'inventory.json').write_text(json.dumps(inventory, indent=2) + '\n')
        print(json.dumps(inventory, indent=2))
        print('Local keys retrieved. Run ac setup to discover and validate your physical IR hub.')
