"""Deterministic, entirely synthetic photo metadata fixtures.

No fixture contains real people, cameras or places.  Pixel data is absent by
design: the collector only reads marker segments and metadata structures.
"""

import struct

EXIF_SIGNATURE = b'Exif\x00\x00'
XMP_SIGNATURE = b'http://ns.adobe.com/xap/1.0/\x00'

TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}

def _encode(typ, value, endian):
    if typ == 2:
        return value.encode('utf-8') + b'\x00'
    if typ == 1:
        values = value if isinstance(value, list) else [value]
        return bytes(values)
    if typ == 7:
        return bytes(value)
    if typ == 3:
        values = value if isinstance(value, list) else [value]
        return struct.pack(endian + 'H' * len(values), *values)
    if typ == 4:
        values = value if isinstance(value, list) else [value]
        return struct.pack(endian + 'I' * len(values), *values)
    if typ == 5:
        pairs = value if isinstance(value[0], (list, tuple)) else [value]
        return b''.join(struct.pack(endian + 'II', *pair) for pair in pairs)
    if typ == 10:
        pairs = value if isinstance(value[0], (list, tuple)) else [value]
        return b''.join(struct.pack(endian + 'ii', *pair) for pair in pairs)
    raise ValueError(f'unsupported fixture type {typ}')

def _table_size(entries):
    return 2 + 12 * len(entries) + 4

def build_tiff(ifd0, exif=None, gps=None, thumbnail=None, endian='<',
               pointer_overrides=None, value_offset_overrides=None):
    """Build a TIFF body with a deterministic, bounded IFD layout.

    All offsets are relative to the TIFF start.  Out-of-line value bytes are
    placed directly after their IFD table and counted in the layout, so
    following IFDs and pointers stay valid.  ``pointer_overrides`` and
    ``value_offset_overrides`` inject deliberate out-of-range pointers for
    negative fixtures.
    """
    pointer_overrides = pointer_overrides or {}
    value_offset_overrides = value_offset_overrides or {}
    ifd0 = list(ifd0)
    if exif is not None:
        ifd0.append((0x8769, 4, [0]))
    if gps is not None:
        ifd0.append((0x8825, 4, [0]))
    ifd0.sort(key=lambda entry: entry[0])
    exif = sorted(exif or [], key=lambda entry: entry[0])
    gps = sorted(gps or [], key=lambda entry: entry[0])
    thumbnail = sorted(thumbnail or [], key=lambda entry: entry[0])

    blocks = [('ifd0', ifd0)]
    if exif is not None:
        blocks.append(('exif', exif))
    if gps is not None:
        blocks.append(('gps', gps))
    if thumbnail:
        blocks.append(('thumbnail', thumbnail))

    tag_pointers = {}
    if exif is not None:
        tag_pointers[0x8769] = pointer_overrides.get(0x8769, None)
    if gps is not None:
        tag_pointers[0x8825] = pointer_overrides.get(0x8825, None)

    def out_of_line(entries, context):
        result = {}
        for tag, typ, value in entries:
            raw = _encode(typ, value, endian)
            if (context, tag) in value_offset_overrides:
                result[tag] = None  # size is irrelevant; pointer is injected
            elif tag in tag_pointers and context == 'ifd0':
                result[tag] = 0
            elif len(raw) > 4:
                result[tag] = len(raw)
        return result

    offsets = {}
    cursor = 8
    for context, entries in blocks:
        size = _table_size(entries) + sum(out_of_line(entries, context).values())
        offsets[context] = cursor
        cursor += size

    offsets['ifd0_end'] = offsets['ifd0'] + _table_size(ifd0)
    if exif is not None:
        tag_pointers[0x8769] = pointer_overrides.get(0x8769, offsets['exif'])
    if gps is not None:
        tag_pointers[0x8825] = pointer_overrides.get(
            0x8825, offsets['gps'])
    next0 = pointer_overrides.get('next_ifd0', offsets.get('thumbnail', 0))

    body = bytearray()
    body += (b'II' if endian == '<' else b'MM')
    body += struct.pack(endian + 'HI', 42, offsets['ifd0'])

    for context, entries in blocks:
        body.extend(b'\x00' * (offsets[context] - len(body)))
        table = bytearray(struct.pack(endian + 'H', len(entries)))
        inline_data = []
        data_cursor = offsets[context] + _table_size(entries)
        for tag, typ, value in entries:
            raw = _encode(typ, value, endian)
            count = len(raw) // TYPE_SIZES[typ]
            table += struct.pack(endian + 'HHI', tag, typ, count)
            if context == 'ifd0' and tag in tag_pointers:
                table += struct.pack(endian + 'I', tag_pointers[tag])
            elif (context, tag) in value_offset_overrides:
                table += struct.pack(endian + 'I',
                                     value_offset_overrides[(context, tag)])
            elif len(raw) <= 4:
                table += raw + b'\x00' * (4 - len(raw))
            else:
                table += struct.pack(endian + 'I', data_cursor)
                inline_data.append(raw)
                data_cursor += len(raw)
        table += struct.pack(endian + 'I',
                             next0 if context == 'ifd0' else 0)
        body.extend(table)
        for raw in inline_data:
            body.extend(raw)
    return bytes(body)

