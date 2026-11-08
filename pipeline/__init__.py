"""Exam-dataset pipeline (spec 001-exam-data-aggregation).

An optional package that builds the exam catalog and the questions dataset and
exports suite-ready question banks. The harness never imports this package
(FR-030, enforced by tests/pipeline/test_guard.py), and this module imports
nothing so that ``python -m pipeline.corpus`` needs only the standard library
and PyYAML, not the ``pipeline`` extra.
"""
