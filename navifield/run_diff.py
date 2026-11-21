"""Show what differed between the starting inputs of two retained runs."""
from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
import tempfile

from . import project

_TEXT_LIMIT = 1024 * 1024


def _inputs(root, rid):
    """Files kept when the run started: name -> path, and the entries that were not copied."""
    run = project.read_run(root, rid)
    kept = run.get('input_snapshot') or {}
    files, skipped = {}, {}
    base = kept.get('path')
    for item in kept.get('copied') or []:
        try:
            if isinstance(item, str) and base:
                files[item] = project.resolve_path(root, base + '/' + item)
            elif isinstance(item, dict) and item.get('source') and item.get('path'):
                files['[declared] ' + item['source']] = project.resolve_path(root, item['path'])
        except ValueError:
            continue
    for item in kept.get('skipped') or []:
        if isinstance(item, dict) and item.get('path'):
            skipped[item['path']] = {key: item[key] for key in ('size', 'modified_time', 'reason') if key in item}
    request = run.get('execution_request') or {}
    command = {'program': request.get('program'), 'argv': request.get('argv'),
               'cores': (request.get('resources') or {}).get('cores')}
    return files, skipped, command, bool(kept)


def _earlier_output(name):
    # Monitor tables found in a case at start were written by an earlier run; they are not inputs.
    return name.startswith('postProcessing/')


def _text(path):
    try:
        if path.stat().st_size > _TEXT_LIMIT:
            return None
        return path.read_bytes().decode('utf-8').splitlines()
    except (OSError, UnicodeDecodeError):
        return None


def compare(root, first, second):
    """Differences of kept inputs and of the command; nothing is said about their effect."""
    root = Path(root).resolve()
    if first == second:
        raise ValueError('Name two different runs.')
    left, left_skipped, left_command, left_kept = _inputs(root, first)
    right, right_skipped, right_command, right_kept = _inputs(root, second)
    changed, same, unreadable = [], 0, []
    for name in sorted(set(left) & set(right)):
        if _earlier_output(name):
            continue
        try:
            a, b = left[name].read_bytes(), right[name].read_bytes()
        except OSError:
            unreadable.append(name)
            continue
        if a == b:
            same += 1
            continue
        old, new = _text(left[name]), _text(right[name])
        if old is None or new is None:
            changed.append({'file': name, 'kind': 'binary or not UTF-8', 'sizes': [len(a), len(b)]})
            continue
        lines = list(difflib.unified_diff(old, new, f'{first}/{name}', f'{second}/{name}', lineterm='', n=2))
        changed.append({'file': name, 'kind': 'text',
                        'removed': sum(1 for line in lines if line.startswith('-') and not line.startswith('---')),
                        'added': sum(1 for line in lines if line.startswith('+') and not line.startswith('+++')),
                        'diff': lines})
    large = []
    for name in sorted(set(left_skipped) | set(right_skipped)):
        a, b = left_skipped.get(name), right_skipped.get(name)
        if a != b:
            large.append({'file': name, first: a, second: b})
    return {'runs': [first, second],
            'inputs_kept': {first: left_kept, second: right_kept},
            'command': {'same': left_command == right_command, first: left_command, second: right_command},
            'identical_files': same, 'changed': changed,
            'only_in': {first: sorted(name for name in set(left) - set(right) if not _earlier_output(name)),
                        second: sorted(name for name in set(right) - set(left) if not _earlier_output(name))},
            'results_already_in_case': sorted(name for name in set(left) ^ set(right) if _earlier_output(name)),
            'not_copied_and_different': large, 'unreadable': unreadable,
            'scope': 'Compares the small input files kept when each run started and the command. Files over '
                     '1 MiB, such as meshes and fields, were not kept; for those only size and time are compared.'}


def write(root, result):
    """Write the complete result as JSON and the text differences as one file; return relative paths."""
    root = Path(root).resolve()
    first, second = result['runs']
    paths = []
    text = []
    for item in result['changed']:
        text.extend(item.get('diff') or [f"Binary or non-UTF-8 file differs: {item['file']}"])
        text.append('')
    for suffix, content in (('json', json.dumps(result, ensure_ascii=False, indent=2)), ('diff', '\n'.join(text))):
        target = project.resolve_path(root, f'report/input-diff-{first}-{second}.{suffix}')
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n', dir=target.parent,
                                             suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        paths.append(target.relative_to(root).as_posix())
    return paths


def preview(result, json_path, diff_path, files=8, lines=6):
    """A short result for the terminal: what changed, with the first few changed lines per file."""
    first, second = result['runs']
    changed = []
    for item in result['changed'][:files]:
        entry = {'file': item['file']}
        if item['kind'] == 'text':
            body = [line[:160] for line in item['diff']
                    if line[:1] in '+-' and not line.startswith(('---', '+++'))]
            entry.update(removed=item['removed'], added=item['added'], lines=body[:lines])
        else:
            entry['kind'] = item['kind']
        changed.append(entry)
    return {'runs': result['runs'], 'inputs_kept': result['inputs_kept'], 'command_same': result['command']['same'],
            'command_changes': {key: {rid: result['command'][rid].get(key) for rid in (first, second)}
                                for key in ('program', 'argv', 'cores')
                                if result['command'][first].get(key) != result['command'][second].get(key)},
            'identical_files': result['identical_files'], 'changed_files': len(result['changed']), 'changed': changed,
            'only_in': {first: result['only_in'][first][:10], second: result['only_in'][second][:10]},
            'not_copied_and_different': [item['file'] for item in result['not_copied_and_different']][:10],
            'results_already_in_case': len(result['results_already_in_case']),
            'details': json_path, 'diff': diff_path}
