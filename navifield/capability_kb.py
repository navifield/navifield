"""Offline, version-scoped capability database and directed dependency queries."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

DATA = Path(__file__).with_name('capability_data')
DATABASE = DATA / 'capabilities.sqlite3'
TOKEN = re.compile(r'^[A-Za-z0-9_\u3400-\u9fff.:-]{1,100}$')
EVIDENCE = ('documentation', 'integration', 'workstation', 'reference')
FINDING_SUMMARY_LIMIT = 240


class InvalidFindingSummary(ValueError):
    """A research agent can correct this field and submit the same gap again."""


FEATURE_STATUSES = ('covered', 'partial', 'unmapped', 'unsupported')
GAP_STATUSES = ('unsupported_in_version', 'insufficient_evidence', 'not_applicable')
COUPLING_FIELDS = ('from_field', 'to_field', 'quantity', 'unit', 'transfer', 'implementation', 'custom_work')


def _check_couplings(record):
    links = record.get('couplings', [])
    if not isinstance(links, list):
        raise ValueError(f'Invalid couplings in {record["id"]}')
    for link in links:
        if not isinstance(link, dict) or not all(isinstance(link.get(key), str) and link[key].strip() for key in COUPLING_FIELDS):
            raise ValueError(f'Incomplete coupling in {record["id"]}')
        if link['implementation'] not in {'native', 'configured', 'adapter', 'custom'}:
            raise ValueError(f'Invalid coupling implementation in {record["id"]}')


def _gap_assessments(entry):
    if 'gap_assessments' not in entry:
        return None
    assessments = entry['gap_assessments']
    if not isinstance(assessments, list):
        raise ValueError(f"Invalid gap assessments for {entry['module']}")
    for finding in assessments:
        if (not isinstance(finding, dict) or finding.get('status') not in GAP_STATUSES
                or not all(isinstance(finding.get(k), str) and finding[k].strip()
                           for k in ('gap', 'source', 'reason'))):
            raise ValueError(f"Invalid gap assessment for {entry['module']}")
        source = urlsplit(finding['source'])
        if source.scheme not in ('http', 'https') or not source.netloc:
            raise ValueError(f"Invalid gap evidence URL for {entry['module']}")
    names = [f['gap'] for f in assessments]
    if len(set(names)) != len(names) or sorted(names) != sorted(entry['gaps']):
        raise ValueError(f"Gap assessments must cover each remaining gap once for {entry['module']}")
    return json.dumps(assessments, ensure_ascii=False)


def _manual_rows(data):
    modules = data.get('manual_modules', [])
    ids = {record['id'] for record in data['capabilities']}
    mapped = set()
    for entry in modules:
        if not all(isinstance(entry.get(k), str) and entry[k].strip() for k in ('module', 'source')):
            raise ValueError(f'Invalid manual module in {data["solver"]}')
        for k in ('records', 'gaps'):
            if not isinstance(entry.get(k), list) or not all(isinstance(v, str) and v.strip() for v in entry[k]):
                raise ValueError(f'Invalid {k} for {entry["module"]}')
        if not set(entry['records']) <= ids or not (entry['records'] or entry['gaps']):
            raise ValueError(f'Unknown record or empty audit for {entry["module"]}')
        mapped.update(entry['records'])
        yield (data['solver'], data['version'], entry['module'], entry['source'],
               json.dumps(entry['records'], ensure_ascii=False), json.dumps(entry['gaps'], ensure_ascii=False),
               _gap_assessments(entry))
    if 'manual_modules' in data and ids != mapped:
        raise ValueError(f'Unmapped records in {data["solver"]}: {sorted(ids - mapped)}')


def _load_feature_inventory(db, source, records):
    """Preserve the scope of each upstream enumeration and its reviewed crosswalk."""
    db.executescript("""
        CREATE TABLE feature_indexes (
            solver TEXT, version TEXT, id TEXT, source TEXT NOT NULL,
            scope TEXT NOT NULL, limitations TEXT NOT NULL, PRIMARY KEY(solver,version,id));
        CREATE TABLE features (
            solver TEXT, version TEXT, id TEXT, index_id TEXT NOT NULL, name TEXT NOT NULL,
            section TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, details TEXT NOT NULL,
            PRIMARY KEY(solver,version,id),
            FOREIGN KEY(solver,version,index_id) REFERENCES feature_indexes(solver,version,id));
        CREATE INDEX feature_status ON features(solver,version,status);
        CREATE INDEX feature_by_reference ON features(solver,version,index_id,status);
        CREATE TABLE feature_records (
            solver TEXT, version TEXT, feature_id TEXT, capability_id TEXT REFERENCES capabilities(id),
            PRIMARY KEY(solver,version,feature_id,capability_id),
            FOREIGN KEY(solver,version,feature_id) REFERENCES features(solver,version,id));
        CREATE INDEX feature_reverse ON feature_records(capability_id);
        CREATE TABLE unmatched_feature_records (
            capability_id TEXT PRIMARY KEY REFERENCES capabilities(id), reason TEXT NOT NULL);
        CREATE VIEW capability_feature_coverage AS
            SELECT c.solver,c.version,c.id AS capability_id,
                   (SELECT count(*) FROM feature_records r WHERE r.capability_id=c.id) AS linked_features,
                   u.reason FROM capabilities c LEFT JOIN unmatched_feature_records u ON u.capability_id=c.id;
        CREATE VIEW feature_index_coverage AS
            SELECT f.solver,f.version,f.index_id,i.source,i.scope,i.limitations,
                   count(*) AS total,sum(status='covered') AS covered,sum(status='partial') AS partial,
                   sum(status='unmapped') AS unmapped,sum(status='unsupported') AS unsupported
              FROM features f JOIN feature_indexes i
                ON (f.solver,f.version,f.index_id)=(i.solver,i.version,i.id)
             GROUP BY f.solver,f.version,f.index_id;
        CREATE VIEW feature_coverage AS
            SELECT solver,version,count(*) AS total,sum(status='covered') AS covered,
                   sum(status='partial') AS partial,sum(status='unmapped') AS unmapped,
                   sum(status='unsupported') AS unsupported FROM features GROUP BY solver,version;
    """)
    owners = {r['id']: (r['solver'], r['version']) for r in records}
    versions = set(owners.values())
    for file in sorted((Path(source) / 'features').glob('*.json')):
        data = json.loads(file.read_text(encoding='utf-8-sig'))
        owner = (data['solver'], data['version'])
        if owner not in versions or not data['indexes'] or not data['features']:
            raise ValueError(f'Unknown software version or empty inventory: {file.name}')
        for idx in data['indexes']:
            if not all(isinstance(idx.get(k), str) and idx[k].strip() for k in ('id','source','scope','limitations')):
                raise ValueError(f'Incomplete feature index: {file.name}')
            db.execute('INSERT INTO feature_indexes VALUES (?,?,?,?,?,?)',
                       (*owner, idx['id'], idx['source'], idx['scope'], idx['limitations']))
        mapped = set()
        for f in data['features']:
            fields = ('id','index','name','section','source','status','reason')
            if not all(isinstance(f.get(k), str) and f[k].strip() for k in fields):
                raise ValueError(f'Incomplete feature: {file.name} {f.get("id")}')
            links = f['records']
            if (urlsplit(f['source']).scheme not in ('http','https') or f['status'] not in FEATURE_STATUSES
                    or not isinstance(links, list) or len(set(links)) != len(links)
                    or any(owners.get(k) != owner for k in links)
                    or bool(links) != (f['status'] in ('covered','partial'))):
                raise ValueError(f'Invalid feature evidence or mapping: {f["id"]}')
            details = f.get('details', {})
            if not isinstance(details, dict):
                raise ValueError(f'Invalid feature details: {f["id"]}')
            db.execute('INSERT INTO features VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (*owner, *(f[k] for k in fields), json.dumps(details, ensure_ascii=False)))
            mapped.update(links)
            db.executemany('INSERT INTO feature_records VALUES (?,?,?,?)',
                           [(*owner, f['id'], cid) for cid in links])
        for r in data.get('unmatched_records', []):
            if (owners.get(r['id']) != owner or r['id'] in mapped
                    or not isinstance(r.get('reason'), str) or not r['reason'].strip()):
                raise ValueError(f'Invalid unmatched feature record: {r["id"]}')
            db.execute('INSERT INTO unmatched_feature_records VALUES (?,?)', (r['id'], r['reason']))


def build(source=DATA, destination=DATABASE):
    """Build from reviewed JSON; fail on duplicate IDs or malformed records."""
    records = []
    modules = []
    baselines = []
    for file in sorted(Path(source).glob('*.json')):
        data = json.loads(file.read_text(encoding='utf-8-sig'))
        if data.get('schema_version') != 1 or not isinstance(data.get('capabilities'), list):
            raise ValueError(f'Invalid capability source: {file.name}')
        for item in data['capabilities']:
            item = dict(item, solver=data['solver'], version=data['version'])
            if not all(isinstance(item.get(k), str) and item[k] for k in ('id', 'solver', 'version', 'scope')):
                raise ValueError(f'Missing identity or scope in {file.name}')
            for direction in ('inputs', 'outputs'):
                if not isinstance(item.get(direction), list) or any(not isinstance(q, dict) or not TOKEN.fullmatch(q.get('id', '')) for q in item[direction]):
                    raise ValueError(f'Invalid {direction} in {item["id"]}')
            if not isinstance(item.get('conditions'), list) or any(not isinstance(c, str) or not TOKEN.fullmatch(c) for c in item['conditions']):
                raise ValueError(f'Invalid conditions in {item["id"]}')
            if not isinstance(item.get('evidence'), dict) or any(not isinstance(item['evidence'].get(k), list) for k in EVIDENCE):
                raise ValueError(f'Invalid evidence in {item["id"]}')
            _check_couplings(item)
            records.append(item)
        modules.extend(_manual_rows(data))
        if 'manual_baseline' in data:
            baseline = data['manual_baseline']
            ids = baseline['records']
            if (not re.fullmatch('[0-9a-f]{40}', baseline['revision'])
                    or not isinstance(ids, list) or len(set(ids)) != len(ids)
                    or not set(ids) <= {r['id'] for r in data['capabilities']}):
                raise ValueError(f'Invalid manual audit baseline in {file.name}')
            baselines.append((data['solver'], data['version'], baseline['revision'], json.dumps(ids)))
    if not records:
        raise ValueError('No capability sources found')
    dest = Path(destination)
    if dest.exists():
        raise ValueError('Destination already exists; build to a new path')
    try:
        db = sqlite3.connect(str(dest))
        db.execute('PRAGMA foreign_keys=ON')
        with db:
            db.executescript('''CREATE TABLE capabilities (id TEXT PRIMARY KEY, solver TEXT NOT NULL, version TEXT NOT NULL, scope TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE quantities (capability_id TEXT NOT NULL REFERENCES capabilities(id), direction TEXT NOT NULL, quantity TEXT NOT NULL, alias TEXT NOT NULL);
                CREATE INDEX quantity_lookup ON quantities(direction, alias);
                CREATE INDEX quantity_aliases ON quantities(quantity, alias);
                CREATE INDEX cap_filter ON capabilities(solver, version, scope);''')
            db.execute("""CREATE TABLE manual_modules (
                solver TEXT NOT NULL, version TEXT NOT NULL, module TEXT NOT NULL,
                source TEXT NOT NULL, records TEXT NOT NULL, gaps TEXT NOT NULL, gap_assessments TEXT,
                PRIMARY KEY (solver, version, module))""")
            db.execute("""CREATE TABLE manual_baselines (
                solver TEXT NOT NULL, version TEXT NOT NULL, revision TEXT NOT NULL, records TEXT NOT NULL,
                PRIMARY KEY (solver, version))""")
            db.executemany('INSERT INTO manual_baselines VALUES (?,?,?,?)', baselines)
            db.executemany('INSERT INTO manual_modules VALUES (?,?,?,?,?,?,?)', modules)
            db.execute("""CREATE VIEW assessed_gaps AS
                SELECT m.solver, m.version, m.module, json_extract(j.value, '$.gap') AS gap,
                       json_extract(j.value, '$.status') AS status, json_extract(j.value, '$.reason') AS reason,
                       json_extract(j.value, '$.source') AS source
                FROM manual_modules AS m, json_each(m.gap_assessments) AS j""")
            db.execute("""CREATE VIEW field_couplings AS
                SELECT c.solver, c.version, c.id AS capability_id, j.key AS direction_index,
                       json_extract(j.value, '$.from_field') AS from_field,
                       json_extract(j.value, '$.to_field') AS to_field,
                       json_extract(j.value, '$.quantity') AS quantity,
                       json_extract(j.value, '$.unit') AS unit,
                       json_extract(j.value, '$.transfer') AS transfer,
                       json_extract(j.value, '$.implementation') AS implementation,
                       json_extract(j.value, '$.custom_work') AS custom_work
                FROM capabilities AS c, json_each(c.body, '$.couplings') AS j""")
            for item in records:
                db.execute('INSERT INTO capabilities VALUES (?,?,?,?,?)', (item['id'], item['solver'], item['version'], item['scope'], json.dumps(item, ensure_ascii=False, sort_keys=True)))
                for direction in ('inputs', 'outputs'):
                    for q in item[direction]:
                        for alias in sorted(set([q['id']] + q.get('aliases', []))):
                            if not isinstance(alias, str) or not alias:
                                raise ValueError('Invalid alias')
                            db.execute('INSERT INTO quantities VALUES (?,?,?,?)', (item['id'], direction, q['id'], alias.casefold()))
            from .turbulence import load_profiles
            _load_feature_inventory(db, source, records)
            load_profiles(db, source)
            from .model_selection import load_models
            load_models(db, source)
            from .device_chains import load_chains
            load_chains(db, source)
            from .target_selection import load_targets
            load_targets(db, source)
            from .paper_cases import load_papers
            load_papers(db, source)
        db.close()
    except Exception:
        if 'db' in locals(): db.close()
        dest.unlink(missing_ok=True)
        raise


def connect(path=DATABASE):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)


def inventory(solver=None, database=DATABASE):
    """List indexed software versions, or the records for one software family."""
    with closing(connect(database)) as db:
        db.row_factory = sqlite3.Row
        if solver is None:
            rows = db.execute(
                "SELECT solver, version, COUNT(*) AS capabilities FROM capabilities "
                "GROUP BY solver, version ORDER BY solver, version"
            )
        else:
            rows = db.execute(
                "SELECT id, version, scope FROM capabilities WHERE solver=? ORDER BY version, id",
                (solver,),
            )
        return [dict(row) for row in rows]


def feature_summary(solver=None, database=DATABASE, *, by_index=False):
    with closing(connect(database)) as db:
        db.row_factory = sqlite3.Row
        query = ("SELECT * FROM feature_index_coverage WHERE (? IS NULL OR solver=?) ORDER BY solver,version,index_id"
                 if by_index else "SELECT * FROM feature_coverage WHERE (? IS NULL OR solver=?) ORDER BY solver,version")
        rows = db.execute(query, (solver, solver))
        return [dict(r, remaining=r['total']-r['covered']) for r in rows]


def find_features(solver=None, database=DATABASE, *, term='', status=None, capability=None, index=None, limit=20, offset=0):
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError('limit must be 1..200 and offset must be non-negative')
    with closing(connect(database)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""
            SELECT f.*,
              (SELECT json_group_array(capability_id) FROM feature_records r
               WHERE (r.solver,r.version,r.feature_id)=(f.solver,f.version,f.id)) AS records
            FROM features f
            WHERE (? IS NULL OR f.solver=?) AND (? IS NULL OR f.status=?) AND (? IS NULL OR f.index_id=?)
              AND (?='' OR instr(lower(f.name),lower(?)) OR instr(lower(f.section),lower(?)) OR f.id=?)
              AND (? IS NULL OR EXISTS(SELECT 1 FROM feature_records r
                  WHERE (r.solver,r.version,r.feature_id)=(f.solver,f.version,f.id) AND r.capability_id=?))
            ORDER BY f.solver,f.version,f.id LIMIT ? OFFSET ?""",
            (solver,solver,status,status,index,index,term,term,term,term,capability,capability,limit,offset))
        return [dict(r, records=json.loads(r['records']), details=json.loads(r['details'])) for r in rows]


