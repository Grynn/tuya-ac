import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import ac
import setup_keys


class SetupTests(unittest.TestCase):
    def test_missing_configuration_fails_before_key_lookup(self):
        with TemporaryDirectory() as directory, patch.object(ac, 'CONFIG', Path(directory) / 'config.json'), patch.object(ac.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'Missing configuration'):
                ac.connect()
            run.assert_not_called()

    def test_configured_hub_uses_keychain(self):
        device = Mock()
        device._get_socket.return_value = True
        with patch.object(ac, 'config', return_value={'device_id': 'example-id', 'ip': '192.0.2.1'}), patch.object(ac.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='0123456789abcdef\n')) as run, patch.object(ac.tinytuya, 'Device', return_value=device) as constructor:
            ac.connect()
            self.assertIn('example-id', run.call_args.args[0])
            self.assertEqual(constructor.call_args.args, ('example-id', '192.0.2.1', '0123456789abcdef'))

    def test_dry_run_never_connects(self):
        with patch.object(ac, 'config', return_value={'ip': '192.0.2.1'}), patch.object(ac, 'connect') as connect, patch('builtins.print'):
            ac.send('cool-28', dry_run=True)
            connect.assert_not_called()

    def test_bad_key_rejected_before_keychain(self):
        with patch.object(setup_keys.subprocess, 'run') as run:
            with self.assertRaises(ValueError): setup_keys.save_key('example-id', 'short')
            run.assert_not_called()

    def test_qr_login_saves_key_but_not_tokens(self):
        login = Mock()
        login.qr_code.return_value = {'success': True, 'result': {'qrcode': 'sample-qr-token'}}
        login.login_result.return_value = (True, {'endpoint': 'https://example.invalid', 'terminal_id': 'terminal', 'access_token': 'sample-access-token'})
        manager = Mock()
        manager.device_map = {'example-id': SimpleNamespace(id='example-id', name='IR Hub', category='wnykq', local_key='0123456789abcdef')}
        with TemporaryDirectory() as directory, patch('builtins.input', return_value='user-code'), patch('builtins.print') as output, patch.object(setup_keys, 'LoginControl', return_value=login), patch.object(setup_keys, 'Manager', return_value=manager), patch.object(setup_keys, 'save_key') as save, patch.object(setup_keys.subprocess, 'run'):
            setup_keys.login(Path(directory))
            save.assert_called_once_with('example-id', '0123456789abcdef')
            data = (Path(directory) / 'inventory.json').read_text()
            self.assertTrue(json.loads(data)[0]['local_key_saved'])
            for secret in ('0123456789abcdef', 'sample-access-token', 'sample-qr-token'):
                self.assertNotIn(secret, data)
                self.assertNotIn(secret, str(output.call_args_list))


if __name__ == '__main__': unittest.main()
