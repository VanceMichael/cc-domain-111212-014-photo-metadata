"""Structured collection of embedded EXIF/XMP and configured XMP sidecars.

The collector never decodes image pixels.  It walks JPEG marker segments up
to the first scan, parses a bounded set of TIFF/EXIF fields, and parses XMP
packets independently from the segment or sidecar in which they were found.
Every field keeps its candidates, namespace, locator (TIFF offsets or XMP
XML path) and an explicit adoption reason.  Ambiguous values are never
resolved by read order, and file modification times are never used as a
fallback.
"""

import copy
import datetime as dt
import hashlib
import re
import struct
from zoneinfo import ZoneInfo

from source import scan_segments

EXIF_SIGNATURE = b'Exif\x00\x00'
XMP_STANDARD_SIGNATURE = b'http://ns.adobe.com/xap/1.0/\x00'
XMP_EXTENSION_SIGNATURE = b'http://ns.adobe.com/xmp/extension/\x00'

MAX_SIDECAR_BYTES = 4 * 1024 * 1024
MAX_IFD_ENTRIES = 512
MAX_IFDS = 16
MAX_VALUE_BYTES = 1024 * 1024

RDF = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
DC = 'http://purl.org/dc/elements/1.1/'
EXIF_NS = 'http://ns.adobe.com/exif/1.0/'
TIFF_NS = 'http://ns.adobe.com/tiff/1.0/'
XMP_NS = 'http://ns.adobe.com/xap/1.0/'
PHOTOSHOP_NS = 'http://ns.adobe.com/photoshop/1.0/'
AUX_NS = 'http://ns.adobe.com/exif/1.0/aux/'
XML_NS = 'http://www.w3.org/XML/1998/namespace'
ADOBE_META_NS = 'adobe:ns:meta/'

NS_PREFIXES = {
    RDF: 'rdf', DC: 'dc', EXIF_NS: 'exif', TIFF_NS: 'tiff',
    XMP_NS: 'xmp', PHOTOSHOP_NS: 'photoshop', AUX_NS: 'aux',
    XML_NS: 'xml', ADOBE_META_NS: 'x',
}

TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
TYPE_NAMES = {1: 'BYTE', 2: 'ASCII', 3: 'SHORT', 4: 'LONG', 5: 'RATIONAL',
              6: 'SBYTE', 7: 'UNDEFINED', 8: 'SSHORT', 9: 'SLONG',
              10: 'SRATIONAL', 11: 'FLOAT', 12: 'DOUBLE'}

TIFF_TAG_NAMESPACE = 'exif:tiff-tag'

# Deliberately limited mapping.  DateTime (0x0132) is file change time, not
# capture time, so it is collected for evidence only and never adopted as
# capture time.
IFD0_FIELDS = {
    0x010F: ('camera_make', 'Make'),
    0x0110: ('camera_model', 'Model'),
    0x0132: ('metadata_change_time', 'DateTime'),
    0x013B: ('creator', 'Artist'),
    0x8298: ('copyright', 'Copyright'),
}
EXIF_IFD_FIELDS = {
    0x9003: ('capture_time', 'DateTimeOriginal'),
    0x9004: ('digitized_time', 'DateTimeDigitized'),
    0x9011: ('capture_offset', 'OffsetTimeOriginal'),
    0x9012: ('digitized_offset', 'OffsetTimeDigitized'),
    0x9291: ('capture_subsecond', 'SubSecTimeOriginal'),
    0xA430: ('camera_owner_name', 'CameraOwnerName'),
    0xA431: ('body_serial_number', 'BodySerialNumber'),
    0xA435: ('lens_serial_number', 'LensSerialNumber'),
}
GPS_FIELDS = {
    1: ('gps_latitude_ref', 'GPSLatitudeRef'),
    2: ('gps_latitude', 'GPSLatitude'),
    3: ('gps_longitude_ref', 'GPSLongitudeRef'),
    4: ('gps_longitude', 'GPSLongitude'),
}
THUMBNAIL_FIELDS = {tag: pair for tag, pair in
                    {**IFD0_FIELDS, **EXIF_IFD_FIELDS}.items()}

POINTER_EXIF = 0x8769
POINTER_GPS = 0x8825

XMP_FIELDS = {
    (DC, 'rights'): ('copyright', True),
    (DC, 'creator'): ('creator', True),
    (TIFF_NS, 'Make'): ('camera_make', False),
    (TIFF_NS, 'Model'): ('camera_model', False),
    (EXIF_NS, 'DateTimeOriginal'): ('capture_time', False),
    (EXIF_NS, 'DateTimeDigitized'): ('digitized_time', False),
    (EXIF_NS, 'OffsetTimeOriginal'): ('capture_offset', False),
    (EXIF_NS, 'SubSecTimeOriginal'): ('capture_subsecond', False),
    (PHOTOSHOP_NS, 'DateCreated'): ('capture_time', False),
    (EXIF_NS, 'BodySerialNumber'): ('body_serial_number', False),
    (EXIF_NS, 'LensSerialNumber'): ('lens_serial_number', False),
    (EXIF_NS, 'CameraOwnerName'): ('camera_owner_name', False),
    (AUX_NS, 'SerialNumber'): ('body_serial_number', False),
}

TIME_FIELDS = {'capture_time', 'digitized_time', 'metadata_change_time'}
GPS_FIELD = 'gps'

FIELD_ORDER = ('capture_time', 'digitized_time', 'metadata_change_time',
               'copyright', 'creator', 'camera_make', 'camera_model',
               'camera_owner_name', 'body_serial_number', 'lens_serial_number',
               GPS_FIELD)


class TiffError(Exception):
    """A bounded TIFF structure could not be read as declared."""


def _read(tiff, offset, size, what):
    if offset < 0 or size < 0 or offset + size > len(tiff):
        raise TiffError(f'{what}越界：offset={offset} size={size} tiff={len(tiff)}')
    return tiff[offset:offset + size]


def _u16(buf, offset, endian, what):
    return struct.unpack(endian + 'H', _read(buf, offset, 2, what))[0]


