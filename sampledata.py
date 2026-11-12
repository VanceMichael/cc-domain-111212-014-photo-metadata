import struct

def samples():
    tiff = b'II' + struct.pack('<HIH', 42, 8, 1)
    tiff += struct.pack('<HHII', 0x0132, 2, 20, 26) + struct.pack('<I', 0)
    tiff += b'2026:11:12 09:30:00\x00'
    exif = b'Exif\x00\x00' + tiff
    document = b'\xff\xd8\xff\xe1' + struct.pack('>H', len(exif) + 2) + exif + b'\xff\xd9'
    sidecar = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/" dc:rights="Synthetic owner"/></rdf:RDF></x:xmpmeta>'
    return {'sample.jpg': document, 'sample.xmp': sidecar,
            'invalid.jpg': document[:10]}
