import struct

def inspect_bytes(data):
    if data[:2] != b'\xff\xd8':
        raise ValueError('JPEG SOI 缺失')
    position = 2
    result = []
    while position < len(data):
        if data[position:position + 1] != b'\xff':
            raise ValueError('JPEG 标记不同步')
        marker = data[position + 1]
        if marker in (0xD9, 0xDA):
            break
        if position + 4 > len(data):
            raise ValueError('JPEG 段头截断')
        length = struct.unpack_from('>H', data, position + 2)[0]
        if length < 2 or position + 2 + length > len(data):
            raise ValueError('JPEG 段长度越界')
        result.append({'marker': f'{marker:02x}', 'offset': position, 'payload_size': length - 2})
        position += 2 + length
    return result
