"""Exceptions raised by the ECHONET Lite core."""

from __future__ import annotations

from collections.abc import Iterable


class EchonetError(Exception):
    """Base class for request failures."""


class EchonetTimeoutError(EchonetError, TimeoutError):
    """No response arrived in time."""


class EchonetConnectionClosed(EchonetError, ConnectionError):
    """The transport is not open (or was closed while waiting)."""


class EchonetRequestRejected(EchonetError):
    """The node answered with an error service code (…_SNA)."""

    def __init__(self, message: str, rejected: Iterable[int]) -> None:
        super().__init__(message)
        self.rejected = frozenset(rejected)