def exif_app1(tiff):
    payload = EXIF_SIGNATURE + tiff
    return b'\xff\xe1' + struct.pack('>H', len(payload) + 2) + payload

def xmp_app1(xml):
    payload = XMP_SIGNATURE + xml
    return b'\xff\xe1' + struct.pack('>H', len(payload) + 2) + payload

def jpeg(*segments):
    return b'\xff\xd8' + b''.join(segments) + b'\xff\xd9'

def xmp_document(properties, about=''):
    """properties: list of (qualified-name, text) attributes."""
    attributes = ' '.join(f'{name}="{text}"' for name, text in properties)
    attr_about = f' rdf:about="{about}"' if about else ''
    return (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:tiff="http://ns.adobe.com/tiff/1.0/" '
        'xmlns:exif="http://ns.adobe.com/exif/1.0/" '
        'xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
        f'{attributes}{attr_about}/>'
        '</rdf:RDF></x:xmpmeta>').encode('utf-8')

def _multi_document():
    tiff = build_tiff(
        ifd0=[(0x010F, 2, 'Synthetic Optics'), (0x0110, 2, 'VirtualCam X1'),
              (0x0132, 2, '2026:11:13 18:45:00'),
              (0x8298, 2, 'Synthetic Studio')],
        exif=[(0x9003, 2, '2026:11:12 09:30:00'),
              (0x9004, 2, '2026:11:12 09:31:10'),
              (0x9011, 2, '+02:00'),
              (0x9291, 2, '250'),
              (0xA431, 2, 'SN-SYNTHETIC-001'),
              (0xA435, 2, 'LENS-SYNTHETIC-9')],
        gps=[(1, 2, 'N'), (2, 5, [(31, 1), (14, 1), (36, 1)]),
             (3, 2, 'E'), (4, 5, [(121, 1), (28, 1), (12, 1)])],
        thumbnail=[(0x0132, 2, '2019:01:01 00:00:00'),
                   (0x9003, 2, '2019:01:01 00:00:00'),
                   (0x8298, 2, 'THUMBNAIL-NOT-MAIN')],
        endian='<')
    embedded = xmp_document([
        ('dc:rights', 'Synthetic Studio'),
        ('exif:DateTimeOriginal', '2026:11:12 09:30:00+02:00'),
        ('exif:BodySerialNumber', 'SN-SYNTHETIC-001'),
        ('tiff:Make', 'Synthetic Optics')])
    sidecar = xmp_document([
        ('dc:rights', 'Synthetic Studio'),
        ('exif:DateTimeOriginal', '2026:11:12 09:30:00+02:00'),
        ('exif:BodySerialNumber', 'SN-SYNTHETIC-001')])
    return jpeg(exif_app1(tiff), xmp_app1(embedded)), sidecar

def _conflict_document():
    tiff = build_tiff(
        ifd0=[(0x010F, 2, 'Synthetic Optics'),
              (0x8298, 2, 'Rights EXIF')],
        exif=[(0x9003, 2, '2026:03:04 08:15:00'),
              (0xA431, 2, 'SN-EXIF')],
        endian='<')
    embedded = xmp_document([
        ('dc:rights', 'Rights Embedded XMP'),
        ('exif:DateTimeOriginal', '2026:03:04 08:15:00'),
        ('exif:BodySerialNumber', 'SN-EMBEDDED')])
    sidecar = xmp_document([
        ('dc:rights', 'Rights Sidecar XMP'),
        ('exif:DateTimeOriginal', '2026:03:04 08:15:00+00:00'),
        ('exif:BodySerialNumber', 'SN-SIDECAR')])
    return jpeg(exif_app1(tiff), xmp_app1(embedded)), sidecar

