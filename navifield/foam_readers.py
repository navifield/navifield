"""Small OpenFOAM observations used by the generic execution path."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re


def strip_foam_comments(text: str) -> str:
    return re.sub(r'//[^\r\n]*', '', re.sub(r'/\*.*?\*/', '', text, flags=re.S))


def field_header(prefix: bytes) -> tuple[dict[str, str], tuple[float, ...] | None]:
    """Read bounded header metadata without interpreting any field payload."""
    head = prefix.split(b'internalField', 1)[0].decode('ascii')
    text = strip_foam_comments(head)
    block = re.search(r'\bFoamFile\s*\{([^{}]*)\}', text, flags=re.S)
    if not block:
        return {}, None
    values = dict(re.findall(r'\b(format|class|object)\s+([^;\s]+)\s*;', block.group(1)))
    match = re.search(r'\bdimensions\s*\[([^\]\r\n]{1,96})\]\s*;', text)
    if not match:
        return values, None
    try:
        dimensions = tuple(float(value) for value in match.group(1).split())
    except ValueError:
        return values, None
    if len(dimensions) != 7 or not all(math.isfinite(value) for value in dimensions):
        return values, None
    return values, dimensions


def _field_tokens(stream):
    """Yield small whitespace-delimited field tokens without loading the file."""
    def characters():
        while chunk := stream.read(65536):
            yield from chunk

    chars = iter(characters())
    word = []
    for char in chars:
        if char == '"':
            if word:
                raise ValueError('unsupported quoted field token')
            escaped = False
            for char in chars:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    break
            else:
                raise ValueError('unterminated quoted field token')
            yield '"'
            continue
        if char == '/':
            nxt = next(chars, '')
            if nxt == '/':
                for char in chars:
                    if char == '\n':
                        break
                char = ' '
            elif nxt == '*':
                previous = ''
                for char in chars:
                    if previous == '*' and char == '/':
                        break
                    previous = char
                else:
                    raise ValueError('unterminated field comment')
                char = ' '
            else:
                word.append('/')
                if not nxt:
                    break
                char = nxt
        if char.isspace() or char in '();{}':
            if word:
                yield ''.join(word)
                word = []
            if char in '();{}':
                yield char
        else:
            word.append(char)
            if len(word) > 4096:
                raise ValueError('unsupported field token')
    if word:
        yield ''.join(word)


def complete_internal_field(path, field_class: str) -> bool:
    """Observe a complete finite ASCII internalField, not restart validity."""
    kinds = (('SphericalTensorField', 'sphericalTensor', 1), ('SymmTensorField', 'symmTensor', 6),
             ('ScalarField', 'scalar', 1), ('VectorField', 'vector', 3), ('TensorField', 'tensor', 9))
    kind = next(((name, width) for suffix, name, width in kinds if field_class.endswith(suffix)), None)
    if kind is None:
        return False
    name, width = kind

    def number(token):
        return math.isfinite(float(token))

    def value(tokens):
        first = next(tokens)
        if width == 1:
            return number(first)
        if first != '(':
            return False
        for _ in range(width):
            if not number(next(tokens)):
                return False
        return next(tokens) == ')'

    try:
        with path.open('r', encoding='ascii', newline='') as stream:
            tokens = iter(_field_tokens(stream))
            depth = 0
            for token in tokens:
                if token == '{':
                    depth += 1
                elif token == '}':
                    depth -= 1
                    if depth < 0:
                        return False
                elif token == 'internalField' and depth == 0:
                    break
            else:
                return False
            mode = next(tokens)
            if mode == 'uniform':
                if not value(tokens) or next(tokens) != ';':
                    return False
            else:
                if mode != 'nonuniform' or next(tokens) != f'List<{name}>':
                    return False
                count = next(tokens)
                if not count.isascii() or not count.isdecimal() or next(tokens) != '(':
                    return False
                for _ in range(int(count)):
                    if not value(tokens):
                        return False
                if next(tokens) != ')' or next(tokens) != ';':
                    return False
            for token in tokens:
                if token == '{':
                    depth += 1
                elif token == '}':
                    depth -= 1
                    if depth < 0:
                        return False
            return depth == 0
    except (OSError, UnicodeError, ValueError, OverflowError, StopIteration):
        return False


@dataclass(frozen=True, slots=True)
class ResidualPoint:
    axis: float
    region: str
    equation: str
    initial: float
    final: float
    iterations: int
    segment: int = 0


class ResidualParser:
    """Incrementally observe first equation solves of completed log steps."""

    _time = re.compile(r'^Time\s*=\s*([\d.eE+\-]+)(?:\s+s)?\s*$')
    _region = re.compile(r'^Solving for (?:fluid|solid) region\s+(\S+)\s*$')
    _outer = re.compile(r'^PIMPLE:\s*iteration\s+(\d+)\b', re.I)
    _solve = re.compile(r'Solving for ([^,\s]+),\s*Initial residual\s*=\s*([^,]+),\s*Final residual\s*=\s*([^,]+),\s*No Iterations\s+(\d+)\s*$')

    def __init__(self):
        self.partial = ''
        self.discard = False
        self.axis = None
        self.region = ''
        self.outer = 0
        self.rows = {}
        self.completed = False
        self.segment = 0

    def _finish(self):
        if self.axis is None or self.completed:
            return []
        self.completed = True
        return [ResidualPoint(self.axis, region, equation, initial, final, count, self.segment)
                for region, equation, initial, final, count in self.rows.values()]

    def _line(self, line):
        match = self._time.fullmatch(line)
        if match:
            value = float(match.group(1))
            if not math.isfinite(value) or value < 0:
                return []
            ready = self._finish()
            if self.axis is not None and value <= self.axis:
                self.segment += 1
            self.axis, self.region, self.outer, self.rows, self.completed = value, '', 0, {}, False
            return ready
        if self.axis is None:
            return []
        if line.startswith('ExecutionTime =') or line == 'End':
            return self._finish()
        if self.completed:
            return []
        match = self._region.fullmatch(line)
        if match:
            self.region = match.group(1)
            return []
        match = self._outer.match(line)
        if match:
            current = int(match.group(1))
            if current == 1 and self.outer == 0:
                self.rows = {}
            self.outer = current
            return []
        if self.outer > 1:
            return []
        match = self._solve.search(line)
        if match:
            equation, first, last, iterations = match.groups()
            initial, final = float(first), float(last)
            if all(math.isfinite(x) and x >= 0 for x in (initial, final)):
                self.rows.setdefault((self.region, equation), (self.region, equation, initial, final, int(iterations)))
        return []

    def feed(self, data: bytes):
        pieces = (self.partial + data.decode('latin-1')).split('\n')
        self.partial = pieces.pop()
        output = []
        for piece in pieces:
            if self.discard:
                self.discard = False
                continue
            if len(piece) > 65536:
                continue
            try:
                output.extend(self._line(piece.rstrip('\r').strip()))
            except (ValueError, OverflowError):
                continue
        if len(self.partial) > 65536:
            self.partial = ''
            self.discard = True
        return tuple(output)


@dataclass(frozen=True)
class CheckMeshResult:
    cells: int | None
    max_non_ortho: float | None
    max_skewness: float | None
    failed_checks: int | None
    severe_non_ortho_faces: int | None
    regions: int | None
    min_volume: float | None
    illegal_faces: int | None
    boundary_openness: tuple[float, float, float] | None
    mesh_ok: bool
    complete: bool
    missing: tuple[str, ...]
    failed_categories: tuple[str, ...]

    def to_dict(self):
        return asdict(self)


_NUMBER = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'


def parse_checkmesh(text: str) -> CheckMeshResult:
    """Retain the native report; incomplete observations never imply success."""
    def one(pattern, kind=float):
        matches = re.findall(pattern, text, flags=re.I | re.M)
        if len(matches) != 1:
            return None
        value = kind(matches[0])
        return value if math.isfinite(value) else None

    metrics = {
        'cells': one(r'^\s*cells:\s*(\d+)\s*$', int),
        'max_non_ortho': one(r'(?:Max non-orthogonality|Mesh non-orthogonality Max)\s*[=:]?\s*(' + _NUMBER + ')'),
        'max_skewness': one(r'Max skewness\s*=\s*(' + _NUMBER + ')'),
        'regions': one(r'(?:Number of regions|regions)\s*:\s*(\d+)', int),
        'min_volume': one(r'Min(?:imum)? volume\s*=\s*(' + _NUMBER + ')'),
        'illegal_faces': one(r'^\s*Number of illegal faces:\s*(\d+)\s*$', int),
        'failed_checks': one(r'Failed\s+(\d+)\s+mesh checks?\.', int),
        'severe_non_ortho_faces': one(r'Number of severely non-orthogonal[^\n]*?faces:\s*(\d+)', int),
    }
    if metrics['illegal_faces'] is None and all(re.search(r'^\s*' + name + r' OK\.\s*$', text, re.M) for name in ('Face vertices', 'Face pyramids')):
        metrics['illegal_faces'] = 0
    ok = bool(re.search(r'^\s*Mesh OK\.\s*$', text, re.M))
    if metrics['failed_checks'] is None and ok:
        metrics['failed_checks'] = 0
    if metrics['severe_non_ortho_faces'] is None and re.search(r'Non-orthogonality check OK\.', text):
        metrics['severe_non_ortho_faces'] = 0
    vectors = re.findall(r'Boundary openness\s*\(\s*(' + _NUMBER + r')\s+(' + _NUMBER + r')\s+(' + _NUMBER + r')\s*\)', text)
    metrics['boundary_openness'] = tuple(map(float, vectors[0])) if len(vectors) == 1 else None
    missing = [key for key in ('cells', 'max_non_ortho', 'max_skewness', 'regions', 'min_volume', 'failed_checks', 'severe_non_ortho_faces', 'illegal_faces', 'boundary_openness') if metrics[key] is None]
    ends = re.findall(r'^[ \t]*End[ \t]*\r?$', text, re.M)
    if len(ends) != 1 or not re.search(r'^\s*End\s*$\s*\Z', text, re.M):
        missing.append('End')
    fatal = re.search(r'FOAM FATAL|MPI_ABORT', text, re.I)
    for line in text.splitlines():
        if re.search(r'(?:^\s*|:\s*(?:\d+\s+)?)(?:Floating point exception|Segmentation fault)(?:\s*\((?:core dumped|[0-9]+)\)(?:\s+.*)?|\s*:\s*.*|\s*)$', line, re.I) or re.search(r'^\s*(?:.*:\s*)?(?:Received|Caught) signal (?:8|11)\s*\((?:SIGFPE|SIGSEGV)\)\s*$', line, re.I) or re.fullmatch(r'\s*(?:SIGFPE|SIGSEGV)\s*', line, re.I):
            fatal = True
    if fatal:
        missing.append('normal_exit')
    if ok and metrics['failed_checks']:
        missing.append('conflicting_verdict')
    if re.search(r'Number of regions[^\n]*\(FAILED\)', text, re.I):
        missing.append('topology_status')
    categories = tuple(line.strip() for line in text.splitlines() if '***' in line)
    return CheckMeshResult(metrics['cells'], metrics['max_non_ortho'], metrics['max_skewness'], metrics['failed_checks'],
                           metrics['severe_non_ortho_faces'], metrics['regions'], metrics['min_volume'], metrics['illegal_faces'],
                           metrics['boundary_openness'], not missing and ok and metrics['failed_checks'] == 0 and not categories,
                           not missing, tuple(missing), categories)
