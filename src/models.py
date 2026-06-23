"""LIFT 评测的数据模型：suite 定义、phase 执行结果、Langfuse trace 结构与报告 JSON。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator


class TaskRequirements(BaseModel):
    """单个 task 的运行时资源需求（skills、materials 等）。"""

    default_skills: list[str] = Field(
        default_factory=list,
        description="默认 skills 目录名或路径列表",
    )
    extra_skills_dir: str | None = Field(
        default=None,
        description="额外 skills 目录路径（相对项目根或绝对路径）",
    )
    material_dir: str | None = Field(
        default=None,
        description="task 级 materials 目录路径",
    )


class ExpectedResult(BaseModel):
    """task 的期望结果与评判标准。"""

    content_reqs: str = Field(default="", description="当前任务的内容相关需求")
    trajectory_reqs: str = Field(default="", description="当前任务的轨迹相关需求")

    @model_validator(mode="before")
    @classmethod
    def _compat_description_field(cls, data: Any) -> Any:
        """兼容旧 schema：将 ``description`` 映射为 ``content_reqs``。"""
        if not isinstance(data, dict):
            return data
        # Backward compatible with old schema: {"description": "..."}.
        if "content_reqs" not in data and "description" in data:
            data["content_reqs"] = data["description"]
        if "trajectory_reqs" not in data:
            data["trajectory_reqs"] = ""
        return data


class SuiteTask(BaseModel):
    """评测集内的一条 task 定义。"""

    name: str = Field(description="task 名称（suite 内唯一）")
    query: str = Field(description="发给 work agent 的用户 query")
    requirements: TaskRequirements = Field(description="task 运行时资源需求")
    expected_result: ExpectedResult = Field(description="内容与轨迹评判标准")
    category_name: str | None = Field(
        default=None,
        description="所属场景分类名（Suite.from_json_file 时由 Suite.category 填充）",
    )


class Suite(BaseModel):
    """标准评测集：warmup（train）与 hold-out（test）两组 task。"""

    name: str = Field(description="suite 名称")
    category: str = Field(description="场景分类名（如 hello、coding）")
    warmup_tasks: list[SuiteTask] = Field(
        default_factory=list,
        description="warmup 题列表（对应 benchmark_mds/train）",
    )
    holdout_tasks: list[SuiteTask] = Field(
        default_factory=list,
        description="hold-out 终测题列表（对应 benchmark_mds/test）",
    )

    @classmethod
    def from_json_file(cls, file_path: str | Path) -> Suite:
        """从 suite JSON 文件加载并填充各 task 的 ``category_name``。"""
        data = json.loads(Path(file_path).read_text(encoding="utf-8"))
        suite = cls.model_validate(data)
        for task in suite.warmup_tasks + suite.holdout_tasks:
            task.category_name = suite.category
        return suite


def _utc_now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串（用于报告时间戳）。"""
    return datetime.now(timezone.utc).isoformat()


# 业务侧 turn trace 的 name 集合：openclaw 走 "openclaw-plugin"，hermes 走 "Hermes turn"，
# GenericAgent 走 "genericagent-plugin"。三者 trace.metadata 都需写成 OpenClaw schema
# （messages / toolCallBlocks）。
LANGFUSE_PLUGIN_TRACE_NAMES: tuple[str, ...] = (
    "openclaw-plugin",
    "Hermes turn",
    "genericagent-plugin",
)
"""Langfuse 上插件侧 trace 的 name 集合（OpenClaw / Hermes / GenericAgent）。"""

# 兼容旧引用：默认/占位 trace 名仍取首项。
LANGFUSE_PLUGIN_TRACE_NAME = LANGFUSE_PLUGIN_TRACE_NAMES[0]
"""默认插件 trace 名（``LANGFUSE_PLUGIN_TRACE_NAMES`` 首项，兼容旧引用）。"""


