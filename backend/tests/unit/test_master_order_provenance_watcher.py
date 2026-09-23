from __future__ import annotations

import inspect

from app.workers.watcher import Watcher


def test_master_order_provenance_enrichment_is_background_and_live_only() -> None:
    process_source = inspect.getsource(Watcher.process_fill)
    enrich_source = inspect.getsource(Watcher._enrich_master_event_order_provenance)

    assert "if create_copy_jobs and fill.get('oid') is not None" in process_source
    assert "self._spawn_order_provenance_enrichment" in process_source
    assert "priority=Priority.DIAGNOSTIC" in enrich_source
    assert "_hypercopy_order_provenance" in enrich_source
