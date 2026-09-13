"""Offline, additive audit. Never edits the source experiment or sends requests."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from tools.live_comparison import phase_accounting
from tools.live_smoke import dump


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or source in output.parents:
        raise ValueError('audit_output_must_be_separate')
    files = {p.relative_to(source).as_posix():file_hash(p) for p in source.rglob('*') if p.is_file()}
    ledger = json.loads((source/'ledger.json').read_text(encoding='utf-8'))
    original = json.loads((source/'metrics.json').read_text(encoding='utf-8'))
    accounting = {phase:phase_accounting(ledger['events'],phase) for phase in ledger['counts']}
    if any(accounting[p]['reserved_attempts']!=n for p,n in ledger['counts'].items()):
        raise ValueError('ledger_event_count_mismatch')
    corrected = [{**row,**accounting[row['method']]} for row in original]
    manifest = {'source':str(source),'files_sha256':files,'accounting':accounting,
                'reserved_attempts':sum(x['reserved_attempts'] for x in accounting.values()),
                'actual_calls':sum(x['actual_calls'] for x in accounting.values()),
                'historical_cause':'undetermined; missing handoff evidence is not proof of a specific OS error'}
    dump(output/'freeze-manifest.json',manifest)
    dump(output/'metrics.json',corrected)
    with (output/'metrics.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(dict.fromkeys(k for row in corrected for k in row)))
        writer.writeheader()
        writer.writerows({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in row.items()} for row in corrected)
    lines=['# comparison-v1 离线计数审计','','原始文件未改写；以下计数由原始账本重新计算。',
           '|阶段|预算预留|确认发送|发送未确认|重试|完整响应|','|---|---:|---:|---:|---:|---:|']
    for phase,row in accounting.items():
        lines.append('|'+phase+'|'+'|'.join(str(row[k]) for k in ('reserved_attempts','actual_calls','unconfirmed_reservations','retries','complete_responses'))+'|')
    lines+=['','旧 actual_calls 部分记录的是预算预留数。更正统计不改变几何、参考标签或误差评价。',
            '缺少发送证据的预留继续占用预算。历史本地异常的具体原因未确定。']
    (output/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    if any(file_hash(source/p)!=h for p,h in files.items()):
        raise ValueError('source_changed_during_audit')
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=audit(args.source,args.output)
    print(json.dumps({k:result[k] for k in ('reserved_attempts','actual_calls')}))
