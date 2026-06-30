"""Node functions for the LangGraph workflow.

Each function receives AgentState and returns a partial state update dict.
Do NOT mutate input state — return new values only.

LLM REQUIREMENT:
- classify_node MUST use a real LLM call (structured output for intent classification)
- answer_node MUST use a real LLM call (grounded response generation)
- evaluate_node SHOULD use LLM-as-judge (bonus points; heuristic acceptable for base score)
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

from .llm import get_llm
from .state import AgentState, ApprovalDecision, Route, make_event


class ClassificationResult(BaseModel):
    route: Literal["simple", "tool", "missing_info", "risky", "error"] = Field(
        description="The best support-ticket route."
    )
    reason: str = Field(description="Short reason for the selected route.")


def _route_fallback(query: str) -> str:
    """Small local fallback used only when no LLM provider is configured."""
    text = query.lower()
    risky_words = ["refund", "delete", "send", "cancel", "remove", "close account"]
    tool_words = ["lookup", "look up", "order", "status", "tracking", "search", "find"]
    vague_words = ["fix it", "help me", "it?", "this?", "can you fix"]
    error_words = ["timeout", "failure", "error", "crash", "unavailable", "cannot recover"]

    if any(word in text for word in risky_words):
        return Route.RISKY.value
    if any(word in text for word in tool_words):
        return Route.TOOL.value
    if any(word in text for word in vague_words) or len(text.split()) <= 3:
        return Route.MISSING_INFO.value
    if any(word in text for word in error_words):
        return Route.ERROR.value
    return Route.SIMPLE.value


def _text_from_llm_response(response: object) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, list):
        return "\n".join(str(item) for item in content)
    return str(content)


# ─── EXAMPLE: working node (provided for reference) ──────────────────
def intake_node(state: AgentState) -> dict:
    """Normalize raw query. This node is provided as a working example."""
    query = state.get("query", "").strip()
    return {
        "query": query,
        "messages": [f"intake:{query[:40]}"],
        "events": [make_event("intake", "completed", "query normalized")],
    }


# ─── TODO(student): implement ALL nodes below ────────────────────────


def classify_node(state: AgentState) -> dict:
    """Classify the query into a route using an LLM.

    *** MUST use a real LLM call — keyword-only heuristics will lose points. ***

    Use .with_structured_output() or equivalent to get reliable enum classification.
    The LLM should classify into one of: simple, tool, missing_info, risky, error.

    Hints:
    - See llm.py for the get_llm() helper
    - Use Pydantic model or TypedDict with .with_structured_output()
    - Set risk_level to "high" for risky routes, "low" otherwise
    - Priority guide: risky > tool > missing_info > error > simple

    Return: {"route": str, "risk_level": str, "events": [make_event(...)]}
    """
    query = state.get("query", "").strip()
    prompt = f"""
Classify this support ticket into exactly one route.

Routes:
- risky: actions with side effects, such as refunds, deletions, sending email, cancellation.
- tool: information lookup, order status, tracking, search, account lookup.
- missing_info: vague or incomplete request that lacks enough context.
- error: system failures, timeout, crash, service unavailable.
- simple: general support question answerable without tools.

Priority when multiple routes seem possible: risky > tool > missing_info > error > simple.

