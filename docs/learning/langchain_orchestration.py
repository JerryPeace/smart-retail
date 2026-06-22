"""LangChain orchestration tutorial — learn LCEL step by step using this project's "dealer recommendation" domain

How to read: top to bottom. Each SECTION is one concept, evolving from how you write code today
toward full orchestration. This file "runs as-is" (mock LLM, no Bedrock calls, no cost):

    python docs/learning/langchain_orchestration.py

Mapped to your project's current state:
  - src/recommender/services/agent_service.py      → currently stuck at the SECTION 1 style
  - src/recommender/services/evaluation_service.py → same, plus structured_output

After this you'll know how to chain "analyze → evaluate" into one real chain.

═══════════════════════════════════════════════════════════════════════════
  Core mental model: everything is a Runnable
═══════════════════════════════════════════════════════════════════════════

The one and only protagonist of LangChain orchestration is the Runnable. Prompt templates, LLMs,
parsers, and even functions you write yourself — as long as they're wrapped as a Runnable, they all
share the same interface:

    .invoke(x)      # synchronous: takes input x, returns output
    .ainvoke(x)     # asynchronous (this is what your project uses)
    .batch([x,...]) # runs many inputs at once (auto-parallel)
    .stream(x)      # streams tokens

Because the interface is unified, you can connect them with `|` (pipe):

    chain = prompt | llm | parser
    #        Runnable Runnable Runnable  → once composed, it's "still" a Runnable

`|` is not magic — it's just syntactic sugar for `RunnableSequence(prompt, llm, parser)`.
The output on the left feeds the input on the right, as intuitive as a shell pipe.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field


# ===========================================================================
# First, build a fake LLM (mock) so this whole file can run for free
# In a real project you'd swap this for langchain_aws.ChatBedrockConverse(...)
# ===========================================================================
from langchain_core.language_models.fake_chat_models import FakeListChatModel


def make_fake_llm(responses: list[str]) -> FakeListChatModel:
    """Return a fake LLM that "emits fixed strings in order", with the same interface as a real ChatBedrockConverse."""
    return FakeListChatModel(responses=responses)


# ===========================================================================
# SECTION 1 — how you write it today (the equivalent of agent_service.py)
# ===========================================================================
# Right now agent_service does this: hand a whole f-string to llm.ainvoke().
# It works, but the prompt and the logic are glued together — you can't recompose it,
# can't parallelize it, can't swap the parser.
async def section1_current_style() -> None:
    print("\n=== SECTION 1: current style (plain ainvoke) ===")
    llm = make_fake_llm(["P3C001 iPhone 16 Pro Max — confidence 0.91"])

    customer_id = "D-2049"
    # Exactly the style of your agent_service.py: manually concatenating strings
    prompt = (
        f"You are the company's marketing analyst. Please recommend a product for dealer {customer_id}."
    )
    result = await llm.ainvoke(prompt)
    print("Output:", result.content)
    # Problem: prompt construction is scattered everywhere, can't auto-connect to a downstream parser, batching means writing your own for loop.


# ===========================================================================
# SECTION 2 — extract the prompt into a PromptTemplate, start using `|`
# ===========================================================================
# ChatPromptTemplate is a Runnable: it takes a dict and outputs "messages".
# It separates the "template" from the "data" — aligned with your CLAUDE.md "prompt versioning" principle,
# because the template can be loaded from DB / file, with variables injected only at runtime.
async def section2_prompt_template() -> None:
    print("\n=== SECTION 2: PromptTemplate + pipe ===")
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.output_parsers import StrOutputParser

    llm = make_fake_llm(["Recommend P3C001 as the headline product, paired with P3C003 as an accessory"])

    # {dealer} is a placeholder, filled only at invoke time. The template itself can be stored for A/B testing (prompt_variants table).
    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are the company's marketing analyst. Output only the recommendation conclusion, no explanation."),
        ("human", "Please recommend this month's products for dealer {dealer}."),
    ])

    # Three Runnables chained together. StrOutputParser extracts the LLM's Message object into a plain string.
    chain = prompt | llm | StrOutputParser()

    # Note: the input to invoke is a dict, whose key matches the template's {dealer}
    result = await chain.ainvoke({"dealer": "D-2049"})
    print("Chain output (already a plain string):", result)


# ===========================================================================
# SECTION 3 — structured output (the with_structured_output your evaluation_service uses)
# ===========================================================================
# Under "ETL First, LLM Last", the LLM's output must be a "structure consumable by downstream code", not free text.
# with_structured_output(PydanticModel) automatically translates the Pydantic schema into a JSON Schema,
# injects it into the LLM's tool-calling, and parses the response back into your Pydantic object.
class Recommendation(BaseModel):
    sku: str = Field(description="product SKU")
    reason: str = Field(description="recommendation rationale")
    confidence: float = Field(ge=0, le=1, description="confidence 0~1")


async def section3_structured_output() -> None:
    print("\n=== SECTION 3: structured output ===")
    # Real: llm.with_structured_output(Recommendation)
    # In the mock environment FakeListChatModel doesn't support tool-calling, so here we just "illustrate the concept":
    print("Real-world form (this is how agent_service.py should look):")
    print("    structured_llm = llm.with_structured_output(Recommendation)")
    print("    rec: Recommendation = await (prompt | structured_llm).ainvoke({...})")
    print("    # rec.sku / rec.confidence are ready to use, no manual json.loads needed")
    # Key point: after adding prompt |, structured_llm is still a Runnable and can keep being chained.


# ===========================================================================
# SECTION 4 — parallel orchestration: RunnableParallel (run several at once, merge results)
# ===========================================================================
# Your scenario: from the same dealer data, you want to produce both a "recommendation" and a "risk assessment" narrative at once.
# Wrapping them in a dict makes it parallel — LangChain runs them concurrently and merges only when all return.
# This is cleaner than writing your own asyncio.gather, and each branch is "still a Runnable" that can be tested on its own.
async def section4_parallel() -> None:
    print("\n=== SECTION 4: parallel orchestration RunnableParallel ===")
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.runnables import RunnableParallel

    rec_chain = (
        ChatPromptTemplate.from_template("Recommend products for {dealer}")
        | make_fake_llm(["Headline product P3C001 iPhone"])
        | StrOutputParser()
    )
    risk_chain = (
        ChatPromptTemplate.from_template("Assess the bad-debt risk of {dealer}")
        | make_fake_llm(["Low risk, good payment history"])
        | StrOutputParser()
    )

    # One dict = one RunnableParallel. Both chains receive "the same input" and run in parallel.
    combined = RunnableParallel(recommendation=rec_chain, risk=risk_chain)

    result = await combined.ainvoke({"dealer": "D-2049"})
    print("Merged result:", result)  # {'recommendation': '...', 'risk': '...'}


# ===========================================================================
# SECTION 5 — chaining two stages: analyze → evaluate (your project's real need)
# ===========================================================================
# After agent_service produces a recommendation, evaluation_service runs LLM-as-judge to score it.
# Today these are two services each doing their own ainvoke. With LCEL you can chain them into one:
#
#   analyze_chain | (convert the recommendation into the judge's input) | judge_chain
#
# That middle "conversion" is just a plain function wrapped in a RunnableLambda —
# this is how you insert "your own Python logic" into an LCEL pipeline.
async def section5_two_stage_pipeline() -> None:
    print("\n=== SECTION 5: chain analyze → evaluate into one chain ===")
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.runnables import RunnableLambda

    analyze = (
        ChatPromptTemplate.from_template("Produce a recommendation for {dealer}")
        | make_fake_llm(["Recommend P3C001, rationale: loyalty to the Apple ecosystem"])
        | StrOutputParser()
    )

    # RunnableLambda: turns a plain function into a Runnable. Here it repackages the upstream
    # recommendation string into the dict input the judge chain needs (the equivalent of your _build_prompt).
    def to_judge_input(recommendation: str) -> dict:
        return {"rec": recommendation}

    judge = (
        ChatPromptTemplate.from_template("Score the soundness of this recommendation (0-10): {rec}")
        | make_fake_llm(["Score 8/10, well-reasoned but lacks quantification"])
        | StrOutputParser()
    )

    # End to end: recommend → convert → score. Upstream output auto-feeds downstream.
    pipeline = analyze | RunnableLambda(to_judge_input) | judge

    verdict = await pipeline.ainvoke({"dealer": "D-2049"})
    print("Final score:", verdict)
    # Benefits: the whole pipeline is a single Runnable — you can .batch() it to run all dealers at once,
    #           hook up LangSmith to auto-trace every step, and insert a retry at any node.


# ===========================================================================
# SECTION 6 — resilience: retry / fallback (a must before going to prod)
# ===========================================================================
# Bedrock occasionally throttles or returns unstructured content. LCEL lets you attach retry and fallback
# directly to "any Runnable" without changing the chain's internal logic.
async def section6_resilience() -> None:
    print("\n=== SECTION 6: retry + fallback ===")
    primary = make_fake_llm(["primary model response"])
    backup = make_fake_llm(["backup model response"])

    # .with_retry(): automatic retries (exponential backoff). Aligned with safety.md "Bedrock costs money" —
    # set a stop_after_attempt cap to avoid infinite retries burning tokens.
    robust = primary.with_retry(stop_after_attempt=3)

    # .with_fallbacks(): switch to a backup when the primary chain fails entirely (e.g. Sonnet → Haiku downgrade).
    robust_with_backup = robust.with_fallbacks([backup])

    out = await robust_with_backup.ainvoke("Recommend for D-2049")
    print("Resilient chain output:", out.content)


# ===========================================================================
# Main program: run all sections in order
# ===========================================================================
async def main() -> None:
    await section1_current_style()
    await section2_prompt_template()
    await section3_structured_output()
    await section4_parallel()
    await section5_two_stage_pipeline()
    await section6_resilience()
    print("\nAll done. Now go compare with agent_service.py — you'll see it stops at SECTION 1.")


if __name__ == "__main__":
    asyncio.run(main())
