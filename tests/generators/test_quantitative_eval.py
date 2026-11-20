# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Unit tests for the quantitative evaluation helpers (gap-fill).

These pure functions predate the power fault work but had no test coverage.
"""

from aiopslab.orchestrator.evaluators.quantitative import (
    in_tokens,
    is_exact_match,
    is_exact_match_lower,
    is_in_range,
    is_subset,
    is_superset,
    num_steps_taken,
    out_tokens,
)
from aiopslab.session import SessionItem


def item(role, content):
    return SessionItem(role=role, content=content)


class TestExactMatch:
    def test_string_vs_string(self):
        assert is_exact_match("user", "user") is True
        assert is_exact_match("user", "geo") is False

    def test_string_vs_single_element_list(self):
        assert is_exact_match("user", ["user"]) is True
        assert is_exact_match(["user"], "user") is True

    def test_lists(self):
        assert is_exact_match(["a", "b"], ["a", "b"]) is True
        assert is_exact_match(["a", "b"], ["b", "a"]) is False
        assert is_exact_match(["a"], ["a", "b"]) is False

    def test_numbers(self):
        assert is_exact_match(9090, 9090) is True
        assert is_exact_match(9999, 9090) is False


class TestExactMatchLower:
    def test_case_and_whitespace_insensitive(self):
        assert is_exact_match_lower("  Virtualization ", "virtualization") is True
        assert is_exact_match_lower("Application", "Misconfiguration") is False


class TestRange:
    def test_boundaries_inclusive(self):
        assert is_in_range(5, 10, 5) is True
        assert is_in_range(15, 10, 5) is True
        assert is_in_range(10, 10, 5) is True

    def test_out_of_range(self):
        assert is_in_range(16, 10, 5) is False
        assert is_in_range(4, 10, 5) is False


class TestSubsets:
    def test_subset(self):
        assert is_subset(["user"], ["user", "geo"]) is True
        assert is_subset(["user", "geo"], ["user"]) is False

    def test_superset(self):
        assert is_superset(["user", "geo"], ["user"]) is True
        assert is_superset(["user"], ["user", "geo"]) is False


class TestTraceMetrics:
    def test_num_steps_counts_assistant_only(self):
        trace = [
            item("user", "do something"),
            item("assistant", '```submit("Yes")```'),
            item("env", "observation"),
            item("assistant", '```submit("No")```'),
        ]
        assert num_steps_taken(trace) == 2

    def test_out_tokens_positive_for_nonempty_output(self):
        trace = [item("assistant", "take action now")]
        assert out_tokens(trace) > 0

    def test_in_tokens_positive_for_nonempty_input(self):
        trace = [item("env", "here is an observation")]
        assert in_tokens(trace) > 0

    def test_empty_trace_is_zero_cost(self):
        assert num_steps_taken([]) == 0
        assert out_tokens([]) == 0
        assert in_tokens([]) == 0