def _u32(buf, offset, endian, what):
    return struct.unpack(endian + 'I', _read(buf, offset, 4, what))[0]


def _split_ascii(raw):
    parts = [part.decode('utf-8', 'replace') for part in raw.split(b'\x00')
             if part != b'']
    if not parts:
        return ''
    return parts[0] if len(parts) == 1 else parts


def _decode_value(typ, count, raw, endian):
    if typ == 2:
        return _split_ascii(raw)
    if typ == 1:
        return list(raw) if count != 1 else raw[0]
    if typ == 7:
        return raw.hex() if count != 1 else raw[0]
    if typ in (3, 8):
        fmt = 'H' if typ == 3 else 'h'
        values = [struct.unpack_from(endian + fmt, raw, i * 2)[0]
                  for i in range(count)]
        return values[0] if count == 1 else values
    if typ in (4, 9):
        fmt = 'I' if typ == 4 else 'i'
        values = [struct.unpack_from(endian + fmt, raw, i * 4)[0]
                  for i in range(count)]
        return values[0] if count == 1 else values
    if typ in (5, 10):
        fmt = 'I' if typ == 5 else 'i'
        values = [list(struct.unpack_from(endian + fmt + fmt, raw, i * 8))
                  for i in range(count)]
        return values[0] if count == 1 else values
    if typ in (11, 12):
        fmt = 'f' if typ == 11 else 'd'
        size = TYPE_SIZES[typ]
        values = [struct.unpack_from(endian + fmt, raw, i * size)[0]
                  for i in range(count)]
        return values[0] if count == 1 else values
    raise TiffError(f'不支持的 TIFF 类型 {typ}')


def _entry_scalar(entry):
    value = entry.get('value')
    if isinstance(value, list):
        return value[0] if len(value) == 1 else None
    return value


def _parse_ifd(tiff, endian, offset, name, context, source_id, base_file_offset,
               seen, depth):
    """Parse one IFD table.

    A cyclic or over-deep pointer aborts the IFD; a bad entry value is
    recorded on that entry only, so readable fields from the same table are
    still evidence.  Every offset is validated against the TIFF buffer.
    """
    if depth > MAX_IFDS:
        raise TiffError('IFD 层级超过限制')
    if offset in seen:
        raise TiffError(f'{name} 指针循环')
    seen.add(offset)
    count = _u16(tiff, offset, endian, f'{name} 条目数')
    if count > MAX_IFD_ENTRIES:
        raise TiffError(f'{name} 条目数超过限制：{count}')
    table_end = offset + 2 + count * 12 + 4
    _read(tiff, offset, table_end - offset, f'{name} 条目表')
    entries = []
    for index in range(count):
        entry_offset = offset + 2 + index * 12
        tag, typ, value_count = struct.unpack_from(endian + 'HHI', tiff, entry_offset)
        entry = dict(tag=f'0x{tag:04x}', tag_number=tag, type=TYPE_NAMES.get(typ, f'type-{typ}'),
                     count=value_count, ifd=name, context=context)
        if typ not in TYPE_SIZES:
            entry.update(status='unsupported', reason=f'不支持的 TIFF 类型 {typ}',
                         locator=dict(kind='tiff', ifd=name, context=context,
                                      entry_offset=entry_offset,
                                      file_offset=base_file_offset + entry_offset))
            entries.append(entry)
            continue
        size = TYPE_SIZES[typ] * value_count
        if value_count < 0 or size > MAX_VALUE_BYTES:
            entry.update(status='error', reason='字段值长度超过限制',
                         locator=dict(kind='tiff', ifd=name, context=context,
                                      entry_offset=entry_offset,
                                      file_offset=base_file_offset + entry_offset))
            entries.append(entry)
            continue
        if size <= 4:
            value_offset = entry_offset + 8
            inline = True
        else:
            value_offset = _u32(tiff, entry_offset + 8, endian, f'{name} 值偏移')
            inline = False
        locator = dict(kind='tiff', ifd=name, context=context,
                       entry_offset=entry_offset,
                       value_offset=value_offset,
                       values_inline=inline,
                       file_offset=base_file_offset + value_offset,
                       tag=f'0x{tag:04x}')
        try:
            raw = _read(tiff, value_offset, size, f'{name} 字段 0x{tag:04x} 值')
            entry.update(value=_decode_value(typ, value_count, raw, endian),
                         status='ok', locator=locator)
        except TiffError as error:
            entry.update(status='error', reason=str(error), locator=locator)
        entries.append(entry)
    next_offset = _u32(tiff, table_end - 4, endian, f'{name} 下一 IFD 指针')
    return entries, next_offset


def _context(name, context, offset):
    return dict(ifd=name, context=context, tiff_offset=offset,
                status='parsed', reason=None, entries=[])


