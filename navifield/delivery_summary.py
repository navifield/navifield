"""Produce a factual pre-delivery summary from existing local run evidence."""
from __future__ import annotations
from collections import defaultdict
from html import escape
import json,math,re
from pathlib import Path
from . import project
from .foam_readers import ResidualParser,field_header,parse_checkmesh,strip_foam_comments,_field_tokens
from .postprocess import EngineeringReport,ReportParagraph,ReportSection,_history_sections,_html

MISSING='未取得；不代表不存在'
NUMBER=r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'

def _read(path):
    try:return path.read_text(encoding='utf-8',errors='replace')
    except OSError:return None

def _json(path):
    try:return json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError,TypeError):return None

def _mesh(root,run):
    current=int(run['run_id'][1:]); case=run.get('execution_case_path')
    candidates=[]
    folder=root/'.navifield/records'
    for file in folder.glob('r*.json') if folder.is_dir() else ():
        record=_json(file)
        if not isinstance(record,dict) or not str(record.get('solver',{}).get('program','')).lower().startswith('checkmesh'):
            continue
        try:
            rid=record['run_id']; previous=project.read_run(root,rid)
            if int(rid[1:])<=current and previous.get('execution_case_path')==case:
                candidates.append((int(rid[1:]),rid))
        except (KeyError,ValueError,OSError):continue
    if not candidates:return MISSING,'未发现同案例的既有 checkMesh 记录'
    rid=max(candidates)[1];text=_read(root/'result'/rid/'native.log')
    if text is None:return MISSING,'checkMesh 日志未取得'
    data=parse_checkmesh(text)
    def value(x):return '未取得' if x is None else str(x)
    threshold=re.findall(r'severely non-orthogonal[^\r\n]*?(?:>|greater than)\s*('+NUMBER+r')',text,re.I)
    count=data.severe_non_ortho_faces if len(threshold)==1 and float(threshold[0])==70 else None
    return (f'最大非正交角 {value(data.max_non_ortho)}°；超过70°的面数 {value(count)}；最大偏斜 {value(data.max_skewness)}',
            f'同案例 {rid} 的原生 checkMesh 日志；是否与本次网格完全一致未自动确认')

def _schemes(root,run):
    path=root/'model'/run['run_id']/'inputs/system/fvSchemes';text=_read(path)
    if text is None:return MISSING,'未取得运行开始时 fvSchemes 副本'
    match=re.search(r'\bdivSchemes\s*\{(.*?)\}',strip_foam_comments(text),re.S)
    if not match:return MISSING,'运行开始时 fvSchemes 中未取得对流格式'
    rows=[]
    for item in match.group(1).split(';'):
        line=' '.join(item.split())
        if not line or line.startswith('default '):continue
        if re.search(r'\blinearUpwind\b',line):order='二阶线性迎风'
        elif re.search(r'\bupwind\b',line):order='一阶迎风'
        elif re.search(r'\b(?:limitedLinear|vanLeer|limitedLinearV|linearUpwindV)\b',line):order='限幅或混合格式，具体阶次视情形而定'
        elif re.search(r'\bGauss\s+linear\b',line):order='线性插值；具体项见原文'
        else:order='阶次未确定'
        rows.append(line+' ['+order+']')
    return ('；'.join(rows) if rows else MISSING,'运行开始时 fvSchemes 副本；逐项列出，不将混合格式统一归类')

def _residuals(root,run):
    text=_read(root/'result'/run['run_id']/'native.log')
    if text is None:return MISSING,'原生日志未取得'
    parser=ResidualParser();rows=parser.feed((text+'\n').encode('utf-8'))
    equations=defaultdict(list)
    for row in rows:equations[(row.region,row.equation)].append(row)
    if not equations:return MISSING,'日志中未取得完整时间步残差'
    values=[]
    for (region,name),points in sorted(equations.items()):
        window=points[-10:];last=window[-1]
        values.append(f'{region+"/" if region else ""}{name}: 初始残差末值 {last.initial:g}，末段 {window[0].axis:g}–{last.axis:g}，{len(window)} 点')
    return '；'.join(values),f'{run["run_id"]} 原生日志中每方程最近最多10个完整时间步；不判断收敛'

