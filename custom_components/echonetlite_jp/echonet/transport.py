"""Asyncio UDP transport for ECHONET Lite."""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .errors import EchonetTimeoutError
from .protocol import (
    CONTROLLER,
    ECHONET_PORT,
    EOJ,
    ESV,
    MULTICAST_ADDRESS,
    Frame,
    FrameError,
    Property,
)

_LOGGER = logging.getLogger(__name__)

NotificationListener = Callable[[str, Frame], None]


@dataclass
class _Pending:
    host: str | None  # None for multicast requests
    queue: asyncio.Queue[tuple[str, Frame]] = field(default_factory=asyncio.Queue)


class EchonetTransport(asyncio.DatagramProtocol):
    """Sends requests and routes responses back to the waiting caller by TID.

    ECHONET Lite nodes answer to port 3610 regardless of the source port, so
    the transport must own that port on the host.
    """

    def __init__(
        self,
        *,
        interface: str | None = None,
        bind_host: str = "",
        port: int = ECHONET_PORT,
        multicast: bool = True,
        source: EOJ = CONTROLLER,
    ) -> None:
        self._interface = interface or None
        self._bind_host = bind_host
        self._port = port
        self._multicast = multicast
        self._source = source
        self._transport: asyncio.DatagramTransport | None = None
        self._pending: dict[int, _Pending] = {}
        self._next_tid = 0
        self._listeners: list[NotificationListener] = []

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        sock = self._create_socket()
        transport, _ = await loop.create_datagram_endpoint(lambda: self, sock=sock)
        self._transport = transport

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        for pending in self._pending.values():
            pending.queue.put_nowait(("", _CLOSED))
        self._pending.clear()

    @property
    def is_open(self) -> bool:
        return self._transport is not None

    def add_listener(self, listener: NotificationListener) -> Callable[[], None]:
        """Receive unsolicited frames (INF/INFC). Returns an unsubscribe callback."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    async def request(
        self,
        host: str,
        deoj: EOJ,
        esv: ESV,
        properties: Iterable[Property],
        *,
        timeout: float,
    ) -> Frame:
        tid, pending = self._register(host)
        try:
            self._send(host, Frame(tid, self._source, deoj, esv, tuple(properties)))
            try:
                async with asyncio.timeout(timeout):
                    _, frame = await pending.queue.get()
            except TimeoutError as err:
                raise EchonetTimeoutError(f"no response from {host} {deoj}") from err
            if frame is _CLOSED:
                raise ConnectionError("transport closed")
            return frame
        finally:
            self._pending.pop(tid, None)

    async def multicast(
        self, deoj: EOJ, esv: ESV, properties: Iterable[Property], *, wait: float
    ) -> list[tuple[str, Frame]]:
        """Send to the multicast group and collect every response within ``wait``."""
        tid, pending = self._register(None)
        responses: list[tuple[str, Frame]] = []
        try:
            self._send(MULTICAST_ADDRESS, Frame(tid, self._source, deoj, esv, tuple(properties)))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + wait
            while (remaining := deadline - loop.time()) > 0:
                try:
                    async with asyncio.timeout(remaining):
                        item = await pending.queue.get()
                except TimeoutError:
                    break
                if item[1] is _CLOSED:
                    break
                responses.append(item)
        finally:
            self._pending.pop(tid, None)
        return responses

    # asyncio.DatagramProtocol

    def datagram_received(self, data: bytes, addr: tuple[str | object, int]) -> None:
        host = str(addr[0])
        try:
            frame = Frame.decode(data)
        except FrameError:
            return
        if frame.esv == ESV.INFC:
            self._send(
                host,
                Frame(
                    frame.tid,
                    frame.deoj,
                    frame.seoj,
                    ESV.INFC_RES,
                    tuple(Property(p.epc) for p in frame.properties),
                ),
            )
        if 0x60 <= frame.esv <= 0x6F:
            return  # requests addressed to us (including our own multicast echo)

        pending = self._pending.get(frame.tid)
        if pending is not None and pending.host in (None, host):
            pending.queue.put_nowait((host, frame))
            return
        if frame.esv in (ESV.INF, ESV.INFC):
            for listener in list(self._listeners):
                try:
                    listener(host, frame)
                except Exception:
                    _LOGGER.exception("error in notification listener")

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("socket error: %s", exc)

    # internals

    def _register(self, host: str | None) -> tuple[int, _Pending]:
        if self._transport is None:
            raise ConnectionError("transport is not open")
        for _ in range(0xFFFF):
            self._next_tid = self._next_tid % 0xFFFF + 1
            if self._next_tid not in self._pending:
                break
        pending = _Pending(host)
        self._pending[self._next_tid] = pending
        return self._next_tid, pending

    def _send(self, host: str, frame: Frame) -> None:
        if self._transport is None:
            raise ConnectionError("transport is not open")
        self._transport.sendto(frame.encode(), (host, self._port))

    def _create_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self._bind_host, self._port))
            if self._multicast:
                iface = socket.inet_aton(self._interface or "0.0.0.0")
                mreq = socket.inet_aton(MULTICAST_ADDRESS) + iface
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                if self._interface:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, iface)
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        return sock


# Sentinel pushed to waiters when the transport closes.
_CLOSED = Frame(0, CONTROLLER, CONTROLLER, 0)