def parse_exif_payload(payload, source_id, payload_file_offset):
    """Parse one APP1 Exif payload with its own TIFF base and contexts."""
    if not payload.startswith(EXIF_SIGNATURE):
        return None, dict(status='error', reason='APP1 不是 Exif 签名')
    tiff = payload[len(EXIF_SIGNATURE):]
    if len(tiff) < 8:
        return None, dict(status='error', reason='TIFF 头截断')
    order = tiff[:2]
    if order == b'II':
        endian = '<'
    elif order == b'MM':
        endian = '>'
    else:
        return None, dict(status='error', reason='TIFF 字节序标记无效')
    try:
        magic = _u16(tiff, 2, endian, 'TIFF 魔数')
        if magic != 42:
            raise TiffError(f'TIFF 魔数无效：{magic}')
        ifd0_offset = _u32(tiff, 4, endian, 'IFD0 指针')
        contexts = []
        observations = []
        seen = set()
        ifd0, next_offset = _parse_ifd(tiff, endian, ifd0_offset, 'IFD0', 'main',
                                       source_id, payload_file_offset + 6, seen, 0)
        main = _context('IFD0', 'main', ifd0_offset)
        main['entries'] = ifd0
        contexts.append(main)
        observations.extend(_tiff_observations(ifd0, IFD0_FIELDS, source_id,
                                               payload_file_offset + 6, 'main'))
        pointers = {}
        for entry in ifd0:
            if entry.get('status') == 'ok' and entry['tag_number'] in (POINTER_EXIF, POINTER_GPS):
                pointers[entry['tag_number']] = _entry_scalar(entry)
        partial_errors = []

        exif_context = _context('ExifIFD', 'main', pointers.get(POINTER_EXIF, 0))
        if POINTER_EXIF in pointers:
            try:
                exif_ifd, exif_next = _parse_ifd(
                    tiff, endian, pointers[POINTER_EXIF], 'ExifIFD', 'main',
                    source_id, payload_file_offset + 6, seen, 1)
                exif_context['tiff_offset'] = pointers[POINTER_EXIF]
                exif_context['entries'] = exif_ifd
                observations.extend(_tiff_observations(
                    exif_ifd, EXIF_IFD_FIELDS, source_id,
                    payload_file_offset + 6, 'main'))
                if exif_next:
                    partial_errors.append('ExifIFD 的下一 IFD 未作为主图继续读取')
            except TiffError as error:
                exif_context.update(status='error', reason=str(error))
                partial_errors.append(str(error))
        else:
            exif_context.update(status='absent', reason='IFD0 未声明 ExifIFD')
        contexts.append(exif_context)

        gps_context = _context('GPSIFD', 'gps', pointers.get(POINTER_GPS, 0))
        if POINTER_GPS in pointers:
            try:
                gps_ifd, gps_next = _parse_ifd(
                    tiff, endian, pointers[POINTER_GPS], 'GPSIFD', 'gps',
                    source_id, payload_file_offset + 6, seen, 1)
                gps_context['tiff_offset'] = pointers[POINTER_GPS]
                gps_context['entries'] = gps_ifd
                observations.extend(_tiff_observations(
                    gps_ifd, GPS_FIELDS, source_id,
                    payload_file_offset + 6, 'gps'))
                if gps_next:
                    partial_errors.append('GPSIFD 的下一 IFD 被忽略')
            except TiffError as error:
                gps_context.update(status='error', reason=str(error))
                partial_errors.append(str(error))
        else:
            gps_context.update(status='absent', reason='IFD0 未声明 GPSIFD')
        contexts.append(gps_context)

        thumbnail_context = _context('IFD1', 'thumbnail', next_offset)
        if next_offset:
            try:
                thumb_ifd, thumb_next = _parse_ifd(
                    tiff, endian, next_offset, 'IFD1', 'thumbnail',
                    source_id, payload_file_offset + 6, seen, 1)
                thumbnail_context['tiff_offset'] = next_offset
                thumbnail_context['entries'] = thumb_ifd
                # Thumbnail entries are evidence only.  Their mapped names are
                # never turned into observations and their pointers are not
                # followed into the main image context.
                for entry in thumb_ifd:
                    if entry['tag_number'] in THUMBNAIL_FIELDS:
                        entry['field_hint'] = THUMBNAIL_FIELDS[
                            entry['tag_number']][0]
                if thumb_next:
                    thumbnail_context['next_offset'] = thumb_next
                    partial_errors.append('IFD1 的下一 IFD 未继续读取')
            except TiffError as error:
                thumbnail_context.update(status='error', reason=str(error))
                partial_errors.append(str(error))
        else:
            thumbnail_context.update(status='absent', reason='没有缩略图 IFD')
        contexts.append(thumbnail_context)

        status = 'partial' if partial_errors else 'parsed'
        return dict(contexts=contexts, observations=observations), dict(
            status=status, reason='；'.join(partial_errors) if partial_errors else None)
    except TiffError as error:
        return None, dict(status='error', reason=str(error))


def _tiff_observations(entries, mapping, source_id, base_file_offset, context):
    observations = []
    for entry in entries:
        if entry.get('status') != 'ok' or entry['tag_number'] not in mapping:
            continue
        field, name = mapping[entry['tag_number']]
        observations.append(dict(
            field=field, namespace=TIFF_TAG_NAMESPACE, source_id=source_id,
            media='exif', context=context, label=name,
            value=entry['value'], locator=entry['locator']))
    return observations

# ---------------------------------------------------------------- XMP packets

def _split_qualified(name):
    if name.startswith('{') and '}' in name:
        uri, local = name[1:].split('}', 1)
        return uri, local
    return '', name


def _display_name(uri, local):
    prefix = NS_PREFIXES.get(uri)
    return f'{prefix}:{local}' if prefix else (f'{{{uri}}}{local}' if uri else local)


class Packet:
    """One XMP packet together with where it came from.  Packets are parsed
    independently; a property from packet A never borrows a context declared
    in packet B."""

    def __init__(self, kind, payload, index, locator=None):
        self.kind = kind
        self.payload = payload
        self.index = index
        self.locator = locator


