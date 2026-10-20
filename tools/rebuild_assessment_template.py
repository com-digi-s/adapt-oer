#!/usr/bin/env python3
"""Rebuild the checked-in assessment ZIP from explicit, idempotent changes.

Usage: python3 tools/rebuild_assessment_template.py [path/to/assessment-template.zip]
The archive remains self-contained; this tool is not an export-time repair.
"""
import json
from pathlib import Path
import re
import sys
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from adapt_persistence_bootstrap import bootstrap_runtime


def rebuild(path):
    with ZipFile(path) as archive:
        entries = {name: archive.read(name) for name in archive.namelist() if not name.endswith('/')}
    css = entries['adapt.css'].decode('utf-8')
    # Native AAT question stems live in title/displayTitle and ordinary paragraphs.
    # Never globally suppress either. Container headings can remain hidden.
    css = re.sub(r'(?m)^\.component__title\s*\{\s*display:\s*none;\s*\}', '', css)
    css = re.sub(r'(?m)^p\s*\{\s*display:\s*none;\s*\}', '', css)
    entries['adapt.css'] = css.replace('\r\n', '\n').encode('utf-8')
    blocks = json.loads(entries['course/en/blocks.json'])
    for block in blocks:
        # This block only hosts the result UI, not a diagnostic question bank.
        block.pop('_assessment', None)
    entries['course/en/blocks.json'] = (json.dumps(blocks, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    components = json.loads(entries['course/en/components.json'])
    components[0]['instruction'] = (Path(__file__).resolve().parent.parent / 'assessment-template/results.html').read_text(encoding='utf-8')
    entries['course/en/components.json'] = (json.dumps(components, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    runtime = entries['adapt/js/adapt.min.js'].decode('utf-8')
    component_ready = (Path(__file__).resolve().parent.parent / 'assessment-template/submit-all-component-ready.js').read_text(encoding='utf-8').rstrip()
    runtime, replacements = re.subn(r'        onComponentViewRendered: function\(view\) \{[\s\S]*?\n        \},(?=\n        onInteraction:)', lambda _: component_ready, runtime)
    if replacements != 1:
        raise ValueError('Expected exactly one known SubmitAll component-ready lifecycle method')
    entries['adapt/js/adapt.min.js'] = runtime.encode('utf-8')
    entries['adapt/js/adapt.min.js'], _ = bootstrap_runtime(entries['adapt/js/adapt.min.js'])
    with ZipFile(path, 'w', compression=ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)


if __name__ == '__main__':
    rebuild(Path(sys.argv[1] if len(sys.argv) > 1 else 'static/assessment-template.zip'))
