"""Run one merchant question through an agent wired to the connector.

Usage: python ask.py "question"
Prints each tool call the agent makes, then its answer.
"""

import json
import subprocess
import sys

SYSTEM = (
    "You are a support assistant for an online store. Use the zoho-inventory tools to answer. "
    "Reply in at most 4 short plain-text lines, no markdown, no bullet symbols."
)


def main() -> None:
    question = sys.argv[1]
    cmd = [
        "claude", "-p", question,
        "--model", "sonnet",
        "--mcp-config", "mcp.json", "--strict-mcp-config",
        "--tools", "",
        "--allowedTools", "mcp__zoho-inventory",
        "--append-system-prompt", SYSTEM,
        "--output-format", "stream-json", "--verbose",
        "--no-session-persistence",
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
    for line in proc.stdout:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "assistant":
            for block in event["message"].get("content", []):
                if block.get("type") == "tool_use":
                    name = block["name"].split("__")[-1]
                    args = ", ".join(f"{k}={json.dumps(v)}" for k, v in block["input"].items())
                    print(f"  tool  {name}({args})", flush=True)
        elif event.get("type") == "result":
            print("\n" + event.get("result", "").strip() + "\n", flush=True)
    proc.wait()


if __name__ == "__main__":
    main()