def _value_from_element(element, path, uri, local, context, source_id, media,
                        packet_index, observations, diagnostics):
    """Read one mapped property element.  Arrays are accepted only as a flat
    list of simple items; structured nodes produce a diagnostic instead of a
    guessed scalar.  Returns the path(s) the value came from."""
    labels = XMP_FIELDS[(uri, local)]
    field, allows_list = labels
    for child in element:
        child_uri, child_local = _split_qualified(child.tag)
        if child_uri == RDF and child_local in ('Bag', 'Seq', 'Alt'):
            items = []
            languages = []
            for item_index, item in enumerate(child):
                item_uri, item_local = _split_qualified(item.tag)
                if item_uri != RDF or item_local != 'li' or item.get(
                        '{' + RDF + '}parseType') == 'Resource':
                    return _structured(uri, local, path, context, source_id,
                                       media, field, observations, diagnostics)
                if len(list(item)) or any(
                        _split_qualified(grand.tag)[0] != RDF
                        for grand in item):
                    return _structured(uri, local, path, context, source_id,
                                       media, field, observations, diagnostics)
                text = (item.text or '').strip()
                if not text:
                    return _structured(uri, local, path, context, source_id,
                                       media, field, observations, diagnostics)
                items.append(text)
                languages.append(item.get('{%s}lang' % XML_NS))
            if not items:
                return _structured(uri, local, path, context, source_id,
                                   media, field, observations, diagnostics)
            if child_local == 'Alt':
                # A language alternative is not an ordered list: only an
                # unambiguous x-default (or a sole item) is adoptable.
                defaults = [text for text, language in zip(items, languages)
                            if language == 'x-default']
                if len(defaults) == 1:
                    value = defaults[0]
                elif len(defaults) == 0 and len(items) == 1:
                    value = items[0]
                else:
                    _structured(uri, local, path, context, source_id, media,
                                field, observations, diagnostics,
                                reason='XMP 语言备选不唯一且无 x-default，'
                                       '不按顺序取最后一项')
                    return False
            elif allows_list:
                value = items
            elif len(items) == 1:
                value = items[0]
            else:
                _structured(uri, local, path, context, source_id, media,
                            field, observations, diagnostics,
                            reason='XMP 标量字段含多个数组项，不按顺序选择')
                return False
            observations.append(dict(
                field=field, namespace=uri, source_id=source_id, media=media,
                context=context, label=_display_name(uri, local),
                value=value,
                locator=dict(kind='xmp', xml_path=path, collection=child_local,
                             languages=languages,
                             item_paths=[f'{path}/{child_local}/{i}'
                                         for i in range(len(items))]),
                xmp_packet=packet_index))
            return True
    if len(list(element)):
        return _structured(uri, local, path, context, source_id, media, field,
                           observations, diagnostics)
    text = (element.text or '').strip()
    if not text:
        return _structured(uri, local, path, context, source_id, media, field,
                           observations, diagnostics)
    observations.append(dict(
        field=field, namespace=uri, source_id=source_id, media=media,
        context=context, label=_display_name(uri, local), value=text,
        locator=dict(kind='xmp', xml_path=path,
                     qualified_name=_display_name(uri, local)),
        xmp_packet=packet_index))
    return True


def _structured(uri, local, path, context, source_id, media, field,
                observations, diagnostics,
                reason='XMP 属性为结构化节点，不猜测其值'):
    diagnostics.append(dict(
        status='unsupported', reason=reason,
        field=field, namespace=uri, source_id=source_id, media=media,
        context=context, locator=dict(kind='xmp', xml_path=path)))
    return False


def _walk_property(element, path, context, source_id, media, packet_index,
                   observations, diagnostics, depth=0):
    if depth > 32:
        diagnostics.append(dict(status='error', reason='XMP 嵌套过深',
                                locator=dict(kind='xmp', xml_path=path),
                                source_id=source_id, media=media))
        return
    uri, local = _split_qualified(element.tag)
    if (uri, local) in XMP_FIELDS:
        _value_from_element(element, path, uri, local, context, source_id,
                            media, packet_index, observations, diagnostics)
        return
    for index, child in enumerate(element):
        child_uri, _ = _split_qualified(child.tag)
        if child_uri == RDF:
            _walk_rdf(child, f'{path}/{local}/{index}', context, source_id,
                      media, packet_index, observations, diagnostics, depth + 1)
        else:
            _walk_property(child, f'{path}/{local}/{index}', context, source_id,
                           media, packet_index, observations, diagnostics,
                           depth + 1)


def _walk_rdf(element, path, context, source_id, media, packet_index,
              observations, diagnostics, depth=0):
    if depth > 32:
        diagnostics.append(dict(status='error', reason='XMP 嵌套过深',
                                locator=dict(kind='xmp', xml_path=path),
                                source_id=source_id, media=media))
        return
    uri, local = _split_qualified(element.tag)
    if uri != RDF:
        _walk_property(element, path, context, source_id, media,
                       packet_index, observations, diagnostics, depth)
        return
    if local == 'Description':
        about = element.get('{%s}about' % RDF) or ''
        if about:
            context = dict(context, about=about)
        attributes = sorted(element.attrib)
        for name in attributes:
            if name == '{%s}about' % RDF:
                continue
            attr_uri, attr_local = _split_qualified(name)
            if (attr_uri, attr_local) not in XMP_FIELDS:
                continue
            field = XMP_FIELDS[(attr_uri, attr_local)][0]
            value = element.attrib[name].strip()
            if not value:
                _structured(attr_uri, attr_local, path, context, source_id,
                            media, field, observations, diagnostics)
                continue
            observations.append(dict(
                field=field, namespace=attr_uri, source_id=source_id,
                media=media, context=context,
                label=_display_name(attr_uri, attr_local), value=value,
                locator=dict(kind='xmp', xml_path=f'{path}/@{_display_name(attr_uri, attr_local)}',
                             attribute=True),
                xmp_packet=packet_index))
        for index, child in enumerate(element):
            child_uri, child_local = _split_qualified(child.tag)
            if child_uri == RDF:
                _walk_rdf(child, f'{path}/Description/{index}', context,
                          source_id, media, packet_index, observations,
                          diagnostics, depth + 1)
            else:
                _walk_property(child, f'{path}/Description/{index}', context,
                               source_id, media, packet_index, observations,
                               diagnostics, depth + 1)
        return
    if local in ('Bag', 'Seq', 'Alt', 'li'):
        for index, child in enumerate(element):
            _walk_rdf(child, f'{path}/{local}/{index}', context, source_id,
                      media, packet_index, observations, diagnostics, depth + 1)
        return
    for index, child in enumerate(element):
        child_uri, _ = _split_qualified(child.tag)
        _walk_rdf(child, f'{path}/{local}/{index}', context, source_id, media,
                  packet_index, observations, diagnostics, depth + 1)


