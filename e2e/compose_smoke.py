#!/usr/bin/env python3
"""Check the exact local-demo Compose recipe; reuse an already built image.

No registry push, real providers, mounted configs or production changes.
"""
import json
from pathlib import Path
import subprocess
import sys
import uuid

from docker_smoke import docker, check, verify_http


ROOT = Path(__file__).resolve().parents[1]


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: compose_smoke.py BUILT_IMAGE")
    image = sys.argv[1]
    expected_image = docker("image", "inspect", "--format", "{{.Id}}", image)
    # The checked-in recipe uses this local-only tag. No build or registry pull.
    docker("tag", image, "llmgate:local")
    project = "llmgate-smoke-" + uuid.uuid4().hex[:12]
    command = ["docker", "compose", "--project-name", project,
               "--file", str(ROOT / "docker-compose.yml")]

    def compose(*args):
        return subprocess.check_output(command + list(args), text=True, timeout=90).strip()

    try:
        config = json.loads(compose("config", "--format", "json"))
        ports = config["services"]["gateway"]["ports"]
        check(len(ports) == 1 and ports[0].get("host_ip") == "127.0.0.1"
              and int(ports[0]["target"]) == 8000
              and str(ports[0]["published"]) == "8000",
              "exact Compose recipe publishes only IPv4 loopback port8000")
        compose("up", "--detach", "--no-build", "--pull", "never")
        container = compose("ps", "--quiet", "gateway")
        check(bool(container) and "\n" not in container, "one Compose gateway started")
        inspect = json.loads(docker("inspect", container))[0]
        check(inspect["Image"] == expected_image, "Compose runs the tested image, no pull or rebuild")
        bindings = inspect["NetworkSettings"]["Ports"]["8000/tcp"]
        check(len(bindings) == 1 and bindings[0]["HostIp"] == "127.0.0.1"
              and bindings[0]["HostPort"] == "8000",
              "running Compose container is published to127.0.0.1 only")
        docker("exec", "--workdir", "/tmp", container, "python", "-c",
               "import os, llmgate, actas; assert os.getuid() != 0; "
               "assert all('/site-packages/' in m.__file__ for m in (llmgate, actas))")
        check(True, "Compose runs installed packages as a non-root user")
        verify_http("http://127.0.0.1:8000", 0)
        print("PASS exact default Compose recipe and loopback HTTP, simulated only")
    except BaseException:
        subprocess.run(command + ["logs", "--no-color"], check=False, timeout=30)
        raise
    finally:
        subprocess.run(command + ["down", "--volumes", "--remove-orphans"],
                       check=True, timeout=90)


if __name__ == "__main__":
    main()
