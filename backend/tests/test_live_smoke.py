import asyncio
import json

import httpx
import pytest

from tools.live_smoke import Ledger, GuardedTransport, LiveGuardError


def test_specialized_ledger_uses_its_own_phase_budget(tmp_path):
    class ReviewLedger(Ledger):
        phase_limits = {'qps': 2}
        total_limit = 2
    with ReviewLedger(tmp_path) as ledger:
        ledger.reserve('qps', request(), 100)
        ledger.reserve('qps', request(), 101)
    with ReviewLedger(tmp_path) as ledger:
        assert ledger.data['counts'] == {'qps': 2}
        with pytest.raises(LiveGuardError):
            ledger.reserve('qps', request(), 102)


def test_transport_rechecks_time_after_early_wakeup(tmp_path):
    async def run():
        with Ledger(tmp_path) as ledger:
            now, sends = [100.0], []
            async def sleep(delay):
                now[0] += delay - .01 if delay > .02 else delay
            def handler(req):
                sends.append(now[0])
                return httpx.Response(200, json={'status': 0, 'result': {'routes': []}})
            guard = GuardedTransport(ledger, httpx.MockTransport(handler), clock=lambda: now[0], sleep=sleep)
            guard.phase = 'smoke'
            for _ in range(5):
                await guard.handle_async_request(request())
            assert all(b-a >= 1/3-1e-9 for a,b in zip(sends,sends[1:]))
    asyncio.run(run())


def test_resume_reparses_recorded_endpoints_and_preserves_budget(tmp_path):
    from pathlib import Path
    from tools.live_smoke import endpoint_structure, ORIGIN, diagnostic_destination
    payload = json.loads((Path(__file__).resolve().parents[2] / 'life-circle-algorithm/tests/fixtures/guodingyi_endpoint_strings.json').read_text())
    with Ledger(tmp_path) as ledger:
        ledger.data.update(halted='no_verified_route', map_checked=True, smoke_started=True)
        ledger.data['counts']['smoke'] = 9
        ledger.data['events'] = [{'id': 9, 'diagnostic': True, 'phase': 'smoke', 'http_status': 200,
            'baidu_status': 0, 'origin': list(ORIGIN), 'destination': list(diagnostic_destination()),
            'duration': 367, 'endpoint_structure': [endpoint_structure(payload['result']['routes'][0]['steps'])]}]
        ledger.resume_after_endpoint_fix()
        assert ledger.data['counts'] == {'smoke': 9, 'cancel': 0, 'analysis': 0}
        assert ledger.data['halted'] is None and ledger.data['smoke_passed']
        assert ledger.data['resumptions'][0]['previous_halt'] == 'no_verified_route'
        with pytest.raises(LiveGuardError):
            ledger.resume_after_endpoint_fix()
    with Ledger(tmp_path) as ledger:
        assert ledger.data['counts']['smoke'] == 9


@pytest.mark.parametrize('halt', ['permission', 'quota', 'no_verified_route'])
def test_resume_requires_real_verified_evidence(tmp_path, halt):
    with Ledger(tmp_path) as ledger:
        ledger.data['halted'] = halt
        with pytest.raises(LiveGuardError):
            ledger.resume_after_endpoint_fix()
        assert ledger.data['halted'] == halt and not ledger.data['smoke_passed']


def test_one_diagnostic_uses_remaining_budget_without_clearing_stop(tmp_path):
    from tools.live_smoke import diagnostic_destination
    req = request()
    req.url = req.url.copy_set_param('destination', f'{diagnostic_destination()[1]},{diagnostic_destination()[0]}')
    with Ledger(tmp_path) as ledger:
        ledger.reserve('smoke', req, 100)
        ledger.data['halted'] = 'no_verified_route'
        ledger.reserve('smoke', req, 101, diagnostic=True)
        assert ledger.data['counts']['smoke'] == 2
        assert ledger.data['halted'] == 'no_verified_route'
        with pytest.raises(LiveGuardError):
            ledger.reserve('smoke', req, 102, diagnostic=True)
        with pytest.raises(LiveGuardError):
            ledger.reserve('analysis', req, 102)
    with Ledger(tmp_path) as ledger:
        with pytest.raises(LiveGuardError):
            ledger.reserve('smoke', req, 103, diagnostic=True)