def parse_xmp_packet(packet, source_id):
    """Parse a complete XMP packet from a JPEG APP1 segment or a sidecar."""
    import xml.etree.ElementTree as ET
    raw = packet.payload
    document = dict(source_id=source_id, kind=packet.kind,
                    sha256=hashlib.sha256(raw).hexdigest(), size=len(raw),
                    packet_index=packet.index)
    if packet.locator:
        document['locator'] = dict(packet.locator)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        document.update(status='error', reason='XMP XML 解析失败：' + str(error))
        return [], [], [document]
    root_uri, root_local = _split_qualified(root.tag)
    document.update(status='parsed', root=_display_name(root_uri, root_local))
    context = dict(xmp_packet=packet.index, source_id=source_id,
                   media=packet.kind)
    observations, diagnostics = [], []
    base_path = f'/{root_local}'
    for index, child in enumerate(root):
        child_uri, child_local = _split_qualified(child.tag)
        path = f'{base_path}/{index}'
        if child_uri == RDF:
            _walk_rdf(child, path, context, source_id, packet.kind,
                      packet.index, observations, diagnostics)
        else:
            _walk_property(child, path, context, source_id, packet.kind,
                           packet.index, observations, diagnostics)
    if not observations and not diagnostics:
        document['reason'] = 'XMP 中没有受支持的字段'
    return observations, diagnostics, [document]


# -------------------------------------------------------------- time handling

EXIF_TIME = re.compile(
    r'^(?P<y>\d{4})[:\-](?P<mo>\d{2})[:\-](?P<d>\d{2})[ T]'
    r'(?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})(?:\.(?P<f>\d+))?'
    r'(?P<z>Z|[+-]\d{2}:?\d{2})?$')
OFFSET_TIME = re.compile(r'^(?P<sign>[+-])(?P<h>\d{2}):?(?P<m>\d{2})$')

def _parse_offset(text):
    if not isinstance(text, str):
        return None
    match = OFFSET_TIME.match(text.strip())
    if not match:
        return None
    hours = int(match.group('h'))
    minutes = int(match.group('m'))
    if hours > 23 or minutes > 59:
        return None
    return f"{match.group('sign')}{hours:02d}:{minutes:02d}"

def parse_time_text(value, offset_hint=None, subsecond_hint=None):
    """Parse the limited EXIF/XMP time grammar.  Returns ``None`` when the
    value is not a recognisable clock; nothing is inferred from file times."""
    if not isinstance(value, str):
        return None
    match = EXIF_TIME.match(value.strip())
    if not match:
        return None
    try:
        local = dt.datetime(int(match.group('y')), int(match.group('mo')),
                            int(match.group('d')), int(match.group('h')),
                            int(match.group('mi')), int(match.group('s')))
    except ValueError:
        return None
    fraction = match.group('f')
    if fraction is None and isinstance(subsecond_hint, str) and \
            re.fullmatch(r'\d{1,9}', subsecond_hint.strip() or '-'):
        fraction = subsecond_hint.strip()
    micro = int((fraction or '').ljust(6, '0')[:6]) if fraction else 0
    local = local.replace(microsecond=micro)
    zone = match.group('z')
    offset = None
    offset_basis = None
    if zone:
        offset = '+00:00' if zone == 'Z' else _parse_offset(zone)
        if offset is None:
            return None
        offset_basis = 'value'
    else:
        hinted = _parse_offset(offset_hint)
        if hinted is not None:
            offset = hinted
            offset_basis = 'offset-tag'
        elif offset_hint not in (None, ''):
            offset_basis = 'invalid-offset-tag'
    return dict(local_clock=local.isoformat(), local=local,
                offset=offset, offset_basis=offset_basis)

def interpret_time(parsed, task_zone):
    """Turn a parsed clock into an explicit evidence record.

    ``utc`` is only present with an explicit offset.  ``region_time`` and a
    derived ``utc`` from a naive clock require a configured task time zone.
    There is no fallback to the file modification time.
    """
    if parsed is None:
        return dict(value=None, status='unknown',
                    reason='时间格式不可识别，不使用文件修改时间代替',
                    local_clock=None, offset=None, utc=None, region_time=None,
                    zone_basis='unknown-zone')
    local = parsed['local']
    offset = parsed['offset']
    record = dict(local_clock=parsed['local_clock'], offset=offset,
                  zone_basis='unknown-zone', utc=None, region_time=None,
                  status='parsed')
    instant = None
    if offset is not None:
        sign = 1 if offset[0] == '+' else -1
        delta = dt.timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6]))
        instant = local.replace(tzinfo=dt.timezone(sign * delta))
        record['utc'] = instant.astimezone(dt.timezone.utc).isoformat()
        record['zone_basis'] = 'explicit-offset'
    if task_zone is not None:
        if instant is not None:
            region = instant.astimezone(ZoneInfo(task_zone))
        else:
            # The task time zone is an explicit configuration, so interpreting
            # the naive clock in it is allowed and marked as such.
            region = local.replace(tzinfo=ZoneInfo(task_zone))
        record['region_time'] = region.isoformat()
        record['region_zone'] = task_zone
        if instant is None:
            record['utc'] = region.astimezone(dt.timezone.utc).isoformat()
            record['zone_basis'] = 'task-timezone'
        else:
            record['zone_basis'] = 'explicit-offset'
    elif parsed.get('offset_basis') == 'invalid-offset-tag':
        record['status'] = 'partial'
        record['reason'] = '偏移字段无效，仅保留本地时钟'
    return record


# --------------------------------------------------------------------- GPS

def _gps_degrees(value):
    if not isinstance(value, list) or len(value) != 3:
        return None
    parts = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            return None
        numerator, denominator = item
        if denominator == 0:
            return None
        parts.append(numerator / denominator)
    degrees = parts[0] + parts[1] / 60 + parts[2] / 3600
    if not (0 <= degrees <= 180):
        return None
    return round(degrees, 9)