class LangfuseAgentTraceInput(BaseModel):
    """``work_agent`` / ``judge_agent`` pre-chat span 的 input（与 ``CustomTags`` / ``emit_pre_chat_state`` 一致）。"""

    run: str = Field(description="评测批次 ID（CustomTags.run）")
    task: str = Field(description="task 标识（通常为 category_name + task name）")
    task_query: str = Field(description="task 原始 query")
    is_final_task: bool = Field(default=False, description="是否为 hold-out 最后一轮 task")
    is_evolve_turn: bool = Field(default=False, description="是否为进化后的 evolved 阶段")
    content_reqs: str = Field(default="", description="内容评判标准")
    trajectory_reqs: str = Field(default="", description="轨迹评判标准")
    content_score: float = Field(default=0.0, description="当前 content_score（0-1）")


class LangfusePluginTraceMetadata(BaseModel):
    """``openclaw-plugin`` trace 的 metadata（``langfuse-tracer`` 在 agent_end 写入）。"""

    success: bool = Field(default=True, description="当轮 agent 是否成功完成")
    error: str | None = Field(default=None, description="失败时的错误信息")
    message_count: int = Field(default=0, description="transcript 中的 message 条数")
    tool_roundtrips: int = Field(default=0, description="工具调用往返轮次")
    tool_call_blocks: int = Field(default=0, description="assistant 消息中 toolCall 块数")
    tool_names_distinct: str | None = Field(
        default=None,
        description="去重后的工具名列表（逗号分隔）",
    )
    messages: list[Any] = Field(
        default_factory=list,
        description="完整 event.messages（OpenClaw transcript）",
    )
    messages_truncated: bool = Field(
        default=False,
        alias="messagesTruncated",
        description="是否有任意 message 因 binary/不可序列化做了单条清洗",
    )
    messages_sanitized_count: int = Field(
        default=0,
        alias="messagesSanitizedCount",
        description="被清洗的 message 条数",
    )
    messages_serialized_chars: int = Field(
        default=0,
        alias="messagesSerializedChars",
        description="清洗后 metadata.messages 序列化的字符长度（观测用，不触发截断）",
    )

    model_config = {"populate_by_name": True}

    @classmethod
    def from_langfuse_dict(cls, raw: dict[str, Any]) -> LangfusePluginTraceMetadata:
        """从 Langfuse API 返回的 metadata dict 解析（兼容 camelCase / snake_case 键名）。"""
        msgs = raw.get("messages")
        return cls(
            success=bool(raw.get("success", True)),
            error=raw.get("error") if raw.get("error") is not None else None,
            message_count=int(raw.get("messageCount") or raw.get("message_count") or 0),
            tool_roundtrips=int(raw.get("toolRoundtrips") or raw.get("tool_roundtrips") or 0),
            tool_call_blocks=int(raw.get("toolCallBlocks") or raw.get("tool_call_blocks") or 0),
            tool_names_distinct=raw.get("toolNamesDistinct") or raw.get("tool_names_distinct"),
            messages=list(msgs) if isinstance(msgs, list) else [],
            messages_truncated=bool(raw.get("messagesTruncated")),
            messages_sanitized_count=int(raw.get("messagesSanitizedCount") or 0),
            messages_serialized_chars=int(raw.get("messagesSerializedChars") or 0),
        )

    @property
    def tool_names(self) -> list[str]:
        """将 ``tool_names_distinct`` 解析为工具名列表。"""
        if not self.tool_names_distinct:
            return []
        return [n.strip() for n in self.tool_names_distinct.split(",") if n.strip()]


class LangfuseTraceTokens(BaseModel):
    """当轮 LLM token（来自配对 ``openclaw-plugin`` trace 的 GENERATION usage）。"""

    input_tokens: int = Field(default=0, description="输入 token 数")
    output_tokens: int = Field(default=0, description="输出 token 数")
    total_tokens: int = Field(default=0, description="总 token 数")


