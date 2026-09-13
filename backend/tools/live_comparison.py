"""Single authorized 2000-attempt experiment. Never reset or auto-resume a run."""
import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack
import csv
from datetime import datetime, timezone
import hashlib
import html
from importlib.metadata import version
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time

import httpx
import numpy as np
from shapely.geometry import MultiPolygon, Point, box

from app.analyses import LimitedProvider, RateGate
from app.baidu import silence_transport_logs
from app.config import Settings
from life_circle.baselines import compute_radial
from life_circle.coordinates import LocalProjection, normalize
from life_circle.engine import compute_isochrone
from life_circle.field import multipolygon
from life_circle.models import CancelToken, IsochroneRequest
from life_circle.providers import BaiduProvider
from life_circle.scheduler import Clock, Scheduler
from tools.live_smoke import Ledger, LiveGuardError, ORIGIN, dump
from tools.qps_review import AuditedTransport, QpsLedger, metrics as timing_metrics

BASELINE = 'guodingyi-baseline-20260913'
ROOT = Path('D:/CodexOutputs/guodingyi-stability-comparison-20260913')
LIMITS = {'stability': 288, 'reference': 512, 'adaptive': 400, 'uniform': 400, 'radial': 400}
METHODS = ('adaptive', 'uniform', 'radial')
SEED = 20260911
PROJECTION = LocalProjection(ORIGIN)
FATAL = {'permission', 'quota', 'invalid_parameter', 'rate_limit'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def make_protocol():
    rng = random.Random(SEED)
    validation = []
    for x in range(16):
        for y in range(16):
            # A one metre margin keeps six-decimal normalization inside its stratum.
            local = (-1600+200*x+rng.uniform(1,199), -1600+200*y+rng.uniform(1,199))
            point = normalize(PROJECTION.to_geographic(local))
            validation.append({'cell': [x,y], 'point': list(point), 'local': list(PROJECTION.to_local(point))})
    points = [list(normalize(PROJECTION.to_geographic(p))) for d in (200,300,500,600)
              for p in ((d,0),(-d,0),(0,d),(0,-d))]
    order = list(METHODS)
    random.Random(SEED).shuffle(order)
    return {'origin': list(ORIGIN), 'coordinateSystem': 'bd09ll', 'threshold':900,
            'extent':1600, 'expand':False, 'qps':3, 'effective_concurrency':1,
            'timeout':8, 'max_attempts':2, 'deadline_seconds':600, 'seed':SEED,
            'limits':dict(LIMITS), 'total_limit':2000, 'stability_batches':16,
            'stability_points':points, 'validation':validation,
            'validation_sha256':digest(validation), 'order':order}


class ExperimentLedger(Ledger):
    phase_limits = LIMITS
    total_limit = 2000

    def __init__(self, root):
        super().__init__(root)
        self.batch = 0
        self.token = CancelToken()
        self.deadline = float('inf')
        self.clock = time.monotonic

    def start_once(self):
        if self.data.get('experiment_started') or sum(self.data['counts'].values()):
            raise LiveGuardError('experiment_already_started_no_resume')
        try:
            with (self.root/'started.marker').open('x',encoding='utf-8') as marker:
                marker.write(datetime.now(timezone.utc).isoformat())
                marker.flush()
                os.fsync(marker.fileno())
        except OSError:
            raise LiveGuardError('start_marker_failed_or_already_exists') from None
        self.data['experiment_started'] = True
        self.data['started_utc'] = datetime.now(timezone.utc).isoformat()
        self.save()

    def save(self):
        try:
            super().save()
        except OSError:
            # A failed durable write must not become a recoverable Provider
            # error. The attempt stays reserved; later sends are disarmed.
            self.data['halted'] = 'ledger_write_failed'
            raise LiveGuardError('ledger_write_failed') from None

    def reserve(self, phase, request, monotonic, *, diagnostic=False):
        if self.token.cancelled or self.clock() >= self.deadline:
            raise LiveGuardError('cancelled_or_deadline_before_send')
        lat,lng = map(float, request.url.params['destination'].split(','))
        destination = list(normalize((lng,lat)))
        origin_lat,origin_lng = map(float,request.url.params['origin'].split(','))
        if normalize((origin_lng,origin_lat)) != ORIGIN or diagnostic:
            raise LiveGuardError('unexpected_comparison_request')
        attempts = sum(e['phase'] == phase and e.get('batch',0) == self.batch
                       and e['destination'] == destination for e in self.data['events'])
        if attempts >= 2:
            raise LiveGuardError('destination_attempt_budget')
        event = super().reserve(phase, request, monotonic)
        event.update(batch=self.batch, attempt=attempts+1)
        self.save()
        return event


class ComparisonTransport(AuditedTransport):
    async def handle_async_request(self, request):
        self.batch = self.ledger.batch
        if self.ledger.data['halted'] or self.ledger.token.cancelled:
            raise LiveGuardError('comparison_stopped')
        sent = [e for e in self.ledger.data['events'] if 'dispatchPerf' in e]
        reason = None
        if sent:
            previous = sent[-1]
            if 'responsePerf' not in previous:
                reason = 'inflight_violation'
            else:
                cooldown = 1 if previous.get('outcome') in ('timeout','cancelled_in_flight') else 1/3
                if self.clock()-previous['responsePerf'] < cooldown-1e-9:
                    reason = 'response_gap_violation'
        if reason:
            self.ledger.data['halted'] = reason
            self.ledger.data.setdefault('audit_blocks', []).append(reason)
            self.ledger.save()
            raise LiveGuardError(reason)
        try:
            return await super().handle_async_request(request)
        except asyncio.CancelledError:
            self.ledger.token.cancel()
            self.ledger.data['halted'] = 'cancelled'
            raise
        finally:
            events = self.ledger.data['events']
            if events and events[-1].get('outcome') in FATAL:
                self.ledger.data['halted'] = events[-1]['outcome']
            self.ledger.save()


def valid_reference(sample):
    return sample.get('duration') is not None and sample.get('endpoint_verified') is True


def phase_accounting(events, phase):
    selected = [e for e in events if e['phase'] == phase]
    confirmed = sum('dispatchPerf' in e for e in selected)
    return {'reserved_attempts':len(selected), 'actual_calls':confirmed,
            'unconfirmed_reservations':len(selected)-confirmed,
            'complete_responses':sum('dispatchPerf' in e and 'http_status' in e for e in selected),
            'retries':sum(e.get('attempt',1)>1 for e in selected),
            'attempt_outcomes':dict(Counter(e.get('outcome') for e in selected))}


def stability_assessment(samples, events, halted):
    measured = timing_metrics(events)
    good = sum(valid_reference(s) for s in samples)
    errors = sum(e.get('outcome') in ('temporary','network_error','timeout') for e in events)
    rate = errors / len(events) if events else 1
    gaps_ok = measured['minimum_response_gap'] is not None and measured['minimum_response_gap'] >= 1/3-1e-9
    passed = (len(samples) == 256 and all(s.get('attempts',0) >= 1 for s in samples)
              and good >= .95*256 and 256 <= len(events) <= 288 and not halted
              and not any(e.get('outcome') in FATAL for e in events)
              and measured['guard_blocks'] == 0 and measured['max_inflight'] <= 1
              and measured['max_in_one_second'] <= 3 and gaps_ok and rate <= .01)
    latencies = [e['responsePerf']-e['dispatchPerf'] for e in events if 'responsePerf' in e]
    variation = []
    for point in sorted({tuple(s['point']) for s in samples}):
        values = [s['duration'] for s in samples if tuple(s['point']) == point and valid_reference(s)]
        variation.append({'point':point, 'valid_observations':len(values), 'minimum_seconds':min(values,default=None),
                          'maximum_seconds':max(values,default=None)})
    return {'passed':passed, 'observations':len(samples), 'valid_verified':good,
            'network_error_fraction':rate, 'timing':measured,
            'latency_median_seconds':float(np.median(latencies)) if latencies else None,
            'latency_p95_seconds':float(np.percentile(latencies,95)) if latencies else None,
            'repeated_point_variation':variation}


def point_metrics(geometry, unknown, references, sampled):
    overlap = [s for s in references if tuple(s['point']) in sampled]
    refs = [s for s in references if tuple(s['point']) not in sampled and valid_reference(s)]
    tp = fp = missed = unknown_count = truth_positive = predicted_positive = 0
    quadrants = Counter()
    for sample in refs:
        p = Point(sample['local'])
        truth = sample['duration'] <= 900
        unknown_prediction = geometry is None or (unknown is not None and unknown.covers(p))
        predicted = not unknown_prediction and geometry.covers(p)
        truth_positive += truth
        predicted_positive += predicted
        tp += truth and predicted
        fp += not truth and predicted
        missed += truth and not predicted  # Includes unknown reachable points.
        unknown_count += unknown_prediction
        x,y = sample['local']
        quadrants[('E' if x >= 0 else 'W')+('N' if y >= 0 else 'S')] += 1
    return {'valid_reference_count':len(refs), 'invalid_reference_count':sum(not valid_reference(s) for s in references),
            'invalid_reference_fraction':sum(not valid_reference(s) for s in references)/len(references) if references else None,
            'excluded_overlap_count':len(overlap), 'reference_quadrants':dict(quadrants),
            'true_reachable_count':truth_positive, 'predicted_reachable_count':predicted_positive,
            'true_positive':tp, 'false_positive':fp, 'missed_reachable':missed, 'unknown_points':unknown_count,
            'false_inclusion_rate':fp/predicted_positive if predicted_positive else None,
            'miss_rate':missed/truth_positive if truth_positive else None,
            'unknown_point_fraction':unknown_count/len(refs) if refs else None}


def sample_record(observation, **extra):
    return {'point':list(observation.destination), 'local':list(PROJECTION.to_local(observation.destination)),
            'duration':observation.duration, 'reason':observation.reason,
            'endpoint_verified':observation.endpoint_verified, 'attempts':observation.attempts, **extra}


def write_artifacts(root, report, results, references):
    dump(root/'result.json', report)
    dump(root/'metrics.json', report['metrics'])
    rows = [{k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in row.items()} for row in report['metrics']]
    with (root/'metrics.csv').open('w',encoding='utf-8-sig',newline='') as f:
        fields = list(dict.fromkeys(k for row in rows for k in row))
        writer = csv.DictWriter(f,fieldnames=fields or ['method','status'])
        writer.writeheader()
        writer.writerows(rows)
    # No reference boundary: dots are independent labels, polygons are estimates.
    panels = []
    for index,method in enumerate(METHODS):
        result = results.get(method)
        parts = [f'<g transform="translate({index*3500+1750},1850)">',
                 '<rect x="-1600" y="-1600" width="3200" height="3200" fill="white" stroke="#777"/>',
                 f'<text x="-1600" y="-1700" font-size="70">{method}: {html.escape(report["stages"][method]["status"])}</text>']
        if result:
            for geometry,color in ((result.local_unknown,'#94a3b8'),(result.local_geometry,'#2563eb')):
                for polygon in multipolygon(geometry if geometry is not None else MultiPolygon()).geoms:
                    rings = ['M '+' L '.join(f'{x:.2f},{-y:.2f}' for x,y in ring.coords)+' Z' for ring in [polygon.exterior,*polygon.interiors]]
                    parts.append(f'<path d="{" ".join(rings)}" fill="{color}" fill-opacity=".3" fill-rule="evenodd"/>')
        for sample in references:
            x,y = sample['local']
            color = '#888' if not valid_reference(sample) else '#15803d' if sample['duration'] <= 900 else '#b91c1c'
            parts.append(f'<circle cx="{x:.2f}" cy="{-y:.2f}" r="12" fill="{color}"/>')
        panels.append(''.join(parts)+'</g>')
    (root/'comparison.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10500 3650">'
        +''.join(panels)+'<text x="100" y="3600" font-size="60">局部米制；蓝=算法可达，灰面=未知；绿点=参考可达，红点=参考不可达，灰点=无效参考。没有参考边界。</text></svg>',encoding='utf-8')
    lines = ['# 国定一社区三算法比较执行报告','',
             '本报告为独立验证点评价，不包含真实面积 IoU、边界 P95 或拓扑准确率。',
             f'预算预留：{sum(report["counts"].values())}/{report.get("total_limit",2000)}；停止原因：{report["halted"] or "无"}。','',
             '|阶段|状态|预算预留|确认发送|发送未确认|重试|完整响应|','|---|---|---:|---:|---:|---:|---:|']
    for name,state in report['stages'].items():
        counts = report['accounting'][name]
        lines.append(f'|{name}|{state["status"]}|'+ '|'.join(str(counts[k]) for k in
            ('reserved_attempts','actual_calls','unconfirmed_reservations','retries','complete_responses'))+'|')
    lines += ['', '详细时序、门槛、错误和指标见 result.json、stability.json、reference.json、metrics.json。',
              '未执行和失败方法保留占位记录；图中没有结果的面板不能视为有效空几何。',
              '单次固定顺序可能有时间漂移；不能从单社区结果得出普遍优劣。']
    (root/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


async def run_experiment(ledger, ak, *, inner, clock=None, gate=None, progress=None):
    clock = clock or Clock()
    ledger.clock = clock.time
    transport = ComparisonTransport(ledger, inner, clock=clock.time if gate is not None else time.perf_counter)
    shared_gate = gate or RateGate(3)
    protocol = make_protocol()
    dump(ledger.root/'protocol.json', protocol)  # Fixed before any request.
    ledger.start_once()
    report = {'stages':{p:{'status':'not_run'} for p in LIMITS}, 'metrics':[], 'protocol_sha256':digest(protocol)}
    results, references, stable_samples = {}, [], []
    def notify(message):
        if progress:
            progress(message)
    def begin(phase, batch=0, *, reset_deadline=True):
        if reset_deadline:
            ledger.deadline = clock.time()+600
        ledger.batch = transport.batch = batch
        transport.phase = phase
        report['stages'][phase] = {'status':'running'}
    async def observe(phase, points, batch=0, *, reset_deadline=True):
        begin(phase,batch,reset_deadline=reset_deadline)
        remaining = LIMITS[phase]-ledger.data['counts'][phase]
        if remaining <= 0 or ledger.deadline <= clock.time() or ledger.data['halted']:
            return []
        request_config = IsochroneRequest(ORIGIN,'bd09ll',budget=remaining,qps=3,concurrency=1,
            deadline_seconds=ledger.deadline-clock.time(),expand=False)
        scheduler = Scheduler(request_config,provider,ledger.token,clock=clock)
        observations = await scheduler.observe_many([tuple(p) for p in points])
        if scheduler.stop_reason in ('cancelled','upstream_failure'):
            ledger.data['halted'] = scheduler.stop_reason
            ledger.save()
        return [sample_record(o,batch=batch) for o in observations]
    async with httpx.AsyncClient(transport=transport,trust_env=False,follow_redirects=False) as client:
        provider = LimitedProvider(BaiduProvider(ak,client=client),shared_gate)
        try:
            stable_start = clock.time()
            begin('stability')
            for batch in range(1,17):
                if ledger.data['halted'] or ledger.token.cancelled or clock.time() >= ledger.deadline:
                    break
                stable_samples.extend(await observe('stability',protocol['stability_points'],batch,reset_deadline=False))
                dump(ledger.root/'stability-samples.json',stable_samples)
                notify({'stage':'stability','batch':batch,'calls':ledger.data['counts']['stability']})
            assessment = stability_assessment(stable_samples,ledger.data['events'],ledger.data['halted'])
            assessment['elapsed_seconds'] = clock.time()-stable_start
            dump(ledger.root/'stability.json',assessment)
            report['stages']['stability'] = {'status':'passed' if assessment['passed'] else 'failed',**assessment}
            if not assessment['passed']:
                ledger.data['halted'] = ledger.data['halted'] or 'stability_gate_failed'
            else:
                notify({'stage':'reference','status':'starting'})
                references = await observe('reference',[p['point'] for p in protocol['validation']])
                dump(ledger.root/'reference.json',references)
                valid = sum(valid_reference(s) for s in references)
                reference_passed = len(references) == 256 and valid >= 128 and not ledger.data['halted']
                report['stages']['reference'] = {'status':'passed' if reference_passed else 'failed','valid':valid,'count':len(references)}
                if not reference_passed:
                    ledger.data['halted'] = ledger.data['halted'] or 'reference_coverage_insufficient'
                else:
                    for method in protocol['order']:
                        if ledger.data['halted'] or ledger.token.cancelled:
                            break
                        notify({'stage':method,'status':'starting'})
                        begin(method)
                        start = clock.time()
                        request_config = IsochroneRequest(ORIGIN,'bd09ll',budget=400,qps=3,expand=False)
                        try:
                            if method == 'radial':
                                result = await compute_radial(request_config,provider,ledger.token,clock=clock)
                            else:
                                result = await compute_isochrone(request_config,provider,ledger.token,clock=clock,method=method)
                            results[method] = result
                            dump(ledger.root/f'{method}.json',result.to_dict())
                            report['stages'][method] = {'status':'failed' if result.stop_reason == 'geometry_error' or ledger.data['halted'] else 'completed',
                                'quality':result.quality,'stop_reason':result.stop_reason,'elapsed_seconds':clock.time()-start}
                            if result.stop_reason in ('cancelled','upstream_failure'):
                                ledger.data['halted'] = result.stop_reason
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            report['stages'][method] = {'status':'failed','reason':'algorithm_exception','elapsed_seconds':clock.time()-start}
                        notify({'stage':method,'status':report['stages'][method]['status'],'calls':ledger.data['counts'][method]})
        except asyncio.CancelledError:
            ledger.token.cancel()
            ledger.data['halted'] = 'cancelled'
        except Exception:
            ledger.data['halted'] = ledger.data['halted'] or 'experiment_error'
        finally:
            if ledger.token.cancelled:
                ledger.data['halted'] = ledger.data['halted'] or 'cancelled'
            for phase,state in report['stages'].items():
                if state['status'] == 'running':
                    state.update(status='failed',reason=ledger.data['halted'] or 'interrupted')
            sampled = {tuple(e['destination']) for e in ledger.data['events'] if e['phase'] in METHODS}
            for method in METHODS:
                result = results.get(method)
                geometry = result.local_geometry if result else None
                unknown = result.local_unknown if result else box(-1600,-1600,1600,1600)
                row = {'method':method, **report['stages'][method], **phase_accounting(ledger.data['events'],method),
                       'provider_failures':dict(result.statistics.failures) if result else {},
                       **point_metrics(geometry,unknown,references,sampled),
                       'unknown_area_fraction':unknown.area/(3200**2) if result and unknown is not None else None,
                       'components':len(multipolygon(geometry).geoms) if geometry is not None else None,
                       'holes':sum(len(p.interiors) for p in multipolygon(geometry).geoms) if geometry is not None else None}
                report['metrics'].append(row)
                if result is None:
                    dump(ledger.root/f'{method}.json',{'status':report['stages'][method]['status'],'isochrone':None})
            report.update(counts=dict(ledger.data['counts']),halted=ledger.data['halted'],
                          accounting={p:phase_accounting(ledger.data['events'],p) for p in ledger.phase_limits},
                          total_limit=ledger.total_limit,
                          finished_utc=datetime.now(timezone.utc).isoformat())
            ledger.save()
            write_artifacts(ledger.root,report,results,references)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-live',action='store_true',required=True)
    parser.parse_args()
    silence_transport_logs()
    try:
        settings = Settings()
        if not (settings.analysis_provider == 'baidu' and settings.ak_configured and settings.analysis_qps == 3):
            raise LiveGuardError('invalid_live_configuration')
        repo = Path(__file__).resolve().parents[2]
        def git(*args):
            return subprocess.check_output(['git','-C',str(repo),*args],text=True,stderr=subprocess.DEVNULL).strip()
        baseline = git('rev-parse',BASELINE+'^{commit}')
        if git('diff',BASELINE,'--','life-circle-algorithm','backend/app'):
            raise LiveGuardError('baseline_algorithm_or_service_changed')
        prior_specs = [(Ledger,'guodingyi-live-smoke'),(QpsLedger,'guodingyi-qps-review'),(QpsLedger,'guodingyi-qps-response-paced-review')]
        with ExitStack() as stack:
            prior = []
            for cls,name in prior_specs:
                path = Path('D:/CodexOutputs')/name
                if not (path/'ledger.json').exists():
                    raise LiveGuardError('missing_historical_ledger')
                prior.append(stack.enter_context(cls(path)))
            hashes = {str(p.file):hashlib.sha256(p.file.read_bytes()).hexdigest() for p in prior}
            ledger = stack.enter_context(ExperimentLedger(ROOT))
            if ledger.data.get('experiment_started') or sum(ledger.data['counts'].values()):
                raise LiveGuardError('experiment_already_started_no_resume')
            sources = [Path(__file__),repo/'backend/tools/live_smoke.py',repo/'backend/tools/qps_review.py',repo/'backend/app/analyses.py']
            dump(ROOT/'config.json',{'baseline':baseline,'head':git('rev-parse','HEAD'),'protocol':make_protocol(),
                'started_utc':datetime.now(timezone.utc).isoformat(),'source_sha256':{str(p.relative_to(repo)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
                'python':sys.version.split()[0],'packages':{p:version(p) for p in ('httpx','numpy','shapely','contourpy','fastapi')},
                'prior_ledgers':hashes})
            result = asyncio.run(run_experiment(ledger,settings.baidu_map_ak.get_secret_value(),
                inner=httpx.AsyncHTTPTransport(retries=0,trust_env=False),progress=lambda p:print(json.dumps(p),flush=True)))
            result['prior_ledgers_unchanged'] = all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in hashes.items())
            dump(ROOT/'result.json',result)
            print(json.dumps({'counts':result['counts'],'halted':result['halted'],'prior_ledgers_unchanged':result['prior_ledgers_unchanged']}))
    except Exception:
        raise SystemExit('Experiment stopped; sensitive error details suppressed. Inspect sanitized ledger.') from None


if __name__ == '__main__':
    main()
