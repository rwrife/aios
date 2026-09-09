"""Canonical tiny profile portraits; no filenames, URLs, EXIF or image decoders."""
import base64
import binascii
import struct
import zlib


def portrait(rgb):
    if rgb in (None, ''):
        return ''
    if not isinstance(rgb, str) or len(rgb) != 16384:
        raise ValueError('Invalid profile photo')
    try:
        pixels = base64.b64decode(rgb, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('Invalid profile photo') from None
    if len(pixels) != 64 * 64 * 3:
        raise ValueError('Invalid profile photo')
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    rows = b''.join(b'\0' + pixels[offset:offset + 192] for offset in range(0, len(pixels), 192))
    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 64, 64, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))
    return 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')
