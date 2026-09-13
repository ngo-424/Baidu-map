import asyncio
import json

import httpx
import pytest

from tools.live_smoke import LiveGuardError
from tools.qps_review import QpsLedger, AuditedTransport, metrics, review_root


def request():
    return httpx.Request('GET', 'https://api.map.baidu.com/directionlite/v1/walking', params={
        'ak': 'fixture-never-save', 'origin': '31.313077,121.513926', 'destination': '31.313077,121.516031',
        'coord_type': 'bd09ll', 'ret_coordtype': 'bd09ll', 'steps_info': '1'})


def test_review_does_not_pace_and_blocks_a_premature_send(tmp_path):
    async def run():
        with QpsLedger(tmp_path) as ledger:
            now, calls = [100.0], []
            def handler(req):
                calls.append(now[0]); return httpx.Response(200, json={'status': 0, 'result': {'routes': []}})
            transport = AuditedTransport(ledger, httpx.MockTransport(handler), clock=lambda: now[0])
            await transport.handle_async_request(request())
            now[0] += .328
            with pytest.raises(LiveGuardError):
                await transport.handle_async_request(request())
            assert len(calls) == 1 and now[0] == 100.328
            assert ledger.data['halted'] == 'qps_gap_violation'
            assert ledger.data['counts']['qps'] == 2  # Conservative pre-send reservation.
    asyncio.run(run())


def test_review_budget_retry_and_secret_exclusion_survive_restart(tmp_path):
    async def run():
        for batch in range(2):
            with QpsLedger(tmp_path) as ledger:
                now = [100.0+batch*10]
                def handler(req):
                    return httpx.Response(200, json={'status': 401, 'message': 'fixture-never-save'})
                transport = AuditedTransport(ledger, httpx.MockTransport(handler), clock=lambda: now[0])
                for _ in range(10):
                    now[0] += .35
                    await transport.handle_async_request(request())
                    ledger.data['halted'] = None  # Test budget independently of consecutive-failure stopping.
                    ledger.save()
                if batch:
                    with pytest.raises(LiveGuardError):
                        await transport.handle_async_request(request())
                assert ledger.data['counts']['qps'] == (batch+1)*10
        assert 'fixture-never-save' not in (tmp_path/'ledger.json').read_text()
    asyncio.run(run())


def test_metrics_include_cross_batch_window_and_guard_block():
    events = [{'dispatchPerf': t, 'batch': 1 if i < 2 else 2} for i,t in enumerate([100.,100.34,100.68,101.02])]
    assert metrics(events)['max_in_one_second'] == 3
    assert metrics(events)['cross_batch_gap'] == pytest.approx(.34)
    assert metrics(events+[{'guard_blocked': True}])['guard_blocks'] == 1


def test_fixed_review_rounds_do_not_reuse_or_reset_old_ledger():
    assert review_root('original').name == 'guodingyi-qps-review'
    assert review_root('response-paced').name == 'guodingyi-qps-response-paced-review'
    with pytest.raises(ValueError):
        review_root('arbitrary-new-budget')


def test_metrics_measure_inflight_and_response_to_next_send():
    events = [{'dispatchPerf': 100., 'responsePerf': 100.2},
              {'dispatchPerf': 100.54, 'responsePerf': 100.6}]
    assert metrics(events)['max_inflight'] == 1
    assert metrics(events)['minimum_response_gap'] == pytest.approx(.34)
    events[1]['dispatchPerf'] = 100.1
    assert metrics(events)['max_inflight'] == 2
