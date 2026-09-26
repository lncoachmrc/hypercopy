from __future__ import annotations


# This is the single continuous-worker source for ADR-0002's
# perps_permission_and_fund_movement_path_absent criterion. Its truth is guarded
# structurally by test_risex_continuous_capabilities_red_unit.py, which proves
# the signed continuous transport can submit only the typed Perps order path.
FUND_MOVEMENT_PATH_ABSENT = True
