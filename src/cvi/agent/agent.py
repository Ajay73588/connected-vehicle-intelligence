"""Fleet Intelligence Agent orchestrator:
question -> input guardrails -> intent -> tool plan -> permission+schema checks -> execution -> output validation ->
answer composition (+ optional grounded LLM narration) -> audit. Every step is appended to a hash-chained audit table."""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid

import orjson
from pydantic import ValidationError

from cvi.agent import llm
from cvi.agent.guardrails import (
    GuardrailViolation,
    new_confirmation_token,
    screen_input,
    validate_tool_output,
    verify_grounding,
)
from cvi.agent.planner import plan as make_plan
from cvi.agent.responder import compose
from cvi.agent.tools import TOOLS
from cvi.api.context import AppContext
from cvi.api.security import Principal
from cvi.common import metrics
from cvi.storage.pg import append_audit, tenant_tx

logger = logging.getLogger("agent")
CONFIRM_TTL_S = 300
MAX_TOOL_CALLS = 6


class Auditor:
    def __init__(self, ctx: AppContext, p: Principal, session_id: str):
        self.ctx, self.p, self.session = ctx, p, session_id

    def log(self, step: str, tool: str | None = None, payload: dict | None = None, allowed: bool = True) -> None:
        with tenant_tx(self.ctx.pool, self.p.tenant_id) as c:
            append_audit(c, "agent_audit", self.p.tenant_id, {"user_id": self.p.user_id, "session_id": self.session, "step": step,
                                                              "tool": tool, "payload": payload or {}, "allowed": allowed})


def _run_tool(ctx: AppContext, p: Principal, aud: Auditor, name: str, raw_args: dict, confirmed: bool = False) -> dict:
    tool = TOOLS.get(name)
    if tool is None:
        aud.log("guardrail", name, {"reason": "unknown tool"}, allowed=False)
        raise GuardrailViolation("unknown_tool", f"tool {name} does not exist")
    if not p.can(tool.permission):
        metrics.agent_tool_calls_total.labels(name, "denied").inc()
        aud.log("guardrail", name, {"reason": f"missing permission {tool.permission}"}, allowed=False)
        raise GuardrailViolation("forbidden", f"Your role is not allowed to use '{name}' (needs {tool.permission}).")
    try:
        args = tool.args(**{k: v for k, v in raw_args.items() if v is not None})
    except ValidationError as e:
        metrics.agent_tool_calls_total.labels(name, "invalid").inc()
        aud.log("guardrail", name, {"reason": "invalid arguments", "errors": e.errors(include_url=False, include_context=False)[:3]}, allowed=False)
        raise GuardrailViolation("invalid_args", f"I could not use '{name}' with those arguments: {e.errors()[0]['msg']}.") from e
    aud.log("tool_call", name, {"args": args.model_dump(), "confirmed": confirmed})
    t0 = time.perf_counter()
    try:
        out = validate_tool_output(tool.fn(ctx, p, args))
    except GuardrailViolation:
        raise
    except Exception as e:  # noqa: BLE001
        metrics.agent_tool_calls_total.labels(name, "error").inc()
        aud.log("tool_result", name, {"error": str(e)[:200]}, allowed=False)
        raise
    metrics.agent_tool_calls_total.labels(name, "ok").inc()
    aud.log("tool_result", name, {"ms": round((time.perf_counter() - t0) * 1000, 1), "bytes": len(orjson.dumps(out, default=str))})
    return out


