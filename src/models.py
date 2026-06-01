from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator


class TaskRequirements(BaseModel):
    default_skills: list[str] = Field(default_factory=list)
    extra_skills_dir: str | None = None
    material_dir: str | None = None


class ExpectedResult(BaseModel):
    content_reqs: str = Field(default="", description="当前任务的内容相关需求")
    trajectory_reqs: str = Field(default="", description="当前任务的轨迹相关需求")

    @model_validator(mode="before")
    @classmethod
    def _compat_description_field(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # Backward compatible with old schema: {"description": "..."}.
        if "content_reqs" not in data and "description" in data:
            data["content_reqs"] = data["description"]
        if "trajectory_reqs" not in data:
            data["trajectory_reqs"] = ""
        return data


class SuiteTask(BaseModel):
    name: str
    query: str
    requirements: TaskRequirements
    expected_result: ExpectedResult
    category_name: str | None = None


class SuiteSpec(BaseModel):
    name: str
    category: str
    tasks: list[SuiteTask] = Field(default_factory=list)

    @classmethod
    def from_json_file(cls, file_path: str | Path) -> SuiteSpec:
        data = json.loads(Path(file_path).read_text(encoding="utf-8"))
        spec = cls.model_validate(data)
        for task in spec.tasks:
            task.category_name = spec.category
        return spec


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# 业务侧 turn trace 的 name 集合：openclaw 走 "openclaw-plugin"，hermes 走 "Hermes turn"。
# 两者 trace.metadata 都需写成 OpenClaw schema（messages / toolCallBlocks）。
LANGFUSE_PLUGIN_TRACE_NAMES: tuple[str, ...] = ("openclaw-plugin", "Hermes turn")
# 兼容旧引用：默认/占位 trace 名仍取首项。
LANGFUSE_PLUGIN_TRACE_NAME = LANGFUSE_PLUGIN_TRACE_NAMES[0]


class LangfuseAgentTraceInput(BaseModel):
    """``work_agent`` / ``judge_agent`` pre-chat span 的 input（与 ``CustomTags`` / ``emit_pre_chat_state`` 一致）。"""

    run: str
    task: str
    task_query: str
    is_final_task: bool = False
    is_evolve_turn: bool = False
    content_reqs: str = ""
    trajectory_reqs: str = ""
    content_score: float = 0.0


class LangfusePluginTraceMetadata(BaseModel):
    """``openclaw-plugin`` trace 的 metadata（``langfuse-tracer`` 在 agent_end 写入）。"""

    success: bool = True
    error: str | None = None
    message_count: int = 0
    tool_roundtrips: int = 0
    tool_call_blocks: int = 0
    tool_names_distinct: str | None = None
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
        if not self.tool_names_distinct:
            return []
        return [n.strip() for n in self.tool_names_distinct.split(",") if n.strip()]


class LangfuseTraceTokens(BaseModel):
    """当轮 LLM token（来自配对 ``openclaw-plugin`` trace 的 GENERATION usage）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class LangfuseTraceRef(BaseModel):
    """
    一轮对话的串联快照：``*_agent`` pre-chat + 紧随其后的 ``openclaw-plugin``（1:1）。

    ``id`` 为 agent trace id；插件侧见 ``plugin_trace_id`` 与 ``plugin_*`` 字段。
    """

    id: str
    name: str | None = None
    timestamp: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    tags: list[str] = Field(default_factory=list)
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
    latency_seconds: float | None = Field(
        default=None,
        description="当轮耗时（秒），合并后取配对 openclaw-plugin trace 的 latency",
    )


class LangfuseObservationBrief(BaseModel):
    """单条 observation 的用量摘要（便于 JSON 落盘）。"""

    id: str
    type: str = ""
    name: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class LangfuseTokenToolStats(BaseModel):
    """Token（来自 Langfuse GENERATION usage）与工具调用相关计数。"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
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

    id: str
    name: str | None = None
    timestamp: str | None = None
    session_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    agent_input: LangfuseAgentTraceInput | None = None
    plugin_prompt: str | None = None
    plugin_response: str | None = None
    plugin_metadata: LangfusePluginTraceMetadata | None = None
    latency_seconds: float | None = None
    observations: list[LangfuseObservationBrief] = Field(default_factory=list)


class LangfuseWorkChatTurn(BaseModel):
    """Work 侧单次对话（与 ``work_agent_traces`` 一项一一对应）。"""

    turn_index: int
    agent_trace_id: str
    plugin_trace_id: str | None = None
    latency_seconds: float | None = None
    stats: LangfuseTokenToolStats = Field(default_factory=LangfuseTokenToolStats)


class LangfuseDialogueTurn(BaseModel):
    """一轮 work 对话的 input/output（已合并 agent + plugin，便于阅读对话内容）。"""

    turn_index: int
    name: str | None = None
    timestamp: str | None = None
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
    work_session_id: str
    judge_session_id: str
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
    success: bool
    content_score: float = Field(
        default=0.0,
        description="judge 给出的最近一次 content_score（0-1）；任务超出最大尝试次数时为最后一轮的分数",
    )
    workspace_dir: str | None = Field(default=None, description="该 phase 使用的 agent workspace 目录")
    langfuse: PhaseLangfuseBundle | None = Field(
        default=None,
        description="可选：从 Langfuse 拉取并填充的 trace 串联（见 src.langfuse_trace_stitch、src.langfuse_work_analytics）",
    )


class TaskRun(BaseModel):
    """单个 task：进化前 baseline + 进化后 evolved（未跑 evolved 时为 null）。"""

    task_name: str
    category: str
    baseline: PhaseRun
    evolved: PhaseRun | None = None


class SuiteRun(BaseModel):
    """一次 repeat 内、单个 suite JSON 的执行结果。"""

    suite_name: str | None = None
    suite_path: str | None = None
    category: str | None = None
    tasks: list[TaskRun] = Field(default_factory=list)


class EvalRepeat(BaseModel):
    """``--repeat`` 的一轮完整执行（所选 suite 各跑一遍）。"""

    started_at: str = Field(default_factory=_utc_now_iso)
    completed_at: str | None = None
    suites: list[SuiteRun] = Field(default_factory=list)


class EvalReport(BaseModel):
    """一次 evobench 评测 run 的汇总（``run_id`` 对应一份 report JSON）。"""

    run_id: str
    categories: list[str] = Field(default_factory=list)
    started_at: str = Field(default_factory=_utc_now_iso)
    completed_at: str | None = None
    runs: list[EvalRepeat] = Field(default_factory=list)

    def write_json(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def from_json_file(cls, file_path: str | Path) -> EvalReport:
        raw = Path(file_path).read_text(encoding="utf-8")
        return cls.model_validate_json(raw)

    @classmethod
    def from_json_str(cls, text: str) -> EvalReport:
        return cls.model_validate_json(text)


def _bool_to_tag(value: bool | None) -> str:
    if value is None:
        return ""
    return "1" if value else "0"


def _sanitize_tag_value(value: str) -> str:
    # CustomTags uses comma-separated key=value pairs.
    return value.replace(",", " ").replace("\n", " ").strip()


@dataclass
class CustomTags:
    run: str
    task: str
    task_query: str
    is_final_task: bool
    is_evolve_turn: bool
    content_reqs: str
    trajectory_reqs: str
    content_score: float
    agent_name: str

    @classmethod
    def init_tags(cls, task: SuiteTask, run_id: str) -> CustomTags:
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
