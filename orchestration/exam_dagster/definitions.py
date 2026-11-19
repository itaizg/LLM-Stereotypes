"""The Dagster code location.

    orchestration/dev.sh

which runs ``dagster dev -m exam_dagster.definitions -d orchestration`` with the
persistent DAGSTER_HOME that holds the writer-pool limit.

No schedules and no sensors: runs are manual. The partitions are the exams in the
catalog when these definitions load (see ``assets.load_exam_ids``).
"""

from __future__ import annotations

import dagster as dg

from exam_dagster.assets import build_assets, load_exam_ids

_acquisition = build_assets(load_exam_ids())

defs = dg.Definitions(
    assets=_acquisition.assets, jobs=[_acquisition.job, _acquisition.catalog_job]
)
