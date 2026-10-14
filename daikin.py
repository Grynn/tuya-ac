"""Daikin200 frames, using measured timings from this remote as the template."""
import base64
import statistics
import struct


def decode(pulses):
    if len(pulses) != 408:
        raise ValueError('Expected a two-section, 200-bit Daikin frame')
    frame = bytearray()
    for start, count in ((2, 7), (118, 18)):
        for byte in range(count):
            value = 0
            for bit in range(8):
                offset = start + 16 * byte + 2 * bit
                mark, space = pulses[offset:offset + 2]
                if not 200 <= mark <= 1000 or not (400 <= space <= 900 or 1400 <= space <= 2300):
                    raise ValueError('Unexpected Daikin bit timing')
                value |= (space > 1000) << bit
            frame.append(value)
    if frame[:3] != bytes.fromhex('11da17') or frame[7:10] != frame[:3]:
        raise ValueError('Invalid Daikin signature')
    if sum(frame[:6]) & 255 != frame[6] or sum(frame[7:24]) & 255 != frame[24]:
        raise ValueError('Invalid Daikin checksum')
    return frame


def encode(template, temperature, power=True):
    if not 16 <= temperature <= 30:
        raise ValueError('Temperature must be 16–30°C')
    frame = decode(template)
    if frame[14] & 0x70 != 0x20 or frame[12] & 0x70 != 0x70:
        raise ValueError('Template must be in cooling mode')
    frame[13] = 0  # Normal operation, rather than a mode-button event.
    frame[14] = (frame[14] & 0xFE) | int(power)
    frame[17] = (frame[17] & 0x81) | ((temperature - 9) << 1)
    if not power:
        frame[18] &= 0x0F
    frame[24] = sum(frame[7:24]) & 255
    spaces = [template[i] for start, count in ((2, 56), (118, 144))
              for i in range(start + 1, start + 2 * count, 2)]
    zero = round(statistics.median(x for x in spaces if x < 1000))
    one = round(statistics.median(x for x in spaces if x > 1000))
    pulses = list(template)
    for start, section in ((2, frame[:7]), (118, frame[7:])):
        for index, byte in enumerate(section):
            for bit in range(8):
                offset = start + index * 16 + bit * 2 + 1
                # Preserve original measured timing when the bit is unchanged.
                if (pulses[offset] > 1000) != bool(byte & (1 << bit)):
                    pulses[offset] = one if byte & (1 << bit) else zero
    return base64.b64encode(struct.pack('<408H', *pulses)).decode()
