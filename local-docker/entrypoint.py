# Copyright 2018 Datawire. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
This commands has two modes: proxy, and wait.

== Proxy mode ==
Run sshuttle-telepresence via SSH IP and port given on command line.

The SSH server will run on the host, so the sshuttle-telepresence talking to it
somehow needs to get the IP of the host. So we read port and an optional IP
from the command line.

* If host is macOS an IP will be given.
* If host is Linux no IP will be given, and then we fall back to IP of default
  route.

The program expects to receive a JSON-encoded object as command line argument,
with parameters:

1. "port", the port number to connect ssh to.
2. "ip", optional, the ip of the ssh server.
3. "cidrs", a list of CIDRs for sshuttle.

References:

* https://stackoverflow.com/q/22944631/7862510
* https://docs.docker.com/docker-for-mac/networking/


== Wait mode ==

Wait mode should be run in same network namespace as the proxy. It will do the
'hellotelepresence' loop used to correct DNS on the k8s proxy, and to detect
when the proxy is working.

When the process exits with exit code 100 that means the proxy is active.
"""

import sys
import typing
from json import loads
from socket import gaierror, gethostbyname
from subprocess import Popen
from time import sleep, time

from telepresence.connect import SSH, expose_local_services
from telepresence.outbound import get_sshuttle_command
from telepresence.runner import Runner


def main() -> None:
    """Dispatch to the correct mode"""
    command = sys.argv[1]
    if command == "proxy":
        proxy(loads(sys.argv[2]))
    elif command == "wait":
        wait()


def proxy(config: typing.Dict[str, typing.Any]) -> None:
    """Start sshuttle proxy to Kubernetes."""
    inclusions = config["include_cidrs"]
    exclude_cidrs = config["exclude_cidrs"]
    expose_ports = config["expose_ports"]
    to_pod = config["to_pod"]
    from_pod = config["from_pod"]

    # Launch local sshd so Tel outside can forward 38023 to the cluster
    runner = Runner("-", False)
    runner.check_call(["/usr/sbin/sshd", "-e"])

    # Wait for the cluster to be available
    ssh = SSH(runner, 38023, "telepresence@127.0.0.1")
    if not ssh.wait():
        raise RuntimeError(
            "SSH from local container to the cluster failed to start."
        )

    # Figure out IP addresses to exclude, from the incoming ssh
    exclusions = []
    for cidr in exclude_cidrs:
        exclusions.extend(["-x", cidr])
    netstat_output = runner.get_output(["netstat", "-n"])
    for line in netstat_output.splitlines():
        if not line.startswith("tcp") or "ESTABLISHED" not in line:
            continue
        parts = line.split()
        try:
            for address in (parts[3], parts[4]):
                ip, port = address.split(":")
                exclusions.extend(["-x", ip])
        except (IndexError, ValueError):
            runner.write("Failed on line: " + line)
            raise
    assert exclusions, netstat_output

    # Start the sshuttle VPN-like thing:
    sshuttle_cmd = get_sshuttle_command(ssh, "nat") + exclusions + inclusions
    main_process = Popen(sshuttle_cmd, universal_newlines=True)

    # Start the SSH tunnels to expose local services:
    expose_local_services(runner, ssh, expose_ports, to_pod, from_pod)

    # Wait for everything to exit:
    runner.wait_for_exit(main_process)


def wait() -> None:
    """Wait for proxying to be live."""
    start = time()
    while time() - start < 30:
        try:
            gethostbyname("kubernetes.default")
            sleep(1)  # just in case there's more to startup
            sys.exit(100)
        except gaierror:
            sleep(0.1)
    sys.exit("Failed to connect to proxy in remote cluster.")


if __name__ == '__main__':
    main()
