"""Fixed local CLI worker; consumes only the framed local execute operation."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys

from .worker_protocol import read_frame, write_frame
from .local_execution import execute_run


def main():
    request = read_frame(sys.stdin.buffer)
    if set(request) != {'job_id', 'payload'} or not isinstance(request['job_id'], str):
        raise ValueError('invalid worker request')
    payload = request['payload']
    if payload['operation'] != 'run:execute':
        raise ValueError('unsupported local operation')
    for key in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
        os.environ[key] = str(payload['resources']['cores'])
    with open('private-output.log', 'w', encoding='utf-8') as output, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        result = execute_run(Path(payload['root']), {**payload['body'], '_toolchains': payload['toolchains']})
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
    Path('response.json').write_bytes(encoded)
    write_frame(sys.stdout.buffer, {'job_id': request['job_id'], 'artifact': {
        'path': 'response.json', 'size': len(encoded), 'sha256': hashlib.sha256(encoded).hexdigest()}})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