Ticket: {query}
"""
    try:
        classifier = get_llm(temperature=0.0).with_structured_output(ClassificationResult)
        result = classifier.invoke(prompt)
        route = result.route
        reason = result.reason
        event_type = "completed"
    except Exception as exc:
        route = _route_fallback(query)
        reason = f"fallback classification used: {exc}"
        event_type = "fallback"

    risk_level = "high" if route == Route.RISKY.value else "low"
    return {
        "route": route,
        "risk_level": risk_level,
        "messages": [f"classify:{route}"],
        "events": [
            make_event(
                "classify",
                event_type,
                "query classified",
                route=route,
                reason=reason,
            )
        ],
    }


def tool_node(state: AgentState) -> dict:
    """Execute a mock tool call.

    Simulate transient failures for error-route scenarios to test retry loops.

    Requirements:
    - Read current attempt count from state
    - If route is "error" and attempt < 2: return error result (string containing "ERROR")
    - Otherwise: return a mock success result string
    - Append result to tool_results list

    Return: {"tool_results": [result_string], "events": [make_event(...)]}
    """
    route = state.get("route", "")
    attempt = int(state.get("attempt", 0) or 0)
    query = state.get("query", "")

    if route == Route.ERROR.value and attempt < 2:
        result = f"ERROR: transient backend failure while processing attempt {attempt}"
        event_type = "failed"
    else:
        result = (
            f"SUCCESS: mock tool completed for route '{route}'. "
            f"Query='{query}'. Attempt={attempt}."
        )
        event_type = "completed"

    return {
        "tool_results": [result],
        "messages": [f"tool:{event_type}"],
        "events": [make_event("tool", event_type, "tool execution finished", attempt=attempt)],
    }


def evaluate_node(state: AgentState) -> dict:
    """Evaluate tool results — the retry-loop gate.

    Check whether the latest tool result is satisfactory or needs retry.

    SHOULD use LLM-as-judge for bonus points. Heuristic (e.g., check for "ERROR" substring)
    is acceptable for base score.

    Requirements:
    - Read the latest entry from tool_results
    - Set evaluation_result to "needs_retry" or "success"
    - This field drives route_after_evaluate conditional edge

    Note: You may need to add 'evaluation_result' to AgentState if not present.

    Return: {"evaluation_result": str, "events": [make_event(...)]}
    """
    latest = (state.get("tool_results") or [""])[-1]
    evaluation_result = "needs_retry" if "ERROR" in latest.upper() else "success"
    return {
        "evaluation_result": evaluation_result,
        "messages": [f"evaluate:{evaluation_result}"],
        "events": [
            make_event(
                "evaluate",
                "completed",
                "tool result evaluated",
                evaluation_result=evaluation_result,
            )
        ],
    }


def answer_node(state: AgentState) -> dict:
    """Generate a final response using an LLM.

    *** MUST use a real LLM call — hardcoded strings will lose points. ***

    The LLM should generate a helpful response grounded in available context:
    - tool_results (if any)
    - approval decision (if risky route)
    - original query

    Return: {"final_answer": str, "events": [make_event(...)]}
    """
    query = state.get("query", "")
    route = state.get("route", "")
    tool_results = state.get("tool_results") or []
    approval = state.get("approval")
    context = "\n".join(tool_results) if tool_results else "No tool result was needed."
    prompt = f"""
You are a concise support agent.
Answer the user in a helpful way, grounded only in the context below.
Do not claim that a real external action happened unless the context says a mock tool succeeded.

