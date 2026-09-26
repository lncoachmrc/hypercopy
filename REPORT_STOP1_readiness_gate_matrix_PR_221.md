# REPORT — STOP 1 readiness gate matrix — PR #221

## Identità

- Base: `main = 0c0e073b9e892fea14e4e4f2e41c6de1eff90d88`.
- Branch: `codex/risex-readiness-testnet-gate-matrix`.
- PR: #221, non draft.
- RED commit: `42822b73878a67b6e6758d79230267b798f8d061` — `test: RED continuous readiness follows RISEx gate matrix`.
- GREEN commit: `d563fde9741457f004d86773a0d31ea04181faee` — `fix: use RISEx worker gate matrix for continuous readiness`.
- Railway: nessuna modifica e nessun servizio acceso.

## Correzione applicata

`run_global_risex_continuous_readiness()` non mantiene più una seconda implementazione della policy. I controlli manuali su `RISEX_SIGNED_WRITES_ENABLED` e `ADR_0006_MAINNET_GATE_ACCEPTED` sono sostituiti da:

```python
assert_risex_worker_write_allowed(
    network='testnet',
    env=os.environ,
)
```

La funzione condivisa aggiunge l'opt-in `RISEX_SIGNED_WRITES_ENABLED` e delega la matrice ambiente/network ad `assert_risex_environment_allowed`. La readiness continua resta hardwired su `testnet`. Il guard main-wallet, il fingerprint pinnato e `operatorhub_bypass_disabled` restano nello stesso ordine relativo rispetto alla parte non modificata.

## Test modificato preesistente — prima / dopo

L'unica asserzione preesistente autorizzata a cambiare era il controllo sorgente che pretendeva che la readiness contenesse direttamente i due gate ora delegati. L'asserzione stessa resta identica; vengono rimossi soltanto i due materiali che non devono più essere duplicati nella readiness.

### Prima

```python
@pytest.mark.parametrize(
    "required_material",
    [
        "PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT",
        "RISEX_SIGNED_WRITES_ENABLED",
        "ADR_0006_MAINNET_GATE_ACCEPTED",
        "operatorhub_bypass_disabled",
    ],
)
def test_global_readiness_contains_required_fail_closed_gate_unit(required_material: str) -> None:
    runner, _attestation = _require_global_readiness_contract()
    source = inspect.getsource(runner)
    assert required_material in source, (
        f"RED: global continuous readiness does not enforce {required_material}"
    )
```

### Dopo

```python
@pytest.mark.parametrize(
    "required_material",
    [
        "PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT",
        "operatorhub_bypass_disabled",
    ],
)
def test_global_readiness_contains_required_fail_closed_gate_unit(required_material: str) -> None:
    runner, _attestation = _require_global_readiness_contract()
    source = inspect.getsource(runner)
    assert required_material in source, (
        f"RED: global continuous readiness does not enforce {required_material}"
    )
```

Le asserzioni relative a `PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT` e `operatorhub_bypass_disabled` restano invariate.

## Ragione specifica per cui passa ciascun test introdotto/modificato

| Test | Ragione specifica |
|---|---|
| `test_global_readiness_contains_required_fail_closed_gate_unit` | La readiness continua contiene ancora direttamente il fingerprint pinnato e il guard `operatorhub_bypass_disabled`; i due controlli delegati non sono più richiesti come letterali nel runner. |
| `test_global_readiness_allows_isolated_testnet_gate_matrix_with_latch_false_unit` | Con `ENABLE_LIVE_TRADING="false"`, `RISEX_SIGNED_WRITES_ENABLED="true"`, network hardwired `testnet` e latch `False`, `assert_risex_worker_write_allowed` ammette il test stack; i transport/preflight finti completano la readiness e viene restituita l'attestazione. |
| `test_global_readiness_blocks_live_environment_when_latch_false_unit` | `ENABLE_LIVE_TRADING="true"` viene interpretato come ambiente live/real-capital; con latch `False`, `assert_risex_environment_allowed` solleva `SignedTestnetBlocked`. |
| `test_global_readiness_blocks_missing_or_malformed_live_environment_unit[None]` | `ENABLE_LIVE_TRADING` assente non identifica il test stack isolato; con latch `False` il gate shared fallisce chiuso. |
| `test_global_readiness_blocks_missing_or_malformed_live_environment_unit[False]` | La stringa `"False"` è malformata perché l'unico valore isolato valido è raw `"false"`; il gate la tratta come live e blocca. |
| `test_risex_worker_gate_blocks_mainnet_on_isolated_test_stack_unit` | Anche con `ENABLE_LIVE_TRADING="false"`, il shared environment gate rifiuta `network="mainnet"`. |
| `test_global_readiness_blocks_without_signed_write_opt_in_unit` | Il worker gate richiede esattamente `RISEX_SIGNED_WRITES_ENABLED="true"`; `"false"` viene bloccato prima delle letture provider. |
| `test_global_readiness_preserves_operatorhub_bypass_guard_unit` | Dopo aver superato la matrice testnet, `operatorhub_bypass_disabled=False` continua a produrre `SignedTestnetBlocked` con il messaggio esistente. |
| `test_global_readiness_rejects_main_wallet_key_and_arm_never_opens_unit` (preesistente, invariato) | `reject_main_wallet_key_inputs(os.environ)` resta il primo controllo della readiness; la presenza della main-wallet key blocca prima del gate provider. |

