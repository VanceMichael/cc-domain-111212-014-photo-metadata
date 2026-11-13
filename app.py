import argparse
import base64
import json
from pathlib import Path
import sys
import tempfile

from metadata import collect_bytes, export, export_index
from source import inspect_bytes
from sampledata import samples
from storage import Archive
from transport import ReplaySource, fetch

MAX_INPUT = 16*1024*1024

def read_input(path):
    with Path(path).open('rb') as handle:
        data = handle.read(MAX_INPUT+1)
    if len(data) > MAX_INPUT:
        raise ValueError('输入超过接收限制')
    return data

def demo():
    fixtures = samples()
    name = 'sample-multi.jpg'
    data = fixtures[name]
    sidecar = fixtures['sample-multi.xmp']
    routes = {'GET /sample':[{'base64':base64.b64encode(data).decode()}],
              'GET /sample.xmp':[{'base64':base64.b64encode(sidecar).decode()}]}
    with tempfile.TemporaryDirectory() as directory, ReplaySource(routes) as server:
        status, headers, raw = fetch(server.url + '/sample')
        if status != 200:
            raise ValueError('来源请求失败')
        status, headers, sidecar_raw = fetch(server.url + '/sample.xmp')
        if status != 200:
            raise ValueError('侧车请求失败')
        rows = inspect_bytes(raw)
        report = collect_bytes(raw, sidecars=[dict(name='sample-multi.xmp',
                                                   data=sidecar_raw)])
        missing = collect_bytes(raw, sidecars=[dict(
            name='absent.xmp', data=None, reason='配置的侧车文件不存在')])
        with Archive(Path(directory)/'archive.sqlite3') as archive:
            first = archive.save('local-demo', raw, rows)
            repeated = archive.save('local-demo', raw, rows)
            return dict(input=name, bytes=len(raw), captures=len(archive.list()),
                        same_capture=first==repeated, rows=rows,
                        metadata=export_index(report, 'public'),
                        missing_sidecar=[packet['status'] for packet in
                                         missing['packets']
                                         if packet['kind'] == 'sidecar-xmp'],
                        requests=len(server.requests))

def metadata_command(args):
    raw = read_input(args.path)
    sidecars = []
    if args.sidecar:
        path = Path(args.sidecar)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        sidecars.append(dict(name=path.name, data=path.read_bytes()))
    report = collect_bytes(raw, sidecars=sidecars,
                           task_timezone=args.task_timezone)
    if args.index:
        return export_index(report, args.profile)
    return export(report, args.profile)

def main(argv=None):
    parser = argparse.ArgumentParser(description='来源检查与原件留存')
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('path')
    metadata = commands.add_parser('metadata')
    metadata.add_argument('path')
    metadata.add_argument('--sidecar')
    metadata.add_argument('--task-timezone')
    metadata.add_argument('--profile', default='public')
    metadata.add_argument('--index', action='store_true')
    for name in ('capture',):
        command = commands.add_parser(name)
        command.add_argument('path')
        command.add_argument('--database', required=True)
        command.add_argument('--source', required=True)
    listing = commands.add_parser('list')
    listing.add_argument('--database', required=True)
    fixtures = commands.add_parser('fixtures')
    fixtures.add_argument('directory')
    commands.add_parser('demo')
    args = parser.parse_args(argv)
    try:
        if args.command == 'demo':
            result = demo()
        elif args.command == 'fixtures':
            destination = Path(args.directory)
            destination.mkdir(parents=True, exist_ok=True)
            for name, raw in samples().items():
                path = destination/name
                if path.exists():
                    raise ValueError('不覆盖已有夹具文件：'+name)
                path.write_bytes(raw)
            result = dict(files=sorted(samples()))
        elif args.command == 'list':
            with Archive(args.database) as archive:
                result = archive.list()
        elif args.command == 'metadata':
            result = metadata_command(args)
        else:
            raw = read_input(args.path)
            rows = inspect_bytes(raw)
            if args.command == 'inspect':
                result = rows
            else:
                with Archive(args.database) as archive:
                    result = dict(capture_id=archive.save(args.source,raw,rows), rows=len(rows))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps(dict(error=type(error).__name__, reason=str(error)), ensure_ascii=False), file=sys.stderr)
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