User query: {query}
Route: {route}
Approval: {approval}
Context:
{context}
"""
    try:
        response = get_llm(temperature=0.2).invoke(prompt)
        final_answer = _text_from_llm_response(response).strip()
        event_type = "completed"
    except Exception as exc:
        final_answer = (
            "I can help with this request. "
            f"Route: {route}. "
            f"Available context: {context}"
        )
        event_type = "fallback"
        context = f"{context}\nFallback reason: {exc}"

    return {
        "final_answer": final_answer,
        "messages": ["answer:completed"],
        "events": [make_event("answer", event_type, "final answer generated", context=context)],
    }


def ask_clarification_node(state: AgentState) -> dict:
    """Ask for missing information instead of hallucinating.

    Generate a specific clarification question based on the vague/incomplete query.

    Note: You may need to add 'pending_question' to AgentState if not present.

    Return: {"pending_question": str, "final_answer": str, "events": [make_event(...)]}
    """
    query = state.get("query", "").strip()
    pending_question = (
        "Bạn cần cung cấp thêm thông tin cụ thể: yêu cầu cần xử lý là gì, "
        "liên quan tới tài khoản/đơn hàng nào, và kết quả mong muốn là gì?"
    )
    final_answer = f"Mình chưa đủ thông tin để xử lý yêu cầu '{query}'. {pending_question}"
    return {
        "pending_question": pending_question,
        "final_answer": final_answer,
        "messages": ["clarify:pending"],
        "events": [make_event("clarify", "completed", "clarification requested")],
    }


def risky_action_node(state: AgentState) -> dict:
    """Prepare a risky action for human approval.

    Describe the proposed action and why it requires approval.

    Note: You may need to add 'proposed_action' to AgentState if not present.

    Return: {"proposed_action": str, "events": [make_event(...)]}
    """
    query = state.get("query", "")
    proposed_action = (
        f"Proposed risky support action: {query}. "
        "This may change customer data or trigger an external side effect, so approval is required."
    )
    return {
        "proposed_action": proposed_action,
        "messages": ["risky_action:prepared"],
        "events": [make_event("risky_action", "completed", "risky action prepared")],
    }


def approval_node(state: AgentState) -> dict:
    """Human-in-the-loop approval step.

    Default behavior: mock approval (approved=True) so tests and CI run offline.
    Extension: if env LANGGRAPH_INTERRUPT=true, use langgraph.types.interrupt() for real HITL.

    Return approval decision and an audit event.
    """
    proposed_action = state.get("proposed_action", "")
    if os.getenv("LANGGRAPH_INTERRUPT", "").lower() == "true":
        from langgraph.types import interrupt

        payload = interrupt(
            {
                "proposed_action": proposed_action,
                "question": "Approve this risky support action?",
            }
        )
        approved = bool(payload.get("approved")) if isinstance(payload, dict) else bool(payload)
        comment = "human approval received through interrupt"
        reviewer = "human-reviewer"
    else:
        approved = True
        comment = "mock approval for offline lab execution"
        reviewer = "mock-reviewer"

    approval = ApprovalDecision(approved=approved, reviewer=reviewer, comment=comment).model_dump()
    return {
        "approval": approval,
        "messages": [f"approval:{approved}"],
        "events": [make_event("approval", "completed", "approval decision recorded")],
    }


def retry_or_fallback_node(state: AgentState) -> dict:
    """Record a retry attempt.

    Increment the attempt counter and log the transient failure.

    Requirements:
    - Read current attempt from state, increment by 1
    - Add an error message to errors list
    - Return updated attempt count

    Return: {"attempt": int, "errors": [str], "events": [make_event(...)]}
    """
    next_attempt = int(state.get("attempt", 0) or 0) + 1
    error_message = f"retry attempt {next_attempt} scheduled after route '{state.get('route', '')}'"
    return {
        "attempt": next_attempt,
        "errors": [error_message],
        "messages": [f"retry:{next_attempt}"],
        "events": [
            make_event("retry", "completed", "retry attempt recorded", attempt=next_attempt)
        ],
    }


def dead_letter_node(state: AgentState) -> dict:
    """Handle unresolvable failures after max retries exceeded.

    This is the third layer: retry → fallback → dead letter.
    Log the failure and set a final_answer explaining that the request could not be completed.

    Return: {"final_answer": str, "events": [make_event(...)]}
    """
    final_answer = (
        "The request could not be completed after the allowed retry attempts. "
        "It has been escalated for manual review."
    )
    return {
        "final_answer": final_answer,
        "messages": ["dead_letter:escalated"],
        "events": [make_event("dead_letter", "completed", "max retries exhausted")],
    }


def finalize_node(state: AgentState) -> dict:
    """Emit a final audit event. All routes must pass through here before END.

    Return: {"events": [make_event("finalize", "completed", "workflow finished")]}
    """
    return {
        "messages": ["finalize:done"],
        "events": [make_event("finalize", "completed", "workflow finished")],
    }