def _thumbnail_document():
    tiff = build_tiff(
        ifd0=[(0x8298, 2, 'Main Image Owner')],
        thumbnail=[(0x0132, 2, '2018:01:01 00:00:00'),
                   (0x9003, 2, '2018:01:01 00:00:00'),
                   (0x9004, 2, '2018:01:01 00:00:00'),
                   (0x8298, 2, 'Thumbnail Owner')],
        endian='<')
    return jpeg(exif_app1(tiff))

def _bad_pointer_document():
    tiff = build_tiff(
        ifd0=[(0x010F, 2, 'Bounded Optics'),
              (0x0110, 2, 'BrokenCam'),
              (0x8298, 2, 'Boundary Owner')],
        exif=[(0x9003, 2, '2026:01:01 00:00:00')],
        gps=[(1, 2, 'N'), (2, 5, [(1, 1), (2, 1), (3, 1)])],
        pointer_overrides={0x8769: 0x0000FF00, 0x8825: 0xFFFFFFF0},
        endian='<')
    return jpeg(exif_app1(tiff))

def _split_context_document():
    first = build_tiff(
        ifd0=[(0x8298, 2, 'Split Context')],
        exif=[(0x9003, 2, '2026:05:06 07:08:09')],
        endian='<')
    second = build_tiff(
        ifd0=[(0x010F, 2, 'Offset Only Camera')],
        exif=[(0x9011, 2, '+09:00')],
        endian='<')
    return jpeg(exif_app1(first), exif_app1(second))

def _big_endian_document():
    tiff = build_tiff(
        ifd0=[(0x010F, 2, 'BigEndian Optics'),
              (0x0110, 2, 'VirtualCam BE'),
              (0x8298, 2, 'BigEndian Owner')],
        exif=[(0x9003, 2, '2026:07:08 10:11:12'),
              (0x9011, 2, '-05:00')],
        gps=[(1, 2, 'S'), (2, 5, [(12, 1), (30, 1), (0, 1)]),
             (3, 2, 'W'), (4, 5, [(70, 1), (15, 1), (0, 1)])],
        endian='>')
    return jpeg(exif_app1(tiff))

def _digitized_only_document():
    tiff = build_tiff(
        ifd0=[(0x8298, 2, 'Digitized Only')],
        exif=[(0x9004, 2, '2026:02:03 04:05:06')],
        endian='<')
    return jpeg(exif_app1(tiff))

def _broken_xmp_sidecar():
    return b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description '

def samples():
    multi, multi_sidecar = _multi_document()
    conflict, conflict_sidecar = _conflict_document()
    tiff = b'II' + struct.pack('<HIH', 42, 8, 1)
    tiff += struct.pack('<HHII', 0x0132, 2, 20, 26) + struct.pack('<I', 0)
    tiff += b'2026:11:12 09:30:00\x00'
    exif = EXIF_SIGNATURE + tiff
    document = jpeg(b'\xff\xe1' + struct.pack('>H', len(exif) + 2) + exif)
    sidecar = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/" dc:rights="Synthetic owner"/></rdf:RDF></x:xmpmeta>'
    return {
        'sample.jpg': document,
        'sample.xmp': sidecar,
        'sample-multi.jpg': multi,
        'sample-multi.xmp': multi_sidecar,
        'sample-conflict.jpg': conflict,
        'sample-conflict.xmp': conflict_sidecar,
        'sample-thumbnail.jpg': _thumbnail_document(),
        'sample-bad-pointer.jpg': _bad_pointer_document(),
        'sample-split-context.jpg': _split_context_document(),
        'sample-big-endian.jpg': _big_endian_document(),
        'sample-digitized-only.jpg': _digitized_only_document(),
        'sample-broken.xmp': _broken_xmp_sidecar(),
        'invalid.jpg': document[:10],
    }