def _continuity(root,run):
    text=_read(root/'result'/run['run_id']/'native.log')
    if text is None:return MISSING,'原生日志未取得'
    expression=r'continuity errors\s*:\s*sum local\s*=\s*('+NUMBER+r')\s*,\s*global\s*=\s*('+NUMBER+r')\s*,\s*cumulative\s*=\s*('+NUMBER+r')'
    axis=None;rows=[]
    for line in text.splitlines():
        time_match=re.match(r'^\s*Time\s*=\s*('+NUMBER+r')\s*$',line)
        if time_match:axis=float(time_match.group(1))
        match=re.search(expression,line,re.I)
        if match:rows.append((axis,match.groups()))
    if not rows:return MISSING,'未取得原生连续性误差输出'
    try:values=[(axis,tuple(map(float,row))) for axis,row in rows[-10:]]
    except ValueError:return MISSING,'连续性误差不可解析'
    if not all(math.isfinite(x) for _,row in values for x in row):return MISSING,'连续性误差包含非有限值'
    last=values[-1][1];axes=[axis for axis,_ in values if axis is not None and math.isfinite(axis)]
    window=f'{axes[0]:g}–{axes[-1]:g}' if len(axes)==len(values) else '时间范围未取得'
    return f'末条 sum local={last[0]:g}，global={last[1]:g}，cumulative={last[2]:g}；末段 {len(values)} 条',f'{run["run_id"]} 原生日志 {window}；这不是进出口通量差'

def _series(path):
    text=_read(path)
    if text is None:return None
    rows={}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):continue
        parts=line.replace('(',' ').replace(')',' ').replace(',',' ').split()
        if len(parts)!=2:return None
        try:t=float(parts[0])
        except (ValueError,IndexError):return None
        if not math.isfinite(t):return None
        try:v=float(parts[1])
        except (ValueError,IndexError):v=None
        rows[t]=v if v is not None and math.isfinite(v) else None
    return rows

def _flux(root,run):
    try:case=project.resolve_path(root,run['execution_case_path'])
    except (KeyError,ValueError):return MISSING,'案例路径未取得'
    tables=[]
    for name in ('inletFlux','outletFlux'):
        files=list((case/'postProcessing'/name).glob('*/surfaceFieldValue.dat'))
        if not files:return MISSING,'未取得一对入出口通量表；不以连续性残差代替'
        try:
            keyed=[(float(file.parent.name),file) for file in files]
            if not all(math.isfinite(key) for key,_ in keyed) or len({key for key,_ in keyed})!=len(keyed):
                return MISSING,'通量表时间目录含无效值或重复数值'
            files=[file for _,file in sorted(keyed,key=lambda pair:pair[0])]
        except ValueError:return MISSING,'通量表时间目录不可解析'
        merged={}
        for file in files:
            series=_series(file)
            if series is None:return MISSING,'通量表无法解析；未使用更早样本替代'
            merged.update(series)
        tables.append(merged)
    common=sorted(set(tables[0])|set(tables[1]))[-10:]
    if not common:return MISSING,'未取得共同通量样本'
    if any(t not in tables[0] or t not in tables[1] or tables[0][t] is None or tables[1][t] is None for t in common):
        return MISSING,'末段通量样本缺失或包含无效值'
    last=common[-1];inside=tables[0][last];outside=tables[1][last]
    denominator=max(abs(inside),abs(outside));ratio=abs(inside+outside)/denominator if denominator else None
    ratios=[]
    for t in common:
        a,b=tables[0][t],tables[1][t];scale=max(abs(a),abs(b))
        if scale:ratios.append(abs(a+b)/scale)
    maximum=max(ratios) if len(ratios)==len(common) else '未取得'
    fluctuation=('仅1点，波动未能估计' if len(common)==1 else
                 f'入口末段范围 {min(tables[0][t] for t in common):g}–{max(tables[0][t] for t in common):g}')
    return (f'末样本入={inside:g}、出={outside:g}，相对净通量={ratio if ratio is not None else "未取得"}；'
            f'末段最大相对净通量={maximum}；{fluctuation}；{len(common)} 个共同样本',
            f'当前案例现有入出口 phi 求和表 {common[0]:g}–{last:g}；未与所选运行或网格绑定；phi 的质量或体积单位未确认')

