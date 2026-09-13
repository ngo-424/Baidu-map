import asyncio
import json
import math
import time

import httpx
import pytest
from shapely.geometry import MultiPolygon, box

from app.analyses import RateGate
from tools.live_smoke import LiveGuardError, ORIGIN
from tools.live_comparison import (
    ExperimentLedger, ComparisonTransport, make_protocol, point_metrics,
    run_experiment, stability_assessment,
)


class FakeClock:
    def __init__(self):
        self.now = time.monotonic() + 10000

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


def request(destination='31.313077,121.516031'):
    return httpx.Request('GET', 'https://api.map.baidu.com/directionlite/v1/walking', params={
        'ak': 'fixture-never-save', 'origin': '31.313077,121.513926', 'destination': destination,
        'coord_type': 'bd09ll', 'ret_coordtype': 'bd09ll', 'steps_info': '1'})


def test_protocol_has_fixed_independent_strata_and_seed():
    a, b = make_protocol(), make_protocol()
    assert a == b
    assert a['limits'] == {'stability': 288, 'reference': 512, 'adaptive': 400, 'uniform': 400, 'radial': 400}
    assert sum(a['limits'].values()) == 2000
    assert len(a['validation']) == len({tuple(p['point']) for p in a['validation']}) == 256
    assert {tuple(p['cell']) for p in a['validation']} == {(x,y) for x in range(16) for y in range(16)}
    assert len(a['stability_points']) == 16
    assert sorted(a['order']) == ['adaptive', 'radial', 'uniform']


def test_ledger_budget_attempts_and_started_survive_restart(tmp_path):
    class Small(ExperimentLedger):
        phase_limits = {'stability': 2, 'reference': 2}
        total_limit = 3
    with Small(tmp_path) as ledger:
        ledger.start_once()
        ledger.reserve('stability', request(), 100)
        ledger.reserve('stability', request(), 101)
        with pytest.raises(LiveGuardError):
            ledger.reserve('stability', request('31.313077,121.511821'), 102)
        ledger.reserve('reference', request(), 102)
        with pytest.raises(LiveGuardError):
            ledger.reserve('reference', request(), 103)
    with Small(tmp_path) as ledger:
        assert sum(ledger.data['counts'].values()) == 3
        with pytest.raises(LiveGuardError):
            ledger.start_once()
    assert 'fixture-never-save' not in (tmp_path/'ledger.json').read_text()


@pytest.mark.parametrize('status,reason', [(401,'rate_limit'), (210,'permission'), (301,'quota'), (2,'invalid_parameter')])
def test_fatal_response_stops_before_next_send(tmp_path, status, reason):
    async def run():
        clock, calls = FakeClock(), []
        with ExperimentLedger(tmp_path) as ledger:
            def handle(req):
                calls.append(1)
                return httpx.Response(200, json={'status':status, 'message':'fixture-never-save'})
            guard = ComparisonTransport(ledger, httpx.MockTransport(handle), clock=clock.time)
            guard.phase = 'stability'
            await guard.handle_async_request(request())
            await clock.sleep(2)
            with pytest.raises(LiveGuardError):
                await guard.handle_async_request(request())
            assert ledger.data['halted'] == reason and len(calls) == 1
    asyncio.run(run())


def test_guard_blocks_response_gap_without_pacing(tmp_path):
    async def run():
        clock = FakeClock()
        with ExperimentLedger(tmp_path) as ledger:
            guard = ComparisonTransport(ledger, httpx.MockTransport(lambda _: httpx.Response(200,json={'status':7})), clock=clock.time)
            guard.phase = 'stability'
            await guard.handle_async_request(request())
            await clock.sleep(.32)
            with pytest.raises(LiveGuardError):
                await guard.handle_async_request(request())
            assert ledger.data['counts']['stability'] == 1
            assert ledger.data['halted'] == 'response_gap_violation'
    asyncio.run(run())


def test_same_position_attempt_cap_is_per_phase_and_batch(tmp_path):
    with ExperimentLedger(tmp_path) as ledger:
        ledger.reserve('stability', request(), 100)
        ledger.reserve('stability', request(), 101)
        with pytest.raises(LiveGuardError):
            ledger.reserve('stability', request(), 102)
        ledger.batch = 1
        ledger.reserve('stability', request(), 103)
        assert ledger.data['counts']['stability'] == 3


