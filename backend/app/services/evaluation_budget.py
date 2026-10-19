"""Opt-in local evaluation transport with durable, conservative spend reservations.

This module is never enabled by server configuration. The caller must explicitly
supply a key, fixed model, price bounds, and a private local ledger destination.
"""
import hashlib
import json
import sqlite3
import time
from pathlib import Path
import httpx


class EvaluationBudgetError(RuntimeError):
    pass


class EvaluationClient:
    def __init__(self, ledger: Path, *, key: str, model="deepseek-flash", limit_usd=0.50,
                 input_rate=0.30, output_rate=1.20, transport=None):
        if not key or model != "deepseek-flash":
            raise EvaluationBudgetError("Explicit project credential and priced model required")
        if not 0 < limit_usd <= 0.50:
            raise EvaluationBudgetError("Evaluation authorization is capped at USD 0.50")
        ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger, self.key, self.model = ledger, key, model
        self.limit, self.input_rate, self.output_rate = limit_usd, input_rate, output_rate
        self.transport = transport or httpx.Client(timeout=120)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS evaluation_calls (id TEXT PRIMARY KEY, arm TEXT, status TEXT, reserve REAL, charge REAL, request_json TEXT, response_json TEXT, latency REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS evaluation_policy (id INTEGER PRIMARY KEY, model TEXT, limit_usd REAL, input_rate REAL, output_rate REAL)")
            expected=(model,limit_usd,input_rate,output_rate)
            saved=db.execute("SELECT model,limit_usd,input_rate,output_rate FROM evaluation_policy WHERE id=1").fetchone()
            if saved and tuple(saved)!=expected:
                raise EvaluationBudgetError("Existing ledger policy differs; authorization cannot be silently expanded")
            db.execute("INSERT OR IGNORE INTO evaluation_policy VALUES(1,?,?,?,?)",expected)

    def connect(self):
        return sqlite3.connect(self.ledger, timeout=30)

    def summary(self):
        with self.connect() as db:
            rows = db.execute("SELECT arm,status,reserve,charge FROM evaluation_calls").fetchall()
        return {"limit_usd": self.limit, "accounted_upper_usd": sum(r[3] if r[3] is not None else r[2] for r in rows),
                "requests": len(rows), "uncertain_requests": sum(r[1] != "completed" for r in rows)}

    def chat(self, arm, messages, max_tokens=3000, *, call_limit=16):
        body = {"model": self.model, "messages": messages, "temperature": 0.3,
                "thinking": {"type": "disabled"}, "max_tokens": max_tokens,"response_format":{"type":"json_object"}}
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True)
        identity = hashlib.sha256((arm + encoded).encode()).hexdigest()
        # UTF-8 byte length is a deliberately loose upper bound on text tokens.
        # No cache-hit discount is assumed. Unknown/failed requests stay reserved.
        input_bound = len(encoded.encode()) + 1024
        reserved = (input_bound * self.input_rate + max_tokens * self.output_rate) / 1_000_000
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT status,response_json FROM evaluation_calls WHERE id=?", (identity,)).fetchone()
            if saved and saved[0] == "completed":
                return json.loads(saved[1])
            if db.execute("SELECT count(*) FROM evaluation_calls WHERE status != 'completed'").fetchone()[0]:
                raise EvaluationBudgetError("Uncertain prior billing: paid evaluation stopped")
            spent = db.execute("SELECT coalesce(sum(coalesce(charge,reserve)),0) FROM evaluation_calls").fetchone()[0]
            arm_calls = db.execute("SELECT count(*) FROM evaluation_calls WHERE arm=?", (arm,)).fetchone()[0]
            prior_usage = [json.loads(row[0]).get("usage", {}) for row in db.execute("SELECT response_json FROM evaluation_calls WHERE arm=? AND status='completed'", (arm,))]
            used_input = sum(item.get("prompt_tokens", 0) for item in prior_usage)
            used_output = sum(item.get("completion_tokens", 0) for item in prior_usage)
            if used_input + input_bound > 200000 or used_output + max_tokens > 30000:
                raise EvaluationBudgetError("Common per-arm token budget reached")
            if spent + reserved > self.limit - 0.01 or arm_calls >= call_limit:
                raise EvaluationBudgetError("Conservative spend/call limit reached")
            db.execute("INSERT INTO evaluation_calls VALUES(?,?,?, ?,NULL,?,NULL,NULL)", (identity, arm, "reserved", reserved, encoded))
        started = time.perf_counter()
        try:
            response = self.transport.post("https://api.deepseek.com/chat/completions", headers={"Authorization": "Bearer " + self.key}, json=body)
            if response.status_code != 200:
                raise EvaluationBudgetError(f"HTTP_{response.status_code}; reservation retained, no retry")
            data = response.json()
            usage = data.get("usage", {})
            prompt_tokens, output_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if not isinstance(prompt_tokens, int) or not isinstance(output_tokens, int) or min(prompt_tokens, output_tokens) < 0:
                raise EvaluationBudgetError("Missing usage: billing cannot be established")
            charge = (prompt_tokens * self.input_rate + output_tokens * self.output_rate) / 1_000_000
            if prompt_tokens > input_bound or output_tokens > max_tokens or charge > reserved:
                raise EvaluationBudgetError("Usage exceeded reserved bound: paid evaluation stopped")
            if not data.get("choices"):
                raise EvaluationBudgetError("Missing completion; reservation retained")
            with self.connect() as db:
                db.execute("UPDATE evaluation_calls SET status='completed',charge=?,response_json=?,latency=? WHERE id=?",
                    (charge, json.dumps(data, ensure_ascii=False), time.perf_counter() - started, identity))
            return data
        except EvaluationBudgetError:
            raise
        except Exception as exc:
            # Exception text can contain headers/response bodies. Expose type only.
            raise EvaluationBudgetError(f"Transport_{type(exc).__name__}; reservation retained") from None


class BudgetedStageProvider:
    display_name = "DeepSeek local evaluation"
    def __init__(self, client, arm):
        self.client, self.arm = client, arm
        self.default_model = client.model

    def generate_stage(self, stage, prompt, config, *, max_output_tokens):
        limits = {"chunk_understanding": 4200, "course_model": 12000, "study_blueprint": 3000,
                  "optional_artifacts": 6000, "unit_draft": 3500, "unit_critic": 4000, "unit_repair": 3500, "course_synthesis": 8192}
        response = self.client.chat(self.arm, [{"role": "user", "content": prompt}], limits.get(stage, min(max_output_tokens,3000)))
        if response["choices"][0].get("finish_reason") == "length":
            raise EvaluationBudgetError("Truncated stage output; cached for diagnosis, not accepted")
        return response["choices"][0]["message"]["content"]
