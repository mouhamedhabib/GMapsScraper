"""Low-overhead network protection relay shared by discovery and completion."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
from enum import Enum
import socket
from time import monotonic, sleep
from typing import Callable


class NetworkState(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    PAUSED = "PAUSED"
    RECOVERING = "RECOVERING"


class NetworkPauseExceeded(RuntimeError):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


class TargetSiteNetworkError(RuntimeError):
    """A target failed while independent connectivity probes were healthy."""


NETWORK_MESSAGE_MARKERS = {
    "ERR_INTERNET_DISCONNECTED": "INTERNET_DISCONNECTED",
    "ERR_NETWORK_CHANGED": "NETWORK_CHANGED",
    "ERR_CONNECTION_RESET": "CONNECTION_RESET",
    "ERR_CONNECTION_REFUSED": "CONNECTION_REFUSED",
    "ERR_CONNECTION_TIMED_OUT": "CONNECTION_TIMEOUT",
    "ERR_NAME_NOT_RESOLVED": "DNS_RESOLUTION",
    "temporary failure in name resolution": "DNS_RESOLUTION",
    "name or service not known": "DNS_RESOLUTION",
    "nodename nor servname provided": "DNS_RESOLUTION",
    "network is unreachable": "NETWORK_UNREACHABLE",
    "no route to host": "NETWORK_UNREACHABLE",
    "connection reset": "CONNECTION_RESET",
    "connection refused": "CONNECTION_REFUSED",
    "connection timed out": "CONNECTION_TIMEOUT",
}

ERRNO_TYPES = {
    errno.ECONNRESET: "CONNECTION_RESET",
    errno.ECONNREFUSED: "CONNECTION_REFUSED",
    errno.ENETUNREACH: "NETWORK_UNREACHABLE",
    errno.EHOSTUNREACH: "NETWORK_UNREACHABLE",
    errno.ETIMEDOUT: "CONNECTION_TIMEOUT",
}


def classify_network_error(error: BaseException) -> str | None:
    """Return a stable network type without treating page timeouts as outages."""
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in chain:
        chain.append(current)
        reason = getattr(current, "reason", None)
        current = (
            current.__cause__ or current.__context__
            or (reason if isinstance(reason, BaseException) else None)
        )
    for item in chain:
        message = str(item).casefold()
        for marker, error_type in NETWORK_MESSAGE_MARKERS.items():
            if marker.casefold() in message:
                return error_type
        if isinstance(item, socket.gaierror):
            return "DNS_RESOLUTION"
        if isinstance(item, ConnectionRefusedError):
            return "CONNECTION_REFUSED"
        if isinstance(item, (ConnectionResetError, ConnectionAbortedError)):
            return "CONNECTION_RESET"
        if isinstance(item, TimeoutError):
            return "CONNECTION_TIMEOUT"
        if isinstance(item, OSError) and item.errno in ERRNO_TYPES:
            return ERRNO_TYPES[item.errno]
    return None


def connectivity_probe(
    *, timeout: float = 3.0,
    resolver: Callable[..., object] = socket.getaddrinfo,
    connector: Callable[..., socket.socket] = socket.create_connection,
) -> bool:
    """Use bounded DNS plus either of two independent TCP endpoints."""
    dns_ok = False
    for hostname in ("google.com", "cloudflare.com"):
        try:
            resolver(hostname, 443, type=socket.SOCK_STREAM)
            dns_ok = True
            break
        except OSError:
            continue
    if not dns_ok:
        return False
    for target in (("1.1.1.1", 443), ("8.8.8.8", 443)):
        try:
            connection = connector(target, timeout=timeout)
            connection.close()
            return True
        except OSError:
            continue
    return False


@dataclass(frozen=True)
class RelaySnapshot:
    state: str
    network_pauses: int
    network_pause_seconds: float
    network_failures: int
    network_recoveries: int
    last_network_failure: str | None
    last_successful_probe: str | None


class NetworkProtectionRelay:
    """Gate expensive work using failure-window and recovery hysteresis."""

    def __init__(
        self, *, enabled: bool = True, failure_window: int = 4,
        pause_after_failures: int = 3, resume_successes: int = 3,
        probe_interval: float = 10, probe_max_interval: float = 30,
        max_pause_seconds: float = 600, probe_timeout: float = 3,
        probe_function: Callable[..., bool] = connectivity_probe,
        sleep_function: Callable[[float], None] = sleep,
        clock: Callable[[], float] = monotonic,
        output: Callable[[str], None] = print,
    ):
        if failure_window < 1 or not 1 <= pause_after_failures <= failure_window:
            raise ValueError("invalid network failure window/threshold")
        if resume_successes < 1 or probe_interval <= 0 or probe_max_interval < probe_interval:
            raise ValueError("invalid network recovery settings")
        if max_pause_seconds < 0 or probe_timeout <= 0:
            raise ValueError("invalid network pause/probe timeout")
        self.enabled = enabled
        self.failure_window = failure_window
        self.pause_after_failures = pause_after_failures
        self.resume_successes = resume_successes
        self.probe_interval = probe_interval
        self.probe_max_interval = probe_max_interval
        self.max_pause_seconds = max_pause_seconds
        self.probe_timeout = probe_timeout
        self.probe_function = probe_function
        self.sleep_function = sleep_function
        self.clock = clock
        self.output = output
        self.state = NetworkState.HEALTHY
        self._checks = deque(maxlen=failure_window)
        self._recovery_successes = 0
        self.network_pauses = 0
        self.network_pause_seconds = 0.0
        self.network_failures = 0
        self.network_recoveries = 0
        self.last_network_failure: str | None = None
        self.last_successful_probe: str | None = None
        self.exhausted = False

    def snapshot(self) -> RelaySnapshot:
        return RelaySnapshot(
            self.state.value, self.network_pauses,
            round(self.network_pause_seconds, 3), self.network_failures,
            self.network_recoveries, self.last_network_failure,
            self.last_successful_probe,
        )

    def _probe(self) -> bool:
        try:
            success = bool(self.probe_function(timeout=self.probe_timeout))
        except Exception:
            success = False
        if success:
            self.last_successful_probe = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
        return success

    def _record_check(self, success: bool) -> None:
        self._checks.append(success)
        failures = self._checks.count(False)
        if success and self.state == NetworkState.DEGRADED and failures == 0:
            self.state = NetworkState.HEALTHY
        elif not success and self.state == NetworkState.HEALTHY:
            self.state = NetworkState.DEGRADED
        if not success and self.state not in {NetworkState.PAUSED, NetworkState.RECOVERING}:
            self.output(
                f"[NETWORK] Connectivity degraded: {failures}/"
                f"{len(self._checks)} recent checks failed"
            )
        if failures >= self.pause_after_failures and self.state not in {
            NetworkState.PAUSED, NetworkState.RECOVERING,
        }:
            self.state = NetworkState.PAUSED
            self.network_pauses += 1
            self.output("[NETWORK] Protection relay triggered")
            self.output("[NETWORK] Scraping paused to protect CPU/resources")

    def wait_until_healthy(self, *, context: str = "work", page_start: int | None = None) -> None:
        if not self.enabled or self.state not in {
            NetworkState.PAUSED, NetworkState.RECOVERING,
        }:
            return
        if self.exhausted:
            raise NetworkPauseExceeded(
                "LOCAL_NETWORK_BAD", "network protection pause already expired"
            )
        pause_started = self.clock()
        interval = self.probe_interval
        while True:
            elapsed = self.clock() - pause_started
            if self.max_pause_seconds and elapsed >= self.max_pause_seconds:
                self.network_pause_seconds += elapsed
                self.exhausted = True
                raise NetworkPauseExceeded(
                    "LOCAL_NETWORK_BAD",
                    f"network did not stabilize within {self.max_pause_seconds:g}s",
                )
            wait_for = interval
            if self.max_pause_seconds:
                wait_for = min(wait_for, max(0, self.max_pause_seconds - elapsed))
            self.sleep_function(wait_for)
            if not self._probe():
                self.network_failures += 1
                self.state = NetworkState.PAUSED
                self._recovery_successes = 0
                self.output(f"[NETWORK] Probe failed — next check in {interval:g}s")
                interval = min(self.probe_max_interval, max(interval, 1) + 5)
                continue
            self._recovery_successes += 1
            self.state = NetworkState.RECOVERING
            self.output(
                f"[NETWORK] Probe succeeded {self._recovery_successes}/"
                f"{self.resume_successes}"
            )
            if self._recovery_successes < self.resume_successes:
                continue
            elapsed = self.clock() - pause_started
            self.network_pause_seconds += elapsed
            self.network_recoveries += 1
            self._checks.clear()
            self._recovery_successes = 0
            self.state = NetworkState.HEALTHY
            self.output("[NETWORK] Connection stable")
            self.output(f"[NETWORK] Resuming {context}")
            if page_start is not None:
                self.output(f"[NETWORK] Resuming from page {page_start // 10 + 1}")
            return

    def protect(self, operation: Callable[[], object], *, context: str,
                page_start: int | None = None):
        """Run expensive work only while allowed, retrying its safe checkpoint."""
        if not self.enabled:
            return operation()
        while True:
            self.wait_until_healthy(context=context, page_start=page_start)
            try:
                result = operation()
            except Exception as error:
                error_type = classify_network_error(error)
                if not error_type:
                    raise
                self.last_network_failure = f"{error_type}: {error}"
                if self._probe():
                    self._record_check(True)
                    raise TargetSiteNetworkError(
                        f"TARGET_SITE_BAD/{error_type}: {error}"
                    ) from error
                self.network_failures += 1
                self._record_check(False)
                continue
            self._record_check(True)
            return result
