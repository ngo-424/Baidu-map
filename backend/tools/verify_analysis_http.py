"""Local real-HTTP smoke; no external API requests or secrets. Run from backend."""
import json
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn

from app.config import Settings
from app.main import create_app


def verify():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(Settings(_env_file=None, baidu_map_ak="")),
                                          log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    records = []
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Local test server failed to start")
            time.sleep(.02)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30) as client:
            for name in ("complete", "partial", "failed", "empty"):
                result = client.get(f"/api/v1/analysis/mock/{name}")
                assert result.status_code == 200 and result.json()["status"] == name
                records.append({"endpoint": f"mock/{name}", "http_status": result.status_code, "status": name})
            for name in ("plane", "local_failure", "global_failure"):
                result = client.post("/api/v1/analysis/synthetic", json={
                    "origin": {"lng":116.4,"lat":39.9}, "coordinate_system":"bd09ll", "scenario":name,
                }, headers={"Origin":"http://localhost:5173"})
                assert result.status_code == 200
                body = result.json()
                assert result.headers["access-control-allow-origin"] == "http://localhost:5173"
                assert body["algorithm"]["statistics"]["network_requests"] == 0
                records.append({"endpoint":"synthetic", "scenario":name, "http_status":result.status_code,
                    "status":body["status"], "quality":body["algorithm"]["quality"],
                    "requests":body["algorithm"]["statistics"]["requests"], "network_requests":0,
                    "geometry_is_null":body["data"]["geometry"] is None})
            invalid = client.post("/api/v1/analysis/synthetic", json={})
            assert invalid.status_code == 422 and invalid.json()["status"] == "failed"
            records.append({"endpoint":"synthetic-invalid", "http_status":422,"status":"failed"})
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
    payload = {"verified_at":datetime.now(timezone.utc).isoformat(), "transport":"local TCP HTTP",
               "algorithm_version":"2d63015", "external_network_requests":0, "checks":records}
    target = Path(__file__).resolve().parents[1] / "docs/n04-n05-http-evidence.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    verify()
