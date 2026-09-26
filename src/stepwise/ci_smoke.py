from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def _absolute_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("StepWise container smoke test timed out")
    return min(5.0, remaining)


def _read_json(target: str | Request, deadline: float) -> dict[str, Any]:
    try:
        with urlopen(target, timeout=_remaining(deadline)) as response:
            payload = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"StepWise request failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ConnectionError(str(exc.reason)) from exc
    parsed = json.loads(payload.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise TypeError("StepWise response must be a JSON object")
    return parsed


def _multipart_request(url: str, walking_path: Path) -> Request:
    boundary = f"----stepwise-{uuid.uuid4().hex}"
    contents = walking_path.read_bytes()
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            (
                'Content-Disposition: form-data; name="walking"; '
                f'filename="{walking_path.name}"\r\n'
            ).encode(),
            b"Content-Type: text/plain\r\n\r\n",
            contents,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )


def run_container_smoke(
    base_url: str,
    walking_path: Path,
    *,
    timeout_seconds: float = 60,
) -> dict[str, str | int]:
    if not walking_path.is_file():
        raise FileNotFoundError(walking_path)
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    deadline = time.monotonic() + timeout_seconds
    health_url = _absolute_url(base_url, "/healthz")
    while True:
        try:
            if _read_json(health_url, deadline).get("status") == "ok":
                break
        except (ConnectionError, RuntimeError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)

    created = _read_json(
        _multipart_request(_absolute_url(base_url, "/api/v1/analyses"), walking_path),
        deadline,
    )
    run_id = created.get("run_id")
    status_path = created.get("status_url")
    result_path = created.get("result_url")
    if not all(isinstance(value, str) and value for value in (run_id, status_path, result_path)):
        raise RuntimeError("analysis creation response is missing run URLs")

    while True:
        status = _read_json(_absolute_url(base_url, str(status_path)), deadline)
        state = status.get("status")
        if state == "succeeded":
            break
        if state == "failed":
            error = status.get("error") or {}
            raise RuntimeError(
                f"analysis failed: {error.get('code', 'unknown')} - "
                f"{error.get('message', 'no message')}"
            )
        if state not in {"queued", "running"}:
            raise RuntimeError(f"unexpected analysis status: {state!r}")
        time.sleep(0.25)

    result = _read_json(_absolute_url(base_url, str(result_path)), deadline)
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise RuntimeError("analysis result did not contain an artifact manifest")
    artifact = artifacts[0]
    if not isinstance(artifact, dict) or not isinstance(artifact.get("name"), str):
        raise TypeError("analysis result contained an invalid artifact entry")
    artifact_name = artifact["name"]
    artifact_url = _absolute_url(
        base_url,
        f"/api/v1/analyses/{run_id}/artifacts/{quote(artifact_name)}",
    )
    try:
        with urlopen(artifact_url, timeout=_remaining(deadline)) as response:
            artifact_bytes = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"artifact download failed with HTTP {exc.code}: {detail}") from exc
    expected_size = artifact.get("size_bytes")
    if isinstance(expected_size, int) and len(artifact_bytes) != expected_size:
        raise RuntimeError(
            f"artifact size mismatch: expected {expected_size}, received {len(artifact_bytes)}"
        )

    return {
        "run_id": str(run_id),
        "artifact": artifact_name,
        "artifact_bytes": len(artifact_bytes),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a running StepWise container.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--walking", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=60)
    args = parser.parse_args(argv)
    result = run_container_smoke(
        args.base_url,
        args.walking,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