def _monitor(root,relative):
    if not relative:return MISSING,'未声明监测表'
    try:path=project.resolve_path(root,relative)
    except ValueError:return MISSING,'监测表路径未取得'
    series=_series(path)
    if not series:return MISSING,'监测表无可读数值'
    times=sorted(series)[-10:];values=[series[t] for t in times]
    if any(value is None for value in values):return MISSING,'末段监测量包含无效值'
    if len(times)==1:return f'末值 {values[-1]:g}；仅1点，波动未能估计',relative+'；单位未声明'
    return f'末值 {values[-1]:g}；末段范围 {min(values):g}–{max(values):g}；{len(times)} 点',f'{relative}，{times[0]:g}–{times[-1]:g}；单位未声明'

def _absolute_pressure(root,relative,mesh_count):
    if not relative:return MISSING,'未声明绝对压力场；表压或运动压负值不能按负绝压统计'
    try:
        path=project.resolve_path(root,relative)
        with path.open('rb') as stream:header,dims=field_header(stream.read(65536))
    except (OSError,ValueError,UnicodeError):return MISSING,'绝对压力场不可读'
    if header.get('format')!='ascii' or header.get('class')!='volScalarField' or dims!=(1.,-1.,-2.,0.,0.,0.,0.):
        return MISSING,'仅支持用户声明为绝对压且量纲为 Pa 的 ASCII 标量场'
    try:
        with path.open('r',encoding='ascii') as stream:
            tokens=iter(_field_tokens(stream));depth=0
            for token in tokens:
                if token=='{':depth+=1
                elif token=='}':depth-=1
                elif token=='internalField' and depth==0:break
            else:return MISSING,'未取得顶层内部场'
            mode=next(tokens)
            if mode=='uniform':
                val=float(next(tokens))
                if next(tokens)!=';' or not math.isfinite(val):return MISSING,'均匀场值不可读'
                return f'均匀绝对压力 {val:g} Pa；单元数未取得',relative+'；用户声明绝对压力，字段量纲为 Pa'
            if mode!='nonuniform' or next(tokens)!='List<scalar>':return MISSING,'内部标量场格式不可读'
            count=int(next(tokens))
            if count<0 or next(tokens)!='(':return MISSING,'内部场数量不可读'
            negative=nonfinite=0
            for _ in range(count):
                value=float(next(tokens))
                if not math.isfinite(value):nonfinite+=1
                elif value<0:negative+=1
            if next(tokens)!=')' or next(tokens)!=';':return MISSING,'内部场数量不符'
    except (OSError,UnicodeError,ValueError,StopIteration,OverflowError):return MISSING,'内部场不可读'
    return (f'负绝对压力单元数 {negative}；非有限值单元数 {nonfinite}',
            relative+'；用户声明绝对压力，字段量纲为 Pa')

def _declared(facts):
    if not isinstance(facts,dict):return MISSING,MISSING,MISSING
    assumptions=facts.get('assumptions')
    if isinstance(assumptions,list):
        unconfirmed=[str(x.get('item')) for x in assumptions if isinstance(x,dict) and x.get('confirmed') is False and x.get('item')]
        assumptions_text='；'.join(unconfirmed) if unconfirmed else '已提供声明，但未列出未确认项；不推断其他输入'
    else:assumptions_text=MISSING
    grid=str(facts['grid_comparison']) if facts.get('grid_comparison') is not None else MISSING
    force=MISSING;item=facts.get('forces')
    if isinstance(item,dict):
        try:
            components=item['components'];net=item['net']
            if len(net)!=3 or not components or any(len(v)!=3 for v in components):raise ValueError()
            nums=[float(x) for vector in [net,*components] for x in vector]
            if not all(math.isfinite(x) for x in nums):raise ValueError()
            size=lambda v:math.sqrt(sum(float(x)**2 for x in v))
            denominator=sum(size(v) for v in components)
            ratio=size(net)/denominator if denominator else None
            force=f'净力模 / 分项力模之和 = {ratio if ratio is not None else "未取得"}；单位 {item.get("unit","未声明")}；比例较小时可检查分项方向与抵消'
        except (TypeError,ValueError,KeyError,OverflowError):pass
    return assumptions_text,grid,force