def manual_inventory(solver=None, database=DATABASE):
    with closing(connect(database)) as db:
        db.row_factory = sqlite3.Row
        sections = db.execute(
            "SELECT * FROM manual_modules WHERE (? IS NULL OR solver=?) ORDER BY solver, version, module",
            (solver, solver),
        )
        baselines = {(r['solver'], r['version']): (r['revision'], set(json.loads(r['records'])))
                     for r in db.execute("SELECT * FROM manual_baselines WHERE (? IS NULL OR solver=?)", (solver, solver))}
        results = []
        for row in sections:
            section = dict(row, records=json.loads(row['records']), gaps=json.loads(row['gaps']))
            assessments = section.pop('gap_assessments', None)
            if assessments is not None:
                section['gap_assessments'] = json.loads(assessments)
            baseline = baselines.get((row['solver'], row['version']))
            if baseline is not None:
                section['baseline_revision'] = baseline[0]
                section['existing'] = [i for i in section['records'] if i in baseline[1]]
                section['added'] = [i for i in section['records'] if i not in baseline[1]]
            results.append(section)
        return results


def assessed_gaps(solver=None, database=DATABASE, *, status=None):
    """Read version-bound decisions for reviewed gaps, with their supporting evidence."""
    with closing(connect(database)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT * FROM assessed_gaps WHERE (? IS NULL OR solver=?) AND (? IS NULL OR status=?)
               ORDER BY solver, version, module, gap""", (solver, solver, status, status),
        )
        return [dict(row) for row in rows]


def field_couplings(solver=None, database=DATABASE, *, field=None, implementation=None):
    with closing(connect(database)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT * FROM field_couplings AS e
               WHERE (? IS NULL OR solver=?) AND (? IS NULL OR implementation=?)
                 AND (? IS NULL OR instr(lower(from_field), lower(?)) > 0
                                OR instr(lower(to_field), lower(?)) > 0
                                OR instr(lower(quantity), lower(?)) > 0
                                OR EXISTS (SELECT 1 FROM quantities AS q WHERE q.quantity = e.quantity
                                           AND instr(q.alias, lower(?)) > 0))
               ORDER BY solver, capability_id, direction_index""",
            (solver, solver, implementation, implementation, field, field, field, field, field),
        )
        return [dict(row) for row in rows]


