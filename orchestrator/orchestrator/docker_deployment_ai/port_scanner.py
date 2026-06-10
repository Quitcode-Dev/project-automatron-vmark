"""Parse listening port data from remote `ss` / `netstat` output."""

from __future__ import annotations

import logging
import re
from typing import Any

from orchestrator.docker_deployment_ai.models import ListeningPort

logger = logging.getLogger(__name__)

# ss -tlnp output example:
#   State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process
#   LISTEN 0      128    0.0.0.0:80        0.0.0.0:*         users:(("nginx",pid=123,fd=6))
_SS_LINE_RE = re.compile(
    r"LISTEN\s+\d+\s+\d+\s+"
    r"(?P<addr>[^\s]+):(?P<port>\d+)\s+"
    r"[^\s]+(?:\s+users:\((?P<process>.+?)\))?"
)
_PROCESS_RE = re.compile(r'"([^"]+)"')


def _extract_process_name(users_str: str | None) -> str | None:
    if not users_str:
        return None
    m = _PROCESS_RE.search(users_str)
    return m.group(1) if m else None


def parse_ss_output(raw: str, proto: str = "tcp") -> list[ListeningPort]:
    ports = []
    for line in raw.splitlines():
        m = _SS_LINE_RE.search(line)
        if not m:
            continue
        port_num = int(m.group("port"))
        addr = m.group("addr").lstrip("[").rstrip("]") or "0.0.0.0"
        process = _extract_process_name(m.group("process"))
        ports.append(ListeningPort(port=port_num, proto=proto, process=process, address=addr))
    return ports


def parse_netstat_output(raw: str) -> list[ListeningPort]:
    """Parse `netstat -tlnp` (fallback when ss is unavailable)."""
    # Proto Recv-Q Send-Q Local Address           Foreign Address         State       PID/Program name
    ports = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        proto = parts[0].lower().rstrip("6")  # strip ipv6 suffix
        if proto not in ("tcp", "udp"):
            continue
        local = parts[3]
        if ":" not in local:
            continue
        addr, _, port_str = local.rpartition(":")
        try:
            port_num = int(port_str)
        except ValueError:
            continue
        process: str | None = None
        if len(parts) >= 7:
            pid_prog = parts[6]
            if "/" in pid_prog:
                process = pid_prog.split("/", 1)[1]
        ports.append(ListeningPort(port=port_num, proto=proto, process=process, address=addr or "0.0.0.0"))
    return ports


def port_owner(ports: list[ListeningPort], port_num: int) -> str | None:
    """Return the process name owning *port_num*, or None."""
    for p in ports:
        if p.port == port_num:
            return p.process
    return None


def is_port_occupied(ports: list[ListeningPort], port_num: int) -> bool:
    return any(p.port == port_num for p in ports)
