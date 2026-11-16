"""Tests for System Map bookmark selection."""

import unittest
from unittest.mock import Mock

from panels.system_map import EDSystemMap


class SystemMapBookmarkTestCase(unittest.TestCase):
    def test_legacy_navigation_bookmark_types_are_rejected_without_inputs(self):
        system_map = EDSystemMap.__new__(EDSystemMap)
        system_map.is_odyssey = True
        system_map.keys = Mock()
        system_map.goto_system_map = Mock()

        for bookmark_type in ("Navigation", "Navigation Panel"):
            with self.subTest(bookmark_type=bookmark_type):
                self.assertFalse(system_map.set_sys_map_dest_bookmark(Mock(), bookmark_type, 2))

        system_map.goto_system_map.assert_not_called()
        system_map.keys.send.assert_not_called()