class LangfuseTraceRef(BaseModel):
    """
    一轮对话的串联快照：``*_agent`` pre-chat + 紧随其后的 ``openclaw-plugin``（1:1）。

    ``id`` 为 agent trace id；插件侧见 ``plugin_trace_id`` 与 ``plugin_*`` 字段。
    """

    id: str = Field(description="agent pre-chat trace id")
    name: str | None = Field(default=None, description="trace 名称（如 work_agent）")
    timestamp: str | None = Field(default=None, description="trace 时间戳")
    session_id: str | None = Field(default=None, description="Langfuse session id")
    user_id: str | None = Field(default=None, description="Langfuse user id")
    tags: list[str] = Field(default_factory=list, description="trace 标签列表")
    plugin_trace_id: str | None = Field(
        default=None,
        description="配对的 plugin trace id（openclaw-plugin / Hermes turn）",
    )
    plugin_trace_name: str | None = Field(
        default=None,
        description="配对的 plugin trace name（用于 postprocess 区分 OpenClaw / Hermes 计算路径）",
    )
    agent_input: LangfuseAgentTraceInput | None = Field(
        default=None,
        description="pre-chat span：CustomTags 全量字段",
    )
    plugin_prompt: str | None = Field(
        default=None,
        description="插件 trace input：当轮用户 prompt 文本",
    )
    plugin_response: str | None = Field(
        default=None,
        description="插件 trace output：当轮 assistant 回复",
    )
    plugin_metadata: LangfusePluginTraceMetadata | None = Field(
        default=None,
        description="插件 metadata：工具轮次等",
    )
    tokens: LangfuseTraceTokens | None = Field(
        default=None,
        description="当轮 token，来自 plugin trace 的 GENERATION observations",
    )
    tool_observation_count: int = Field(
        default=0,
        description=(
            "Plugin trace 下挂的 ``type=TOOL`` observation 数量（runtime-agnostic 兜底）。"
            "只要 runtime 的 langfuse overlay 给每次 tool 调用挂 ``as_type='tool'`` span，"
            "本字段就有值；用于无 metadata.toolRoundtrips 的 runtime（如 GA）做 dashboard "
            "tool_calls 兜底。"
        ),
    )
    latency_seconds: float | None = Field(
        default=None,
        description="当轮耗时（秒），合并后取配对 openclaw-plugin trace 的 latency",
    )
    provider_retry_count: int = Field(
        default=0,
        description=(
            "同一 turn 内 provider 错误（LLM 超时 / 限流等）重试次数 = "
            "本 agent span 下挂的 plugin trace 数 - 1。"
            "0 表示首发即成功，无重试。重试时 eval 侧不再 emit pre-chat span，"
            "因此后处理 _pair_single_session 走扩展贪心累积同 agent 下的所有 plugin trace。"
        ),
    )


class LangfuseObservationBrief(BaseModel):
    """单条 observation 的用量摘要（便于 JSON 落盘）。"""

    id: str = Field(description="observation id")
    type: str = Field(default="", description="observation 类型（如 GENERATION、TOOL）")
    name: str | None = Field(default=None, description="observation 名称")
    input_tokens: int = Field(default=0, description="输入 token 数")
    output_tokens: int = Field(default=0, description="输出 token 数")
    total_tokens: int = Field(default=0, description="总 token 数")


class LangfuseTokenToolStats(BaseModel):
    """Token（来自 Langfuse GENERATION usage）与工具调用相关计数。"""

    input_tokens: int = Field(default=0, description="输入 token 数")
    output_tokens: int = Field(default=0, description="输出 token 数")
    total_tokens: int = Field(default=0, description="总 token 数")
    tool_roundtrips: int = Field(
        default=0,
        description="与 OpenClaw transcript 中 toolResult 轮次一致，优先取插件 trace.metadata.toolRoundtrips",
    )
    tool_call_blocks: int = Field(
        default=0,
        description="assistant 消息中 toolCall 块数，优先 metadata.toolCallBlocks",
    )
    tool_observation_count: int = Field(
        default=0,
        description="Langfuse observation type=TOOL 的数量（元数据缺失时的兜底）",
    )


