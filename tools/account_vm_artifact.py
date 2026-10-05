#!/usr/bin/env python3
"""Account frozen VM bytes and prove shared-node encoding is lossless.

Usage: python3 tools/account_vm_artifact.py BEFORE.json AFTER.json
Sizes use compact UTF-8 JSON; enclosing field names/punctuation are reported
separately. This reads artifacts only: it adds no compiler/runtime semantics.
"""
import collections
import json
from pathlib import Path
import sys


def size(value):
    return len(json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode())


def expand(graph):
    nodes = graph['nodes']
    def at(index):
        node = nodes[index]
        if node == 'N':
            return None
        kind, value = next(iter(node.items()))
        if kind in ('B', 'I', 'S'):
            return value
        if kind == 'A':
            return [at(ref) for ref in value]
        if kind == 'O':
            return {at(key): at(ref) for key, ref in value}
        raise ValueError(kind)
    return at(graph['root'])


def account(path):
    artifact = json.loads(path.read_text())
    artifact = artifact.get('artifact', artifact)
    wire_module = artifact['code']['mncs-selected-ssa']['module']
    shared = 'nodes' in wire_module
    module = expand(wire_module) if shared else wire_module
    sections = {key: size(value) for key, value in artifact.items()}
    strings = collections.Counter()
    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                strings[key] += 1
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            strings[value] += 1
    walk(module)
    result = {
        'disk_bytes': path.stat().st_size, 'compact_bytes': size(artifact),
        'suite_json_bytes': len(json.dumps(artifact).encode()),
        'artifact_sections_bytes': sections,
        'artifact_field_names_and_punctuation_bytes': size(artifact) - sum(sections.values()),
        'expanded_ssa_sections_bytes': {key: size(value) for key, value in module.items()},
        'functions': len(module['functions']),
        'blocks': sum(len(f['blocks']) for f in module['functions']),
        'instructions': sum(len(b['instructions']) for f in module['functions'] for b in f['blocks']),
        'ssa_string_occurrences_including_keys': sum(strings.values()),
        'ssa_unique_strings_including_keys': len(strings),
        'ssa_repeated_string_bytes': sum(size(s) * (n - 1) for s, n in strings.items()),
        'contains_program_or_session': any(k in module for k in ('program', 'session', 'cache', 'compiler')),
    }
    if shared:
        categories = collections.Counter()
        for node in wire_module['nodes']:
            categories['N' if node == 'N' else next(iter(node))] += size(node)
        result['shared_node_payload_bytes'] = dict(categories)
        result['shared_nodes'] = len(wire_module['nodes'])
    return artifact, module, result


def main():
    before, old_ssa, old_report = account(Path(sys.argv[1]))
    after, new_ssa, new_report = account(Path(sys.argv[2]))
    assert old_ssa == new_ssa, 'SSA changed: encoding must be lossless'
    old_outer = {k: v for k, v in before.items() if k not in ('artifact_id', 'code')}
    new_outer = {k: v for k, v in after.items() if k not in ('artifact_id', 'code')}
    assert old_outer == new_outer, 'outer contract facts changed'
    print(json.dumps({'before': old_report, 'after': new_report,
                      'all_ssa_facts_preserved': True,
                      'outer_contract_facts_preserved': True,
                      'compact_bytes_saved': old_report['compact_bytes'] - new_report['compact_bytes']}, indent=2))


if __name__ == '__main__':
    main()
