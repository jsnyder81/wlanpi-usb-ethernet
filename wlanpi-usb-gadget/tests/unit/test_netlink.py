"""Unit tests for rtnetlink binary message parsing."""

from __future__ import annotations

import socket
import struct
import unittest

from wlanpi_usb_gadget.netlink import (
    IFINFOMSG_FMT,
    IFLA_IFNAME,
    IFF_LOWER_UP,
    IFF_UP,
    NDA_DST,
    NDMSG_FMT,
    NLMSG_HDR_FMT,
    NUD_REACHABLE,
    RTA_FMT,
    RTM_NEWLINK,
    RTM_NEWNEIGH,
    LinkEvent,
    NeighEvent,
    parse_netlink_messages,
)


def _make_rta(rta_type: int, payload: bytes) -> bytes:
    rta_len = struct.calcsize(RTA_FMT) + len(payload)
    pad = (4 - (rta_len % 4)) % 4
    return struct.pack(RTA_FMT, rta_len, rta_type) + payload + (b"\x00" * pad)


class TestNetlink(unittest.TestCase):
    def test_parse_newlink_and_newneigh(self) -> None:
        ifinfo = struct.pack(IFINFOMSG_FMT, socket.AF_UNSPEC, 0, 1, 42, IFF_UP | IFF_LOWER_UP, 0)
        rta_name = _make_rta(IFLA_IFNAME, b"usbncm0\x00")
        link_payload = ifinfo + rta_name
        link_msg = struct.pack(NLMSG_HDR_FMT, 16 + len(link_payload), RTM_NEWLINK, 0, 1, 0) + link_payload

        ndmsg = struct.pack(NDMSG_FMT, socket.AF_INET, 0, 0, 99, NUD_REACHABLE, 0, 0)
        rta_dst = _make_rta(NDA_DST, socket.inet_aton("198.18.42.10"))
        neigh_payload = ndmsg + rta_dst
        neigh_msg = struct.pack(NLMSG_HDR_FMT, 16 + len(neigh_payload), RTM_NEWNEIGH, 0, 2, 0) + neigh_payload

        events = parse_netlink_messages(link_msg + neigh_msg)
        self.assertEqual(len(events), 2)
        self.assertIsInstance(events[0], LinkEvent)
        self.assertEqual(events[0].ifname, "usbncm0")
        self.assertTrue(events[0].carrier)

        self.assertIsInstance(events[1], NeighEvent)
        self.assertTrue(events[1].valid)
        self.assertEqual(events[1].dst, "198.18.42.10")


if __name__ == "__main__":
    unittest.main()