class LangfuseTraceDetailRecord(BaseModel):
    """``trace.get`` 后的单条 trace（结构化 payload + observation 摘要）。"""

    id: str = Field(description="trace id")
    name: str | None = Field(default=None, description="trace 名称")
    timestamp: str | None = Field(default=None, description="trace 时间戳")
    session_id: str | None = Field(default=None, description="Langfuse session id")
    tags: list[str] = Field(default_factory=list, description="trace 标签列表")
    agent_input: LangfuseAgentTraceInput | None = Field(
        default=None,
        description="pre-chat span input",
    )
    plugin_prompt: str | None = Field(
        default=None,
        description="插件 trace 的当轮用户 prompt",
    )
    plugin_response: str | None = Field(
        default=None,
        description="插件 trace 的当轮 assistant 回复",
    )
    plugin_metadata: LangfusePluginTraceMetadata | None = Field(
        default=None,
        description="插件 trace metadata",
    )
    latency_seconds: float | None = Field(
        default=None,
        description="trace 耗时（秒）",
    )
    observations: list[LangfuseObservationBrief] = Field(
        default_factory=list,
        description="该 trace 下 observation 用量摘要列表",
    )


class LangfuseWorkChatTurn(BaseModel):
    """Work 侧单次对话（与 ``work_agent_traces`` 一项一一对应）。"""

    turn_index: int = Field(description="对话轮次序号（0 起）")
    agent_trace_id: str = Field(description="work agent pre-chat trace id")
    plugin_trace_id: str | None = Field(
        default=None,
        description="配对的 plugin trace id",
    )
    latency_seconds: float | None = Field(
        default=None,
        description="当轮耗时（秒）",
    )
    stats: LangfuseTokenToolStats = Field(
        default_factory=LangfuseTokenToolStats,
        description="当轮 token 与工具统计",
    )


class LangfuseDialogueTurn(BaseModel):
    """一轮 work 对话的 input/output（已合并 agent + plugin，便于阅读对话内容）。"""

    turn_index: int = Field(description="对话轮次序号（0 起）")
    name: str | None = Field(default=None, description="trace 名称")
    timestamp: str | None = Field(default=None, description="trace 时间戳")
    input: Any = Field(
        default=None,
        description="当轮用户侧内容，通常来自合并后的 plugin_prompt",
    )
    output: Any = Field(
        default=None,
        description="当轮 assistant 回复，通常来自 plugin_response",
    )
    latency_seconds: float | None = Field(
        default=None,
        description="当轮耗时（秒），来自配对 plugin trace",
    )


class LangfuseWorkSessionAnalytics(BaseModel):
    """
    仅 **work** session：对话链路 + 按轮统计 + 全局汇总。

    **不包含** judge（模拟用户反馈，不参与统计）。
    """

    trace_chain: list[LangfuseDialogueTurn] = Field(
        default_factory=list,
        description="与 work_agent_traces 一一对应，仅 input/output（无独立 plugin 行）",
    )
    chat_turns: list[LangfuseWorkChatTurn] = Field(
        default_factory=list,
        description="以每次 agent_end（openclaw-plugin）为界的对话轮次",
    )
    global_stats: LangfuseTokenToolStats = Field(
        default_factory=LangfuseTokenToolStats,
        description="各 chat_turn.stats 之和",
    )
    total_latency_seconds: float = Field(
        default=0.0,
        description="各 chat_turn.latency_seconds 之和",
    )
    all_messages: list[Any] = Field(
        default_factory=list,
        description="最后一轮 work 的 plugin metadata.messages（整段会话 transcript，避免多轮 extend 重复）",
    )


