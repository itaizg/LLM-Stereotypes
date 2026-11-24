"""The partitions are the exams ``pipeline exams`` lists, read when definitions load."""

from __future__ import annotations

from typing import TYPE_CHECKING

import dagster as dg
import pytest
from exam_dagster.assets import build_assets, load_exam_ids

if TYPE_CHECKING:
    from fakes import FakeRunner


def test_the_listed_ids_become_the_partition_keys(fake_runner: FakeRunner) -> None:
    fake_runner.script[("exams",)] = (0, "csat\nenem\nexams: count=2\n", "")

    ids = load_exam_ids()

    assert ids == ["csat", "enem"]
    assert build_assets(ids).partitions.get_partition_keys() == ["csat", "enem"]
    assert fake_runner.argvs == [("exams",)]


def test_a_broken_catalog_stops_the_definitions_from_loading(
    fake_runner: FakeRunner,
) -> None:
    fake_runner.script[("exams",)] = (3, "", "refused: catalog invalid")

    with pytest.raises(dg.Failure):
        load_exam_ids()


def test_an_unknown_exam_is_not_a_partition() -> None:
    partitions = build_assets(["csat"]).partitions

    assert not partitions.has_partition_key("nope")