def show(identifier, database=DATABASE):
    with closing(connect(database)) as db:
        row = db.execute('SELECT body FROM capabilities WHERE id=?', (identifier,)).fetchone()
    if row is None: raise ValueError('Unknown capability ID')
    return json.loads(row[0])


def query(output, conditions=(), solver=None, version=None, database=DATABASE):
    with closing(connect(database)) as db:
        rows = db.execute('SELECT DISTINCT c.body FROM capabilities c JOIN quantities q ON q.capability_id=c.id WHERE q.direction=? AND q.alias=? AND (? IS NULL OR c.solver=?) AND (? IS NULL OR c.version=?) ORDER BY c.id', ('outputs', output.casefold(), solver, solver, version, version)).fetchall()
    values = [json.loads(row[0]) for row in rows]
    # Conditions are requirements of a capability, not a reason to claim suitability.
    return [v for v in values if not conditions or set(conditions).issubset(set(v['conditions']))]


def _token(value):
    return isinstance(value, str) and TOKEN.fullmatch(value) is not None


def bind_findings(gap_list, findings):
    gaps = {g['id']: g for g in gap_list}
    annotations = []
    if findings is not None:
        if not isinstance(findings, dict) or findings.get('schema_version') != 1 or not isinstance(findings.get('findings'), list):
            raise ValueError('Invalid findings document')
        for f in findings['findings']:
            if not isinstance(f, dict) or f.get('gap_id') not in gaps: raise ValueError('Finding does not bind to a current gap')
            g = gaps[f['gap_id']]
            if g['action'] != 'research': raise ValueError('Finding must bind to a research gap')
            summary = f.get('summary', '')
            if not isinstance(summary, str) or len(summary) > FINDING_SUMMARY_LIMIT or any(ord(c) < 32 for c in summary):
                raise InvalidFindingSummary(f'Finding summary must be a single line of at most {FINDING_SUMMARY_LIMIT} characters')
            if f.get('scope') != g.get('scope') or f.get('version') != g.get('version'):
                raise ValueError('Finding scope/version does not match gap')
            if f.get('conclusion') not in ('documented_candidate', 'custom_required', 'unverified'):
                raise ValueError('Invalid research conclusion')
            if not isinstance(f.get('evidence'), list) or not f['evidence'] or any(not isinstance(e, dict) or not isinstance(e.get('source'), str) or not e['source'].startswith('https://') or not isinstance(e.get('location'), str) or not e['location'].strip() or e.get('source_kind') not in ('official_documentation', 'paper', 'other') or e.get('scope') != f.get('scope') or e.get('version') != f.get('version') or e.get('gap_id') != f['gap_id'] for e in f['evidence']):
                raise ValueError('Invalid finding evidence')
            if any(not urlsplit(e['source']).hostname for e in f['evidence']): raise ValueError('Finding URL has no host')
            if f['conclusion'] == 'documented_candidate' and not any(e['source_kind'] in ('official_documentation', 'paper') for e in f['evidence']):
                raise ValueError('Documented candidate needs an identified primary source')
            entry = {'gap_id': f['gap_id'], 'conclusion': f['conclusion'], 'evidence': f['evidence'], 'classification': 'external_research_only', 'source_verification': 'supplied_unverified', 'summary': summary}
            if cache_eligible(f):
                entry['external_review'] = {key: f['verification'][key] for key in ('checked_at', 'expires_at')}
            if entry not in annotations: annotations.append(entry)
    return annotations