class PhaseLangfuseBundle(BaseModel):
    """
    单次 phase 在 Langfuse 上的 trace 串联结果。

    - eval 侧 pre-chat span（``langfuse_reporting.emit_pre_chat_state``）的 tags 含 ``tags.run``，
      可用 ``eval_run_tag`` 过滤；name 多为 ``work_agent`` / ``judge_agent`` 等 chat_role。
    - 插件 ``langfuse-tracer`` 上报的 trace 通常 **不带** run_id tag，用 ``session_id`` 与
      ``PhaseRun`` 里的 work/judge session 对齐。
    - ``work_analytics``：对 work session 调用 ``trace.get`` 填充全链路与 chat/全局统计（可选）。
    """

    eval_run_tag: str = Field(description="与 CustomTags.run / propagate tags 中 run 一致")
    work_session_id: str = Field(description="work agent 的 Langfuse session id")
    judge_session_id: str = Field(description="judge agent 的 Langfuse session id")
    work_agent_traces: list[LangfuseTraceRef] = Field(
        default_factory=list,
        description="work 每轮对话一条：agent pre-chat + 配对 plugin（含 plugin_prompt/metadata/tokens）",
    )
    judge_agent_traces: list[LangfuseTraceRef] = Field(
        default_factory=list,
        description="judge 每轮一条，结构同 work_agent_traces",
    )
    work_analytics: LangfuseWorkSessionAnalytics | None = Field(
        default=None,
        description="work session：全链路 input/output + 按 chat / 全局的 token 与工具统计（不含 judge）",
    )


class PhaseRun(BaseModel):
    """单个 task 在 baseline 或 evolved 阶段的一次 run_task 执行。"""

    work_session_id: str = Field(description="传给 work_agent 的 session id（user_session_id）")
    judge_session_id: str = Field(description="传给 judge_agent 的 session id")
    success: bool = Field(description="task 是否成功完成")
    content_score: float = Field(
        default=0.0,
        description="judge 给出的最近一次 content_score（0-1）；任务超出最大尝试次数时为最后一轮的分数",
    )
    turns: int = Field(
        default=0,
        description="该 phase 实际进行的 work↔judge 对话轮数（成功时为达成成功的那轮序号；失败/超限时等于 max_conversation_turns）",
    )
    tool_calls: int | None = Field(
        default=None,
        description="该 phase work agent 调用 tool 的总次数；adapter 自报（OpenClaw 读 trajectory.jsonl），其他 runtime 默认 None",
    )
    workspace_dir: str | None = Field(default=None, description="该 phase 使用的 agent workspace 目录")
    langfuse: PhaseLangfuseBundle | None = Field(
        default=None,
        description="可选：从 Langfuse 拉取并填充的 trace 串联（见 src.langfuse_trace_stitch、src.langfuse_work_analytics）",
    )


class TaskRun(BaseModel):
    """单个 task：进化前 baseline + 进化后 evolved（未跑 evolved 时为 null）。"""

    task_name: str = Field(description="task 名称")
    category: str = Field(description="场景分类名")
    baseline: PhaseRun = Field(description="进化前 baseline 阶段执行结果")
    evolved: PhaseRun | None = Field(
        default=None,
        description="进化后 evolved 阶段执行结果（未执行时为 null）",
    )


class SuiteRun(BaseModel):
    """一次 repeat 内、单个 suite JSON 的执行结果。"""

    suite_name: str | None = Field(default=None, description="suite 名称")
    suite_path: str | None = Field(default=None, description="suite JSON 文件路径")
    category: str | None = Field(default=None, description="场景分类名")
    tasks: list[TaskRun] = Field(
        default_factory=list,
        description="该 suite 下各 task 的执行结果",
    )


class EvalRepeat(BaseModel):
    """``--repeat`` 的一轮完整执行（所选 suite 各跑一遍）。"""

    started_at: str = Field(
        default_factory=_utc_now_iso,
        description="本轮开始时间（UTC ISO 8601）",
    )
    completed_at: str | None = Field(
        default=None,
        description="本轮完成时间（UTC ISO 8601）",
    )
    suites: list[SuiteRun] = Field(
        default_factory=list,
        description="本轮内各 suite 的执行结果",
    )


