"""Explicit 20-attempt live QPS review; no extra pacing that could hide a defect."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone

import httpx

from app.analyses import LimitedProvider, RateGate
from app.baidu import silence_transport_logs
from app.config import Settings
from life_circle.coordinates import LocalProjection, normalize
from life_circle.models import IsochroneRequest, CancelToken
from life_circle.providers import BaiduProvider
from life_circle.scheduler import Scheduler
from tools.live_smoke import Ledger, GuardedTransport, LiveGuardError, ORIGIN, dump


class QpsLedger(Ledger):
    phase_limits = {'qps': 20}
    total_limit = 20


def review_root(round_name):
    names = {'original': 'guodingyi-qps-review', 'response-paced': 'guodingyi-qps-response-paced-review'}
    if round_name not in names:
        raise ValueError('unknown_review_round')
    return Path('D:/CodexOutputs') / names[round_name]


class AuditedTransport(GuardedTransport):
    def __init__(self, ledger, inner, *, clock=time.perf_counter):
        owner = self
        class Probe(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                # No awaits between the clock check and the transport handoff.
                event = ledger.data['events'][-1]
                event['batch'] = owner.batch
                sent = [e['dispatchPerf'] for e in ledger.data['events'] if 'dispatchPerf' in e]
                now = clock()
                if sent and now - sent[-1] < 1/3 - 1e-9:
                    event.update(guard_blocked=True, candidate_gap=now-sent[-1])
                    ledger.data['halted'] = 'qps_gap_violation'
                    raise LiveGuardError('qps_gap_violation')
                if sum(now-1 < t <= now for t in sent) >= 3:
                    event['guard_blocked'] = True
                    ledger.data['halted'] = 'qps_window_violation'
                    raise LiveGuardError('qps_window_violation')
                event['dispatchPerf'] = now
                try:
                    response = await inner.handle_async_request(request)
                    await response.aread()
                    return response
                finally:
                    event['responsePerf'] = clock()

            async def aclose(self):
                await inner.aclose()
        super().__init__(ledger, Probe(), clock=clock, enforce_spacing=False)
        self.phase, self.batch = 'qps', 1


def metrics(events):
    sent = [e for e in events if 'dispatchPerf' in e]
    times = [e['dispatchPerf'] for e in sent]
    gaps = [b-a for a,b in zip(times,times[1:])]
    cross = [b['dispatchPerf']-a['dispatchPerf'] for a,b in zip(sent,sent[1:]) if a.get('batch') != b.get('batch')]
    response_gaps = [b['dispatchPerf']-a['responsePerf'] for a,b in zip(sent,sent[1:]) if 'responsePerf' in a]
    inflight = max((sum(e['dispatchPerf'] <= t < e.get('responsePerf', float('inf')) for e in sent) for t in times), default=0)
    return {'actual_handoffs': len(sent), 'guard_blocks': sum(bool(e.get('guard_blocked')) for e in events),
        'max_inflight': inflight, 'minimum_response_gap': min(response_gaps, default=None),
        'minimum_gap': min(gaps, default=None), 'cross_batch_gap': min(cross, default=None),
        'max_in_one_second': max((sum(t <= s < t+1 for s in times) for t in times), default=0),
        'http_status': dict(Counter(str(e.get('http_status')) for e in sent)),
        'baidu_status': dict(Counter(str(e.get('baidu_status')) for e in sent)),
        'outcomes': dict(Counter(e.get('outcome') for e in sent)),
        'verified_routes': sum(bool(e.get('endpoint_verified')) for e in sent)}


async def review(ledger, settings):
    transport = AuditedTransport(ledger, httpx.AsyncHTTPTransport(retries=0, trust_env=False))
    gate = RateGate(3)
    batches = []
    projection = LocalProjection(ORIGIN)
    async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as client:
        provider = LimitedProvider(BaiduProvider(settings.baidu_map_ak.get_secret_value(), client=client), gate)
        for index, distances in enumerate(((200, 600), (300, 500)), 1):
            if ledger.data['halted']:
                break
            transport.batch = index
            positions = [normalize(projection.to_geographic(p)) for d in distances for p in ((d,0),(-d,0),(0,d),(0,-d))]
            request = IsochroneRequest(ORIGIN, 'bd09ll', budget=10, qps=3, concurrency=2, deadline_seconds=60)
            scheduler = Scheduler(request, provider, CancelToken())
            observations = await scheduler.observe_many(positions)
            batches.append({'batch': index, 'logical_attempts': scheduler.stats.requests, 'retries': scheduler.stats.retries,
                'stop_reason': scheduler.stop_reason, 'samples': [{'destination': o.destination, 'duration': o.duration,
                    'reason': o.reason, 'endpoint_verified': o.endpoint_verified, 'attempts': o.attempts} for o in observations]})
    measured = metrics(ledger.data['events'])
    passed = (len(batches) == 2 and measured['actual_handoffs'] >= 16 and measured['guard_blocks'] == 0
        and measured['max_in_one_second'] <= 3 and measured['minimum_gap'] >= 1/3-1e-9
        and measured['max_inflight'] <= 1 and measured['minimum_response_gap'] is not None
        and measured['minimum_response_gap'] >= 1/3-1e-9
        and all(o['duration'] is not None and o['endpoint_verified'] for b in batches for o in b['samples'])
        and not ledger.data['halted'] and measured['outcomes'].get('rate_limit', 0) == 0)
    return {'passed': passed, 'scope': 'small_sample_production_scheduler_and_shared_gate', 'limit': 20,
        'reserved_attempts': ledger.data['counts']['qps'], 'metrics': measured, 'batches': batches,
        'halted': ledger.data['halted'], 'finished_utc': datetime.now(timezone.utc).isoformat()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-live', action='store_true', required=True)
    parser.add_argument('--round', choices=('original', 'response-paced'), default='original')
    args = parser.parse_args()
    root = review_root(args.round)
    previous_root = Path('D:/CodexOutputs/guodingyi-live-smoke')
    silence_transport_logs()
    try:
        settings = Settings()
        assert settings.analysis_provider == 'baidu' and settings.ak_configured and settings.analysis_qps == 3
        assert previous_root.joinpath('ledger.json').exists()
        with ExitStack() as stack:
            previous = stack.enter_context(Ledger(previous_root))
            prior = [previous]
            if args.round == 'response-paced':
                prior.append(stack.enter_context(QpsLedger(review_root('original'))))
            prior_hashes = {str(p.file): hashlib.sha256(p.file.read_bytes()).hexdigest() for p in prior}
            ledger = stack.enter_context(QpsLedger(root))
            if ledger.data.get('review_started') or sum(ledger.data['counts'].values()):
                raise LiveGuardError('review_already_started_no_budget_reset')
            previous_hash = hashlib.sha256(previous.file.read_bytes()).hexdigest()
            sources = [Path(__file__), Path(__file__).parents[1]/'app/analyses.py',
                Path(__file__).parents[2]/'life-circle-algorithm/src/life_circle/scheduler.py']
            dump(root/'config.json', {'started_utc': datetime.now(timezone.utc).isoformat(), 'limit': 20, 'qps': 3,
                'round': args.round, 'effective_concurrency': 1, 'prior_ledger_sha256': prior_hashes,
                'origin': ORIGIN, 'previous_round_counts': previous.data['counts'], 'previous_ledger_sha256': previous_hash,
                'clock_resolution': time.get_clock_info('monotonic').resolution,
                'measurement_resolution': time.get_clock_info('perf_counter').resolution,
                'source_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}})
            ledger.data['review_started'] = True
            ledger.save()
            result = asyncio.run(review(ledger, settings))
            result['previous_ledger_unchanged'] = hashlib.sha256(previous.file.read_bytes()).hexdigest() == previous_hash
            result['all_prior_ledgers_unchanged'] = all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p,h in prior_hashes.items())
            dump(root/'result.json', result)
            print(json.dumps(result, ensure_ascii=True))
    except Exception:
        raise SystemExit('QPS review stopped; sensitive details suppressed. Check sanitized ledger.') from None


if __name__ == '__main__':
    main()
