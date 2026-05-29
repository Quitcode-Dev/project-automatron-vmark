"""Detect host-level services (nginx, caddy, traefik) from systemctl / ps output."""

from __future__ import annotations

import re


def is_systemd_active(raw_systemctl: str) -> bool:
    """Return True if `systemctl is-active <service>` output says 'active'."""
    return raw_systemctl.strip().lower() == "active"


def _proc_names(ps_output: str) -> list[str]:
    """Extract process names from `ps aux` output (column 11, basename)."""
    names = []
    for line in ps_output.splitlines()[1:]:  # skip header
        parts = line.split(None, 10)
        if len(parts) >= 11:
            cmd = parts[10].strip()
            # Take the binary name (strip args)
            binary = cmd.split()[0] if cmd else ""
            names.append(binary.split("/")[-1])
    return names


def detect_nginx_via_process(ps_output: str) -> bool:
    return any("nginx" in n for n in _proc_names(ps_output))


def detect_caddy_via_process(ps_output: str) -> bool:
    return any("caddy" in n for n in _proc_names(ps_output))


def detect_traefik_via_process(ps_output: str) -> bool:
    return any("traefik" in n for n in _proc_names(ps_output))
