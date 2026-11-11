#!/usr/bin/env python3
"""Smoke-test an already built demo image. No registry or real providers.

Usage: python3 e2e/docker_smoke.py llmgate:ci
Requires Docker and an image built from the candidate checkout.
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, timeout=90).strip()


def request(base, path, key=None, body=None):
    headers = {}
    if key:
        headers["Authorization"] = "Bearer " + key
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, headers=headers,
                                 data=None if body is None else json.dumps(body).encode())
    try:
        response = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, response.read().decode()


def check(condition, label):
    if not condition:
        raise AssertionError(label)
    print("PASS " + label, flush=True)


def verify_http(base, fail_primary):
    for attempt in range(60):
        try:
            status, _, text = request(base, "/healthz")
            if status == 200 and json.loads(text)["ok"]:
                break
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    else:
        raise AssertionError("image did not become ready within 30 seconds")
    check(True, "published loopback HTTP health answers")
    check(request(base, "/v1/models")[0] == 401, "missing bearer denied")
    check(request(base, "/v1/models", "wrong-key")[0] == 401, "wrong bearer denied")
    status, _, text = request(base, "/v1/models", "demo-key")
    check(status == 200 and "gpt-sim" in [m["id"] for m in json.loads(text)["data"]],
          "authenticated model discovery works")
    body = {"model": "gpt-sim", "messages": [{"role": "user", "content": "image smoke"}]}
    status, headers, text = request(base, "/v1/chat/completions", "demo-key", body)
    expected = "sim-b" if fail_primary else "sim-a"
    check(status == 200 and headers.get("x-gateway-provider") == expected
          and bool(json.loads(text)["choices"][0]["message"]["content"]),
          "completion served by " + expected)
    body["stream"] = True
    status, headers, text = request(base, "/v1/chat/completions", "demo-key", body)
    check(status == 200 and "text/event-stream" in headers.get("content-type", "")
          and "data: {" in text and "data: [DONE]" in text,
          "stream delivers SSE frames and terminates")
    body.pop("stream")
    body["model"] = "missing-model"
    check(request(base, "/v1/chat/completions", "demo-key", body)[0] == 404,
          "unknown model rejected")
    check(request(base, "/admin/usage", "demo-key")[0] == 401, "admin usage denies demo key")
    status, _, text = request(base, "/admin/usage", "admin-key")
    check(status == 200 and json.loads(text)["keys"]["demo"]["requests"] == 2,
          "admin usage records completion and stream")


def smoke(image, fail_primary):
    container = docker("run", "--detach", "--rm", "--memory", "256m", "--cpus", "1",
                       "--pids-limit", "128", "--publish", "127.0.0.1::8000",
                       "--env", "SIM_A_FAIL=" + str(fail_primary), image)
    try:
        inspect = json.loads(docker("inspect", container))[0]
        check(inspect["Config"]["User"] == "llmgate", "image runs as llmgate, not root")
        check(bool(inspect["Config"].get("Healthcheck", {}).get("Test")), "image has a healthcheck")
        docker("exec", "--workdir", "/tmp", container, "python", "-c",
               "import os, llmgate, actas; import llmgate.config; "
               "assert os.getuid() != 0; "
               "assert all('/site-packages/' in m.__file__ for m in (llmgate, actas))")
        check(True, "installed image imports gateway and vendored Actas")
        port = inspect["NetworkSettings"]["Ports"]["8000/tcp"][0]["HostPort"]
        base = "http://127.0.0.1:" + port
        verify_http(base, fail_primary)
    except BaseException:
        print(docker("logs", container), file=sys.stderr)
        raise
    finally:
        subprocess.run(["docker", "rm", "--force", container], check=False,
                       stdout=subprocess.DEVNULL, timeout=30)


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: docker_smoke.py IMAGE")
    image = sys.argv[1]
    print("image=" + docker("image", "inspect", "--format", "{{.Id}}", image), flush=True)
    smoke(image, 0)
    smoke(image, 1)
    print("PASS image HTTP smoke, healthy and forced-primary-failure modes; simulated only")


if __name__ == "__main__":
    main()
