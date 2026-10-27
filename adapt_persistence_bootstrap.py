"""Versioned build-time hook for the LXP-owned Adapt persistence adapter.

No changes to components or AAT source builds. This hook runs before data.init,
unlike iframe.onload or a result-component script. Actual Spoor is preferred by
the wrapper adapter. Missing/ambiguous runtime markers are export errors.
"""
import hashlib
import re


START = '/* adapt-oer:lxp-persistence-bootstrap:v1:start */'
END = '/* adapt-oer:lxp-persistence-bootstrap:v1:end */'
DECLARATION = re.compile(r'(?:var|const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*__AMD\(([\"\'])core/js/adapt\2,\s*[A-Za-z_$][\w$]*\);?')


def bootstrap_runtime(runtime):
    source = runtime.decode('utf-8')
    before_sha = hashlib.sha256(runtime).hexdigest()
    if START in source or END in source:
        if source.count(START) != 1 or source.count(END) != 1:
            raise ValueError('Ambiguous existing LXP persistence bootstrap')
        begin, end = source.index(START), source.index(END) + len(END)
        source = source[:begin] + source[end:]
    declarations = list(DECLARATION.finditer(source))
    if len(declarations) != 1:
        raise ValueError('Unsupported Adapt runtime: expected exactly one core/js/adapt declaration')
    declaration = declarations[0]
    bootstrap = (START + "if(window.parent!==window&&window.parent.App&&typeof window.parent.App.attachAdaptPersistence==='function'){window.parent.App.attachAdaptPersistence(" + declaration[1] + ',window);}' + END)
    output = (source[:declaration.end()] + bootstrap + source[declaration.end():]).encode('utf-8')
    return output, {'version': 1, 'input_sha256': before_sha, 'output_sha256': hashlib.sha256(output).hexdigest()}
