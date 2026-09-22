from sqlalchemy import UniqueConstraint

from app.models.entities import AIProfitExitDecision


def test_profit_exit_decision_has_required_durable_identity_columns():
    columns = set(AIProfitExitDecision.__table__.columns.keys())

    assert {
        "id",
        "user_id",
        "execution_epoch_id",
        "execution_provider",
        "execution_network",
        "asset",
        "side",
        "source_cycle_id",
        "source_cycle_open_event_id",
        "source_master_position",
        "follower_position_size",
        "state_version",
        "action",
        "intent_state",
        "net_pnl",
        "pnl_complete",
        "position_verified_at",
        "decision_inputs",
        "decision_reason",
        "model_provider",
        "model_name",
        "model_version",
        "decided_at",
        "expires_at",
        "copy_job_id",
        "created_at",
        "updated_at",
    } <= columns


def test_profit_exit_decision_binds_execution_destination():
    table = AIProfitExitDecision.__table__

    assert table.c.execution_epoch_id.nullable is False
    assert table.c.execution_provider.nullable is False
    assert table.c.execution_network.nullable is False


def test_profit_exit_decision_binds_source_cycle_and_position_state():
    table = AIProfitExitDecision.__table__

    assert table.c.source_cycle_id.nullable is False
    assert table.c.source_cycle_open_event_id.nullable is False
    assert table.c.source_master_position.nullable is False
    assert table.c.follower_position_size.nullable is False
    assert table.c.state_version.nullable is False
    assert table.c.position_verified_at.nullable is False


def test_profit_exit_decision_keeps_ai_reasoning_inputs_and_expiry():
    table = AIProfitExitDecision.__table__

    assert table.c.decision_inputs.nullable is False
    assert table.c.decision_reason.nullable is False
    assert table.c.model_provider.nullable is False
    assert table.c.model_name.nullable is False
    assert table.c.decided_at.nullable is False
    assert table.c.expires_at.nullable is False


def test_profit_exit_decision_id_is_the_idempotent_identity():
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in AIProfitExitDecision.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    # Do not force one decision per source cycle:
    # a FAILED pre-submit decision may be followed by a fresh AI decision
    # in the same master cycle.
    assert (
        "user_id",
        "execution_epoch_id",
        "asset",
        "source_cycle_id",
    ) not in unique_columns
