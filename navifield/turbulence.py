"""Versioned, evidence-backed turbulence model selection from the offline library."""
import json
import math
from pathlib import Path
from urllib.parse import urlsplit

RANKS = ("preferred", "candidate", "caution", "insufficient")
WALL_MODES = {"wall_resolved", "wall_function", "other"}
REQUIRED = ("flow", "target", "regime", "fluid", "surface", "wall_mode", "planned_y_plus",
            "velocity_basis", "velocity_m_s", "length_basis", "length_m", "nu_m2_s")
LENGTHS = {"diameter": ("D", "圆管内径"), "inlet_height": ("H", "入口全高"),
           "hydraulic_diameter": ("Dh", "水力直径 4A/P")}
SPEEDS = {"bulk": "截面平均速度", "centerline": "中心线速度"}


def positive(value):
    return type(value) in (float, int) and math.isfinite(value) and value > 0


def _validate(profile):
    def require(condition, message):
        if not condition:
            raise ValueError(f"{profile.get('id')}: {message}")

    for field in ('id', 'label'):
        require(isinstance(profile.get(field), str) and profile[field].strip(), field)
    for field in ('flows', 'targets', 'limitations'):
        require(profile.get(field) and all(isinstance(s, str) and s.strip()
                                          for s in profile[field]), field)
    evidence = {e['id']: e for e in profile['evidence']}
    require(evidence and len(evidence) == len(profile['evidence']), 'evidence IDs')
    for e in evidence.values():
        url = urlsplit(e['url'])
        require(url.scheme in ('https', 'http') and url.netloc and e['kind'] and
                e['locator'] and e['claim'], 'source and locator')
    def check_refs(entry):
        refs = entry['evidence']
        require(isinstance(refs, list) and refs and set(refs) <= evidence.keys(), 'unknown evidence')
    re_data = profile['reynolds']
    require(re_data['definitions'] and re_data['note'] and
            all(d['symbol'] and d['definition'] for d in re_data['definitions']), 'Re definition')
    for benchmark in re_data['benchmarks']:
        require(benchmark['symbol'] and benchmark['definition'] and benchmark['geometry']
                and positive(benchmark['value']), 'benchmark point')
        check_refs(benchmark)
    for interval in re_data['validity_intervals']:
        require(interval['definition'] and interval['scope'] and interval['symbol'] and
                positive(interval['min']) and positive(interval['max']) and
                interval['min'] < interval['max'], 'model validity interval needs a named scope')
        check_refs(interval)
        require(any(evidence[k]['kind'] == 'documented_validity_range'
                    for k in interval['evidence']), 'interval requires explicit range evidence')
    modes = {w['mode'] for w in profile['walls']}
    require(modes and modes <= WALL_MODES and len(modes) == len(profile['walls']), 'wall mode')
    for wall in profile['walls']:
        require(wall['treatment'] and wall['note'], 'wall setup')
        check_refs(wall)
        y = wall['y_plus']
        if y is not None:
            require(isinstance(y, dict) and all(v is None or positive(v) for v in
                    (y.get('min'), y.get('max'), y.get('target'))), 'y+ guidance')
            require(not (y.get('min') and y.get('max') and y['min'] > y['max']), 'inverted y+')
    for rule in profile['rules']:
        require(rule['flow'] and rule['target'] and rule['rank'] in RANKS and rule['reason']
                and set(rule['wall_modes']) <= modes, 'selection rule')
        check_refs(rule)


def load_profiles(db, source):
    db.executescript("""
        CREATE TABLE turbulence_profiles (
          solver TEXT, version TEXT, model TEXT, capability_id TEXT REFERENCES capabilities(id),
          body TEXT NOT NULL, PRIMARY KEY (solver,version,model));
        CREATE TABLE turbulence_rules (
          solver TEXT, version TEXT, model TEXT, flow TEXT, target TEXT, rank TEXT, body TEXT NOT NULL,
          ordinal INTEGER NOT NULL, PRIMARY KEY (solver,version,model,ordinal),
          FOREIGN KEY (solver,version,model) REFERENCES turbulence_profiles(solver,version,model));
        CREATE INDEX turbulence_lookup ON turbulence_rules(flow,target,solver,version);
    """)
    for file in sorted((Path(source) / 'suitability').glob('*.json')):
        data = json.loads(file.read_text(encoding='utf-8-sig'))
        owner = (data['solver'], data['version'])
        if not db.execute('SELECT 1 FROM capabilities WHERE solver=? AND version=? LIMIT 1', owner).fetchone():
            raise ValueError(f'Unknown solver/version in {file.name}')
        for p in data['models']:
            _validate(p)
            cap = p['capability_id']
            if cap is not None and not db.execute(
                    'SELECT 1 FROM capabilities WHERE solver=? AND version=? AND id=?', (*owner, cap)).fetchone():
                raise ValueError(f'Invalid capability link: {cap}')
            key = (*owner, p['id'])
            db.execute('INSERT INTO turbulence_profiles VALUES (?,?,?,?,?)',
                       (*key, cap, json.dumps(p, ensure_ascii=False)))
            seen = set()
            for ordinal, rule in enumerate(p['rules']):
                modes = {(rule['flow'], rule['target'], mode) for mode in rule['wall_modes']}
                if seen.intersection(modes):
                    raise ValueError(f'Overlapping wall rules for {key}')
                seen.update(modes)
                db.execute('INSERT INTO turbulence_rules VALUES (?,?,?,?,?,?,?,?)',
                           (*key, rule['flow'], rule['target'], rule['rank'], json.dumps(rule, ensure_ascii=False), ordinal))


