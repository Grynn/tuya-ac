import unittest
import ac
import daikin


class DaikinTests(unittest.TestCase):
    def setUp(self):
        self.pulses = ac.validate_code(ac.load_codes()['capture-a']['base64'])

    def test_original_frame_matches_capture(self):
        self.assertEqual(daikin.decode(self.pulses).hex(),
                         '11da174804004e11da17480073002100002250002000000070')

    def test_temperature_range_and_checksums(self):
        original = daikin.decode(self.pulses)
        for temp in range(16, 31):
            pulses = ac.validate_code(daikin.encode(self.pulses, temp))
            frame = daikin.decode(pulses)
            self.assertEqual(((frame[17] & 0x7e) >> 1) + 9, temp)
            self.assertEqual(frame[14] & 1, 1)
            self.assertEqual(frame[:17], original[:17])
            self.assertEqual(frame[18:24], original[18:24])
            self.assertEqual(pulses[::2], self.pulses[::2])
            for i in (1, 115, 117, 407):
                self.assertEqual(pulses[i], self.pulses[i])

    def test_24_known_frame(self):
        frame = daikin.decode(ac.validate_code(daikin.encode(self.pulses, 24)))
        self.assertEqual(frame.hex(), '11da174804004e11da17480073002100001e5000200000006c')

    def test_off(self):
        frame = daikin.decode(ac.validate_code(daikin.encode(self.pulses, 24, False)))
        self.assertEqual(frame[14], 0x20)
        self.assertEqual(frame[18], 0)

    def test_bounds_and_corruption(self):
        for temp in (15, 31):
            with self.assertRaises(ValueError): daikin.encode(self.pulses, temp)
        corrupt = list(self.pulses)
        corrupt[3] = 708
        with self.assertRaises(ValueError): daikin.decode(corrupt)


if __name__ == '__main__': unittest.main()
