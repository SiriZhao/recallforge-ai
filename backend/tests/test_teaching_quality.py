from tests.test_learning_engine_p0 import GroundedProvider, run
from app.services.synthesis_budget import synthesize
from app.services.llm_capabilities import estimate_tokens
import json
import pytest


class RepairProvider(GroundedProvider):
    def __init__(self, repair_succeeds=True):
        super().__init__()
        self.repair_succeeds = repair_succeeds

    def generate_stage(self, stage, prompt, config, *, max_output_tokens):
        if stage == "unit_critic":
            payload = json.loads(prompt)["payload"]
            if "repaired explanation" not in payload["unit"]["markdown"] or not self.repair_succeeds:
                self.calls.append((stage, payload))
                return {"verdict": "NEEDS_REPAIR", "issues": ["Explain the selective transport condition"],
                    "claims": [{"claim": "Condition missing", "basis": "INSUFFICIENT"}]}
        if stage == "unit_repair":
            payload = json.loads(prompt)["payload"]
            self.calls.append((stage, payload))
            return {**payload["unit"], "markdown": payload["unit"]["markdown"] + "\nrepaired explanation: transport is selective."}
        return super().generate_stage(stage, prompt, config, max_output_tokens=max_output_tokens)


def test_critic_automatically_repairs_only_defective_unit_then_rechecks(monkeypatch):
    provider = RepairProvider()
    result, stats = run(monkeypatch, provider)
    names = [name for name, _ in provider.calls]
    assert names.count("unit_draft") == 1 and names.count("unit_repair") == 1 and names.count("unit_critic") == 2
    assert stats.retry_count == 1
    assert result.llm_status == "success"
    assert "repaired explanation" in result.report.overview["artifacts"]["learning_material"]["units"][0]["markdown"]


def test_repair_budget_exhaustion_is_partial_and_keeps_original_unit(monkeypatch):
    provider = RepairProvider(repair_succeeds=False)
    result, _ = run(monkeypatch, provider)
    assert result.llm_status == "partial"
    units = result.report.overview["artifacts"]["learning_material"]["units"]
    assert units[0]["status"] == "NEEDS_REPAIR" and "Membrane" in units[0]["markdown"]
    assert [name for name, _ in provider.calls].count("unit_repair") == 1


def test_synthesis_batches_stay_within_budget_and_do_not_silently_truncate():
    calls = []
    def prompt(name, payload):
        return json.dumps(payload)
    def stage(name, payload, limit, validate, identity="course"):
        assert estimate_tokens(prompt(name, payload)) <= 1000
        calls.append(identity)
        return {"canonical_markdown": "A concise source-linked synthesis."}
    output = synthesize({"unit_drafts": [{"unit_id": str(i), "markdown": "evidence concept " * 200} for i in range(10)]},
        stage=stage, make_prompt=prompt, input_budget=1000, output_budget=200, validate=lambda value: value)
    assert output["canonical_markdown"] and len(calls) > 1
    with pytest.raises(ValueError, match="one unit"):
        synthesize({"unit_drafts": [{"unit_id": "huge", "markdown": "evidence concept " * 10000}]},
            stage=stage, make_prompt=prompt, input_budget=1000, output_budget=200, validate=lambda value: value)
