"""Compare two explicit orders within the literal merger's existing loss gates."""
from __future__ import annotations

from collections import defaultdict
from os import fspath

from .core import (MAX_FILES, MISSING, MergeError, _merge_files, identity, pointer,
                   resource_identity, resource_path, sources)


def _same(left, right):
    """Value equality without Python's bool/int/float equivalence or key ordering."""
    if left is MISSING or right is MISSING:
        return left is right
    if type(left.value) is not type(right.value):
        return False
    if isinstance(left.value, dict):
        return left.value.keys() == right.value.keys() and all(
            _same(v, right.value[k]) for k, v in left.value.items())
    if isinstance(left.value, list):
        return len(left.value) == len(right.value) and all(
            _same(a, b) for a, b in zip(left.value, right.value))
    return left.value == right.value


def _resource_key(kind, ident):
    if kind == 'ports':
        return dict(zip(('host_ip', 'target', 'published', 'protocol'), ident))
    return {'target': ident}


def _resource_histories(project):
    """Associate positional operations with identities at that input, not final indexes.

    A list's before/after snapshots resolve both removals and additions during an
    override. Child events can precede or follow the list event, so index snapshots
    first. One declaration per list per input follows from duplicate mapping gates.
    """
    snapshots = {}
    for event in project.events:
        parts = tuple(p.replace('~1', '/').replace('~0', '~')
                      for p in event['path'][1:].split('/'))
        if resource_path(parts):
            snapshots[event['path'], event['source']['input']] = tuple(
                [resource_identity(parts[2], item) for item in value]
                if isinstance(value, list) else []
                for value in (event['previous'], event['value']))
    history = defaultdict(list)
    for event in project.events:
        parts = event['path'][1:].split('/')
        if len(parts) < 4 or not parts[3].isdecimal():
            continue
        location = '/' + '/'.join(parts[:3])
        snapshot = snapshots.get((location, event['source']['input']))
        if snapshot is None:
            continue
        index = int(parts[3])
        identities = set()
        for present, keys in zip((event['previousPresent'], event['present']), snapshot):
            if present and index < len(keys):
                identities.add(keys[index])
        field = ''.join('/' + p for p in parts[4:])
        for ident in identities:
            operations = history[location, ident, field]
            operation = {'action': event['action'], 'source': dict(event['source'])}
            # Reordering an override can visit one identity at both its old and
            # new positions. Those positional events represent one declaration.
            if not operations or operations[-1] != operation:
                operations.append(operation)
    return history


def compare_files(left_files, right_files):
    """Report literal final-value differences, never equivalence after a failed merge.

    Each order must name the same files exactly once, with identical path spelling.
    Bytes are read once; each order is separately parsed/evaluated with its own
    input indexes and all normal budgets. Resource lists match normalized unique
    identities; generic lists remain whole fields. Order changes among shared
    resources are separate from field/membership differences.
    """
    orders = [list(map(fspath, files)) for files in (left_files, right_files)]
    for side, files in zip(('left', 'right'), orders):
        if not 1 <= len(files) <= MAX_FILES:
            raise MergeError(f'{side}: provide 1..{MAX_FILES} files')
        if len(files) != len(set(files)):
            raise MergeError(f'{side}: comparison requires each file exactly once')
    if set(orders[0]) != set(orders[1]):
        raise MergeError('comparison requires the same files with identical path spelling in both orders')

    contents = {}
    projects = []
    for side, files in zip(('left', 'right'), orders):
        try:
            projects.append(_merge_files(files, contents))
        except MergeError as e:
            raise MergeError(f'{side} order unevaluated: {e}') from e

    histories = []
    for project in projects:
        by_path = defaultdict(list)
        for event in project.events:
            by_path[event['path']].append({'action': event['action'], 'source': dict(event['source'])})
        histories.append(by_path)
    differences = []
    order_changes = []
    resource_histories = [_resource_histories(project) for project in projects]

    def evidence(change, nodes, operations):
        participants = set()
        for side, node, ops in zip(('left', 'right'), nodes, operations):
            winners = sources(node)
            participants.update(s['file'] for s in winners)
            participants.update(op['source']['file'] for op in ops)
            change[side] = {
                'present': node is not MISSING,
                'value': None if node is MISSING else node.plain(),
                'winningSources': winners,
                'operations': ops,
            }
        # These are the field's contributing declarations in each explicit order,
        # not a claim of a globally minimal counterfactual explanation. Keeping
        # the union also covers an early reset whose path did not yet exist.
        change['decisiveOrder'] = {
            side: [{'file': f, 'input': i} for i, f in enumerate(files) if f in participants]
            for side, files in zip(('left', 'right'), orders)
        }
        return change

    def resources(left, right, path):
        kind, location = path[2], pointer(path)
        indexes = [{identity(kind, node): (i, node) for i, node in enumerate(n.value)}
                   for n in (left, right)]
        keys = [list(index) for index in indexes]
        common = indexes[0].keys() & indexes[1].keys()
        if [key for key in keys[0] if key in common] != [key for key in keys[1] if key in common]:
            change = evidence({'path': location}, (left, right),
                              [history.get(location, []) for history in histories])
            for side, ordered in zip(('left', 'right'), keys):
                change[side].pop('value')
                change[side]['identities'] = [_resource_key(kind, key) for key in ordered]
            order_changes.append(change)

        def fields(nodes, ident, field=()):
            a, b = nodes
            if (a is not MISSING and b is not MISSING
                    and isinstance(a.value, dict) and isinstance(b.value, dict)):
                for key in sorted(a.value.keys() | b.value.keys()):
                    fields((a.value.get(key, MISSING), b.value.get(key, MISSING)), ident, field + (key,))
                return
            if _same(a, b):
                return
            relative = pointer(field)
            change = evidence({'path': location, 'resourceKey': _resource_key(kind, ident),
                               'fieldPath': relative}, nodes,
                              [history.get((location, ident, relative), []) for history in resource_histories])
            for side, node, index in zip(('left', 'right'), nodes, indexes):
                change[side]['path'] = (pointer(path + (index[ident][0],) + field)
                                        if node is not MISSING else None)
            differences.append(change)

        for ident in sorted(indexes[0].keys() | indexes[1].keys()):
            fields(tuple(index.get(ident, (None, MISSING))[1] for index in indexes), ident)

    def visit(left, right, path=()):
        if left is not MISSING and right is not MISSING:
            if resource_path(path) and isinstance(left.value, list) and isinstance(right.value, list):
                resources(left, right, path)
                return
            if isinstance(left.value, dict) and isinstance(right.value, dict):
                for key in sorted(left.value.keys() | right.value.keys()):
                    visit(left.value.get(key, MISSING), right.value.get(key, MISSING), path + (key,))
                return
        if not _same(left, right):
            location = pointer(path)
            differences.append(evidence({'path': location}, (left, right),
                                        [history.get(location, []) for history in histories]))

    visit(projects[0].root, projects[1].root)
    return {'leftFiles': orders[0], 'rightFiles': orders[1],
            'equivalent': not differences and not order_changes, 'differences': differences,
            'resourceOrderChanges': order_changes}