def generate(root,rids,*,facts_path=None,monitor=None,absolute_pressure=None):
    root=Path(root).resolve();facts=None
    if facts_path:
        try:facts=_json(project.resolve_path(root,facts_path))
        except ValueError:pass
    assumptions,grid,force=_declared(facts)
    rows=[]
    for rid in rids:
        try:run=project.read_run(root,rid)
        except (OSError,ValueError,KeyError):
            rows.append((rid,'运行记录',MISSING,'指定运行未取得'));continue
        mesh,mesh_source=_mesh(root,run)
        scheme,scheme_source=_schemes(root,run)
        residual,residual_source=_residuals(root,run)
        continuity,continuity_source=_continuity(root,run)
        flux,flux_source=_flux(root,run)
        observed,monitor_source=_monitor(root,monitor)
        # A historical checkMesh cannot prove the current mesh identity.  Do not
        # use its cell count to turn a uniform pressure into a cell count.
        mesh_count=None
        pressure,pressure_source=_absolute_pressure(root,absolute_pressure,mesh_count)
        record=_json(root/'.navifield/records'/f'{rid}.json') or {}
        resources=record.get('resources') or {}
        execution=f'结束状态={resources.get("termination","未取得")}；退出码={resources.get("exit_code")}；核数={resources.get("cores")}；墙钟秒={resources.get("wall_seconds")}'
        for item,value,source in (('执行记录',execution,'运行记录；只表示执行状态'),('网格质量',mesh,mesh_source),('div 格式（含非对流项）',scheme,scheme_source),
                                  ('末段残差',residual,residual_source),('连续性误差',continuity,continuity_source),
                                  ('入出口通量',flux,flux_source),('监测量末段',observed,monitor_source),
                                  ('负绝对压力',pressure,pressure_source),('净力抵消',force,'用户声明；未取得时不推断'),
                                  ('未确认输入',assumptions,'用户声明；未取得时不推断'),('网格对比',grid,'用户声明；未取得时不推断')):
            rows.append((rid,item,str(value)[:1900],str(source)[:1900]))
    sections=_history_sections(key='delivery',title='交付前可信度摘要',columns=('运行','项目','实测或声明','来源与限制'),rows=rows)
    sections+=(ReportSection(key='scope',title='适用范围',blocks=(
        ReportParagraph(text='该摘要不阻断操作；物理含义与工程适用性由使用者判断。'),)),)
    report=EngineeringReport(summary='仅列已有事实、声明和缺失项，不作通过判定。',sections=sections)
    output=project.resolve_path(root,f'report/{rids[0]}/self-check.html' if len(rids)==1 else 'report/self-check.html')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(_html(report,root,output,self_contained=True,title='交付前可信度摘要'),encoding='utf-8')
    fragment='<section id="delivery-summary"><h2>交付前可信度摘要</h2><table><tr><th>运行</th><th>项目</th><th>实测或声明</th><th>来源与限制</th></tr>'
    fragment+=''.join('<tr>'+''.join('<td>'+escape(cell)+'</td>' for cell in row)+'</tr>' for row in rows)+'</table></section>'
    start='<!-- navifield-self-check:start -->';end='<!-- navifield-self-check:end -->'
    for rid in rids:
        try:target=project.resolve_path(root,f'report/{rid}/index.html')
        except ValueError:continue
        existing=_read(target)
        if existing is None:
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(_html(report,root,target,self_contained=True,title='交付前可信度摘要'),encoding='utf-8')
            continue
        existing=re.sub(re.escape(start)+r'.*?'+re.escape(end),'',existing,flags=re.S)
        insertion=start+fragment+end
        target.write_text(existing.replace('</body>',insertion+'</body>') if '</body>' in existing else existing+insertion,encoding='utf-8')
    return output.relative_to(root).as_posix()
