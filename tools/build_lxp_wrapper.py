"""Package a validated Adapt LXP dist/ directory for a pinned OER deployment.

Does not fetch mutable URLs, generate capability approvals or modify .env.
"""
import argparse
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import sys
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lxp_multi_export import _validate_wrapper, safe_file_path, validate_archive_entries


def build_wrapper(source, output):
    source, output = Path(source).resolve(), Path(output).absolute()
    if not source.is_dir():
        raise ValueError('Expected the built Adapt LXP dist/ directory')
    if output.resolve().is_relative_to(source):
        raise ValueError('Write wrapper.zip outside its source directory')
    _validate_wrapper(source)
    entries = {}
    for entry in sorted(source.rglob('*')):
        if entry.is_symlink():
            raise ValueError('Wrapper source must not contain symbolic links')
        if entry.is_file():
            name = entry.relative_to(source).as_posix()
            safe_file_path(name)
            entries[name] = entry.read_bytes()
    # Preserve upstream's license/notices outside dist as well as embedded headers.
    for name in ('LICENSE', 'libraries/react-bootstrap.min.js.LICENSE.txt'):
        notice = source.parent / name
        if notice.is_file() and name not in entries:
            if notice.is_symlink():
                raise ValueError('Wrapper notices must not be symbolic links')
            entries[name] = notice.read_bytes()
    # Validate the exact captured bytes, not a potentially changing source tree.
    with tempfile.TemporaryDirectory(prefix='lxp-wrapper-check-') as temporary:
        for name, content in entries.items():
            target = Path(temporary) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        _validate_wrapper(temporary)
    data = BytesIO()
    with ZipFile(data, 'w', compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in sorted(entries.items()):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, content, compresslevel=9)
    payload = data.getvalue()
    with ZipFile(BytesIO(payload)) as archive:
        validate_archive_entries(archive)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects an already configured deployment artifact.
    with output.open('xb') as target:
        target.write(payload)
    return sha256(payload).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='Compatible Adapt LXP dist/ directory')
    parser.add_argument('--output', type=Path, default=Path('static/lxp-wrapper.zip'))
    args = parser.parse_args(argv)
    try:
        digest = build_wrapper(args.source, args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Wrapper build failed: {error}\n')
    print(f'LXP_WRAPPER_SHA256={digest}')
    print(f'Wrapper: {args.output}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
