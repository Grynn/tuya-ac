"""Offline setup regressions. Every external boundary is blocked or mocked."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import ac
import setup_keys


KEY = 'fixture-key-1234'


class SetupTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.directory = Path(directory)
        self.config_path = self.directory / 'config.json'
        for name, value in [('CONFIG_DIR', self.directory), ('CONFIG', self.config_path),
                            ('CODES', self.directory / 'ir-codes.json')]:
            self.enterContext(patch.object(ac, name, value))
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.enterContext(patch.object(self.output, 'isatty', return_value=True))
        self.enterContext(patch('sys.stdin.isatty', return_value=True))
        self.prompt = self.enterContext(patch('builtins.input', side_effect=AssertionError('Unexpected prompt')))
        self.login = self.enterContext(patch.object(setup_keys, 'login',
                                                  side_effect=AssertionError('Unexpected login')))
        self.enterContext(patch('socket.socket', side_effect=AssertionError('Real network forbidden')))
        self.security = self.enterContext(patch('subprocess.run', side_effect=self.keychain))
        self.scan = self.enterContext(patch.object(ac.tinytuya, 'deviceScan', return_value={}))
        self.device = Mock(spec=['disabledetect', 'set_socketRetryLimit', '_get_socket', 'close'])
        self.device._get_socket.return_value = True
        self.device_factory = self.enterContext(patch.object(ac.tinytuya, 'Device', return_value=self.device))
        for name in ('send', 'control', 'learn'):
            self.enterContext(patch.object(ac, name, side_effect=AssertionError('Device control forbidden')))
        self.missing_keys = set()

    def tearDown(self):
        self.assertNotIn(KEY, self.output.getvalue())

    def keychain(self, args, **kwargs):
        self.assertEqual(args[:4], ['security', 'find-internet-password', '-s', 'tuya-local-device'])
        self.assertEqual(args[4], '-a')
        self.assertEqual(args[6:], ['-w'])
        return subprocess.CompletedProcess(args, 44 if args[5] in self.missing_keys else 0,
                                           stdout=KEY + '\n', stderr='')

    def run_setup(self, *args):
        with patch('sys.argv', ['ac', 'setup', *args]):
            ac.main()

    def inventory(self, *ids):
        (self.directory / 'inventory.json').write_text(json.dumps([
            {'id': device_id, 'name': 'Smart IR ' + device_id, 'category': 'wnykq',
             'local_key_saved': False} for device_id in ids]))

    def devices(self, *ids):
        self.scan.return_value = {
            f'192.0.2.{index}': {'gwId': device_id, 'version': '3.5'}
            for index, device_id in enumerate(ids, 1)}

    def existing(self):
        settings = {'device_id': 'hub-a', 'ip': '192.0.2.99', 'custom': 'preserved'}
        self.config_path.write_text(json.dumps(settings))
        return self.config_path.read_bytes()

    def assert_no_config(self):
        self.assertFalse(self.config_path.exists())

    def test_single_known_hub_selected_and_checked_without_control(self):
        self.inventory('hub-a')
        self.devices('hub-a', 'virtual-remote', 'socket')
        self.run_setup()
        self.assertEqual(json.loads(self.config_path.read_text()),
                         {'device_id': 'hub-a', 'ip': '192.0.2.1'})
        self.device_factory.assert_called_once_with('hub-a', '192.0.2.1', KEY,
                                                    version=3.5, connection_timeout=2, persist=True)
        self.device._get_socket.assert_called_once_with(False)
        self.device.close.assert_called_once_with()
        self.security.assert_called_once()
        self.assertTrue(self.device.disabledetect)
        self.scan.assert_called_once_with(False, 12, poll=False, forcescan=False,
                                          show_timer=False, tuyadevices=[{}])
        self.assertIn('no IR sent', self.output.getvalue())
        self.login.assert_not_called()

    def test_multiple_hubs_require_numbered_choice(self):
        self.inventory('hub-a', 'hub-b')
        self.devices('hub-a', 'hub-b')
        self.prompt.side_effect = ['2']
        self.run_setup()
        self.assertEqual(json.loads(self.config_path.read_text())['device_id'], 'hub-b')
        self.assertIn('Smart IR hub-a', self.output.getvalue())
        self.assertIn('192.0.2.2', self.output.getvalue())

    def test_multiple_hubs_noninteractive_never_guess(self):
        self.inventory('hub-a', 'hub-b')
        self.devices('hub-a', 'hub-b')
        with self.assertRaisesRegex(RuntimeError, 'Multiple IR hubs'):
            self.run_setup('--non-interactive')
        self.prompt.assert_not_called()
        self.device_factory.assert_not_called()
        self.assert_no_config()

    def test_redirected_input_or_output_disables_prompts(self):
        self.inventory('hub-a', 'hub-b')
        self.devices('hub-a', 'hub-b')
        for stream in ('stdin', 'stdout'):
            with self.subTest(stream=stream), patch(f'sys.{stream}.isatty', return_value=False):
                with self.assertRaisesRegex(RuntimeError, 'Multiple IR hubs'):
                    self.run_setup()
        self.prompt.assert_not_called()
        self.assert_no_config()

    def test_explicit_hub_works_without_inventory_or_login(self):
        self.devices('hub-a', 'hub-b')
        self.run_setup('--device-id', 'hub-b', '--non-interactive')
        self.assertEqual(json.loads(self.config_path.read_text())['device_id'], 'hub-b')
        self.login.assert_not_called()

    def test_unknown_devices_are_not_guessed(self):
        self.inventory('absent-hub')
        self.devices('socket', 'virtual-remote')
        with self.assertRaisesRegex(RuntimeError, 'No known physical IR hubs'):
            self.run_setup()
        self.device_factory.assert_not_called()
        self.assert_no_config()

    def test_no_devices_does_not_start_login(self):
        with self.assertRaisesRegex(RuntimeError, 'No Tuya LAN devices'):
            self.run_setup()
        self.login.assert_not_called()
        self.assert_no_config()

    def test_missing_inventory_guides_login_once(self):
        self.devices('hub-a')
        self.prompt.side_effect = ['yes']
        self.login.side_effect = lambda directory: self.inventory('hub-a')
        self.run_setup()
        self.login.assert_called_once_with(self.directory)
        self.assertIn('Review the access', self.output.getvalue())

    def test_missing_inventory_noninteractive_does_not_login(self):
        self.devices('hub-a')
        with self.assertRaisesRegex(RuntimeError, 'login is required'):
            self.run_setup('--non-interactive')
        self.login.assert_not_called()
        self.assert_no_config()

    def test_missing_key_guides_login_only_when_confirmed(self):
        self.inventory('hub-a')
        self.devices('hub-a')
        self.missing_keys.add('hub-a')
        self.prompt.side_effect = ['y']
        self.login.side_effect = lambda directory: self.missing_keys.clear()
        self.run_setup()
        self.login.assert_called_once()
        self.assertTrue(self.config_path.exists())

    def test_login_returns_without_selected_key_leaves_config_untouched(self):
        self.inventory('hub-a')
        self.devices('hub-a')
        self.missing_keys.add('hub-a')
        self.prompt.side_effect = ['y']
        self.login.side_effect = None
        with self.assertRaisesRegex(ac.MissingKey, 'README key alternatives'):
            self.run_setup()
        self.login.assert_called_once()
        self.assert_no_config()

    def test_missing_key_noninteractive_does_not_login(self):
        self.inventory('hub-a')
        self.devices('hub-a')
        self.missing_keys.add('hub-a')
        with self.assertRaisesRegex(RuntimeError, 'login is required'):
            self.run_setup('--non-interactive')
        self.login.assert_not_called()
        self.assert_no_config()

    def test_login_without_key_never_loops(self):
        self.devices('hub-a')
        self.missing_keys.add('hub-a')
        self.prompt.side_effect = ['y']
        self.login.side_effect = lambda directory: self.inventory('hub-a')
        with self.assertRaisesRegex(RuntimeError, 'did not provide'):
            self.run_setup()
        self.login.assert_called_once()
        self.assert_no_config()

    def test_login_failure_is_sanitized(self):
        self.devices('hub-a')
        self.prompt.side_effect = ['y']
        self.login.side_effect = RuntimeError(KEY)
        with self.assertRaisesRegex(RuntimeError, 'failed or expired') as error:
            self.run_setup()
        self.assertNotIn(KEY, str(error.exception))
        self.assert_no_config()

    def test_declined_login_does_not_authorize(self):
        self.devices('hub-a')
        self.prompt.side_effect = ['']
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.run_setup()
        self.login.assert_not_called()
        self.assert_no_config()

    def test_interrupted_login_preserves_previous_configuration(self):
        before = self.existing()
        self.missing_keys.add('hub-a')
        self.prompt.side_effect = ['y']
        self.login.side_effect = KeyboardInterrupt
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            self.run_setup()
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_cancelled_selection_and_eof_do_not_configure(self):
        self.inventory('hub-a', 'hub-b')
        self.devices('hub-a', 'hub-b')
        for answer in ('', '0', '3', 'abc', EOFError, KeyboardInterrupt):
            with self.subTest(answer=answer):
                self.prompt.side_effect = [answer]
                with self.assertRaisesRegex(RuntimeError, 'cancelled|interrupted'):
                    self.run_setup()
                self.assert_no_config()
        self.device_factory.assert_not_called()

    def test_existing_setup_is_not_rewritten_or_rediscovered(self):
        before = self.existing()
        self.run_setup('--non-interactive')
        self.assertEqual(self.config_path.read_bytes(), before)
        self.scan.assert_not_called()
        self.login.assert_not_called()
        self.assertIn('existing hub authenticated', self.output.getvalue())

    def test_existing_setup_with_missing_key_can_login_without_scan(self):
        before = self.existing()
        self.missing_keys.add('hub-a')
        self.prompt.side_effect = ['y']
        self.login.side_effect = lambda directory: self.missing_keys.clear()
        self.run_setup()
        self.assertEqual(self.config_path.read_bytes(), before)
        self.scan.assert_not_called()

    def test_stale_ip_refreshes_same_hub_and_preserves_other_settings(self):
        self.existing()
        self.inventory('hub-a', 'hub-b')
        self.devices('hub-a', 'hub-b')
        self.device._get_socket.side_effect = [False, True]
        self.run_setup('--non-interactive')
        self.assertEqual(json.loads(self.config_path.read_text()),
                         {'device_id': 'hub-a', 'ip': '192.0.2.1', 'custom': 'preserved'})
        self.assertEqual(self.device.close.call_count, 2)
        self.login.assert_not_called()

    def test_stale_hub_is_not_replaced_by_another_candidate(self):
        before = self.existing()
        self.inventory('hub-b')
        self.devices('hub-b')
        self.device._get_socket.return_value = False
        with self.assertRaisesRegex(RuntimeError, 'hub was not discovered'):
            self.run_setup()
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_failed_validation_preserves_previous_bytes_and_closes_socket(self):
        before = self.existing()
        self.devices('hub-a')
        self.device._get_socket.side_effect = [False, OSError('offline')]
        with self.assertRaisesRegex(RuntimeError, 'authentication failed'):
            self.run_setup()
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(self.device.close.call_count, 2)
        self.login.assert_not_called()

    def test_failed_validation_never_creates_new_config(self):
        self.inventory('hub-a')
        self.devices('hub-a')
        self.device._get_socket.return_value = False
        with self.assertRaisesRegex(RuntimeError, 'authentication failed'):
            self.run_setup()
        self.assert_no_config()
        self.login.assert_not_called()

    def test_keychain_denial_never_triggers_login(self):
        before = self.existing()
        self.security.side_effect = None
        self.security.return_value = subprocess.CompletedProcess([], 128, '', KEY)
        with self.assertRaisesRegex(RuntimeError, 'Could not read macOS Keychain'):
            self.run_setup()
        self.assertEqual(self.config_path.read_bytes(), before)
        self.login.assert_not_called()
        self.scan.assert_not_called()

    def test_malformed_config_requires_explicit_replacement(self):
        for content in ('not json', '[]', '{"device_id":"hub-a","ip":42}'):
            with self.subTest(content=content):
                self.config_path.write_text(content)
                with self.assertRaisesRegex(RuntimeError, 'Invalid configuration'):
                    self.run_setup()
                self.assertEqual(self.config_path.read_text(), content)
        self.scan.assert_not_called()
        self.devices('hub-a')
        self.run_setup('--device-id', 'hub-a')
        self.assertEqual(json.loads(self.config_path.read_text())['device_id'], 'hub-a')

    def test_corrupt_inventory_can_be_refreshed(self):
        (self.directory / 'inventory.json').write_text('broken')
        self.devices('hub-a')
        self.prompt.side_effect = ['y']
        self.login.side_effect = lambda directory: self.inventory('hub-a')
        self.run_setup()
        self.assertTrue(self.config_path.exists())

    def test_unsupported_protocol_does_not_connect_or_save(self):
        self.inventory('hub-a')
        self.scan.return_value = {'192.0.2.1': {'gwId': 'hub-a', 'version': '3.3'}}
        with self.assertRaisesRegex(RuntimeError, 'requires Tuya protocol 3.5'):
            self.run_setup()
        self.device_factory.assert_not_called()
        self.assert_no_config()

    def test_conflicting_addresses_are_not_selected_for_explicit_id(self):
        self.devices('hub-a', 'hub-a')
        with self.assertRaisesRegex(RuntimeError, 'Multiple addresses'):
            self.run_setup('--device-id', 'hub-a')
        self.device_factory.assert_not_called()
        self.assert_no_config()

    def test_bad_discovery_entries_ignored_and_secrets_not_rendered(self):
        self.scan.return_value = {'bad-ip': {'gwId': 'broken'},
                                  '192.0.2.1': {'id': 'hub-a', 'version': 3.5, 'key': KEY},
                                  '192.0.2.2': {'version': '3.5'}}
        self.run_setup('--device-id', 'hub-a')
        self.assertEqual(json.loads(self.config_path.read_text())['ip'], '192.0.2.1')

    def test_scan_and_check_remain_non_mutating(self):
        before = self.existing()
        self.devices('hub-a')
        for command in ('scan', 'check'):
            with patch('sys.argv', ['ac', command]):
                ac.main()
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertIn('no IR sent', self.output.getvalue())

    def test_atomic_save_failure_preserves_config_and_removes_temp_file(self):
        before = self.existing()
        with patch.object(Path, 'replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                ac.save_config({'device_id': 'new', 'ip': '192.0.2.1'})
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(list(self.directory.iterdir()), [self.config_path])

    def test_command_failure_exits_nonzero_without_traceback(self):
        self.inventory('hub-a', 'hub-b')
        self.devices('hub-a', 'hub-b')
        with patch.dict(os.environ, {'AC_CONFIG_DIR': str(self.directory)}), \
                patch('sys.argv', ['ac', 'setup', '--non-interactive']):
            with self.assertRaises(SystemExit) as error:
                runpy.run_path(str(ac.ROOT / 'ac.py'), run_name='__main__')
        self.assertIn('Multiple IR hubs', error.exception.code)
        self.assert_no_config()

    def test_interrupted_authentication_closes_socket_without_saving(self):
        self.inventory('hub-a')
        self.devices('hub-a')
        self.device._get_socket.side_effect = KeyboardInterrupt
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            self.run_setup()
        self.device.close.assert_called_once_with()
        self.assert_no_config()


class LoginCleanupTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.enterContext(patch('socket.socket', side_effect=AssertionError('Real network forbidden')))
        self.enterContext(patch('builtins.input', return_value='fixture-user-code'))
        self.control = self.enterContext(patch.object(setup_keys, 'LoginControl')).return_value
        self.control.qr_code.return_value = {'success': True, 'result': {'qrcode': 'fixture-token'}}
        self.open = self.enterContext(patch('subprocess.run', side_effect=self.open_image))
        self.enterContext(patch.object(setup_keys, 'save_key', side_effect=AssertionError('Key writes forbidden')))
        self.enterContext(patch.object(setup_keys, 'Manager', side_effect=AssertionError('Cloud access forbidden')))
        self.image_path = None

    def open_image(self, args, **kwargs):
        self.assertEqual(args[0], 'open')
        self.image_path = Path(args[1])
        self.assertTrue(self.image_path.exists())
        return subprocess.CompletedProcess(args, 0)

    def tearDown(self):
        self.assertNotIn('fixture-token', self.output.getvalue())
        self.assertFalse(self.image_path.exists())
        self.assertFalse((self.directory / 'inventory.json').exists())

    def test_interrupted_login_removes_qr_image(self):
        self.control.login_result.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            setup_keys.login(self.directory)

    def test_expired_login_removes_qr_image(self):
        with patch.object(setup_keys.time, 'monotonic', side_effect=[0, 241]):
            with self.assertRaisesRegex(RuntimeError, 'expired'):
                setup_keys.login(self.directory)


if __name__ == '__main__':
    unittest.main()