def _filters(solver, version, model=None):
    return {key: val for key, val in (('solver', solver), ('version', version), ('model', model))
            if val is not None}


def profiles(db, solver=None, version=None, model=None):
    conditions = _filters(solver, version, model)
    sql = 'SELECT solver,version,model,body FROM turbulence_profiles'
    if conditions:
        sql += ' WHERE ' + ' AND '.join(f'{k}=?' for k in conditions)
    result = []
    for s, v, m, body in db.execute(sql + ' ORDER BY solver,version,model', tuple(conditions.values())):
        p = json.loads(body)
        result.append(dict(solver=s, version=v, model=m, **p) if model else
                      dict(solver=s, version=v, model=m, label=p['label'], rules=len(p['rules'])))
    return result


def _context(req):
    if not isinstance(req, dict):
        raise ValueError('Expected a JSON object')
    extra = set(req) - set(REQUIRED) - {'description'}
    if extra:
        return 'need_input', '存在未识别的工况字段，需核实其对选型的影响。', sorted(extra), None
    missing = [k for k in REQUIRED if req.get(k) in (None, '')]
    if missing:
        return 'need_input', '补全流态、参考尺度和壁面网格方案后可检索。', missing, None
    for k in ('velocity_m_s', 'length_m', 'nu_m2_s', 'planned_y_plus'):
        if not positive(req[k]):
            raise ValueError(f'{k} must be a finite positive number')
    for k in set(REQUIRED) - {'velocity_m_s', 'length_m', 'nu_m2_s', 'planned_y_plus'}:
        if not isinstance(req[k], str):
            raise ValueError(f'{k} must be a string')
    l = LENGTHS.get(req['length_basis'])
    u = SPEEDS.get(req['velocity_basis'])
    bases = {'fully_developed_pipe': ('diameter',), 'diffuser_adverse_gradient': ('inlet_height', 'hydraulic_diameter')}
    if not l or not u or (req['flow'] in bases and req['length_basis'] not in bases[req['flow']]):
        return 'need_input', '请核实几何和速度基准，当前定义无法解释。', ['length_basis', 'velocity_basis'], None
    value = req['velocity_m_s'] * req['length_m'] / req['nu_m2_s']
    if not positive(value):
        raise ValueError('Re is outside the finite positive numeric range')
    suffix = ',cl' if req['velocity_basis'] == 'centerline' else (',b' if l[0] == 'H' else '')
    re_data = dict(symbol=f'Re_{l[0]}{suffix}', value=value, definition=f'{u} × {l[1]} / 运动黏度',
                   velocity_m_s=req['velocity_m_s'], length_m=req['length_m'], nu_m2_s=req['nu_m2_s'])
    for key, supported in (('regime', 'fully_turbulent'), ('fluid', 'single_phase_incompressible_newtonian'),
                           ('surface', 'smooth')):
        if req[key] != supported:
            return 'no_match', '现有场景规则仅涵盖光滑壁、单相不可压牛顿流体的充分湍流。', [key], re_data
    if req['flow'] == 'fully_developed_pipe':
        if req['velocity_basis'] != 'bulk':
            return 'need_input', '直管摩阻规则需要截面平均速度，或流量除以截面积。', ['velocity_basis'], re_data
        if value < 4000:
            re_data['regime_reference'] = 'https://uta.pressbooks.pub/appliedfluidmechanics/chapter/experiment-7/'
            return ('need_input', '此圆管 Re 落在通常的层流或转捩区域，请核实充分湍流声明、入口扰动和单位。'
                    '这里检查流态的一致性。', ['regime'], re_data)
    if req['wall_mode'] not in {'wall_function', 'wall_resolved'}:
        return 'need_input', '无法识别壁面处理。', ['wall_mode'], re_data
    return None, None, [], re_data