def test_cancel_inflight_prevents_late_success_and_more_sends(tmp_path):
    async def run():
        clock, started, release = FakeClock(), asyncio.Event(), asyncio.Event()
        with ExperimentLedger(tmp_path) as ledger:
            async def handle(req):
                started.set()
                await release.wait()
                return httpx.Response(200,json={'status':7})
            guard = ComparisonTransport(ledger,httpx.MockTransport(handle),clock=clock.time)
            guard.phase = 'stability'
            task = asyncio.create_task(guard.handle_async_request(request()))
            await started.wait()
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            release.set()
            await clock.sleep(2)
            with pytest.raises(LiveGuardError):
                await guard.handle_async_request(request())
            assert ledger.data['halted'] == 'cancelled'
            assert ledger.data['events'][0]['outcome'] == 'cancelled_in_flight'
            assert ledger.data['counts']['stability'] == 1
    asyncio.run(run())


def test_deadline_is_checked_before_transport_reservation(tmp_path):
    with ExperimentLedger(tmp_path) as ledger:
        ledger.clock = lambda:100
        ledger.deadline = 100
        with pytest.raises(LiveGuardError):
            ledger.reserve('stability',request(),100)
        assert ledger.data['counts']['stability'] == 0


def test_failed_attempt_and_retry_both_count(tmp_path):
    async def run():
        clock, calls = FakeClock(), []
        with ExperimentLedger(tmp_path) as ledger:
            def handle(req):
                calls.append(1)
                if len(calls) == 1:
                    raise httpx.ReadTimeout('fixture-never-save',request=req)
                return httpx.Response(200,json={'status':7})
            guard = ComparisonTransport(ledger,httpx.MockTransport(handle),clock=clock.time)
            guard.phase = 'reference'
            with pytest.raises(httpx.ReadTimeout):
                await guard.handle_async_request(request())
            await clock.sleep(1)
            await guard.handle_async_request(request())
            assert ledger.data['counts']['reference'] == len(calls) == 2
            assert [e['attempt'] for e in ledger.data['events']] == [1,2]
            assert 'fixture-never-save' not in ledger.file.read_text()
    asyncio.run(run())


def test_ledger_write_failure_stops_before_network_and_persists_halt(tmp_path, monkeypatch):
    import tools.live_smoke as smoke

    async def run():
        clock, calls = FakeClock(), []
        with ExperimentLedger(tmp_path) as ledger:
            original_dump = smoke.dump
            writes = [0]
            def fail_once(path, data):
                writes[0] += 1
                if writes[0] == 3:
                    raise PermissionError('fixture-never-save')
                return original_dump(path,data)
            monkeypatch.setattr(smoke,'dump',fail_once)
            def handle(req):
                calls.append(1)
                return httpx.Response(200,json={'status':7})
            guard = ComparisonTransport(ledger,httpx.MockTransport(handle),clock=clock.time)
            guard.phase = 'adaptive'
            with pytest.raises(LiveGuardError):
                await guard.handle_async_request(request())
            assert ledger.data['halted'] == 'ledger_write_failed'
            await clock.sleep(2)
            with pytest.raises(LiveGuardError):
                await guard.handle_async_request(request())
            assert calls == [] and ledger.data['counts']['adaptive'] == 1
            assert json.loads(ledger.file.read_text())['halted'] == 'ledger_write_failed'
    asyncio.run(run())


def test_budget_reservation_is_not_reported_as_confirmed_send():
    from tools.live_comparison import phase_accounting
    counts = phase_accounting([
        {'phase':'adaptive','attempt':1,'outcome':'dispatched'},
        {'phase':'adaptive','attempt':1,'dispatchPerf':100,'outcome':'success'},
        {'phase':'reference','attempt':1,'dispatchPerf':101,'outcome':'success'},
    ],'adaptive')
    assert counts['reserved_attempts'] == 2
    assert counts['actual_calls'] == 1
    assert counts['unconfirmed_reservations'] == 1


