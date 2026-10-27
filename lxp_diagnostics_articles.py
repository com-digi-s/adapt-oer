"""One explicit authoring convention for diagnostic-only article subtrees.

This is not a general hidden-content filter. Only an article carrying the exact
class token below, explicitly hidden AND optional, may be omitted from learning
delivery. The published source remains the authority for diagnostic questions.
"""
from copy import deepcopy

from lxp_catalogue import PackageValidationError


DIAGNOSTICS_ONLY_CLASS = 'lxp-diagnostics-only'
DATA_COLLECTIONS = ('contentObjects', 'articles', 'blocks', 'components')


def _marked(node):
    classes = node.get('_classes') or ''
    return isinstance(classes, str) and DIAGNOSTICS_ONLY_CLASS in classes.split()


def learning_source_view(source):
    """Return a non-mutating learning view and exact indexed removed nodes.

    Markerless sources are returned unchanged: existing fingerprint semantics,
    IDs, tracking counters, ordering and delivery bytes need no normalization.
    When a marker exists, reject ambiguous IDs or malformed descendant links
    rather than allowing an incomplete subtree to disappear silently.
    """
    data = source['data']
    marked = [(name, node) for name in DATA_COLLECTIONS for node in data[name] if _marked(node)]
    if _marked(source['course']) or any(name != 'articles' for name, _ in marked):
        raise PackageValidationError('lxp-diagnostics-only is permitted only on an article')
    if not marked:
        return source, {}

    nodes, collection_for = {}, {}
    for name in DATA_COLLECTIONS:
        for node in data[name]:
            node_id = node.get('_id')
            if not isinstance(node_id, str) or not node_id or node_id in nodes or node_id == source['course'].get('_id'):
                raise PackageValidationError('Diagnostic-only article requires unique explicit source IDs')
            if not isinstance(node.get('_parentId'), str) or not node['_parentId']:
                raise PackageValidationError('Diagnostic-only article requires explicit source parent IDs')
            nodes[node_id], collection_for[node_id] = node, name
    article_ids = set()
    for _, article in marked:
        if article.get('_isAvailable') is not False or article.get('_isOptional') is not True:
            raise PackageValidationError('lxp-diagnostics-only article must explicitly be unavailable and optional')
        parent = nodes.get(article.get('_parentId'))
        if not parent or collection_for[parent['_id']] != 'contentObjects' or parent.get('_type') != 'page':
            raise PackageValidationError('Diagnostic-only article must belong to an existing learning page')
        article_ids.add(article['_id'])

    removed_ids = set(article_ids)
    while True:
        descendants = {node_id for node_id, node in nodes.items() if node.get('_parentId') in removed_ids}
        new_ids = descendants - removed_ids
        if not new_ids:
            break
        removed_ids.update(new_ids)
    for node_id in removed_ids - article_ids:
        node = nodes[node_id]
        name = collection_for[node_id]
        parent_name = collection_for.get(node.get('_parentId'))
        if (name, parent_name) not in {('blocks', 'articles'), ('components', 'blocks')}:
            raise PackageValidationError('Diagnostic-only article has a malformed descendant hierarchy')

    view = dict(source)
    view['data'] = dict(data)
    removed = {}
    for name in DATA_COLLECTIONS:
        entries = [{'index': index, 'node': deepcopy(node)} for index, node in enumerate(data[name]) if node['_id'] in removed_ids]
        if entries:
            removed[name] = entries
            view['data'][name] = [node for node in data[name] if node['_id'] not in removed_ids]
    return view, removed


def restore_removed_nodes(delivered, removals):
    """Reconstruct raw source JSON for independently checked provenance.

    ``removals`` is already scoped to the three declared changed JSON files.
    Original positions make arbitrary source ordering reproducible; removing the
    reconstructed helper subtrees must reproduce the delivered view exactly.
    """
    restored = dict(delivered)
    restored['data'] = deepcopy(delivered['data'])
    for name, entries in removals.items():
        if name not in {'articles', 'blocks', 'components'} or not isinstance(entries, list) or not entries:
            raise PackageValidationError('Invalid diagnostic-only JSON removal provenance')
        previous = -1
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {'index', 'node'}:
                raise PackageValidationError('Invalid diagnostic-only removed node provenance')
            index, node = entry['index'], entry['node']
            values = restored['data'][name]
            if not isinstance(index, int) or isinstance(index, bool) or index <= previous or index > len(values) or not isinstance(node, dict):
                raise PackageValidationError('Invalid diagnostic-only original node position')
            values.insert(index, deepcopy(node))
            previous = index
    view, expected = learning_source_view(restored)
    if expected != removals or view['data'] != delivered['data']:
        raise PackageValidationError('Diagnostic-only removal provenance does not reproduce delivered learning JSON')
    return restored
