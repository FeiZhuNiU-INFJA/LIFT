"""主动进化复盘 prompt 模板（per-task + suite-level）。

设计原则：

- prompt 不替 GA 做"该写什么"的决策，只触发它自己按
  ``memory/memory_management_sop.md`` 走分层记忆流程；
- per-task prompt 注入 ``task.name`` / ``success`` / ``score`` / ``turns``
  这些 work agent 自身在对话里看不到的元数据，避免 GA 误判任务结果；
- suite-level prompt 不枚举题目（让 GA 阅读 ``memory/`` 下既有痕迹自行
  归纳），与 GA 上游"反射式复盘 = 读已写、再决定要不要补"的工作方式一致。
"""

from __future__ import annotations

from src.models import PhaseRun, SuiteTask


_MEMORY_PATH_NOTE = (
    "IMPORTANT — memory location: all memory files live at absolute path "
    "``/opt/GenericAgent/memory/``. Always read and write via this absolute "
    "path (e.g. ``/opt/GenericAgent/memory/global_mem_insight.txt``, "
    "``/opt/GenericAgent/memory/memory_management_sop.md``). Never use "
    "relative paths like ``memory/...`` or ``../memory/...`` — your cwd is a "
    "bind mount and writes there will NOT persist into the delta image."
)


def per_task_reflection_prompt(task: SuiteTask, result: PhaseRun) -> str:
    """每题完成后的轻量复盘 prompt（B 粒度）。"""
    verdict = "PASSED" if result.success else "FAILED"
    return (
        "You just completed a warmup task. Reflect on it now and update your "
        "memory if (and only if) there is something worth remembering.\n\n"
        f"- task: {task.name}\n"
        f"- judge verdict: {verdict}\n"
        f"- score: {result.content_score:.2f}\n"
        f"- turns used: {result.turns}\n\n"
        f"{_MEMORY_PATH_NOTE}\n\n"
        "Follow your own /opt/GenericAgent/memory/memory_management_sop.md. "
        "Do not invent SOPs for tasks you did not actually execute. Keep the "
        "update minimal: a single new fact in "
        "/opt/GenericAgent/memory/global_mem.txt, an index line in "
        "/opt/GenericAgent/memory/global_mem_insight.txt, or one new "
        "/opt/GenericAgent/memory/*.md SOP at most. "
        "If nothing is worth remembering, write nothing and reply with the "
        "single word DONE."
    )


SUITE_REFLECTION_PROMPT = (
    "All warmup tasks for this suite are now finished. Run a holistic review "
    "of the entire batch and update your memory.\n\n"
    f"{_MEMORY_PATH_NOTE}\n\n"
    "Steps:\n"
    "1. Read the current contents of "
    "/opt/GenericAgent/memory/global_mem_insight.txt, "
    "/opt/GenericAgent/memory/global_mem.txt, and any task-level SOP files "
    "under /opt/GenericAgent/memory/.\n"
    "2. Identify cross-task patterns: recurring failure modes, reusable "
    "skills, environment quirks. Promote them into the right layer per "
    "/opt/GenericAgent/memory/memory_management_sop.md.\n"
    "3. Prune stale or contradictory entries. Keep "
    "/opt/GenericAgent/memory/global_mem_insight.txt within ~30 lines.\n"
    "4. Do NOT fabricate experiences for tasks you did not execute "
    "(No Execution, No Memory).\n\n"
    "When the memory files reflect the final consolidated state, reply with "
    "the single word DONE."
)