def _assess(rule, profile, req):
    rank = rule['rank']
    wall = next((w for w in profile['walls'] if w['mode'] == req['wall_mode']), None)
    notes = []
    if not wall or req['wall_mode'] not in rule['wall_modes']:
        rank = 'insufficient' if rank != 'caution' else rank
        notes.append('此场景规则缺少所选壁面方案的依据。')
    else:
        y = wall['y_plus'] or {}
        planned = req['planned_y_plus']
        outside = (y.get('max') is not None and planned > y['max'] or
                   y.get('min') is not None and planned < y['min'])
        # For integration to the wall, finer planned spacing is compatible with an upper design target.
        too_coarse = (wall['mode'] == 'wall_resolved' and y.get('target') is not None and
                      planned > y['target'] and not math.isclose(planned, y['target']))
        if outside or too_coarse:
            rank = 'caution' if rank != 'insufficient' else rank
            notes.append('计划首层 y+ 与该方案的网格指导不符，调整网格后复查。')
        elif not any(y.get(k) is not None for k in ('min', 'max', 'target')):
            if rank in ('preferred', 'candidate'):
                rank = 'insufficient'
            notes.append('本版本缺少可核对计划 y+ 的数值依据。')
    refs = dict.fromkeys(rule['evidence'] + (wall['evidence'] if wall else []))
    evidence = {e['id']: e for e in profile['evidence']}
    wall_result = wall or dict(mode=req['wall_mode'], treatment=None, y_plus=None,
                               note='此模型缺少该壁面方案的记录。', evidence=[])
    re_data = profile['reynolds']
    return dict(rank=rank, reason=' '.join(notes) if notes else rule['reason'],
                flow_rank=rule['rank'], flow_basis=rule['reason'], checks=notes, wall=wall_result,
                re_evidence=dict(validity_intervals=re_data['validity_intervals'],
                                 benchmark_points=len(re_data['benchmarks']), note=re_data['note']),
                evidence=[evidence[k] for k in refs])


def select(db, req, solver=None, version=None):
    status, reason, fields, reynolds = _context(req)
    result = dict(status=status, reynolds=reynolds, candidates=[], gaps=[])
    if status:
        result['gaps'].append(dict(reason=reason, fields=fields))
        return result
    where = {'flow': req['flow'], 'target': req['target'], **_filters(solver, version)}
    sql = ('SELECT r.solver,r.version,r.model,r.body,p.body FROM turbulence_rules r '
           'JOIN turbulence_profiles p USING (solver,version,model) WHERE ' +
           ' AND '.join(f'r.{k}=?' for k in where))
    by_model = {}
    for s, v, m, rule_json, body in db.execute(sql + ' ORDER BY r.ordinal', tuple(where.values())):
        rule = json.loads(rule_json)
        key = (s, v, m)
        if key in by_model and req['wall_mode'] not in rule['wall_modes']:
            continue
        profile = json.loads(body)
        item = _assess(rule, profile, req)
        by_model[key] = dict(solver=s, version=v, model=m, label=profile['label'], **item)
    result['candidates'] = list(by_model.values())
    rows = result['candidates']
    rows.sort(key=lambda r: (RANKS.index(r['rank']), r['solver'], r['model']))
    result['status'] = ('matched' if any(r['rank'] == 'preferred' for r in rows) else
                        'partial' if rows else 'no_match')
    if not rows:
        result['gaps'].append(dict(reason='该版本、流动与预测量组合缺少已审核规则。', fields=list(where)))
    covered = {(r['solver'], r['version'], r['model']) for r in rows}
    result['models_without_rule'] = [
        {k: p[k] for k in ('solver', 'version', 'model')}
        for p in profiles(db, solver, version) if (p['solver'], p['version'], p['model']) not in covered]
    result['scope'] = '工程选型初筛；Re 为输入工况，单点基准仅供比较。计划 y+ 须在求解后按壁面分布复核。'
    return result


def brief(result):
    """Keep each solver's highest-ranked matches and deduplicate their sources."""
    report = {k: result[k] for k in ('status', 'reynolds', 'gaps')}
    rows = result['candidates']
    report['reviewed_candidates'] = len(rows)
    best = {}
    sources = {}
    report['candidates'] = []
    for row in rows:
        group = (row['solver'], row['version'])
        if row['rank'] != best.setdefault(group, row['rank']):
            continue
        summary = {k: row[k] for k in ('solver', 'version', 'model', 'rank', 'reason')}
        summary['references'] = []
        for source in row['evidence']:
            ref = (source['kind'], source['url'], source['locator'])
            summary['references'].append(sources.setdefault(ref, len(sources)))
        report['candidates'].append(summary)
    report['sources'] = [dict(kind=k, url=u, locator=l) for k, u, l in sources]
    return report
