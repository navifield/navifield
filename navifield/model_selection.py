"""Search versioned engineering models by mechanisms, predicted quantities and assumptions."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

IDENTIFIER = re.compile(r'[A-Za-z0-9_.-]+\Z')
KINDS = {'version_manual', 'version_source', 'family_reference', 'upstream_case'}
PHASE_PHENOMENA = ('melting_solidification', 'boiling', 'condensation')
SUPPORT_STATUSES = ('native', 'limited_scope', 'framework_custom', 'coupling_only', 'not_applicable',
                    'insufficient_evidence', 'unsupported_in_version')
REQUEST_FIELDS = {'description', 'domain', 'mechanisms', 'goals', 'conditions', 'known_inputs'}


def _identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f'Invalid identifier: {value!r}')
    return value


def _tokens(values, *, empty=False):
    if not isinstance(values, list) or (not values and not empty):
        raise ValueError('Expected a list of identifiers')
    result = [_identifier(s) for s in values]
    if len(set(result)) != len(result):
        raise ValueError('Duplicate identifier')
    return result


def _conditions(value):
    if not isinstance(value, dict):
        raise ValueError('Expected condition mapping')
    for k, vals in value.items():
        _identifier(k)
        _tokens(vals)


def _text_fields(obj, keys):
    if not isinstance(obj, dict) or not all(isinstance(obj.get(k), str) and obj[k].strip() for k in keys):
        raise ValueError(f'Missing text in {keys}')


def _check_profile(p):
    _identifier(p['id'])
    _text_fields(p, ('label', 'basis'))
    _conditions(p['conditions'])
    for field in ('goals', 'mechanisms'):
        _tokens(p[field])
    for field in ('assumptions', 'outputs', 'limitations'):
        if not isinstance(p[field], list) or not p[field] or not all(isinstance(v, str) and v.strip() for v in p[field]):
            raise ValueError(f"Invalid {field} for {p['id']}")
    _tokens(p['capability_ids'], empty=True)
    _tokens([v['key'] for v in p['inputs']])
    for v in p['inputs']:
        _text_fields(v, ('meaning',))
        _tokens(v.get('alternatives', []), empty=True)
        _tokens(v.get('derived_from', []), empty=True)
        when = v.get('when', {})
        _conditions(when)
        for key, values in when.items():
            if key in p['conditions'] and not set(values) <= set(p['conditions'][key]):
                raise ValueError(f"Unreachable input condition in {p['id']}: {v['key']}")
    for link in p['couplings']:
        _text_fields(link, ('from_field','to_field','quantity','transfer','custom_work'))
    for e in p['evidence']:
        _text_fields(e, ('kind','url','locator','claim'))
        url = urlsplit(e['url'])
        if e['kind'] not in KINDS or url.scheme not in ('http','https') or not url.netloc:
            raise ValueError(f"Invalid evidence for {p['id']}")
    if not any(e['kind'] in ('version_manual', 'version_source') for e in p['evidence']):
        raise ValueError(f"Missing version-specific implementation evidence for {p['id']}")


def _load_phase_review(db, data):
    review = data['assessment']
    if sorted(r['phenomenon'] for r in review) != sorted(PHASE_PHENOMENA):
        raise ValueError(f"Expected a review for each phase change in {data['solver']}")
    for r in review:
        _text_fields(r, ('scope', 'custom_work'))
        if r['status'] not in SUPPORT_STATUSES or not r['evidence']:
            raise ValueError(f"Invalid phase review for {data['solver']}")
        for e in r['evidence']:
            _text_fields(e, ('kind','url','locator','claim'))
            url = urlsplit(e['url'])
            if e['kind'] not in KINDS or url.scheme not in ('https','http') or not url.netloc:
                raise ValueError(f"Invalid phase review source for {data['solver']}")
        db.execute('INSERT INTO phase_support VALUES (?,?,?,?,?,?,?)',
                   (data['solver'], data['version'], r['phenomenon'], r['status'],
                    r['scope'], r['custom_work'], json.dumps(r['evidence'], ensure_ascii=False)))


def phase_support(db, solver=None, version=None, phenomenon=None, status=None):
    filters = {k:v for k,v in (('solver',solver), ('version',version), ('phenomenon',phenomenon),
                                ('status',status)) if v is not None}
    sql = 'SELECT solver,version,phenomenon,status,scope,custom_work,evidence FROM phase_support'
    if filters:
        sql += ' WHERE ' + ' AND '.join(f'{k}=?' for k in filters)
    return [dict(solver=s,version=v,phenomenon=p,status=t,scope=c,custom_work=w,evidence=json.loads(e))
            for s,v,p,t,c,w,e in db.execute(sql + ' ORDER BY solver,version,phenomenon', tuple(filters.values()))]


def load_models(db, source):
    db.executescript("""
        CREATE TABLE phase_support (
          solver TEXT, version TEXT, phenomenon TEXT, status TEXT NOT NULL, scope TEXT NOT NULL,
          custom_work TEXT NOT NULL, evidence TEXT NOT NULL, PRIMARY KEY(solver,version,phenomenon));
        CREATE INDEX phase_support_by_phenomenon ON phase_support(phenomenon,status);
        CREATE TABLE model_profiles (
          domain TEXT NOT NULL, solver TEXT NOT NULL, version TEXT NOT NULL, model TEXT NOT NULL,
          body TEXT NOT NULL, PRIMARY KEY(domain,solver,version,model));
        CREATE TABLE model_facets (
          domain TEXT,solver TEXT,version TEXT,model TEXT,kind TEXT,value TEXT,
          PRIMARY KEY(domain,kind,value,solver,version,model),
          FOREIGN KEY(domain,solver,version,model) REFERENCES model_profiles);
        CREATE TABLE model_inputs (
          domain TEXT,solver TEXT,version TEXT,model TEXT,name TEXT,meaning TEXT,required_when TEXT,
          alternatives TEXT, derived_from TEXT,
          PRIMARY KEY(domain,solver,version,model,name),
          FOREIGN KEY(domain,solver,version,model) REFERENCES model_profiles);
        CREATE TABLE model_capabilities (
          domain TEXT,solver TEXT,version TEXT,model TEXT,capability_id TEXT REFERENCES capabilities(id),
          PRIMARY KEY(domain,solver,version,model,capability_id),
          FOREIGN KEY(domain,solver,version,model) REFERENCES model_profiles);
    """)
    for path in sorted((Path(source) / 'models').glob('*.json')):
        data = json.loads(path.read_text('utf-8-sig'))
        owner = (data['solver'], data['version'])
        if not db.execute('SELECT 1 FROM capabilities WHERE solver=? AND version=? LIMIT 1', owner).fetchone():
            raise ValueError(f'Unknown solver/version: {owner}')
        if data['domain'] == 'phase_change':
            _load_phase_review(db, data)
        for p in data['models']:
            _check_profile(p)
            if data['domain'] == 'phase_change':
                for field in ('custom_work','correlations'):
                    if not isinstance(p.get(field), list) or not p[field] or not all(
                            isinstance(t,str) and t.strip() for t in p[field]):
                        raise ValueError(f"Missing {field} in {p['id']}")
                if p.get('implementation') not in ('native','configured','custom'):
                    raise ValueError(f"Undefined implementation for {p['id']}")
            key = (_identifier(data['domain']), *owner, p['id'])
            db.execute('INSERT INTO model_profiles VALUES (?,?,?,?,?)', (*key, json.dumps(p, ensure_ascii=False)))
            for k in ('goals', 'mechanisms'):
                db.executemany('INSERT INTO model_facets VALUES (?,?,?,?,?,?)', [(*key,k,v) for v in p[k]])
            for v in p['inputs']:
                db.execute('INSERT INTO model_inputs VALUES (?,?,?,?,?,?,?,?,?)',
                           (*key,v['key'],v['meaning'],json.dumps(v.get('when',{})),
                            json.dumps(v.get('alternatives',[])),json.dumps(v.get('derived_from',[]))))
            for cap in p['capability_ids']:
                if not db.execute('SELECT 1 FROM capabilities WHERE solver=? AND version=? AND id=?', (*owner,cap)).fetchone():
                    raise ValueError(f'{key}: invalid capability link {cap}')
                db.execute('INSERT INTO model_capabilities VALUES (?,?,?,?,?)', (*key,cap))


def profiles(db, domain=None, solver=None, version=None, model=None):
    criteria = {'domain':domain, 'solver':solver, 'version':version, 'model':model}
    filters = {k:v for k,v in criteria.items() if v is not None}
    sql = 'SELECT domain,solver,version,model,body FROM model_profiles'
    if filters:
        sql += ' WHERE ' + ' AND '.join(f'{k}=?' for k in filters)
    result = []
    for d,s,v,m,body in db.execute(sql + ' ORDER BY domain,solver,version,model', tuple(filters.values())):
        p = json.loads(body)
        row = dict(domain=d, solver=s, version=v, model=m, label=p['label'])
        row.update(p if model else {k:p[k] for k in ('mechanisms','goals')})
        result.append(row)
    return result


def _request(req):
    if not isinstance(req, dict) or set(req) - REQUEST_FIELDS:
        raise ValueError('Unrecognised selection fields')
    _identifier(req['domain'])
    for k in ('goals','mechanisms','known_inputs'):
        _tokens(req.get(k, []), empty=k == 'known_inputs')
    cond = req.get('conditions', {})
    if not isinstance(cond, dict):
        raise ValueError('conditions must be an object with one value per key')
    for k,v in cond.items():
        _identifier(k)
        _identifier(v)


def _matches(conditions, requirement):
    return not any(k in requirement and requirement[k] not in values for k, values in conditions.items())


def _assessment(p, req):
    given = req.get('conditions', {})
    conditions = p['conditions']
    if not _matches(conditions, given):
        return None
    missing_cond = set(conditions) - given.keys()
    unreviewed = sorted(given.keys() - conditions.keys())
    needed = []
    derived = {}
    known = set(req.get('known_inputs', []))
    for v in p['inputs']:
        when = v.get('when', {})
        if not _matches(when, given):
            continue
        if when.keys() - given.keys():
            missing_cond.update(when.keys() - given.keys())
        elif not set([v['key'], *v.get('alternatives', [])]).intersection(known):
            if v.get('derived_from') and set(v['derived_from']) <= known:
                derived[v['key']] = {'from': v['derived_from'], 'method': v['meaning']}
            else:
                needed.append(v['key'])
    missing_goals = sorted(set(req['goals']) - set(p['goals']))
    missing_mech = sorted(set(req['mechanisms']) - set(p['mechanisms']))
    gaps = bool(needed or missing_cond or unreviewed or missing_mech or missing_goals
                or p.get('implementation') == 'custom')
    alternatives = {v['key']:v['alternatives'] for v in p['inputs']
                    if v['key'] in needed and v.get('alternatives')}
    return dict(status='partial' if gaps else 'matched', reason=p['basis'], missing_inputs=needed,
                input_alternatives=alternatives, derivable_inputs=derived,
                missing_conditions=sorted(missing_cond), unreviewed_conditions=unreviewed,
                missing_mechanisms=missing_mech, missing_goals=missing_goals,
                outputs=p['outputs'], assumptions=p['assumptions'], limitations=p['limitations'],
                couplings=p['couplings'], evidence=p['evidence'],
                **{k:p[k] for k in ('implementation','correlations','custom_work') if k in p})


def select(db, req, solver=None, version=None, brief=False):
    _request(req)
    domain, goals, mechanisms = req['domain'], req['goals'], req['mechanisms']
    # Use the covering facet index to intersect physical processes and desired outputs.
    where = ['domain=?']
    values = [domain]
    for k, v in (('solver',solver), ('version',version)):
        if v is not None:
            where.append(k + '=?')
            values.append(v)
    branches = []
    for kind, tokens in (('goals',goals),('mechanisms',mechanisms)):
        branches.append('(kind=? AND value IN (' + ','.join('?' for _ in tokens) + '))')
        values.extend([kind, *tokens])
    sql = ('WITH hits AS (SELECT domain,solver,version,model FROM model_facets WHERE ' + ' AND '.join(where) +
           ' AND (' + ' OR '.join(branches) + ') GROUP BY domain,solver,version,model HAVING count(DISTINCT kind)=2)'
           ' SELECT p.solver,p.version,p.model,p.body FROM hits JOIN model_profiles p USING(domain,solver,version,model)')
    rows = []
    for s,v,m,body in db.execute(sql, values):
        p = json.loads(body)
        match = _assessment(p, req)
        if match is not None:
            rows.append(dict(solver=s,version=v,model=m,label=p['label'],**match))
    def ordering(r):
        return (r['status'] != 'matched', len(r['missing_mechanisms']) + len(r['missing_goals']),
                len(r['missing_inputs']) + len(r['missing_conditions']) + len(r['unreviewed_conditions']),
                r['solver'], r['model'])
    rows.sort(key=ordering)
    status = rows[0]['status'] if rows else 'no_match'
    result = dict(status=status, domain=domain, candidates=rows)
    if not rows:
        result['reason'] = '此域、版本、机制、目标与工况组合在已编录模型中没有相容记录。'
    if domain == 'phase_change' and status != 'matched':
        # A scoped negative selection should retain the software's recorded role/limits.
        reviews = [r for kind in mechanisms if kind in PHASE_PHENOMENA
                   for r in phase_support(db, solver, version, kind)]
        result['support_review'] = [{k:r[k] for k in ('solver','version','phenomenon','status','scope')}
                                    for r in reviews]
    if brief:
        result['candidate_count'] = len(rows)
        best = ordering(rows[0])[:3] if rows else None
        sources = {}
        compact = []
        for row in rows:
            if ordering(row)[:3] != best:
                break
            item = {k:v for k,v in row.items() if k not in ('assumptions','outputs','limitations','couplings','evidence')}
            item['references'] = []
            for ref in row['evidence']:
                key = (ref['kind'],ref['url'],ref['locator'])
                item['references'].append(sources.setdefault(key,len(sources)))
            compact.append(item)
        result.update(candidates=compact, sources=[dict(kind=k,url=u,locator=l) for k,u,l in sources])
    return result
