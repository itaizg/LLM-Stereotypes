"""Exam acquisition as Dagster assets, partitioned by exam.

It holds no stage logic (FR-001): every asset is one or two pipeline commands through
``stage.run_stage``. What Dagster adds is order, one isolated partition per exam, and a
warning while candidates wait for a human review. Contract: contracts/assets.md.

    catalog ─► discovered[exam] ─► fetched[exam]

The data stays in the pipeline's own store, so there is no IO manager and nothing is
pickled: an asset's "value" is the metadata it reports (counts and ids, never corpus
text, FR-014).

No ``from __future__ import annotations`` here: Dagster inspects the ``context``
annotation at decoration time and rejects the string form.

``build_assets`` is a factory so the partitions are an argument: ``definitions.py``
feeds it the exams the catalog lists, and tests feed it a fixed list without a
subprocess.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import dagster as dg

from exam_dagster import stage

if TYPE_CHECKING:
    # Dagster returns this type from define_asset_job but does not export it; it is
    # needed only for the annotation, which is a string (Python 3.13 evaluates
    # annotations eagerly, 3.14 lazily, and the image runs 3.13).
    from dagster._core.definitions.unresolved_asset_job_definition import (
        UnresolvedAssetJobDefinition,
    )

GROUP = "exam_acquisition"


def load_exam_ids() -> list[str]:
    """The exams in the catalog, from ``pipeline exams``.

    Read-only: no lock and no database. Called when the definitions load, so an unknown
    exam is not a partition and cannot be launched (FR-026). A broken catalog exits 3
    and the code location fails to load with that message, which is where a maintainer
    should see it. After editing the catalog, use "Reload definitions" in the UI.
    """
    _, lines = stage.run_stage_output(["exams"])
    return stage.listed_ids(lines, "exams")


@dataclass(frozen=True)
class AcquisitionAssets:
    """Everything ``definitions.py`` registers, built for one list of exams."""

    partitions: dg.StaticPartitionsDefinition
    catalog: dg.AssetsDefinition
    discovered: dg.AssetsDefinition
    fetched: dg.AssetsDefinition
    job: "UnresolvedAssetJobDefinition"

    @property
    def assets(self) -> list[dg.AssetsDefinition]:
        return [self.catalog, self.discovered, self.fetched]


def build_assets(
    exam_ids: Sequence[str], *, retry_policy: dg.RetryPolicy = stage.RETRY_POLICY
) -> AcquisitionAssets:
    """The acquisition assets and their job, partitioned by ``exam_ids``."""
    partitions = dg.StaticPartitionsDefinition(list(exam_ids))

    @dg.asset(
        group_name=GROUP,
        pool=stage.PIPELINE_WRITER,
        retry_policy=retry_policy,
        description="Validate the exam catalog once (an invalid one stops everything).",
    )
    def catalog(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
        done = stage.run_stage(["catalog"], logger=context.log)
        return dg.MaterializeResult(metadata={"summary": done["summary"]})

    @dg.asset(
        group_name=GROUP,
        partitions_def=partitions,
        deps=[catalog],
        pool=stage.PIPELINE_WRITER,
        retry_policy=retry_policy,
        check_specs=[
            dg.AssetCheckSpec(
                "pending_review",
                asset="discovered",
                description="Candidates still waiting for a human review (a warning).",
            )
        ],
        description="Candidate URLs for one exam, and the ones still awaiting review.",
    )
    def discovered(
        context: dg.AssetExecutionContext,
    ) -> Iterator[dg.MaterializeResult | dg.AssetCheckResult]:
        exam = context.partition_key
        found = stage.run_stage(
            ["discover", "--exam", exam], exam=exam, logger=context.log
        )
        _, lines = stage.run_stage_output(
            ["candidates", "--exam", exam], exam=exam, logger=context.log
        )
        pending = stage.pending_ids(lines)
        if pending:
            context.log.warning(
                "pending candidates for %s: %d (%s)",
                exam,
                len(pending),
                ", ".join(pending),
            )
        yield dg.MaterializeResult(
            metadata={
                "summary": found["summary"],
                "pending_count": len(pending),
                "pending_ids": dg.MetadataValue.json(pending),
            }
        )
        # Pending candidates are a normal result, never a failure (FR-007): they need a
        # decision the pipeline cannot make. WARN shows it without failing the run.
        yield dg.AssetCheckResult(
            check_name="pending_review",
            passed=not pending,
            severity=dg.AssetCheckSeverity.WARN,
            metadata={"pending_count": len(pending)},
        )

    @dg.asset(
        group_name=GROUP,
        partitions_def=partitions,
        deps=[discovered],
        pool=stage.PIPELINE_WRITER,
        retry_policy=retry_policy,
        description="Fetch the approved sources for one exam.",
    )
    def fetched(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
        exam = context.partition_key
        done = stage.run_stage(["fetch", "--exam", exam], exam=exam, logger=context.log)
        return dg.MaterializeResult(metadata={"summary": done["summary"]})

    job = dg.define_asset_job(
        "exam_acquisition",
        selection=dg.AssetSelection.groups(GROUP),
        description="Launch a backfill over the exams to run (all, or only some).",
    )
    return AcquisitionAssets(partitions, catalog, discovered, fetched, job)