def analyze(req, database=DATABASE, findings=None):
    from . import chain_intake, paper_cases, strip_control
    goals = req.get('goals', [])
    known = req.get('known_quantities', [])
    if not isinstance(goals, list) or not isinstance(known, list) or any(not _token(x) for x in goals + known):
        raise ValueError('goals and known_quantities must contain controlled identifier tokens')
    conditions = []
    for field in ('operating_conditions', 'boundaries', 'materials'):
        values = req.get(field, [])
        if not isinstance(values, list) or any(not _token(x) for x in values):
            raise ValueError(f'{field} must contain controlled identifier tokens')
        conditions.extend(values)
    for field in ('solver', 'version'):
        if req.get(field) is not None and not _token(req[field]):
            raise ValueError(f'{field} must be a controlled identifier')
    context = {'conditions': sorted(set(conditions)), 'goals': sorted(set(goals)), 'known_quantities': sorted(set(known)),
               'solver': req.get('solver'), 'version': req.get('version')}
    request_text = "\n".join(str(req.get(key, "")) for key in ("original_request", "description"))
    research_disabled = (req.get("offline") is True or req.get("allow_public_research") is False or bool(re.search(
        r"(?:不要|不许|不得|禁止|无需|不必|别|不)\s*(?:联网|上网|外查|外发|出网)|"
        r"(?:只|仅)(?:能|允许|用|使用|查)?(?:本地|离线)|纯离线|"
        r"\boffline\s+only\b|\b(?:no|do\s+not|don't)\s+(?:network[ -]access|internet|browse|"
        r"search\s+(?:the\s+)?web|go\s+online|use\s+(?:the\s+)?internet)\b",
        request_text, re.I,
    )))
    measured = chain_intake.measured_pressure_difference(req)
    if measured is not None:
        return {'status': 'needs_review', 'coverage': 'partial' if measured.get('missing') else 'full_coverage', 'capabilities': [],
                'links': [], 'gaps': [], 'research_findings': bind_findings([], findings),
                'engineering_plans': [], 'pressure_readback': measured}
    strip = strip_control.review(req)
    with closing(connect(database)) as db:
        plans = chain_intake.discover(db, req, req.get('solver'), req.get('version'), strip_review=strip)
        paper_reproductions = paper_cases.route_reproductions(db, plans)
        paper_text = '\n'.join(str(req.get(key, '')) for key in ('original_request', 'description'))
        paper_reproductions.extend(paper_cases.explicit_reproduction_records(
            db, paper_cases.explicit_dois(paper_text), paper_reproductions
        ))
    if plans:
        context['engineering_chains'] = sorted({p['chain'] for p in plans})
    planned_goals = {key for plan in plans for key in (*plan['goals'], *plan['goal_aliases'])}
    visited, active, steps, gaps = set(), set(), {}, {}
    def gap(kind, quantity, question=None, **extra):
        extra.setdefault('scope', 'context:' + hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16])
        extra.setdefault('version', req.get('version') or 'unspecified')
        identity = dict(kind=kind, quantity=quantity, context=context, **extra)
        ident = 'gap-' + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
        descriptions = {'unknown_quantity': 'Identify documented methods and inputs for this output quantity.',
                        'unknown_interface': 'Identify a documented transfer from source to target.',
                        'condition_unconfirmed': 'Obtain the missing applicability condition from the user.',
                        'cycle': 'Resolve a feedback dependency or provide an initial/known quantity.',
                        'integration_unverified': 'Check implementation and validate this capability for the stated scope.',
                        'conflicting_conditions': 'Resolve mutually exclusive conditions for the same scope.',
                        'required_input': 'Supply or identify the required physical input quantity.'}
        gaps[ident] = dict(id=ident, **identity, question=question or descriptions[kind],
                           action=('ask_user' if kind in ('condition_unconfirmed', 'required_input', 'conflicting_conditions') else
                                   'verify_local' if kind == 'integration_unverified' else 'research'))
    exclusive = (('steady', 'transient'), ('constant_electrical_resistivity', 'temperature_dependent_resistivity'),
                 ('compressible_fluid', 'incompressible_fluid'), ('choked', 'unchoked'))
    for group in exclusive:
        if len(set(conditions).intersection(group)) > 1:
            gap('conflicting_conditions', '+'.join(sorted(set(conditions).intersection(group))))
    def compatible(cap):
        declared = set(conditions)
        required = set(cap['conditions'])
        return not any(declared.intersection(group) and required.intersection(group) and not declared.intersection(required).intersection(group) for group in exclusive)
    def candidates_for(quantity, source=None):
        alternatives = {p['quantity_aliases'][quantity] for p in plans if quantity in p['quantity_aliases']}
        candidates = [c for name in sorted(alternatives or {quantity})
                      for c in query(name, solver=req.get('solver'), version=req.get('version'), database=database) if compatible(c)]
        if source is not None:
            candidates = [c for c in candidates if source in [q['id'] for q in c['inputs']]]
        if candidates:
            least = min(len(set(c['conditions']) - set(conditions)) for c in candidates)
            candidates = [c for c in candidates if len(set(c['conditions']) - set(conditions)) == least]
        return candidates
    def check_cap(cap, quantity):
        if not cap['evidence']['integration']:
            gap('integration_unverified', quantity, capability_id=cap['id'], scope=cap['scope'], version=cap['version'])
        for condition in sorted(set(cap['conditions']) - set(conditions)):
            gap('condition_unconfirmed', condition, capability_id=cap['id'], scope=cap['scope'], version=cap['version'])
    def walk(quantity, goal=False):
        if quantity in known or quantity in visited: return
        if quantity in active:
            gap('cycle', quantity)
            return
        active.add(quantity)
        candidates = candidates_for(quantity)
        if not candidates and not (goal and quantity in planned_goals):
            gap('unknown_quantity' if goal else 'required_input', quantity)
        for cap in candidates:
            check_cap(cap, quantity)
            for q in cap['inputs']:
                walk(q['id'])
            steps[cap['id']] = cap
        active.remove(quantity)
        visited.add(quantity)
    for goal in goals: walk(goal, goal=True)
    transfers = req.get('transfers', [])
    if not isinstance(transfers, list): raise ValueError('transfers must be an array')
    explicit = [t for t in transfers if isinstance(t, dict) and _token(t.get('source')) and _token(t.get('target'))]
    def consumers(quantity, seen=None):
        seen = set() if seen is None else seen
        if quantity in seen: return []
        seen.add(quantity)
        found = {c['id']: {'id': c['id'], 'solver': c['solver'], 'version': c['version']}
                 for c in steps.values() if quantity in [q['id'] for q in c['inputs']]}
        for edge in explicit:
            if edge['source'] == quantity:
                for candidate in consumers(edge['target'], seen): found[candidate['id']] = candidate
        return list(found.values())
    for transfer in transfers:
        if isinstance(transfer, str):
            if not _token(transfer): raise ValueError('transfers must contain controlled identifiers')
            with closing(connect(database)) as db:
                all_caps = [json.loads(row[0]) for row in db.execute('SELECT body FROM capabilities ORDER BY id')]
            matched = False
            for cap in all_caps:
                if (transfer in cap.get('transfers', []) and compatible(cap)
                        and (req.get('solver') is None or req['solver'] == cap['solver'])
                        and (req.get('version') is None or req['version'] == cap['version'])):
                    matched = True
                    steps[cap['id']] = cap
                    check_cap(cap, transfer)
                    for q in cap['inputs']: walk(q['id'])
            if not matched: gap('unknown_interface', transfer)
            continue
        if not isinstance(transfer, dict) or not _token(transfer.get('source')) or not _token(transfer.get('target')):
            raise ValueError('transfer endpoints must be controlled identifier tokens')
        source, target = transfer['source'], transfer['target']
        for ident, item in list(gaps.items()):
            if item['kind'] == 'required_input' and item['quantity'] == target:
                del gaps[ident]
        candidates = candidates_for(target, source=source)
        if not candidates:
            downstream = consumers(target)
            gap('unknown_interface', target, source=source, target=target,
                candidates=downstream)
        else:
            for c in candidates:
                check_cap(c, target)
                for q in c['inputs']: walk(q['id'])
                steps[c['id']] = c
    for plan in plans:
        for item, prompt in plan['research_needs'].items():
            gap('unverified_closure', item, question=prompt, scope=plan['chain'], solver=plan['solver'], version=plan['version'])
        for item in plan['required_inputs']:
            gap('required_input', item['key'], question=item['meaning'], scope=plan['chain'], version=plan['version'])
        gap('integration_unverified', plan['chain'], scope=plan['chain'], version=plan['version'])
    if not plans and not goals and not transfers and not paper_reproductions and strip is None and req.get('original_request'):
        gap('condition_unconfirmed', 'unspecified_engineering_output',
            question='Specify the physical mechanism and the target observable before researching candidates.')
    annotations = bind_findings(list(gaps.values()), findings)
    if not steps and not plans:
        coverage = 'partial' if paper_reproductions else 'no_match'
    else:
        coverage = 'partial' if gaps else 'full_coverage'
    result = {'status': 'needs_review', 'coverage': coverage, 'capabilities': list(steps.values()),
            'links': [{'id': c['id'], 'inputs': [q['id'] for q in c['inputs']], 'outputs': [q['id'] for q in c['outputs']]} for c in steps.values()],
            'gaps': list(gaps.values()), 'research_findings': annotations, 'engineering_plans': plans}
    for plan in plans:
        if any('ports' in step for step in plan['steps']):
            result['links'].extend(
                {'id': f"{plan['chain']}.{step['id']}", **step['ports']}
                for step in plan['steps'] if 'ports' in step
            )
    if strip is not None:
        result['coverage'] = 'partial'
        result['strip_control'] = strip
    if paper_reproductions:
        result['paper_reproductions'] = paper_reproductions
    if research_disabled:
        result["research_dispatch_disabled"] = True
    return result


