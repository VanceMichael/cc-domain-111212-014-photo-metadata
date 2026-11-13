import struct

STANDALONE_MARKERS = set(range(0xD0, 0xD8)) | {0x01}

def scan_segments(data):
    """Return JPEG marker segments before the image data.

    Offsets are absolute in the supplied bytes.  ``payload_offset`` and
    ``payload_size`` describe the bytes between the length field and the end
    of that segment.  The scan deliberately stops at SOS: metadata segments
    are all before entropy-coded image data and pixels are never decoded.
    """
    if data[:2] != b'\xff\xd8':
        raise ValueError('JPEG SOI 缺失')
    position = 2
    segments = []
    while position < len(data):
        if data[position:position + 1] != b'\xff':
            raise ValueError('JPEG 标记不同步')
        marker_offset = position
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            raise ValueError('JPEG 标记截断')
        marker = data[position]
        position += 1
        if marker == 0x00:
            raise ValueError('JPEG 标记不同步')
        if marker == 0xD9:
            break
        if marker in STANDALONE_MARKERS:
            continue
        if marker == 0xDA:
            # Start of scan: no metadata segment can follow the entropy data
            # in the area this collector is allowed to read.
            break
        if position + 2 > len(data):
            raise ValueError('JPEG 段头截断')
        length = struct.unpack_from('>H', data, position)[0]
        if length < 2 or position + length > len(data):
            raise ValueError('JPEG 段长度越界')
        segments.append(dict(marker=f'{marker:02x}', offset=marker_offset,
                             payload_offset=position + 2, payload_size=length - 2))
        position += length
    return segments

def inspect_bytes(data):
    return [dict(marker=segment['marker'], offset=segment['offset'],
                 payload_size=segment['payload_size'])
            for segment in scan_segments(data)]
