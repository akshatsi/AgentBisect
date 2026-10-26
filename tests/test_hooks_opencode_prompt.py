import shlex
import sys

from agentbisect.hooks.opencode import agent_prompt


def test_agent_prompt_pins_python_to_this_venv():
    out = agent_prompt("Run the tests with `python -m pytest`. Done.")
    assert f"`{shlex.quote(sys.executable)} -m pytest`" in out
    assert out.endswith("\n")