def research_packet(analysis, answered=()):
    """Share controlled context once; retain each gap's binding identity."""
    if analysis.get("research_dispatch_disabled"):
        return {"schema_version": 1, "context": None, "gaps": []}
    from .public_research import vocabulary
    gaps = [g for g in analysis['gaps'] if g['action'] == 'research' and g['id'] not in answered]
    if not gaps:
        return {'schema_version': 1, 'context': None, 'gaps': []}
    words, pairs = vocabulary()
    versions = {version for _, version in pairs}
    solvers = {solver for solver, _ in pairs}
    plans = analysis.get('engineering_plans', [])
    chain_ids = {plan['chain'] for plan in plans}
    questions = {quantity for plan in plans for quantity in plan['research_needs']}
    shared = gaps[0]['context']
    context = {key: [word for word in shared[key] if word in words]
               for key in ('goals', 'conditions', 'known_quantities')}
    context.update(solver=shared['solver'] if shared['solver'] in solvers else None,
                   version=shared['version'] if shared['version'] in versions else None)
    if 'engineering_chains' in shared:
        context['engineering_chains'] = [x for x in shared['engineering_chains'] if x in chain_ids]
    clean = []
    for gap in gaps:
        if gap['version'] not in versions and gap['version'] != 'unspecified':
            continue
        record = {k: v for k, v in gap.items() if k != 'context'}
        for key in ('quantity', 'source', 'target'):
            if key in record and record[key] not in words and record[key] not in questions:
                record[key] = 'unregistered_quantity'
        clean.append(record)
    return {'schema_version': 1, 'context': context if clean else None, 'gaps': clean}