def ask(ctx: AppContext, p: Principal, message: str, session_id: str | None = None) -> dict:
    session = session_id or uuid.uuid4().hex[:12]
    aud = Auditor(ctx, p, session)
    aud.log("question", None, {"text": message[:1000]})
    try:
        if not ctx.state.allow(f"agent:{p.user_id}", 20):
            raise GuardrailViolation("rate_limited", "You are asking too quickly; please wait a moment.")
    except GuardrailViolation:
        raise
    except Exception:  # noqa: BLE001
        pass
    try:
        msg = screen_input(message)
    except GuardrailViolation as v:
        aud.log("guardrail", None, {"code": v.code, "message": v.message}, allowed=False)
        return {"session_id": session, "answer": v.message, "intent": "blocked", "tool_calls": [], "sources": [],
                "recommended_actions": [], "pending_confirmation": None, "llm_used": False, "guardrail": v.code}

    pl = make_plan(msg)
    aud.log("intent", None, {"intent": pl.intent, "entities": pl.entities, "tools": [c.tool for c in pl.calls]})
    if not pl.calls:
        ans = "Please include a 17-character VIN (e.g. 1FT…) so I can answer that." if pl.intent in ("nearby", "create_alert") else \
            "I need an alert number, e.g. 'acknowledge alert 123'." if pl.intent == "ack_alert" else "I couldn't map that to a fleet question."
        aud.log("answer", None, {"text": ans})
        return {"session_id": session, "answer": ans, "intent": pl.intent, "tool_calls": [], "sources": [], "recommended_actions": [],
                "pending_confirmation": None, "llm_used": False}

    # ---- destructive tool => never executed here, only proposed
    if any(TOOLS[c.tool].destructive for c in pl.calls if c.tool in TOOLS):
        call = next(c for c in pl.calls if TOOLS[c.tool].destructive)
        tool = TOOLS[call.tool]
        if not p.can(tool.permission):
            text = f"Your role cannot perform '{call.tool}'. Ask a fleet manager or admin."
            aud.log("guardrail", call.tool, {"reason": "missing permission"}, allowed=False)
            return {"session_id": session, "answer": text, "intent": pl.intent, "tool_calls": [], "sources": [], "recommended_actions": [],
                    "pending_confirmation": None, "llm_used": False, "guardrail": "forbidden"}
        try:
            args = tool.args(**{k: v for k, v in call.args.items() if v is not None})
        except ValidationError as e:
            return {"session_id": session, "answer": f"I couldn't build a valid request: {e.errors()[0]['msg']}.", "intent": pl.intent, "tool_calls": [],
                    "sources": [], "recommended_actions": [], "pending_confirmation": None, "llm_used": False}
        token = new_confirmation_token()
        with tenant_tx(ctx.pool, p.tenant_id) as c:
            c.execute("INSERT INTO agent_pending_actions (token, tenant_id, user_id, action, params, expires_at) VALUES (%s,%s,%s,%s,%s::jsonb,%s)",
                      [token, p.tenant_id, p.user_id, call.tool, orjson.dumps(args.model_dump()).decode(),
                       dt.datetime.now(dt.UTC) + dt.timedelta(seconds=CONFIRM_TTL_S)])
        desc = f"{tool.description.split(' (')[0]} with {args.model_dump()}"
        aud.log("confirmation", call.tool, {"state": "requested", "description": desc})
        return {"session_id": session, "answer": f"I can do that, but it changes data. Please confirm: **{desc}**.", "intent": pl.intent,
                "tool_calls": [], "sources": [], "recommended_actions": [],
                "pending_confirmation": {"token": token, "action": call.tool, "description": desc, "expires_in_s": CONFIRM_TTL_S}, "llm_used": False}

    results: dict[str, dict] = {}
    calls_meta = []
    try:
        for call in pl.calls[:MAX_TOOL_CALLS]:
            t0 = time.perf_counter()
            results[call.tool] = _run_tool(ctx, p, aud, call.tool, call.args)
            calls_meta.append({"tool": call.tool, "args": {k: v for k, v in call.args.items() if v is not None}, "ok": True,
                               "ms": round((time.perf_counter() - t0) * 1000, 1)})
    except GuardrailViolation as v:
        return {"session_id": session, "answer": v.message, "intent": pl.intent, "tool_calls": calls_meta, "sources": [], "recommended_actions": [],
                "pending_confirmation": None, "llm_used": False, "guardrail": v.code}
    except Exception as e:  # noqa: BLE001
        logger.exception("tool failure")
        return {"session_id": session, "answer": "A data source is temporarily unavailable, so I can't answer reliably right now.", "intent": pl.intent,
                "tool_calls": calls_meta, "sources": [], "recommended_actions": [], "pending_confirmation": None, "llm_used": False,
                "error": type(e).__name__}

    answer, actions = compose(pl, results)
    llm_used = False
    narrated = llm.narrate(msg, answer, results)
    if narrated:
        ok, bad = verify_grounding(narrated, [results, answer])
        if ok:
            answer, llm_used = narrated, True
        else:
            aud.log("guardrail", None, {"reason": "llm narration rejected: ungrounded numbers", "numbers": bad[:5]}, allowed=False)
    aud.log("answer", None, {"text": answer[:2000], "llm_used": llm_used, "actions": actions})
    return {"session_id": session, "answer": answer, "intent": pl.intent, "tool_calls": calls_meta,
            "sources": [{"tool": c["tool"]} for c in calls_meta], "recommended_actions": actions, "pending_confirmation": None, "llm_used": llm_used}


def confirm(ctx: AppContext, p: Principal, token: str, approve: bool = True) -> dict:
    session = "confirm-" + uuid.uuid4().hex[:8]
    aud = Auditor(ctx, p, session)
    with tenant_tx(ctx.pool, p.tenant_id) as c:
        row = c.execute("SELECT * FROM agent_pending_actions WHERE token = %s AND tenant_id = %s AND user_id = %s AND confirmed_at IS NULL "
                        "FOR UPDATE", [token, p.tenant_id, p.user_id]).fetchone()
        if not row or row["expires_at"] < dt.datetime.now(dt.UTC):
            aud.log("confirmation", None, {"state": "rejected", "reason": "unknown or expired token"}, allowed=False)
            raise GuardrailViolation("bad_token", "That confirmation is unknown, expired or already used.")
        c.execute("UPDATE agent_pending_actions SET confirmed_at = now() WHERE token = %s", [token])  # single use
    if not approve:
        aud.log("confirmation", row["action"], {"state": "declined"})
        return {"executed": False, "message": "Cancelled. Nothing was changed."}
    aud.log("confirmation", row["action"], {"state": "approved"})
    out = _run_tool(ctx, p, aud, row["action"], row["params"], confirmed=True)
    return {"executed": True, "action": row["action"], "result": out}
