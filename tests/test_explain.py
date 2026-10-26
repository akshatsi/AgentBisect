from agentbisect.explain import MAX_WORDS, build_prompt, explain
from agentbisect.llm import ChatResponse, FakeLLM
from agentbisect.models import (
    Analysis,
    BisectResult,
    BisectStatus,
    CulpritReport,
    OracleResult,
    OracleRun,
    Step,
    ToolKind,
    Verdict,
)


def make_culprit(censored: bool = False) -> CulpritReport:
    step = Step(
        index=8,
        tool="edit_file",
        kind=ToolKind.EDIT,
        args={"path": "shop/catalog.py"},
        reasoning="I will make add replace an existing SKU.",
    )
    probe = OracleResult(
        step_index=8,
        snapshot="abc",
        verdict=Verdict.FAIL,
        runs=[
            OracleRun(exit_code=1, duration_s=0.1, output_tail="FAILED test_add_duplicate_raises")
        ],
    )
    return CulpritReport(
        run_id="r1",
        check="pytest tests/test_catalog.py::test_add_duplicate_raises -q",
        bisect=BisectResult(check="x", status=BisectStatus.FOUND, decisive_step=8, probes=[probe]),
        analysis=Analysis(
            t_err=8,
            t_obs=None if censored else 11,
            lag=None if censored else 3,
            censored=censored,
            steps_after_err=6,
            wasted_tokens=1000,
            total_tokens=5000,
        ),
        diff="-        raise DuplicateProductError(product.sku)\n",
        step=step,
    )


def test_prompt_has_diff_reasoning_and_oracle_tail():
    prompt = build_prompt(make_culprit())
    assert "DuplicateProductError" in prompt
    assert "replace an existing SKU" in prompt
    assert "FAILED test_add_duplicate_raises" in prompt
    assert "step 11" in prompt


def test_prompt_says_when_agent_never_noticed():
    assert "never saw the failure" in build_prompt(make_culprit(censored=True))


def test_explain_returns_model_text_and_leaves_verdict_alone():
    culprit = make_culprit()
    before = culprit.model_dump_json()
    client = FakeLLM([ChatResponse(content="The edit removed the duplicate check.")])
    assert explain(culprit, client) == "The edit removed the duplicate check."
    assert culprit.model_dump_json() == before
    messages, _tools = client.requests[0]
    assert messages[0]["role"] == "system" and messages[1]["role"] == "user"


def test_explain_caps_words_and_drops_code_blocks():
    long = ("This sentence has six words. " * 60) + "```py\nprint(1)\n```"
    client = FakeLLM([ChatResponse(content=long)])
    out = explain(make_culprit(), client)
    assert len(out.split()) <= MAX_WORDS
    assert "print(1)" not in out
    assert out.endswith(".")