class EvalReport(BaseModel):
    """一次 LIFT 评测 run 的汇总（``run_id`` 对应一份 report JSON）。"""

    run_id: str = Field(description="评测批次 ID")
    categories: list[str] = Field(
        default_factory=list,
        description="本次评测涉及的场景分类名列表",
    )
    started_at: str = Field(
        default_factory=_utc_now_iso,
        description="评测开始时间（UTC ISO 8601）",
    )
    completed_at: str | None = Field(
        default=None,
        description="评测完成时间（UTC ISO 8601）",
    )
    runs: list[EvalRepeat] = Field(
        default_factory=list,
        description="各 repeat 的执行结果（对应 --repeat）",
    )

    def write_json(self, path: str | Path) -> None:
        """将报告序列化为 JSON 并写入 ``path``（自动创建父目录）。"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def from_json_file(cls, file_path: str | Path) -> EvalReport:
        """从 JSON 文件加载 ``EvalReport``。"""
        raw = Path(file_path).read_text(encoding="utf-8")
        return cls.model_validate_json(raw)

    @classmethod
    def from_json_str(cls, text: str) -> EvalReport:
        """从 JSON 字符串加载 ``EvalReport``。"""
        return cls.model_validate_json(text)


def _bool_to_tag(value: bool | None) -> str:
    """将布尔值转为 CustomTags env 中的 ``"1"`` / ``"0"`` / ``""``。"""
    if value is None:
        return ""
    return "1" if value else "0"


def _sanitize_tag_value(value: str) -> str:
    """清洗 tag 值：CustomTags 使用逗号分隔的 key=value 对，需去除逗号与换行。"""
    # CustomTags uses comma-separated key=value pairs.
    return value.replace(",", " ").replace("\n", " ").strip()


@dataclass
class CustomTags:
    """Langfuse pre-chat span 与 OpenClaw env 传播用的评测上下文标签。"""

    run: str
    """评测批次 ID。"""
    task: str
    """task 标识（通常为 category_name + task name）。"""
    task_query: str
    """task 原始 query。"""
    is_final_task: bool
    """是否为 hold-out 最后一轮 task。"""
    is_evolve_turn: bool
    """是否为进化后的 evolved 阶段。"""
    content_reqs: str
    """内容评判标准。"""
    trajectory_reqs: str
    """轨迹评判标准。"""
    content_score: float
    """当前 content_score（0-1）。"""
    agent_name: str
    """OpenClaw agent 名称（Hermes 侧可为 unknown）。"""

    @classmethod
    def init_tags(cls, task: SuiteTask, run_id: str) -> CustomTags:
        """从 ``SuiteTask`` 与 ``run_id`` 构造初始 CustomTags（默认值）。"""
        return cls(
            run=run_id,
            task=f"{task.category_name}_{task.name}",
            task_query=task.query,
            is_final_task=False,
            is_evolve_turn=False,
            content_reqs=task.expected_result.content_reqs,
            trajectory_reqs=task.expected_result.trajectory_reqs,
            content_score=0.0,
            agent_name="unknown",
        )

    def to_env_value(self) -> str:
        """序列化为 OpenClaw env 变量值（逗号分隔 key=value，不含 agent_name）。"""
        # 逗号分隔协议：值内不得含逗号（见 _sanitize_tag_value）
        fields_in_order = [
            ("run", self.run),
            ("task", self.task),
            ("task_query", self.task_query),
            ("is_final_task", _bool_to_tag(self.is_final_task)),
            ("is_evolve_turn", _bool_to_tag(self.is_evolve_turn)),
            ("content_reqs", self.content_reqs),
            ("trajectory_reqs", self.trajectory_reqs),
            ("content_score", self.content_score),
        ]
        return ",".join(
            f"{key}={_sanitize_tag_value(str(value))}" for key, value in fields_in_order
        )

    def to_dict(self) -> dict[str, Any]:
        """转为 dict（不含 agent_name，供 Langfuse span input 使用）。"""
        return {
            "run": self.run,
            "task": self.task,
            "task_query": self.task_query,
            "is_final_task": self.is_final_task,
            "is_evolve_turn": self.is_evolve_turn,
            "content_reqs": self.content_reqs,
            "trajectory_reqs": self.trajectory_reqs,
            "content_score": self.content_score,
        }
