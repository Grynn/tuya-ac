import base64
import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import ac


class IRTests(unittest.TestCase):
    def test_nested_report_with_version_header(self):
        payload = b'3.5' + bytes(12) + json.dumps({'data':{'dps':{'202':'capture'}}}).encode()
        self.assertEqual(ac.decode_report(payload), {'202':'capture'})

    def test_missing_code_never_connects(self):
        with patch.object(ac, 'load_codes', return_value={}), patch.object(ac,'connect') as connect:
            with self.assertRaisesRegex(ValueError,'No verified code'):
                ac.send('on')
            connect.assert_not_called()

    def test_failed_ack_is_not_reported_as_success(self):
        dev=Mock()
        dev.raw_recv=[SimpleNamespace(cmd=13,retcode=1,crc_good=True)]
        with self.assertRaisesRegex(RuntimeError,'No authenticated success ACK'):
            ac.control(dev,{'control':'study_exit'})

    def test_unauthenticated_ack_is_rejected(self):
        dev=Mock()
        dev.raw_recv=[SimpleNamespace(cmd=13,retcode=0,crc_good=False)]
        with self.assertRaises(RuntimeError):
            ac.control(dev,{'control':'study_exit'})

    def test_send_transmits_exact_pulses_and_closes_socket(self):
        code=base64.b64encode(struct.pack('<4H',3500,1700,450,420)).decode()
        dev=Mock()
        with patch.object(ac,'load_codes',return_value={'cool-24':{'base64':code}}), patch.object(ac,'connect',return_value=dev), patch.object(ac,'control') as control:
            ac.send('cool-24')
            control.assert_called_once_with(dev,{'control':'send_ir','type':0,'head':'','key1':'1'+code})
            dev.close.assert_called_once()

    def test_invalid_code_never_connects(self):
        with patch.object(ac,'load_codes',return_value={'on':{'base64':'bad!'}}), patch.object(ac,'connect') as connect:
            with self.assertRaises(ValueError):
                ac.send('on')
            connect.assert_not_called()


if __name__=='__main__': unittest.main()
