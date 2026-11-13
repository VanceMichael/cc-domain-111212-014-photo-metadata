import json
import unittest

import metadata
from sampledata import samples

class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = samples()

    def collect(self, name, sidecar_name=None, **options):
        sidecars = []
        if sidecar_name:
            sidecars.append(dict(name=sidecar_name,
                                 data=self.fixtures[sidecar_name]))
        return metadata.collect_bytes(self.fixtures[name], sidecars=sidecars,
                                      **options)

    def test_multi_source_candidates_and_locations(self):
        report = self.collect('sample-multi.jpg', 'sample-multi.xmp')
        copyright = report['fields']['copyright']
        self.assertEqual(copyright['state'], 'confirmed')
        self.assertEqual(len(copyright['candidates']), 3)
        media = {candidate['media'] for candidate in copyright['candidates']}
        self.assertEqual(media, {'exif', 'embedded-xmp', 'sidecar-xmp'})
        namespaces = {candidate['namespace'] for candidate in copyright['candidates']}
        self.assertGreaterEqual(len(namespaces), 2)
        for candidate in copyright['candidates']:
            self.assertIn('locator', candidate)
        tiff_candidate = next(candidate for candidate in copyright['candidates']
                              if candidate['media'] == 'exif')
        self.assertIn('value_offset', tiff_candidate['locator'])
        xmp_candidate = next(candidate for candidate in copyright['candidates']
                             if candidate['media'] == 'sidecar-xmp')
        self.assertTrue(xmp_candidate['locator']['xml_path'].endswith('@dc:rights'))

    def test_conflicting_values_are_not_resolved_by_order(self):
        report = self.collect('sample-conflict.jpg', 'sample-conflict.xmp')
        for field in ('copyright', 'body_serial_number'):
            resolved = report['fields'][field]
            self.assertEqual(resolved['state'], 'conflict')
            self.assertIsNone(resolved['adopted'])
            self.assertGreaterEqual(len(resolved['candidates']), 3)
            values = {json.dumps(candidate['value']) for candidate in resolved['candidates']}
            self.assertGreaterEqual(len(values), 2)

    def test_embedded_and_sidecar_same_value_confirms(self):
        report = self.collect('sample-multi.jpg', 'sample-multi.xmp')
        self.assertEqual(report['fields']['camera_make']['state'], 'confirmed')
        self.assertEqual(report['fields']['camera_make']['adopted'], 'Synthetic Optics')

    def test_missing_sidecar_is_reported_not_substituted(self):
        report = self.collect('sample-conflict.jpg',
                              sidecar_name=None)
        missing = metadata.collect_bytes(
            self.fixtures['sample-conflict.jpg'],
            sidecars=[dict(name='sample-conflict.xmp', data=None,
                           reason='配置的侧车文件不存在')])
        states = [packet['status'] for packet in missing['packets']
                  if packet['kind'] == 'sidecar-xmp']
        self.assertIn('missing', states)
        self.assertEqual(report['fields']['copyright']['state'], 'conflict')

    def test_broken_sidecar_xml_is_isolated(self):
        report = self.collect('sample-multi.jpg', 'sample-broken.xmp')
        broken = [packet for packet in report['packets']
                  if packet['kind'] == 'sidecar-xmp']
        self.assertEqual(broken[0]['status'], 'error')
        self.assertIn('XML', broken[0]['reason'])
        self.assertEqual(report['fields']['copyright']['state'], 'confirmed')

    def test_capture_time_before_digitized_and_unknown_zone(self):
        report = self.collect('sample-digitized-only.jpg')
        self.assertEqual(report['fields']['capture_time']['state'], 'absent')
        digitized = report['fields']['digitized_time']
        self.assertEqual(digitized['state'], 'partial')
        self.assertEqual(digitized['candidates'][0]['time']['zone_basis'],
                         'unknown-zone')
        self.assertIsNone(digitized['adopted'])
        self.assertFalse(report['input']['mtime_used'])

    def test_task_timezone_is_only_conversion_basis(self):
        report = self.collect('sample-digitized-only.jpg',
                              task_timezone='Asia/Shanghai')
        field = report['fields']['digitized_time']
        self.assertEqual(field['zone_basis'], 'task-timezone')
        self.assertEqual(field['adopted'], '2026-02-02T20:05:06+00:00')
        self.assertIn('Asia/Shanghai', field['reason'])
        self.assertIn('未使用文件修改时间', field['reason'])
        with self.assertRaises(ValueError):
            self.collect('sample-digitized-only.jpg',
                         task_timezone='Nowhere/Invalid')

    def test_explicit_offset_gives_utc_without_configuration(self):
        field = self.collect('sample-big-endian.jpg')['fields']['capture_time']
        self.assertEqual(field['state'], 'confirmed')
        self.assertEqual(field['zone_basis'], 'explicit-offset')
        self.assertEqual(field['adopted'], '2026-07-08T15:11:12+00:00')

    def test_exif_change_time_is_not_capture_time(self):
        report = self.collect('sample-multi.jpg', 'sample-multi.xmp')
        self.assertEqual(report['fields']['metadata_change_time']['state'],
                         'partial')
        self.assertNotEqual(report['fields']['capture_time']['clock'],
                            report['fields']['metadata_change_time']['clock'])

    def test_thumbnail_fields_do_not_impersonate_main_image(self):
        report = self.collect('sample-thumbnail.jpg')
        self.assertEqual(report['fields']['copyright']['adopted'],
                         'Main Image Owner')
        self.assertEqual(report['fields']['capture_time']['state'], 'absent')
        contexts = report['packets'][0]['contexts']
        thumbnail = next(context for context in contexts
                         if context['context'] == 'thumbnail')
        self.assertEqual(thumbnail['status'], 'parsed')
        hints = [entry['field_hint'] for entry in thumbnail['entries']
                 if 'field_hint' in entry]
        self.assertIn('capture_time', hints)

    def test_bad_pointer_is_bounded_and_reported(self):
        report = self.collect('sample-bad-pointer.jpg')
        self.assertEqual(report['packets'][0]['status'], 'partial')
        reasons = ' '.join(diagnostic['reason'] for diagnostic in report['diagnostics'])
        self.assertIn('越界', reasons)
        self.assertEqual(report['fields']['camera_model']['adopted'], 'BrokenCam')
        self.assertEqual(report['fields']['gps']['state'], 'absent')

    def test_offsets_do_not_cross_app1_boundaries(self):
        report = self.collect('sample-split-context.jpg')
        capture = report['fields']['capture_time']
        self.assertEqual(capture['state'], 'partial')
        self.assertEqual(capture['candidates'][0]['time']['zone_basis'],
                         'unknown-zone')
        self.assertEqual(len(report['packets']), 2)

    def test_export_hides_fields_without_touching_evidence(self):
        report = self.collect('sample-multi.jpg', 'sample-multi.xmp',
                              task_timezone='Asia/Shanghai')
        internal_gps = report['fields']['gps']['adopted']
        internal_serial = report['fields']['body_serial_number']['adopted']
        exported = metadata.export(report, 'public')
        self.assertEqual(exported['fields']['gps']['state'], 'withheld')
        self.assertEqual(exported['fields']['body_serial_number']['state'],
                         'withheld')
        text = json.dumps(exported, ensure_ascii=False, sort_keys=True)
        self.assertNotIn('31.243', text)
        self.assertNotIn('SN-SYNTHETIC-001', text)
        for field in ('gps', 'body_serial_number', 'lens_serial_number'):
            entry = exported['fields'][field]
            self.assertNotIn('candidates', entry)
            self.assertNotIn('locator', json.dumps(entry, ensure_ascii=False))
            self.assertNotIn('adopted', entry)
        # Evidence is not overwritten.
        self.assertEqual(report['fields']['gps']['adopted'], internal_gps)
        self.assertEqual(report['fields']['body_serial_number']['adopted'],
                         internal_serial)
        # Unexpected profile fails loudly instead of silently exporting all.
        with self.assertRaises(ValueError):
            metadata.export(report, 'unknown-profile')

    def test_internal_profile_keeps_everything(self):
        report = self.collect('sample-multi.jpg', 'sample-multi.xmp')
        exported = metadata.export(report, 'internal')
        self.assertEqual(exported['fields']['gps']['state'],
                         report['fields']['gps']['state'])
        self.assertIn('locator', exported['fields']['gps']['candidates'][0])

    def test_export_index_does_not_leak_hidden_fields(self):
        report = self.collect('sample-multi.jpg', 'sample-multi.xmp')
        index = metadata.export_index(report, 'public')
        text = json.dumps(index, ensure_ascii=False, sort_keys=True)
        self.assertNotIn('31.243', text)
        self.assertNotIn('SN-SYNTHETIC-001', text)
        rows = {row['field']: row for row in index['fields']}
        self.assertEqual(rows['gps']['state'], 'withheld')
        self.assertNotIn('adopted', rows['gps'])
        self.assertEqual(rows['copyright']['state'], 'confirmed')

    def test_collect_is_deterministic(self):
        first = self.collect('sample-multi.jpg', 'sample-multi.xmp')
        second = self.collect('sample-multi.jpg', 'sample-multi.xmp')
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))
        self.assertEqual(self.fixtures, samples())

if __name__ == '__main__':
    unittest.main()