def test_accounting_complete_responses_are_not_timeout_completions():
    from tools.live_comparison import phase_accounting
    rows = [{'phase':'adaptive','attempt':1},
            {'phase':'adaptive','attempt':1,'dispatchPerf':1,'responsePerf':2,'outcome':'timeout'},
            {'phase':'adaptive','attempt':2,'dispatchPerf':3,'responsePerf':4,'http_status':200}]
    actual = phase_accounting(rows,'adaptive')
    assert actual['complete_responses'] == 1
    assert actual['retries'] == 1


def test_offline_audit_preserves_sources_and_csv_matches_json(tmp_path):
    import csv
    from tools.comparison_audit import audit, file_hash
    source,output=tmp_path/'source',tmp_path/'audit'
    source.mkdir()
    (source/'ledger.json').write_text(json.dumps({'counts':{'adaptive':2},'events':[
        {'phase':'adaptive'}, {'phase':'adaptive','dispatchPerf':1,'http_status':200}]}))
    (source/'metrics.json').write_text(json.dumps([{'method':'adaptive','actual_calls':2,'miss_rate':.1}]))
    before={p.name:file_hash(p) for p in source.iterdir()}
    result=audit(source,output)
    assert result['reserved_attempts']==2 and result['actual_calls']==1
    assert before=={p.name:file_hash(p) for p in source.iterdir()}
    row=json.loads((output/'metrics.json').read_text())[0]
    with (output/'metrics.csv').open(encoding='utf-8-sig') as f: csvrow=next(csv.DictReader(f))
    for key in ('actual_calls','reserved_attempts','complete_responses'):
        assert int(csvrow[key])==row[key]
    assert '|adaptive|2|1|1|0|1|' in (output/'report.md').read_text(encoding='utf-8')


def test_start_marker_prevents_restart_even_if_ledger_write_fails(tmp_path, monkeypatch):
    with ExperimentLedger(tmp_path) as ledger:
        monkeypatch.setattr(ledger,'save',lambda: (_ for _ in ()).throw(LiveGuardError('fixture')))
        with pytest.raises(LiveGuardError):
            ledger.start_once()
    with ExperimentLedger(tmp_path) as ledger:
        with pytest.raises(LiveGuardError):
            ledger.start_once()


@pytest.mark.parametrize('persistent',[False,True])
def test_response_write_failure_blocks_future_sends_and_restart(tmp_path,monkeypatch,persistent):
    import tools.live_smoke as smoke
    async def run():
        clock, calls = FakeClock(), []
        with ExperimentLedger(tmp_path) as ledger:
            ledger.start_once()
            original = smoke.dump
            failed = []
            def fail(path,data):
                if data['events'] and 'http_status' in data['events'][-1] and (persistent or not failed):
                    failed.append(1)
                    raise PermissionError('fixture-never-save')
                original(path,data)
            monkeypatch.setattr(smoke,'dump',fail)
            def handle(req):
                calls.append(1)
                return httpx.Response(200,json={'status':7})
            guard=ComparisonTransport(ledger,httpx.MockTransport(handle),clock=clock.time)
            guard.phase='adaptive'
            with pytest.raises(LiveGuardError):
                await guard.handle_async_request(request())
            await clock.sleep(2)
            with pytest.raises(LiveGuardError):
                await guard.handle_async_request(request())
            assert len(calls)==1 and ledger.data['halted']=='ledger_write_failed'
            assert ledger.data['counts']['adaptive']==1
        monkeypatch.setattr(smoke,'dump',original)
        with ExperimentLedger(tmp_path) as ledger:
            assert ledger.data['counts']['adaptive']==1
            with pytest.raises(LiveGuardError): ledger.start_once()
    asyncio.run(run())