@pytest.mark.parametrize('halt,phase', [('permission', 'smoke'), ('quota', 'smoke'), ('no_verified_route', 'analysis')])
def test_diagnostic_cannot_bypass_other_stops(tmp_path, halt, phase):
    with Ledger(tmp_path) as ledger:
        ledger.data['halted'] = halt
        with pytest.raises(LiveGuardError):
            ledger.reserve(phase, request(), 100, diagnostic=True)
        assert sum(ledger.data['counts'].values()) == 0


def test_endpoint_structure_preserves_numeric_types_without_untrusted_text():
    from tools.live_smoke import endpoint_structure
    value = {'start_location': {'lng': '121.5139', 'lat': 31.3130, 'ak': 'fixture-secret'},
        'end_location': {'lng': 'https://example.invalid/?ak=fixture-secret', 'lat': None},
        'instruction': 'fixture-secret'}
    result = endpoint_structure(value)
    assert result['fields']['start_location']['fields']['lng'] == {'type': 'str', 'value': '121.5139'}
    assert result['fields']['start_location']['fields']['lat']['type'] == 'float'
    assert 'fixture-secret' not in json.dumps(result)
    assert 'https' not in json.dumps(result)
    assert endpoint_structure(10**400) == {'type': 'int'}


def test_smoke_retries_are_counted_and_cannot_repeat(tmp_path, monkeypatch):
    from tools import live_smoke as live
    from app.config import Settings
    async def run():
        with Ledger(tmp_path) as ledger:
            sends = []
            def handler(req):
                sends.append(1)
                if len(sends) == 1:
                    raise httpx.ReadTimeout('fixture-never-save', request=req)
                def endpoint(key):
                    lat, lng = map(float, req.url.params[key].split(','))
                    return {'lat': lat, 'lng': lng}
                return httpx.Response(200, json={'status': 0, 'result': {'routes': [
                    {'duration': 123, 'steps': [{'start_location': endpoint('origin'), 'end_location': endpoint('destination')}]}]}})
            session = live.Session(ledger, tmp_path / 'run', Settings(_env_file=None, baidu_map_ak='fixture-never-save', analysis_qps=3),
                transport=httpx.MockTransport(handler))
            assert (await session.smoke()).status_code == 409
            assert not sends
            from time import monotonic
            class Clock:
                value = monotonic()
                def time(self): return self.value
                async def sleep(self, n): self.value += n
            clock = Clock()
            scheduler = live.Scheduler
            monkeypatch.setattr(live, 'Scheduler', lambda *args: scheduler(*args, clock=clock))
            session.guard.clock, session.guard.sleep = clock.time, clock.sleep
            ledger.data['map_checked'] = True
            result = await session.smoke()
            assert result['passed'] and len(sends) == 9
            assert ledger.data['counts']['smoke'] == 9
            assert result['samples'][0]['attempts'] == 2
            assert (await session.smoke()).status_code == 409
            assert len(sends) == 9
            await session.client.aclose()
    asyncio.run(run())


def test_api_cancel_at_first_send_preserves_terminal_state(tmp_path):
    from tools.live_smoke import Session, build_app, ORIGIN
    from app.config import Settings
    async def run():
        with Ledger(tmp_path) as ledger:
            sent, release = asyncio.Event(), asyncio.Event()
            async def handler(req):
                sent.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
                return httpx.Response(200, json={'status': 0, 'result': {'routes': []}})
            session = Session(ledger, tmp_path / 'run', Settings(_env_file=None, baidu_map_ak='fixture-never-save', analysis_qps=3),
                transport=httpx.MockTransport(handler))
            app = build_app(session, 'fixture-browser')
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    body = {'center': {'lng': ORIGIN[0], 'lat': ORIGIN[1]}, 'coordinateSystem': 'bd09ll', 'budget': 200, 'clientRequestId': 'cancel-test'}
                    assert (await client.post('/api/analyses', json=body)).status_code == 409
                    ledger.data['smoke_passed'] = True
                    created = await client.post('/api/analyses', json=body)
                    assert created.status_code == 202
                    task_id = created.json()['taskId']
                    await asyncio.wait_for(sent.wait(), 2)
                    assert (await client.post(f'/api/analyses/{task_id}/cancel')).status_code == 202
                    release.set()
                    await asyncio.wait_for(app.state.analyses.jobs[task_id].task, 2)
                    final = (await client.post('/live/finish-cancel')).json()
                    assert final['passed'] and final['late_sends'] == 0
                    assert ledger.data['counts']['cancel'] == 1
                    assert not any(j.task and not j.task.done() for j in app.state.analyses.jobs.values())
                    assert (await client.get(f'/api/analyses/{task_id}')).json()['status'] == 'cancelled'
    asyncio.run(run())


