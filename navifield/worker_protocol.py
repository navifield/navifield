"""Bounded, job-bound frames for the fixed core worker."""
from __future__ import annotations
import json
import struct

MAX_FRAME = 8 * 1024 * 1024


def write_frame(stream, value):
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if not 0 < len(payload) <= MAX_FRAME:
        raise ValueError('worker frame length outside limit')
    stream.write(struct.pack('!I', len(payload)) + payload)
    stream.flush()


def read_frame(stream):
    def exact(size):
        chunks = bytearray()
        while len(chunks) < size:
            part = stream.read(size - len(chunks))
            if not part:
                raise ValueError('truncated worker frame')
            chunks.extend(part)
        return bytes(chunks)
    size = struct.unpack('!I', exact(4))[0]
    if not 0 < size <= MAX_FRAME:
        raise ValueError('worker frame length outside limit')
    value = json.loads(exact(size).decode('utf-8'))
    if not isinstance(value, dict):
        raise ValueError('worker frame must be an object')
    return value