def gps_pair(entries):
    values = {entry['tag_number']: entry for entry in entries
              if entry.get('status') == 'ok'}
    lat = values.get(2)
    lon = values.get(4)
    lat_ref = _entry_scalar(values.get(1)) if 1 in values else None
    lon_ref = _entry_scalar(values.get(3)) if 3 in values else None
    if lat is None or lon is None:
        return None, 'GPS 缺少经度或纬度'
    lat_deg = _gps_degrees(lat.get('value'))
    lon_deg = _gps_degrees(lon.get('value'))
    if lat_deg is None or lon_deg is None:
        return None, 'GPS 度分秒无效'
    if lat_ref not in ('N', 'S') or lon_ref not in ('E', 'W'):
        return None, 'GPS 半球标识缺失或无效'
    if lat_ref == 'S':
        lat_deg = -lat_deg
    if lon_ref == 'W':
        lon_deg = -lon_deg
    locator = dict(kind='tiff', ifd='GPSIFD', context='gps',
                   entries=[lat['locator'], lon['locator']])
    return dict(latitude=lat_deg, longitude=lon_deg,
                locator=locator), None


# ----------------------------------------------------------------- collector

SOURCE_PRIORITY = {'exif': 0, 'embedded-xmp': 1, 'sidecar-xmp': 2}
MEDIA_LABELS = {'exif': 'EXIF/TIFF', 'embedded-xmp': '内嵌 XMP',
                'sidecar-xmp': '侧车 XMP'}

