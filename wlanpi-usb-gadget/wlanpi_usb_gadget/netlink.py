"""Minimal stdlib-only rtnetlink listener for link and neighbour events."""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

RTMGRP_LINK = 1
RTMGRP_NEIGH = 4

RTM_NEWLINK = 16
RTM_DELLINK = 17
RTM_NEWNEIGH = 28
RTM_DELNEIGH = 29

IFLA_IFNAME = 3
NDA_DST = 1

IFF_UP = 0x1
IFF_RUNNING = 0x40
IFF_LOWER_UP = 0x10000

# Neighbour states (linux/neighbour.h)
NUD_REACHABLE = 0x02
NUD_STALE = 0x04
NUD_DELAY = 0x08
NUD_PROBE = 0x10
NUD_PERMANENT = 0x80
NUD_VALID = NUD_REACHABLE | NUD_STALE | NUD_DELAY | NUD_PROBE | NUD_PERMANENT

NLMSG_HDR_FMT = "=IHHII"
NLMSG_HDR_LEN = struct.calcsize(NLMSG_HDR_FMT)
IFINFOMSG_FMT = "=BBHiII"
IFINFOMSG_LEN = struct.calcsize(IFINFOMSG_FMT)
NDMSG_FMT = "=BBHiHBB"
NDMSG_LEN = struct.calcsize(NDMSG_FMT)
RTA_FMT = "=HH"
RTA_LEN = struct.calcsize(RTA_FMT)


@dataclass(frozen=True)
class LinkEvent:
    """Parsed RTM_NEWLINK / RTM_DELLINK event."""

    kind: str  # "new" or "del"
    ifindex: int
    ifname: str
    flags: int
    carrier: bool


@dataclass(frozen=True)
class NeighEvent:
    """Parsed RTM_NEWNEIGH / RTM_DELNEIGH event."""

    kind: str  # "new" or "del"
    ifindex: int
    family: int
    state: int
    valid: bool
    dst: str


def _align4(n: int) -> int:
    return (n + 3) & ~3


def _parse_rtattrs(buf: bytes) -> dict[int, bytes]:
    attrs: dict[int, bytes] = {}
    offset = 0
    while offset + RTA_LEN <= len(buf):
        rta_len, rta_type = struct.unpack_from(RTA_FMT, buf, offset)
        if rta_len < RTA_LEN or offset + rta_len > len(buf):
            break
        attrs[rta_type] = buf[offset + RTA_LEN : offset + rta_len]
        offset += _align4(rta_len)
    return attrs


def parse_netlink_messages(data: bytes) -> list[LinkEvent | NeighEvent]:
    """Parse raw NETLINK_ROUTE bytes into LinkEvent and NeighEvent objects."""
    events: list[LinkEvent | NeighEvent] = []
    offset = 0
    while offset + NLMSG_HDR_LEN <= len(data):
        nlmsg_len, nlmsg_type, _flags, _seq, _pid = struct.unpack_from(NLMSG_HDR_FMT, data, offset)
        if nlmsg_len < NLMSG_HDR_LEN or offset + nlmsg_len > len(data):
            break
        payload = data[offset + NLMSG_HDR_LEN : offset + nlmsg_len]

        if nlmsg_type in (RTM_NEWLINK, RTM_DELLINK) and len(payload) >= IFINFOMSG_LEN:
            _family, _pad, _type, ifindex, ifi_flags, _change = struct.unpack_from(IFINFOMSG_FMT, payload, 0)
            attrs = _parse_rtattrs(payload[IFINFOMSG_LEN:])
            raw_name = attrs.get(IFLA_IFNAME, b"")
            ifname = raw_name.split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
            carrier = bool(ifi_flags & (IFF_LOWER_UP | IFF_RUNNING))
            events.append(
                LinkEvent(
                    kind="new" if nlmsg_type == RTM_NEWLINK else "del",
                    ifindex=ifindex,
                    ifname=ifname,
                    flags=ifi_flags,
                    carrier=carrier,
                )
            )
        elif nlmsg_type in (RTM_NEWNEIGH, RTM_DELNEIGH) and len(payload) >= NDMSG_LEN:
            ndm_family, _pad1, _pad2, ifindex, ndm_state, _ndm_flags, _ndm_type = struct.unpack_from(
                NDMSG_FMT, payload, 0
            )
            attrs = _parse_rtattrs(payload[NDMSG_LEN:])
            dst_bytes = attrs.get(NDA_DST, b"")
            dst = ""
            if ndm_family == socket.AF_INET and len(dst_bytes) == 4:
                dst = socket.inet_ntop(socket.AF_INET, dst_bytes)
            elif ndm_family == socket.AF_INET6 and len(dst_bytes) == 16:
                dst = socket.inet_ntop(socket.AF_INET6, dst_bytes)
            valid = bool(ndm_state & NUD_VALID) and nlmsg_type == RTM_NEWNEIGH
            events.append(
                NeighEvent(
                    kind="new" if nlmsg_type == RTM_NEWNEIGH else "del",
                    ifindex=ifindex,
                    family=ndm_family,
                    state=ndm_state,
                    valid=valid,
                    dst=dst,
                )
            )

        offset += _align4(nlmsg_len)
    return events


class NetlinkListener:
    """Non-blocking NETLINK_ROUTE listener for link and neighbour groups."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE)
        self._sock.bind((0, RTMGRP_LINK | RTMGRP_NEIGH))
        self._sock.setblocking(False)

    def fileno(self) -> int:
        return self._sock.fileno()

    def read_events(self) -> list[LinkEvent | NeighEvent]:
        events: list[LinkEvent | NeighEvent] = []
        while True:
            try:
                data = self._sock.recv(65536)
            except BlockingIOError:
                break
            if not data:
                break
            events.extend(parse_netlink_messages(data))
        return events

    def close(self) -> None:
        self._sock.close()