def request():
    return httpx.Request('GET', 'https://api.map.baidu.com/directionlite/v1/walking', params={
        'ak': 'fixture-never-save', 'origin': '31.313077,121.513926',
        'destination': '31.313077,121.516000', 'coord_type': 'bd09ll',
        'ret_coordtype': 'bd09ll', 'steps_info': '1'})


def test_persistent_budget_restart_and_secret_exclusion(tmp_path):
    async def run():
        for iteration in range(2):
            with Ledger(tmp_path) as ledger:
                clock = [100.0]
                async def sleep(n):
                    clock[0] += n
                transport = GuardedTransport(ledger, httpx.MockTransport(lambda _: httpx.Response(200, json={'status': 0, 'result': {'routes': []}})),
                    clock=lambda: clock[0], sleep=sleep)
                transport.phase = 'cancel'
                for _ in range(2):
                    response = await transport.handle_async_request(request())
                    await response.aclose()
                if iteration:
                    with pytest.raises(LiveGuardError):
                        await transport.handle_async_request(request())
        text = (tmp_path / 'ledger.json').read_text()
        assert 'fixture-never-save' not in text and '?ak=' not in text
        assert json.loads(text)['counts']['cancel'] == 4
    asyncio.run(run())


def test_permission_stops_before_second_send(tmp_path):
    async def run():
        with Ledger(tmp_path) as ledger:
            calls = []
            def handler(_):
                calls.append(1)
                return httpx.Response(200, json={'status': 210, 'message': 'fixture-never-save'})
            guard = GuardedTransport(ledger, httpx.MockTransport(handler))
            guard.phase = 'smoke'
            await guard.handle_async_request(request())
            with pytest.raises(LiveGuardError):
                await guard.handle_async_request(request())
            assert len(calls) == 1
            assert ledger.data['halted'] == 'permission'
            assert 'fixture-never-save' not in (tmp_path / 'ledger.json').read_text()
    asyncio.run(run())


def test_cancel_and_qps_prevent_extra_sends(tmp_path):
    async def run():
        with Ledger(tmp_path) as ledger:
            now, sends = [100.0], []
            async def sleep(n):
                now[0] += n
            def handler(_):
                sends.append(now[0])
                return httpx.Response(200, json={'status': 0, 'result': {'routes': []}})
            guard = GuardedTransport(ledger, httpx.MockTransport(handler), clock=lambda: now[0], sleep=sleep)
            guard.phase = 'smoke'
            await asyncio.gather(*(guard.handle_async_request(request()) for _ in range(3)))
            assert all(b-a >= 1/3-1e-9 for a,b in zip(sends,sends[1:]))
            guard.cancelled = True
            with pytest.raises(LiveGuardError):
                await guard.handle_async_request(request())
            assert len(sends) == 3
    asyncio.run(run())


def test_errors_are_counted_without_sensitive_exception_text(tmp_path):
    async def run():
        with Ledger(tmp_path) as ledger:
            def handler(req):
                raise httpx.ReadTimeout('fixture-never-save', request=req)
            guard = GuardedTransport(ledger, httpx.MockTransport(handler))
            guard.phase = 'smoke'
            with pytest.raises(httpx.ReadTimeout):
                await guard.handle_async_request(request())
            assert ledger.data['counts']['smoke'] == 1
            assert ledger.data['events'][0]['outcome'] == 'timeout'
            assert 'fixture-never-save' not in (tmp_path / 'ledger.json').read_text()
    asyncio.run(run())


def test_same_output_rejects_concurrent_owner(tmp_path):
    with Ledger(tmp_path):
        with pytest.raises(LiveGuardError):
            with Ledger(tmp_path):
                pass


def test_total_and_failed_ledger_are_fail_closed(tmp_path):
    with Ledger(tmp_path) as ledger:
        ledger.data['counts'] = {'smoke': 16, 'cancel': 4, 'analysis': 200}
        ledger.save()
        with pytest.raises(LiveGuardError):
            ledger.reserve('analysis', request(), 100)
    (tmp_path / 'ledger.json').write_text('{broken', encoding='utf-8')
    with pytest.raises(LiveGuardError):
        with Ledger(tmp_path):
            pass