def _normalise(field, value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return [item.strip() for item in value]
        return value
    return value

def _canonical(value):
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def collect_bytes(data, sidecars=(), task_timezone=None):
    """Collect metadata evidence from one JPEG and configured XMP sidecars.

    ``sidecars`` is an iterable of mappings with ``name``, ``data`` (bytes or
    None when the configured file is missing) and optional ``role``.  Sidecars
    are never discovered by filename; pairing is the caller's configuration.
    """
    if task_timezone is not None:
        try:
            ZoneInfo(task_timezone)
        except Exception as error:
            raise ValueError('无法加载摄影任务时区：' + str(task_timezone)) from error
    digest = hashlib.sha256(data).hexdigest()
    segments = scan_segments(data)
    report = dict(input=dict(sha256=digest, bytes=len(data),
                             mtime_used=False),
                  segments=segments, packets=[], observations=[],
                  diagnostics=[], fields={}, task_timezone=task_timezone)
    packet_index = 0
    for segment in segments:
        start = segment['payload_offset']
        end = start + segment['payload_size']
        payload = data[start:end]
        marker = segment['marker']
        if marker == 'e1' and payload.startswith(EXIF_SIGNATURE):
            parsed, status = parse_exif_payload(
                payload, f'app1-{packet_index}', segment['offset'])
            document = dict(source_id=f'app1-{packet_index}', kind='exif',
                            marker=marker, segment_offset=segment['offset'],
                            payload_offset=start, payload_size=len(payload),
                            sha256=hashlib.sha256(payload).hexdigest(),
                            packet_index=packet_index, **status)
            if parsed:
                document['contexts'] = parsed['contexts']
                for observation in parsed['observations']:
                    observation['packet_index'] = packet_index
                    observation['source_id'] = f'app1-{packet_index}'
                    report['observations'].append(observation)
                for context in parsed['contexts']:
                    if context['context'] != 'gps':
                        continue
                    if context['status'] == 'absent':
                        continue
                    if context['status'] not in ('parsed', 'partial'):
                        report['diagnostics'].append(dict(
                            status='error', reason=context['reason'],
                            field='gps', namespace='exif:gps',
                            source_id=f'app1-{packet_index}', media='exif',
                            packet_index=packet_index,
                            locator=dict(kind='tiff', ifd='GPSIFD',
                                         context='gps')))
                        continue
                    pair, reason = gps_pair(context['entries'])
                    if pair:
                        report['observations'].append(dict(
                            field='gps', media='exif', namespace='exif:gps',
                            label='GPS position', value=pair,
                            locator=pair['locator'], context='gps',
                            source_id=f'app1-{packet_index}',
                            packet_index=packet_index))
                    elif reason:
                        report['diagnostics'].append(dict(
                            status='unsupported', reason=reason,
                            field='gps', namespace='exif:gps',
                            source_id=f'app1-{packet_index}', media='exif',
                            packet_index=packet_index,
                            locator=dict(kind='tiff', ifd='GPSIFD',
                                         context='gps')))
                for context in parsed['contexts']:
                    if context['status'] == 'error' and \
                            context['context'] != 'gps':
                        report['diagnostics'].append(dict(
                            status='error', reason=context['reason'],
                            source_id=f'app1-{packet_index}', media='exif',
                            packet_index=packet_index,
                            locator=dict(kind='tiff', ifd=context['ifd'],
                                         context=context['context'],
                                         tiff_offset=context['tiff_offset'])))
            report['packets'].append(document)
            packet_index += 1
        elif marker == 'e1' and payload.startswith(XMP_STANDARD_SIGNATURE):
            packet = Packet('embedded-xmp',
                            payload[len(XMP_STANDARD_SIGNATURE):],
                            packet_index,
                            dict(kind='jpeg-segment', marker=marker,
                                 segment_offset=segment['offset'],
                                 payload_offset=start))
            observations, diagnostics, documents = parse_xmp_packet(
                packet, f'app1-{packet_index}')
            for document in documents:
                document['packet_index'] = packet_index
            report['packets'].extend(documents)
            report['observations'].extend(observations)
            report['diagnostics'].extend(diagnostics)
            packet_index += 1
        elif marker == 'e1' and payload.startswith(XMP_EXTENSION_SIGNATURE):
            report['diagnostics'].append(dict(
                status='unsupported', reason='不解析 XMP 扩展段，避免跨段拼接上下文',
                source_id=f'app1-{packet_index}', media='embedded-xmp',
                packet_index=packet_index,
                locator=dict(kind='jpeg-segment', marker=marker,
                             segment_offset=segment['offset'])))
            packet_index += 1
        elif marker == 'e1':
            report['packets'].append(dict(
                source_id=f'app1-{packet_index}', kind='app1-unknown',
                status='unsupported', reason='APP1 既非 Exif 也非 XMP',
                packet_index=packet_index,
                locator=dict(kind='jpeg-segment', marker=marker,
                             segment_offset=segment['offset'])))
            packet_index += 1

    for sidecar in sidecars:
        name = sidecar.get('name', 'sidecar.xmp')
        raw = sidecar.get('data')
        if raw is None:
            report['packets'].append(dict(
                source_id=name, kind='sidecar-xmp', status='missing',
                reason=sidecar.get('reason', '配置的侧车不存在或未读取'),
                name=name, packet_index=None))
            continue
        if len(raw) > MAX_SIDECAR_BYTES:
            report['packets'].append(dict(
                source_id=name, kind='sidecar-xmp', status='error',
                reason='侧车超过大小限制', name=name, packet_index=None,
                size=len(raw)))
            continue
        packet = Packet('sidecar-xmp', raw, packet_index,
                        dict(kind='sidecar', name=name))
        observations, diagnostics, documents = parse_xmp_packet(packet, name)
        report['packets'].extend(documents)
        report['observations'].extend(observations)
        report['diagnostics'].extend(diagnostics)
        packet_index += 1

    _build_fields(report)
    return report

# ----------------------------------------------------------------- resolution

def _normalise(field, value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return [item.strip() for item in value]
        return value
    return value


def _canonical(value):
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _hints(observation, grouped):
    """Find offset/sub-second tags in the same packet and media.

    Offsets are field-specific: OffsetTimeOriginal only explains
    DateTimeOriginal, OffsetTimeDigitized only explains DateTimeDigitized.
    A tag from another APP1 segment or another sidecar must not lend context
    across packet boundaries.
    """
    offset_field = {'capture_time': 'capture_offset',
                    'digitized_time': 'digitized_offset'}.get(
                        observation['field'])
    offset = None
    subsecond = None
    if offset_field:
        for hint in grouped.get(offset_field, []):
            if hint['source_id'] == observation['source_id'] and \
                    hint['media'] == observation['media']:
                offset = hint['value']
                break
    if observation['field'] == 'capture_time':
        for hint in grouped.get('capture_subsecond', []):
            if hint['source_id'] == observation['source_id'] and \
                    hint['media'] == observation['media']:
                subsecond = hint['value']
                break
    return offset, subsecond


def _make_candidate(observation, grouped, task_timezone):
    candidate = dict(field=observation['field'],
                     namespace=observation['namespace'],
                     source_id=observation['source_id'],
                     media=observation['media'],
                     context=observation.get('context'),
                     label=observation.get('label'),
                     locator=copy.deepcopy(observation.get('locator')),
                     raw_value=observation['value'])
    field = observation['field']
    if field in TIME_FIELDS:
        offset_hint, subsecond_hint = _hints(observation, grouped)
        parsed = parse_time_text(observation['value'], offset_hint,
                                 subsecond_hint)
        interpreted = interpret_time(parsed, task_timezone)
        candidate.update(
            value=interpreted['local_clock'], status=(
                'ok' if parsed is not None else 'unknown'),
            time=dict(local_clock=interpreted['local_clock'],
                      offset=interpreted['offset'],
                      utc=interpreted['utc'],
                      region_time=interpreted['region_time'],
                      zone_basis=interpreted['zone_basis'],
                      status=interpreted['status'],
                      reason=interpreted.get('reason')))
        if parsed is not None:
            candidate['offset_source'] = ('value' if parsed['offset_basis'] == 'value'
                                          else 'same-packet offset tag'
                                          if parsed['offset_basis'] == 'offset-tag'
                                          else None)
    elif field == 'gps':
        candidate.update(value=dict(latitude=observation['value']['latitude'],
                                    longitude=observation['value']['longitude']),
                         status='ok')
    else:
        candidate['value'] = _normalise(field, observation['value'])
        candidate['status'] = ('ok' if candidate['value'] not in ('', None)
                               else 'unknown')
    return candidate


def _build_fields(report):
    grouped = {}
    for observation in report['observations']:
        grouped.setdefault(observation['field'], []).append(observation)
    report['fields'] = {}
    for field in FIELD_ORDER:
        candidates = [_make_candidate(observation, grouped, report['task_timezone'])
                      for observation in grouped.get(field, [])]
        report['fields'][field] = _resolve_field(field, candidates, report)


def _resolve_field(field, candidates, report):
    namespace = candidates[0]['namespace'] if candidates else None
    result = dict(field=field, namespace=namespace, candidates=candidates,
                  adopted=None, state='absent', confirmed=False,
                  reason='未提供该字段')
    if not candidates:
        return result
    usable = [candidate for candidate in candidates if candidate['status'] == 'ok']
    if not usable:
        result.update(state='unknown',
                      reason='存在声明但值不可识别；不推测，也不以文件修改时间补空')
        return result
    if field in TIME_FIELDS:
        return _resolve_time(result, usable, report)
    if field == 'gps':
        pairs = [(candidate['value']['latitude'], candidate['value']['longitude'])
                 for candidate in usable]
        if len(set(pairs)) == 1:
            result.update(state='confirmed', confirmed=True,
                          adopted=usable[0]['value'],
                          adopted_from=[candidate['source_id'] for candidate in usable],
                          reason='多个来源的坐标一致')
        else:
            result.update(state='conflict',
                          reason='多个来源的坐标不一致，未按读取顺序选择')
        return result
    values = [_canonical(candidate['value']) for candidate in usable]
    if len(set(values)) > 1:
        result.update(state='conflict',
                      reason='同一字段的候选值不一致，不按读取顺序或最后读到者选择')
        return result
    ordered = sorted(usable, key=lambda candidate: (
        SOURCE_PRIORITY.get(candidate['media'], 9),
        candidate['source_id'], candidate.get('context') or ''))
    chosen = ordered[0]
    result.update(
        state='confirmed', confirmed=True, adopted=chosen['value'],
        adopted_from=[chosen['source_id']],
        reason=f"{MEDIA_LABELS.get(chosen['media'], chosen['media'])}"
               f"与其余候选一致，采用 {chosen['label']}")
    return result


def _resolve_time(result, usable, report):
    seconds = {}
    for candidate in usable:
        seconds.setdefault(candidate['value'][:19], []).append(candidate)
    if len(seconds) > 1:
        result.update(state='conflict',
                      reason='本地时钟值不一致；保留各来源上下文，不按读取顺序选择')
        return result
    second, candidates = next(iter(seconds.items()))
    full_clocks = {candidate['value'] for candidate in candidates}
    offsets = {candidate['time']['offset'] for candidate in candidates}
    listed = sorted(candidates, key=lambda candidate: (
        SOURCE_PRIORITY.get(candidate['media'], 9), candidate['source_id']))
    result.update(clock=second, clocks=sorted(full_clocks),
                  precision=('second' if len(full_clocks) > 1 else 'fraction'),
                  offsets=sorted('unknown' if offset is None else offset
                           for offset in offsets),
                  adopted_from=[candidate['source_id'] for candidate in listed])
    preferred = next((candidate for candidate in candidates
                      if candidate['value'] in full_clocks and
                      len(candidate['value']) > 19), candidates[0])
    subsecond_note = ('' if len(full_clocks) == 1 else
                      '；候选间仅亚秒精度不同，秒级一致')
    if len(offsets) > 1:
        result.update(state='conflict', clock_confirmed=True,
                      reason='本地时钟一致但时区偏移候选不一致，未选择偏移')
        return result
    offset = next(iter(offsets))
    if offset is not None:
        result.update(state='confirmed', confirmed=True,
                      adopted=preferred['time']['utc'],
                      zone_basis='explicit-offset',
                      region_time=preferred['time']['region_time'],
                      reason='本地时钟与显式偏移一致，采用该来源的UTC时间' +
                             subsecond_note)
    elif report.get('task_timezone'):
        result.update(state='partial', zone_basis='task-timezone',
                      adopted=preferred['time']['utc'],
                      region_time=preferred['time']['region_time'],
                      reason=('本地时钟一致；按明确配置的摄影任务时区 '
                              f"{report['task_timezone']} 换算，未使用文件修改时间" +
                              subsecond_note))
    else:
        result.update(state='partial', zone_basis='unknown-zone',
                      reason='本地时钟一致，但没有明确时区；不换算地区时间' +
                             subsecond_note)
    return result


# -------------------------------------------------------------------- export

PROFILES = {
    'internal': dict(label='内部证据', hides=()),
    'location-safe': dict(label='仅去 GPS', hides=(GPS_FIELD,)),
    'editorial': dict(label='编辑用', hides=(GPS_FIELD, 'body_serial_number',
                                              'lens_serial_number')),
    'public': dict(label='公开', hides=(GPS_FIELD, 'body_serial_number',
                                        'lens_serial_number',
                                        'camera_owner_name')),
}

def collect_sidecars(directory, image_name):
    """Pair the conventional sidecar for an explicitly configured job.

    Pairing follows the task's declared convention; files that merely look
    similar are never scanned or substituted.  A missing configured sidecar
    is reported as ``missing``.
    """
    import os
    stem = image_name.rsplit('.', 1)[0] if '.' in image_name else image_name
    name = stem + '.xmp'
    path = os.path.join(directory, name)
    if not os.path.isfile(path):
        return [dict(name=name, data=None,
                     reason='配置的侧车文件不存在')], path
    with open(path, 'rb') as handle:
        data = handle.read(MAX_SIDECAR_BYTES + 1)
    if len(data) > MAX_SIDECAR_BYTES:
        return [dict(name=name, data=None,
                     reason='侧车超过大小限制')], path
    return [dict(name=name, data=data)], path


def export(report, profile='public'):
    """Build a structured export for one purpose.

    The export is derived from collected evidence; it never rewrites the
    internal report.  Hidden fields appear only as ``withheld`` markers with
    no values, locators or candidate details.
    """
    if profile not in PROFILES:
        raise ValueError('未知导出用途：' + str(profile))
    hides = set(PROFILES[profile]['hides'])
    fields = {}
    for field in FIELD_ORDER:
        source = report['fields'][field]
        if field in hides:
            fields[field] = dict(field=field, state='withheld',
                                 reason='按导出用途屏蔽', confirmed=False,
                                 candidate_count=len(source['candidates']))
            continue
        fields[field] = copy.deepcopy(source)
    diagnostics = []
    for diagnostic in report['diagnostics']:
        if diagnostic.get('field') in hides:
            diagnostics.append(dict(field=diagnostic['field'],
                                    status='withheld',
                                    reason='按导出用途屏蔽'))
        else:
            diagnostics.append(copy.deepcopy(diagnostic))
    return dict(profile=profile, profile_label=PROFILES[profile]['label'],
                hidden_fields=sorted(hides), input=dict(report['input']),
                fields=fields, diagnostics=diagnostics,
                packets=[_export_packet(packet, hides)
                         for packet in report['packets']],
                task_timezone=report.get('task_timezone'),
                evidence_not_overwritten=True)


def _export_packet(packet, hides):
    exported = {key: copy.deepcopy(value) for key, value in packet.items()
                if key != 'entries'}
    if 'contexts' in exported and hides:
        # Raw contexts enumerate every entry, which would leak hidden
        # serial/GPS values through the packet index.  Keep counts and
        # status only; the internal report retains the full evidence.
        exported['contexts'] = [
            dict(ifd=context['ifd'], context=context['context'],
                 status=context['status'], reason=context.get('reason'),
                 entry_count=len(context.get('entries', [])))
            for context in packet['contexts']]
    return exported


def export_index(report, profile='public'):
    """A compact index for export listings.  Hidden fields are counted, not
    named with their values or locations, so the index cannot leak them."""
    exported = export(report, profile)
    rows = []
    for field in FIELD_ORDER:
        entry = exported['fields'][field]
        row = dict(field=field, state=entry['state'],
                   candidate_count=entry.get('candidate_count',
                                             len(entry.get('candidates', []))))
        if entry['state'] != 'withheld':
            row['adopted'] = entry.get('adopted')
            row['reason'] = entry.get('reason')
        rows.append(row)
    states = {}
    for row in rows:
        states[row['state']] = states.get(row['state'], 0) + 1
    return dict(profile=profile, fields=rows, states=states,
                packets=len(exported['packets']),
                diagnostics=len(exported['diagnostics']))