## Sentinelle richieste

- `test_testnet_adapter_hardwire_must_be_removed_before_adr_0006_acceptance`: file non modificato; full `pytest -q` GREEN sul GREEN head.
- `test_ci_process_environment_keeps_risex_blocked_by_default`: file non modificato; full `pytest -q` GREEN sul GREEN head.

## Prova RED e sensibilità

### 1. Ripristino del controllo latch

Il commit RED `42822b73878a67b6e6758d79230267b798f8d061` conserva la vecchia implementazione della readiness, quindi equivale alla mutazione richiesta “ripristinare il controllo sul latch”. Il primo run CI fu cancellato dal push del GREEN prima di `pytest`; è stato rilanciato sullo stesso SHA senza modificare la storia.

- CI run: `36261359041`.
- Backend: **FAILURE** su `pytest -q`.
- Risultato: **2 failed, 1130 passed, 14 warnings**.
- Fallimento determinante: `test_global_readiness_allows_isolated_testnet_gate_matrix_with_latch_false_unit` → `SignedTestnetBlocked: ADR-0006 mainnet gate is not accepted`.
- Anche `test_global_readiness_preserves_operatorhub_bypass_guard_unit` diventa rosso perché il latch intercetta la readiness prima del guard OperatorHub, producendo lo stesso messaggio ADR-0006.

Questa è la prova eseguibile che il test testnet diventa rosso se viene ripristinato il latch nella readiness.

### 2. Rimozione della delega al shared environment gate

È stata eseguita fuori dal branch, senza commit, una mutazione isolata del corpo corrente di `assert_risex_worker_write_allowed`: mantenuto il controllo `RISEX_SIGNED_WRITES_ENABLED`, rimossa soltanto la delega `return assert_risex_environment_allowed(network=network, env=env)`. Con latch `False`, gli stessi input dei test danno:

| Caso | Implementazione corrente | Mutazione senza shared gate | Esito del test atteso |
|---|---|---|---|
| `ENABLE_LIVE_TRADING="true"`, testnet | bloccato | non bloccato | RED |
| `network="mainnet"`, `ENABLE_LIVE_TRADING="false"` | bloccato | non bloccato | RED |
| `ENABLE_LIVE_TRADING` assente, testnet | bloccato | non bloccato | RED |

Questa mutazione dimostra che i tre casi dipendono dalla singola definizione `assert_risex_environment_allowed`; il worker gate non può perdere quella delega senza rendere rossi i contratti della matrice.

## CI sul GREEN head

GREEN head verificato: `d563fde9741457f004d86773a0d31ea04181faee`.

- CI run `36261378328`: **SUCCESS**.
- `repository-tree`: SUCCESS.
- `backend`: SUCCESS.
  - ruff `E9,F63,F7,F82`: SUCCESS.
  - pyright: SUCCESS.
  - compileall: SUCCESS.
  - `alembic upgrade head`: SUCCESS.
  - `pytest -q`: SUCCESS.
  - `pip-audit -r requirements.txt`: **SUCCESS**.
- `frontend`: SUCCESS, incluso audit.
- `landing`: SUCCESS, incluso audit.
- `secrets` / Gitleaks: SUCCESS.
- CodeQL run `36261378305`: **SUCCESS** per Python e JavaScript/TypeScript.

## Reviewer

Un thread è stato aperto sul commit RED `42822b7387`, segnalando correttamente che quel commit da solo rende `pytest` rosso. È stata pubblicata risposta con lo SHA GREEN `d563fde9741457f004d86773a0d31ea04181faee`, spiegando che RED e GREEN devono restare separati per contratto. Il thread è stato lasciato **unresolved**, come richiesto.

## Diff operativo integrale — base → GREEN

