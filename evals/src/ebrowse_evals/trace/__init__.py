"""Trace schema v1: the contract every harness component reads or writes.

A trace is a run directory containing:
  events.jsonl   -- append-only stream of typed records (one JSON object per line)
  blobs/         -- content-addressed large payloads (screenshots, DomSnapshots)

Records are dicts with a required "type" key; large payloads always go through
the blob store so events.jsonl stays skimmable. Readers must tolerate unknown
record types and unknown fields (forward compatibility) -- the schema grows by
addition only. See evals/docs/trace-schema.md.
"""

from ebrowse_evals.trace.records import (
    SCHEMA_VERSION,
    Anomaly,
    BrowserEvent,
    EbrowseLog,
    RunEnd,
    RunMeta,
    Step,
    Summary,
    record_from_dict,
)
from ebrowse_evals.trace.store import BlobStore, TraceReader, TraceWriter

__all__ = [
    "SCHEMA_VERSION",
    "Anomaly",
    "BlobStore",
    "BrowserEvent",
    "EbrowseLog",
    "RunEnd",
    "RunMeta",
    "Step",
    "Summary",
    "TraceReader",
    "TraceWriter",
    "record_from_dict",
]
