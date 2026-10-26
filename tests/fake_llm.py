"""A stand-in for worker.llm.Claude that answers from a function, with no network."""

import json
from types import SimpleNamespace


class FakeLLM:
    def __init__(self, respond):
        """respond(tool_name, user_text) returns the tool input the model would give."""
        self.respond = respond
        self.calls = []
        self.usage = SimpleNamespace(calls=0)

    def call_tool(self, *, system, user, tool, max_tokens):
        self.calls.append({"tool": tool["name"], "user": user, "max_tokens": max_tokens})
        self.usage.calls += 1
        return self.respond(tool["name"], user)

    def summary(self):
        return f"{self.usage.calls} calls"


def translate_all(tags=("politics",)):
    """Translates every article in the request and gives it the same tags."""

    def respond(tool_name, user):
        if tool_name == "record_enrichment":
            return {
                "articles": [
                    {
                        "id": article["id"],
                        "title_en": f"EN {article['title']}",
                        "summary_en": f"EN {article['summary']}" if article["summary"] else "",
                        "tags": list(tags),
                    }
                    for article in json.loads(user)
                ]
            }
        if tool_name == "record_headlines":
            return {
                "clusters": [
                    {"id": cluster["id"], "headline": f"Headline {cluster['id']}"}
                    for cluster in json.loads(user)
                ]
            }
        if tool_name == "record_same_event":
            return {"same_event": True}
        raise AssertionError(f"unexpected tool {tool_name}")

    return respond
