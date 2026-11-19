import pytest

from cogitator import cli
from cogitator.versions import newer_versions, version_key


def test_cli_newer_with_mixed_numeric_and_alpha_tags_does_not_raise():
    assert cli._newer(["1.2", "1.2a", "1.10"], "1.0") == ["1.10", "1.2a", "1.2"]


def test_newer_pins_existing_behaviour_for_numeric_versions():
    assert newer_versions(["1.0", "1.10", "1.2", "0.9"], "1.2") == ["1.10"]
    assert newer_versions(["1.0", "2.0"], None) == ["2.0", "1.0"]
    assert newer_versions(["1.0", "0.9"], "5.0") == ["1.0"]  # nothing newer: the newest one


@pytest.mark.parametrize("a,b", [("v1.2", "1.2"), ("V1.2", "1.2"), (" 1.2 ", "1.2"), ("1_2", "1.2"), ("1-2", "1.2")])
def test_prefixes_and_separators_are_ignored(a, b):
    assert version_key(a) == version_key(b)


@pytest.mark.parametrize("v", [None, "", "  ", "v", "..", "weird"])
def test_empty_and_odd_input_gives_a_comparable_key(v):
    sorted([version_key(v), version_key("1.2a"), version_key("3")])  # must not raise


def test_ordering_is_total_for_mixed_parts():
    vs = ["1.2", "1.2a", "1.10", "1.2b", "beta", "2", "1.10-rc1", "v3.0.1", "0.9"]
    ordered = sorted(vs, key=version_key)
    assert ordered.index("1.2") < ordered.index("1.2a") < ordered.index("1.2b") < ordered.index("1.10")
    assert newer_versions(vs, None)[0] in vs