def test_point_scoring_unknown_misses_null_empty_and_overlap():
    refs = [
        {'point':[0,0], 'local':[0,0], 'duration':10,'endpoint_verified':True},
        {'point':[1,0], 'local':[1,0], 'duration':10,'endpoint_verified':True},
        {'point':[2,0], 'local':[2,0], 'duration':1000,'endpoint_verified':True},
        {'point':[3,0], 'local':[3,0], 'duration':None,'endpoint_verified':False},
    ]
    score = point_metrics(box(-.5,-1,2.5,1), box(.5,-1,1.5,1), refs, set())
    assert score['valid_reference_count'] == 3
    assert score['miss_rate'] == .5 and score['false_inclusion_rate'] == .5
    assert score['unknown_point_fraction'] == pytest.approx(1/3)
    null = point_metrics(None, MultiPolygon(), refs, set())
    assert null['unknown_point_fraction'] == 1 and null['miss_rate'] == 1
    assert null['false_inclusion_rate'] is None
    empty = point_metrics(MultiPolygon(), MultiPolygon(), refs, set())
    assert empty['unknown_point_fraction'] == 0 and empty['miss_rate'] == 1
    filtered = point_metrics(None, MultiPolygon(), refs, {(0,0),(1,0)})
    assert filtered['excluded_overlap_count'] == 2 and filtered['miss_rate'] is None


def test_stability_requires_complete_observations_and_zero_rate_limits():
    sample = {'duration':10,'endpoint_verified':True,'point':[1,1], 'attempts':1}
    events = [{'dispatchPerf':i*.5, 'responsePerf':i*.5+.1, 'outcome':'success', 'endpoint_verified':True} for i in range(256)]
    assert stability_assessment([sample]*256, events, None)['passed']
    assert not stability_assessment([sample]*255, events, None)['passed']
    events[1]['outcome'] = 'rate_limit'
    assert not stability_assessment([sample]*256, events, None)['passed']


@pytest.mark.parametrize('mode', ['success','limited','bad_reference'])
def test_offline_entire_pipeline_and_stop_gates(tmp_path, mode, monkeypatch):
    async def run():
        clock, requests_by_phase = FakeClock(), {}
        with ExperimentLedger(tmp_path) as ledger:
            # Disk durability has dedicated tests; avoid thousands of full-ledger
            # fsyncs in the purely offline end-to-end algorithm exercise.
            monkeypatch.setattr(ledger, 'save', lambda: None)
            def handle(req):
                phase = next(k for k in ledger.data['counts'] if ledger.data['events'][-1]['phase'] == k)
                requests_by_phase[phase] = requests_by_phase.get(phase,0)+1
                if mode == 'limited':
                    return httpx.Response(200,json={'status':401})
                if mode == 'bad_reference' and phase == 'reference':
                    return httpx.Response(200,json={'status':7})
                def point(key):
                    lat,lng = map(float,req.url.params[key].split(','))
                    return {'lng':lng,'lat':lat}
                start, end = point('origin'), point('destination')
                from life_circle.coordinates import LocalProjection
                x,y = LocalProjection(ORIGIN).to_local((end['lng'],end['lat']))
                return httpx.Response(200,json={'status':0,'result':{'routes':[{'duration':math.hypot(x,y)/1.2,
                    'steps':[{'start_location':start,'end_location':end}]}]}})
            result = await run_experiment(ledger, 'fixture-never-save', inner=httpx.MockTransport(handle),
                clock=clock, gate=RateGate(3, clock=clock.time, sleep=clock.sleep))
            if mode == 'limited':
                assert requests_by_phase == {'stability':1}
                assert result['stages']['reference']['status'] == 'not_run'
            elif mode == 'bad_reference':
                assert set(requests_by_phase) == {'stability','reference'}
                assert result['stages']['reference']['status'] == 'failed'
            else:
                assert requests_by_phase['stability'] == 256  # No cache across repetitions.
                assert requests_by_phase['reference'] == 256
                assert all(result['stages'][m]['status'] == 'completed' for m in ('adaptive','uniform','radial'))
                assert len(result['metrics']) == 3
                assert all(r['valid_reference_count'] >= 128 for r in result['metrics'])
                assert (tmp_path/'comparison.svg').exists()
            assert sum(ledger.data['counts'].values()) <= 2000
        assert 'fixture-never-save' not in ''.join(p.read_text(encoding='utf-8') for p in tmp_path.glob('*.json'))
    asyncio.run(run())
