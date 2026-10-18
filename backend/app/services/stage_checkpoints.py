"""Content-addressed, schema-validated stage execution for the active pipeline."""
import hashlib
import json

PROMPT_VERSION = "learning-engine-p0.1"


class CheckpointWriteError(RuntimeError):
    pass


def stable_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


class StageRunner:
    def __init__(self, config, stats, checkpoints, callback):
        self.config = config
        self.stats = stats
        self.checkpoints = dict(checkpoints or {})
        self.callback = callback

    def run(self, stage, identity, prompt, output_tokens, invoke, validate):
        key = f"v4:{stage}:{stable_hash({'identity': identity, 'prompt': prompt, 'version': PROMPT_VERSION, 'config': self.config.model_dump(exclude={'api_key'}), 'output_tokens': output_tokens})}"
        saved = self.checkpoints.get(key, {})
        if saved.get("status") == "completed" and saved.get("content") is not None:
            try:
                value = validate(saved["content"])
            except (ValueError, TypeError, KeyError):
                pass  # Old/corrupt saved artifacts must pass the current schema.
            else:
                self.stats.resumed_checkpoints += 1
                return value
        self._save(key, "running", None)
        self.stats.llm_calls += 1
        try:
            value = validate(invoke())
        except Exception:
            self._save(key, "failed", None)
            raise
        content = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        self._save(key, "completed", content)
        self.stats.completed_checkpoints += 1
        return value

    def _save(self, key, status, content):
        if self.callback:
            try:
                self.callback(key, 0, 0, status, content, None, None)
            except Exception as exc:
                raise CheckpointWriteError("GENERATION_PERSIST_ERROR: stage checkpoint write failed") from exc
        self.checkpoints[key] = {"status": status, "content": content}
