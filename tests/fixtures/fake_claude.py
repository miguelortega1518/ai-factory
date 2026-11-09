#!/usr/bin/env python3
"""Imita `claude -p --output-format stream-json --json-schema ...` para los tests."""
import json, os, sys
from pathlib import Path

sys.path.insert(0, os.environ["FAKE_SRC"])
from ai_factory.agents import prompts  # noqa: E402

args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "oauth_token"}))
    sys.exit(0)

def opt(name):
    return args[args.index(name) + 1] if name in args else None

system = opt("--append-system-prompt") or ""
prompt = sys.stdin.read()
roles = {"spec_writer": prompts.SPEC_WRITER, "builder": prompts.BUILDER, "approver": prompts.APPROVER,
         "gatekeeper": prompts.GATEKEEPER, **{f"reviewer_{k}": v for k, v in prompts.REVIEWERS.items()}}
role = next(r for r, p in roles.items() if system.startswith(p))
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps({"role": role, "args": args, "env_has_api_key": "ANTHROPIC_API_KEY" in os.environ}) + "\n")

mode = os.environ.get("FAKE_MODE", "ok")
out = {
    "spec_writer": {"summary": "Añadir add()", "problem": "Falta", "acceptance_criteria": ["add(2,3)==5"],
                    "technical_approach": "calc.py", "test_plan": ["tests/test_calc.py"]},
    "builder": {"summary": "hecho", "tests_passing": True},
    "approver": {"approved": True, "reasoning": "ok"},
    "gatekeeper": {"risk": "low", "reasoning": "acotado"},
    "improver": {"summary": "Limpio", "brain_updates": [{"category": "learnings", "title": "Sumas",
                                                          "body": "Las sumas viven en calc.py"}]},
    "scout": {"proposals": []},
}.get(role, {"passed": True, "summary": f"{role} ok"})

if role == "builder":
    Path("calc.py").write_text("def add(a, b):\n    return a + b\n")
    Path("tests/test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")

print(json.dumps({"type": "system", "subtype": "init", "model": opt("--model")}))
if mode == "max_turns" and role == "spec_writer":
    print(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "sin turnos"}))
else:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
                      "total_cost_usd": 0.01, "usage": {"output_tokens": 10}, "structured_output": out}))
    print(json.dumps({"type": "system", "subtype": "task_summary"}))  # el CLI real emite eventos tras el result
