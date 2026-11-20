# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import unittest
from aiopslab.utils.actions import get_actions


class TestGetActions(unittest.TestCase):
    def test_get_actions(self):
        actions = get_actions("detection")
        self.assertEqual(len(actions), 8)
        self.assertEqual(
            set(actions.keys()),
            {
                "get_logs",
                "get_metrics",
                "get_traces",
                "read_metrics",
                "read_traces",
                "get_power_status",
                "exec_shell",
                "submit",
            },
        )

    def test_get_read_actions(self):
        actions = get_actions("detection", "read")
        self.assertEqual(len(actions), 6)
        self.assertEqual(
            set(actions.keys()),
            {
                "get_logs",
                "get_metrics",
                "get_traces",
                "read_metrics",
                "read_traces",
                "get_power_status",
            },
        )

    def test_get_write_actions(self):
        actions = get_actions("detection", "write")
        self.assertEqual(len(actions), 0)

    def test_mitigation_has_power_node_action(self):
        actions = get_actions("mitigation")
        self.assertIn("power_node", actions)
        write_actions = get_actions("mitigation", "write")
        self.assertIn("power_node", write_actions)


if __name__ == "__main__":
    unittest.main()