def brief(analysis):
    """Compact report with one source table; references use zero-based indices."""
    from . import chain_intake
    sources = []
    findings = []
    for f in analysis['research_findings']:
        indices = []
        for evidence in f['evidence']:
            source = evidence['source']
            if source not in sources: sources.append(source)
            index = sources.index(source)
            if index not in indices: indices.append(index)
        findings.append({'gap_id': f['gap_id'], 'conclusion': f['conclusion'],
                         'summary': f['summary'], 'source_verification': f['source_verification'],
                         'source_indices': indices})
        if 'external_review' in f:
            findings[-1]['external_review'] = f['external_review']
            findings[-1]['source_locations'] = [
                {'source_index': sources.index(e['source']), 'location': e['location'][:160]}
                for e in f['evidence'][:3]
            ]
    for plan in analysis.get('engineering_plans', []):
        if any('ports' in step for step in plan['steps']):
            for source in plan['sources']:
                if source['url'] not in sources:
                    sources.append(source['url'])
    result = {'status': analysis['status'], 'coverage': analysis['coverage'], 'links': analysis['links'],
            'user_inputs': sorted({g['quantity'] for g in analysis['gaps'] if g['action'] == 'ask_user'}),
            'gaps': [{k: g[k] for k in ('id', 'kind', 'quantity', 'action')} for g in analysis['gaps'] if g['action'] != 'ask_user'],
            'research_findings': findings, 'sources': sources,
            'engineering_plans': chain_intake.compact(analysis.get('engineering_plans', []))}
    if 'pressure_readback' in analysis:
        result['pressure_readback'] = analysis['pressure_readback']
        result['user_inputs'].extend(analysis['pressure_readback'].get('missing', ()))
    if 'strip_control' in analysis:
        result['strip_control'] = analysis['strip_control']
    if analysis.get('paper_reproductions'):
        result['paper_reproductions'] = [
            {**case, 'results': case['results'][:1], 'gaps': case['gaps'][:1],
             'details_command': (f"navifield capabilities paper-cases --doi {case['doi']}"
                                 if case['doi'] else f"navifield capabilities paper-cases --id {case['case_id']}")}
            for case in analysis['paper_reproductions']
        ]
    return result


