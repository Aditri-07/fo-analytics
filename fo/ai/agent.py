"""Natural-language query agent (Google Gemini backend).

Flow:
  question -> Gemini (with tool schemas) -> model picks a function+args
           -> we run it against SQLite -> results -> Gemini writes answer

The model chooses and phrases; it never computes. Every number in the
final answer comes from our analytics functions.
"""
from __future__ import annotations

import json
import os
import sqlite3

from fo.ai.tools import TOOL_SCHEMAS, dispatch

MODEL = "gemini-3.6-flash"   

SYSTEM = (
    "You are a front-office analytics assistant. Answer questions about "
    "trades, clients, P&L, exposure and sales activity by calling the "
    "provided tools. Never invent numbers — always call a tool and base "
    "your answer on its result. Be concise and specific."
)


def ask(conn: sqlite3.Connection, question: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY not set. Run: export GEMINI_API_KEY=your-key"
        )

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    tools = types.Tool(function_declarations=TOOL_SCHEMAS)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM, tools=[tools]
    )
    contents = [types.Content(role="user",
                              parts=[types.Part(text=question)])]

    # Up to a few rounds of tool calls before the final answer.
    for _ in range(5):
        resp = client.models.generate_content(
            model=MODEL, contents=contents, config=config
        )
        parts = resp.candidates[0].content.parts
        calls = [p.function_call for p in parts if p.function_call]

        if not calls:
            # No more tool calls -> model's text is the answer.
            return "".join(p.text for p in parts if p.text) or "(no answer)"

        contents.append(resp.candidates[0].content)
        for call in calls:
            try:
                result = dispatch(conn, call.name, dict(call.args or {}))
                payload = {"result": result}
            except Exception as e:
                payload = {"error": str(e)}
            contents.append(types.Content(
                role="user",
                parts=[types.Part.from_function_response(
                    name=call.name, response=payload
                )],
            ))

    return "(stopped after too many tool calls)"