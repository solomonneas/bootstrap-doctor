# bootstrap-doctor - Implementation Notes

Running log of design decisions, deviations from the spec, and tradeoffs discovered during build.

## 2026-05-18 - Scaffold
- Initial scaffold created, mirrors memory-doctor's hatchling/src-layout/pytest pattern.
- Added `requests>=2.31` as the only runtime dep (for the gateway client in `judge.py`).
- (subsequent entries appended as work proceeds)
