"""LangChain 編排教學 — 用本專案「經銷商推薦」領域逐步講解 LCEL

閱讀方式:由上而下,每個 SECTION 是一個概念,從你現在的寫法演進到完整編排。
這支檔案「可以直接跑」(mock LLM,不打 Bedrock,不花錢):

    python docs/learning/langchain_orchestration.py

對照你專案現況:
  - src/recommender/services/agent_service.py      → 目前停在 SECTION 1 的寫法
  - src/recommender/services/evaluation_service.py → 同上 + structured_output

學完後你會知道怎麼把「analyze → evaluate」串成一條真正的 chain。

═══════════════════════════════════════════════════════════════════════════
  核心心智模型:一切都是 Runnable
═══════════════════════════════════════════════════════════════════════════

LangChain 編排的唯一主角叫 Runnable。Prompt template、LLM、parser、
甚至你自己寫的 function,只要包成 Runnable,就共享同一套介面:

    .invoke(x)      # 同步,輸入 x 回輸出
    .ainvoke(x)     # 非同步 (你專案用這個)
    .batch([x,...]) # 一次跑多筆 (自動平行)
    .stream(x)      # 串流 token

因為介面統一,才能用 `|` (pipe) 把它們接起來:

    chain = prompt | llm | parser
    #        Runnable Runnable Runnable  → 組合後「還是」一個 Runnable

`|` 不是 magic,它只是 `RunnableSequence(prompt, llm, parser)` 的語法糖。
左邊的輸出餵給右邊的輸入,跟 shell pipe 一樣的直覺。
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field


# ===========================================================================
# 先做一個假 LLM (mock),讓整支檔案不花錢就能跑
# 真實專案你會換成 langchain_aws.ChatBedrockConverse(...)
# ===========================================================================
from langchain_core.language_models.fake_chat_models import FakeListChatModel


def make_fake_llm(responses: list[str]) -> FakeListChatModel:
    """回一個會「依序吐固定字串」的假 LLM,介面跟真 ChatBedrockConverse 一樣。"""
    return FakeListChatModel(responses=responses)


# ===========================================================================
# SECTION 1 — 你現在的寫法 (agent_service.py 的等價物)
# ===========================================================================
# 目前 agent_service 是:把一整段 f-string 丟給 llm.ainvoke()。
# 能動,但 prompt 跟邏輯黏在一起,無法重組、無法平行、無法換 parser。
async def section1_current_style() -> None:
    print("\n=== SECTION 1:目前寫法 (單純 ainvoke) ===")
    llm = make_fake_llm(["P3C001 iPhone 16 Pro Max — 信心 0.91"])

    customer_id = "D-2049"
    # 跟你 agent_service.py 一模一樣的風格:手動拼字串
    prompt = (
        f"你是本公司行銷分析師。請為經銷商 {customer_id} 推薦商品。"
    )
    result = await llm.ainvoke(prompt)
    print("輸出:", result.content)
    # 問題:prompt 拼法散落各處、無法跟下游 parser 自動接、batch 要自己寫 for loop。


# ===========================================================================
# SECTION 2 — 把 prompt 抽成 PromptTemplate,開始用 `|`
# ===========================================================================
# ChatPromptTemplate 是個 Runnable:輸入 dict,輸出「訊息」。
# 它讓「模板」跟「資料」分離 —— 對齊你 CLAUDE.md「prompt 走 versioning」原則,
# 因為模板可以從 DB / 檔案載入,變數 runtime 才注入。
async def section2_prompt_template() -> None:
    print("\n=== SECTION 2:PromptTemplate + pipe ===")
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.output_parsers import StrOutputParser

    llm = make_fake_llm(["建議主推 P3C001,搭配 P3C003 配件"])

    # {dealer} 是佔位符,invoke 時才填。模板本身可入庫做 A/B (prompt_variants 表)。
    prompt = ChatPromptTemplate.from_messages([
        ("system", "你是本公司行銷分析師,只輸出推薦結論,不解釋。"),
        ("human", "請為經銷商 {dealer} 推薦本月商品。"),
    ])

    # 三個 Runnable 串成一條鏈。StrOutputParser 把 LLM 的 Message 物件抽成純字串。
    chain = prompt | llm | StrOutputParser()

    # 注意:invoke 的輸入是 dict,key 對應模板的 {dealer}
    result = await chain.ainvoke({"dealer": "D-2049"})
    print("鏈輸出 (已是純字串):", result)


# ===========================================================================
# SECTION 3 — 結構化輸出 (你 evaluation_service 用的 with_structured_output)
# ===========================================================================
# ETL First, LLM Last 之下,LLM 輸出必須是「可被下游程式消費的結構」,不是自由文字。
# with_structured_output(PydanticModel) 會自動把 Pydantic schema 翻成 JSON Schema
# 塞進 LLM 的 tool-calling,並把回應 parse 回你的 Pydantic 物件。
class Recommendation(BaseModel):
    sku: str = Field(description="商品料號")
    reason: str = Field(description="推薦理由")
    confidence: float = Field(ge=0, le=1, description="信心度 0~1")


async def section3_structured_output() -> None:
    print("\n=== SECTION 3:結構化輸出 ===")
    # 真實:llm.with_structured_output(Recommendation)
    # mock 環境下 FakeListChatModel 不支援 tool-calling,所以這裡用「概念示意」:
    print("真實寫法 (agent_service.py 該長這樣):")
    print("    structured_llm = llm.with_structured_output(Recommendation)")
    print("    rec: Recommendation = await (prompt | structured_llm).ainvoke({...})")
    print("    # rec.sku / rec.confidence 直接可用,不需手動 json.loads")
    # 關鍵:加上 prompt | 之後,structured_llm 仍是 Runnable,可繼續往下串。


# ===========================================================================
# SECTION 4 — 平行編排:RunnableParallel (一次跑多條,合併結果)
# ===========================================================================
# 你的場景:同一份經銷商資料,想同時產「推薦」和「風險評估」兩種 narrative。
# 用 dict 包起來就是平行 —— LangChain 自動 concurrent 跑,等全部回來才合併。
# 這比自己寫 asyncio.gather 乾淨,且每個分支「還是 Runnable」可單獨測試。
async def section4_parallel() -> None:
    print("\n=== SECTION 4:平行編排 RunnableParallel ===")
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.runnables import RunnableParallel

    rec_chain = (
        ChatPromptTemplate.from_template("為 {dealer} 推薦商品")
        | make_fake_llm(["主推 P3C001 iPhone"])
        | StrOutputParser()
    )
    risk_chain = (
        ChatPromptTemplate.from_template("評估 {dealer} 的呆帳風險")
        | make_fake_llm(["風險低,付款紀錄良好"])
        | StrOutputParser()
    )

    # 一個 dict = 一個 RunnableParallel。兩條鏈拿到「同一份輸入」,平行執行。
    combined = RunnableParallel(recommendation=rec_chain, risk=risk_chain)

    result = await combined.ainvoke({"dealer": "D-2049"})
    print("合併結果:", result)  # {'recommendation': '...', 'risk': '...'}


# ===========================================================================
# SECTION 5 — 串接兩階段:analyze → evaluate (你專案的真實需求)
# ===========================================================================
# agent_service 跑完推薦後,evaluation_service 要 LLM-as-judge 評分。
# 目前是兩個 service 各自 ainvoke。用 LCEL 可串成一條:
#
#   analyze_chain | (把推薦轉成 judge 的輸入) | judge_chain
#
# 中間那個「轉換」用 RunnableLambda 包一個普通 function 即可 ——
# 這就是把「你自己的 Python 邏輯」插進 LCEL 管線的方法。
async def section5_two_stage_pipeline() -> None:
    print("\n=== SECTION 5:analyze → evaluate 串成一條鏈 ===")
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.runnables import RunnableLambda

    analyze = (
        ChatPromptTemplate.from_template("為 {dealer} 產出推薦")
        | make_fake_llm(["推薦 P3C001,理由:Apple 生態忠誠"])
        | StrOutputParser()
    )

    # RunnableLambda:把普通 function 變 Runnable。這裡把上游推薦字串
    # 重新打包成 judge 鏈需要的 dict 輸入 (你的 _build_prompt 等價物)。
    def to_judge_input(recommendation: str) -> dict:
        return {"rec": recommendation}

    judge = (
        ChatPromptTemplate.from_template("評分這份推薦的合理性 (0-10):{rec}")
        | make_fake_llm(["評分 8/10,理由充分但缺量化"])
        | StrOutputParser()
    )

    # 一條龍:推薦 → 轉換 → 評分。上游輸出自動餵下游。
    pipeline = analyze | RunnableLambda(to_judge_input) | judge

    verdict = await pipeline.ainvoke({"dealer": "D-2049"})
    print("最終評分:", verdict)
    # 好處:整條 pipeline 是一個 Runnable,可 .batch() 一次跑全部經銷商、
    #       可掛 LangSmith 自動 trace 每一步、可在任一節點插 retry。


# ===========================================================================
# SECTION 6 — 韌性:retry / fallback (上 prod 前一定要的)
# ===========================================================================
# Bedrock 偶爾 throttle 或回非結構化內容。LCEL 在「任何 Runnable」上都能
# 直接掛 retry 與 fallback,不用改鏈內邏輯。
async def section6_resilience() -> None:
    print("\n=== SECTION 6:retry + fallback ===")
    primary = make_fake_llm(["主模型回應"])
    backup = make_fake_llm(["備援模型回應"])

    # .with_retry():自動重試 (指數退避)。對齊 safety.md「Bedrock 會花錢」——
    # 設 stop_after_attempt 上限避免無限重試燒 token。
    robust = primary.with_retry(stop_after_attempt=3)

    # .with_fallbacks():主鏈整個壞掉時切備援 (例:Sonnet → Haiku 降級)。
    robust_with_backup = robust.with_fallbacks([backup])

    out = await robust_with_backup.ainvoke("為 D-2049 推薦")
    print("韌性鏈輸出:", out.content)


# ===========================================================================
# 主程式:依序跑全部 section
# ===========================================================================
async def main() -> None:
    await section1_current_style()
    await section2_prompt_template()
    await section3_structured_output()
    await section4_parallel()
    await section5_two_stage_pipeline()
    await section6_resilience()
    print("\n全部跑完。回頭對照 agent_service.py,你會看到它停在 SECTION 1。")


if __name__ == "__main__":
    asyncio.run(main())