Il diff seguente è l'intero diff operativo della PR prima dell'aggiunta di questo report. Il report è escluso dal diff per evitare auto-riferimento.

```diff
diff --git a/backend/app/services/risex_execution_worker_extension.py b/backend/app/services/risex_execution_worker_extension.py
index abaac30..2fab011 100644
--- a/backend/app/services/risex_execution_worker_extension.py
+++ b/backend/app/services/risex_execution_worker_extension.py
@@ -27,7 +27,10 @@
     SignedTestnetBlocked,
     reject_main_wallet_key_inputs,
 )
-from app.services.risex_order_preparation import ADR_0006_MAINNET_GATE_ACCEPTED
+from app.services.risex_order_preparation import (
+    ADR_0006_MAINNET_GATE_ACCEPTED,
+    assert_risex_worker_write_allowed,
+)
 from app.services.risex_execution_control import (
     arm_finalization_fence,
     claim_control_request,
@@ -132,10 +135,10 @@ async def run_global_risex_continuous_readiness(
     reject_main_wallet_key_inputs(os.environ)
     if not PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT:
         raise SignedTestnetBlocked('RISEx pinned deployment fingerprint is unavailable')
-    if os.environ.get('RISEX_SIGNED_WRITES_ENABLED') != 'true':
-        raise SignedTestnetBlocked('RISEX_SIGNED_WRITES_ENABLED must be explicitly true')
-    if ADR_0006_MAINNET_GATE_ACCEPTED is not True:
-        raise SignedTestnetBlocked('ADR-0006 mainnet gate is not accepted')
+    assert_risex_worker_write_allowed(
+        network='testnet',
+        env=os.environ,
+    )
     if operatorhub_bypass_disabled is not True:
         raise SignedTestnetBlocked('operatorhub_bypass_disabled must be explicitly true')
 
diff --git a/backend/tests/unit/test_risex_part_c_global_window_red_unit.py b/backend/tests/unit/test_risex_part_c_global_window_red_unit.py
index 1a1d4ee..b27d97a 100644
--- a/backend/tests/unit/test_risex_part_c_global_window_red_unit.py
+++ b/backend/tests/unit/test_risex_part_c_global_window_red_unit.py
@@ -1,6 +1,7 @@
 from __future__ import annotations
 
 import inspect
+import os
 import uuid
 from types import SimpleNamespace
 
@@ -8,7 +9,11 @@
 from pydantic import ValidationError
 
 from app.schemas.admin import RISExExecutionControlAction
-from app.services import risex_admin_extension, risex_execution_worker_extension
+from app.services import (
+    risex_admin_extension,
+    risex_execution_worker_extension,
+    risex_order_preparation,
+)
 from app.services.risex_execution_window import (
     RISExExecutionState,
     RISExOperationalWindowController,
@@ -203,8 +208,6 @@ def test_global_readiness_never_loads_user_identity_unit() -> None:
     "required_material",
     [
         "PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT",
-        "RISEX_SIGNED_WRITES_ENABLED",
-        "ADR_0006_MAINNET_GATE_ACCEPTED",
         "operatorhub_bypass_disabled",
     ],
 )
@@ -216,6 +219,160 @@ def test_global_readiness_contains_required_fail_closed_gate_unit(required_mater
     )
 
 
+def _set_gate_latch(monkeypatch: pytest.MonkeyPatch, accepted: bool) -> None:
+    monkeypatch.setattr(
+        risex_execution_worker_extension,
+        "ADR_0006_MAINNET_GATE_ACCEPTED",
+        accepted,
+    )
+    monkeypatch.setattr(
+        risex_order_preparation,
+        "ADR_0006_MAINNET_GATE_ACCEPTED",
+        accepted,
+    )
+
+
+def _stub_passing_global_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
+    monkeypatch.setattr(
+        risex_execution_worker_extension,
+        "PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT",
+        "test-fingerprint",
+    )
+
+    async def valid_deployment(_api, _rpc, *, network):
+        assert network == "testnet"
+        return SimpleNamespace()
+
+    def passing_preflight(_deployment, *, expected_fingerprint):
+        assert expected_fingerprint == "test-fingerprint"
+        return SimpleNamespace(
+            verdict="PASS",
+            deployment_identity_verified=True,
+        )
+
+    monkeypatch.setattr(
+        risex_execution_worker_extension,
+        "collect_runtime_deployment_evidence",
+        valid_deployment,
+    )
+    monkeypatch.setattr(
+        risex_execution_worker_extension,
+        "evaluate_pinned_deployment_preflight",
+        passing_preflight,
+    )
+
+
+@pytest.mark.asyncio
+async def test_global_readiness_allows_isolated_testnet_gate_matrix_with_latch_false_unit(
+    monkeypatch: pytest.MonkeyPatch,
+) -> None:
+    runner, attestation = _require_global_readiness_contract()
+    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
+    monkeypatch.setenv("RISEX_SIGNED_WRITES_ENABLED", "true")
+    _set_gate_latch(monkeypatch, False)
+    _stub_passing_global_readiness(monkeypatch)
+
+    result = await runner(
+        api=object(),
+        rpc=object(),
+        operatorhub_bypass_disabled=True,
+    )
+
+    assert isinstance(result, attestation)
+
+
+@pytest.mark.asyncio
+async def test_global_readiness_blocks_live_environment_when_latch_false_unit(
+    monkeypatch: pytest.MonkeyPatch,
+) -> None:
+    runner, _attestation = _require_global_readiness_contract()
+    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
+    monkeypatch.setenv("RISEX_SIGNED_WRITES_ENABLED", "true")
+    _set_gate_latch(monkeypatch, False)
+
+    with pytest.raises(SignedTestnetBlocked):
+        await runner(
+            api=object(),
+            rpc=object(),
+            operatorhub_bypass_disabled=True,
+        )
+
+
+@pytest.mark.asyncio
+@pytest.mark.parametrize("live_value", [None, "False"])
+async def test_global_readiness_blocks_missing_or_malformed_live_environment_unit(
+    monkeypatch: pytest.MonkeyPatch,
+    live_value: str | None,
+) -> None:
+    runner, _attestation = _require_global_readiness_contract()
+    monkeypatch.setenv("RISEX_SIGNED_WRITES_ENABLED", "true")
+    if live_value is None:
+        monkeypatch.setenv("ENABLE_LIVE_TRADING", "temporary")
+        monkeypatch.delenv("ENABLE_LIVE_TRADING")
+    else:
+        monkeypatch.setenv("ENABLE_LIVE_TRADING", live_value)
+    _set_gate_latch(monkeypatch, False)
+
+    with pytest.raises(SignedTestnetBlocked):
+        await runner(
+            api=object(),
+            rpc=object(),
+            operatorhub_bypass_disabled=True,
+        )
+
+
+def test_risex_worker_gate_blocks_mainnet_on_isolated_test_stack_unit(
+    monkeypatch: pytest.MonkeyPatch,
+) -> None:
+    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
+    monkeypatch.setenv("RISEX_SIGNED_WRITES_ENABLED", "true")
+    _set_gate_latch(monkeypatch, False)
+
+    with pytest.raises(SignedTestnetBlocked):
+        risex_order_preparation.assert_risex_worker_write_allowed(
+            network="mainnet",
+            env=os.environ,
+        )
+
+
+@pytest.mark.asyncio
+async def test_global_readiness_blocks_without_signed_write_opt_in_unit(
+    monkeypatch: pytest.MonkeyPatch,
+) -> None:
+    runner, _attestation = _require_global_readiness_contract()
+    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
+    monkeypatch.setenv("RISEX_SIGNED_WRITES_ENABLED", "false")
+    _set_gate_latch(monkeypatch, False)
+
+    with pytest.raises(SignedTestnetBlocked):
+        await runner(
+            api=object(),
+            rpc=object(),
+            operatorhub_bypass_disabled=True,
+        )
+
+
+@pytest.mark.asyncio
+async def test_global_readiness_preserves_operatorhub_bypass_guard_unit(
+    monkeypatch: pytest.MonkeyPatch,
+) -> None:
+    runner, _attestation = _require_global_readiness_contract()
+    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
+    monkeypatch.setenv("RISEX_SIGNED_WRITES_ENABLED", "true")
+    _set_gate_latch(monkeypatch, False)
+    _stub_passing_global_readiness(monkeypatch)
+
+    with pytest.raises(
+        SignedTestnetBlocked,
+        match="operatorhub_bypass_disabled must be explicitly true",
+    ):
+        await runner(
+            api=object(),
+            rpc=object(),
+            operatorhub_bypass_disabled=False,
+        )
+
+
 def test_continuous_readiness_no_longer_calls_manual_signer_bound_runner_unit() -> None:
     source = inspect.getsource(risex_execution_worker_extension._run_readiness)
     assert "run_signed_testnet_readiness" not in source, (

```

## Esito

STOP 1 corretto senza aprire mainnet e senza introdurre una seconda matrice di gate. STOP 2 e STOP 3 non sono stati toccati. Nessun merge eseguito.