def cache_eligible(finding):
    """Require a time-bounded external verification attestation; no network access."""
    from datetime import datetime, timezone
    try:
        v = finding['verification']
        if v['status'] != 'verified' or not isinstance(v['method'], str) or not v['method'].strip(): return False
        if finding['conclusion'] not in ('documented_candidate', 'custom_required'): return False
        checked = datetime.fromisoformat(v['checked_at'].replace('Z', '+00:00'))
        expires = datetime.fromisoformat(v['expires_at'].replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        return checked.tzinfo is not None and expires.tzinfo is not None and checked <= now < expires and checked < expires
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def load_cache(directory, req, analysis, database=DATABASE):
    """Return only currently bound, valid entries; damaged or stale files are misses."""
    found = []
    root = Path(directory)
    for gap in analysis['gaps']:
        if gap['action'] != 'research': continue
        try:
            f = json.loads((root / (gap['id'] + '.json')).read_text(encoding='utf-8'))
            if not cache_eligible(f): continue
            bind_findings(analysis['gaps'], {'schema_version': 1, 'findings': [f]})
            if f['gap_id'] == gap['id']: found.append(f)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return found


def store_cache(directory, findings):
    """Store already binding-validated and explicitly attested entries atomically."""
    import os
    import tempfile
    root = Path(directory)
    eligible = []
    for f in {f['gap_id']: f for f in findings}.values():
        if cache_eligible(f):
            eligible.append(f)
        else:
            # An explicit negative or expired result supersedes any old attestation.
            (root / (f['gap_id'] + '.json')).unlink(missing_ok=True)
    if eligible: root.mkdir(parents=True, exist_ok=True)
    stored = []
    for f in eligible:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=root, delete=False) as out:
            temporary = Path(out.name)
            try:
                json.dump(f, out, ensure_ascii=False)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, root / (f['gap_id'] + '.json'))
        finally:
            temporary.unlink(missing_ok=True)
        stored.append(f['gap_id'])
    return stored
