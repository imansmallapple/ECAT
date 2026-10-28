"""agentic judge 的并发上限与库竞争重试。

重试只针对 opencode 抢全局 SQLite 的启动期失败；Claude Code 不共享库，
必须一次都不重试 —— 否则等于给它加了一层它不需要的行为。
"""

from __future__ import annotations

from pathlib import Path

from ecat.entropy.engine import _DB_BUSY_RETRIES, _is_db_busy
from ecat.runner.base import AgentRunner, RunResult
from ecat.runner.claude_code import ClaudeCodeRunner
from ecat.runner.opencode import _RATE_LIMIT_STREAK_LIMIT, OpencodeRunner


def _res(returncode: int = 1, stderr: str = "", stdout: str = "") -> RunResult:
    return RunResult(
        returncode=returncode, stdout=stdout, stderr=stderr, duration_s=0.1, transcript_path=None
    )


# ── 并发上限 ────────────────────────────────────────────────────────────────
def test_cap_is_20_for_both_runtimes(tmp_path: Path) -> None:
    """一批跑完所有 agentic 插件（当前 9 个），并留出加插件的余量。"""
    assert AgentRunner.max_concurrent == 20
    assert ClaudeCodeRunner(config_dir=tmp_path).max_concurrent == 20
    assert OpencodeRunner(config_dir=tmp_path).max_concurrent == 20


def test_effective_concurrency_capped_by_plugin_count() -> None:
    """实际并发 = min(插件数, 上限) —— 上限调大不会凭空多起进程。"""
    for n_plugins in (1, 5, 9, 25):
        eff = max(1, min(n_plugins, AgentRunner.max_concurrent))
        assert eff == min(n_plugins, 20)
        assert eff >= 1


# ── 库竞争识别 ──────────────────────────────────────────────────────────────
def test_detects_sqlite_busy() -> None:
    assert _is_db_busy(_res(stderr="Error: SQLITE_BUSY: database is busy"))
    assert _is_db_busy(_res(stdout="sqlite3: database is locked"))


def test_ordinary_failures_are_not_retried() -> None:
    """鉴权失败、模型报错等与库无关，重试只会浪费配额。"""
    assert not _is_db_busy(_res(stderr="401 Unauthorized"))
    assert not _is_db_busy(_res(stderr="model not found"))
    assert not _is_db_busy(_res(stderr=""))


def test_claude_code_failures_never_match(tmp_path: Path) -> None:
    """Claude Code 的典型失败措辞一条都不该命中重试分支。"""
    for stderr in (
        "Credit balance is too low",
        "Usage credits required",
        "Error: process exited with code 1",
        "Background tasks still running after 600s",
    ):
        assert not _is_db_busy(_res(stderr=stderr)), stderr


def test_retry_budget_is_bounded() -> None:
    assert _DB_BUSY_RETRIES == 2  # 最多 3 次调用，不会无限重试烧配额


# ── 429 限流计数按 pid 隔离 ──────────────────────────────────────────────────
# 并发 judge 共享同一个 runner 实例（engine 的 ThreadPoolExecutor），但每次 run()
# 都 Popen 一个独立进程。若把多个进程的 429 混进一个实例计数，A 的 429 会把 B 的
# 进程误杀（实测复现过：各触发 1 次，共享计数到 5，某线程读到别的线程的 abort_reason）。
# 每条 429 属于触发它的那个 pid，所以按 pid 各自累计、只杀自己。


def test_rate_limit_counted_per_pid(tmp_path: Path) -> None:
    """两个进程各自计数，一个到达阈值只杀它自己，另一个不受影响。"""
    runner = OpencodeRunner(config_dir=tmp_path)
    killed: list[int] = []
    # 用 monkeypatch 替代 _kill_process_tree，记录被杀的 pid 而不真杀
    import ecat.runner.opencode as mod

    orig = mod._kill_process_tree
    mod._kill_process_tree = lambda pid: killed.append(pid)
    try:
        # pid=101 连续阈值次限流 → 杀 101
        for _ in range(_RATE_LIMIT_STREAK_LIMIT):
            runner._on_stderr_line(101, '{"statusCode":429,"msg":"rate limit"}')
        # pid=202 只来 3 次 → 不该杀 202
        for _ in range(3):
            runner._on_stderr_line(202, '{"statusCode":429,"msg":"rate limit"}')
    finally:
        mod._kill_process_tree = orig
    assert killed == [101], f"只该杀 pid=101，实际杀了 {killed}"
    assert 202 not in killed
    assert runner._rate_limit_streak_by_pid[101] == _RATE_LIMIT_STREAK_LIMIT
    assert runner._rate_limit_streak_by_pid[202] == 3


def test_rate_limit_success_resets_only_that_pid(tmp_path: Path) -> None:
    """成功标志只清零自己的 pid，不清零别人的。"""
    runner = OpencodeRunner(config_dir=tmp_path)
    runner._on_stderr_line(101, '{"statusCode":429,"msg":"rate limit"}')
    runner._on_stderr_line(101, '{"statusCode":429,"msg":"rate limit"}')
    runner._on_stderr_line(202, '{"statusCode":429,"msg":"rate limit"}')
    # pid=101 恢复
    runner._on_stderr_line(101, '{"type":"message.updated"}')
    assert runner._rate_limit_streak_by_pid[101] == 0, "101 应清零"
    assert runner._rate_limit_streak_by_pid[202] == 1, "202 不受影响"


def test_rate_limit_chinese_wording_triggers_kill(tmp_path: Path) -> None:
    """中文限流文案（zhipu「速率限制」/ retry 的 api_429 标记）也要能累计并触发软中止。

    回归护栏：runs_20260902_1638 iter1 卡死，是因为 zhipu 返回的是中文
    「您的账户已达到速率限制，请您控制请求频率」+ `category=api_429`，旧 pattern 只认
    英文（statusCode:429 / rate limit / too many requests），一个都不匹配，软中止
    从未触发，进程卡在限流重试上直到外层 timeout_s。
    """
    runner = OpencodeRunner(config_dir=tmp_path)
    killed: list[int] = []
    import ecat.runner.opencode as mod

    orig = mod._kill_process_tree
    mod._kill_process_tree = lambda pid: killed.append(pid)
    try:
        # 用 zhipu 实测过的两种真实文案各 5 次（交替验证都命中）
        for _ in range(_RATE_LIMIT_STREAK_LIMIT):
            runner._on_stderr_line(
                301,
                'message="retryable error matched" service=session.retry '
                "provider=zhipu category=api_429 attempt=1 "
                "message=您的账户已达到速率限制，请您控制请求频率",
            )
    finally:
        mod._kill_process_tree = orig
    assert killed == [301], f"中文限流文案应触发软中止杀 pid=301，实际杀了 {killed}"
    assert runner._rate_limit_streak_by_pid[301] == _RATE_LIMIT_STREAK_LIMIT


def test_rate_limit_chinese_no_false_positive(tmp_path: Path) -> None:
    """正常中文日志（不含限流词）不命中，避免误杀。

    回归护栏：stderr 里大量工具调用/模型 loop 的中文行（如「正在编辑」）绝不能
    被当成限流，否则正常生成会被误中止。
    """
    runner = OpencodeRunner(config_dir=tmp_path)
    killed: list[int] = []
    import ecat.runner.opencode as mod

    orig = mod._kill_process_tree
    mod._kill_process_tree = lambda pid: killed.append(pid)
    try:
        # 一堆正常中文日志行 —— 都不该命中
        for line in (
            "message=loop session.id=ses_x step=84",
            'message="tool execution completed" service=tool tool=edit',
            'message="正在编辑文件" file="entry/src/main/ets/pages/X.ets"',
            "message=stream providerID=zhipu modelID=glm-5.3-flash",
        ):
            for _ in range(10):  # 连续多次也不该误判
                runner._on_stderr_line(401, line)
    finally:
        mod._kill_process_tree = orig
    assert killed == [], f"正常日志不应触发软中止，实际杀了 {killed}"
    assert runner._rate_limit_streak_by_pid.get(401, 0) == 0


def test_rate_limit_expands_to_retryable_api_500(tmp_path: Path) -> None:
    """非 429 的 retryable 错误（网络/服务端 api_500）持续出现也会触发软中止。

    这是"软中止扩展到 retryable 标记"的目的：deveco/opencode 把 429/500/网络/超时
    都统一打 `retryable error matched ... category=api_XXX`，任何这类错误持续出现
    （网络断了、服务端持续 500）都会像 429 一样无限重试、进程不退、卡到 timeout_s。
    旧 pattern 只认 429，这类错误识别不到。实测 runs_20260902_1638 出现过
    `category=api_500`（网络错误）。
    """
    runner = OpencodeRunner(config_dir=tmp_path)
    killed: list[int] = []
    import ecat.runner.opencode as mod

    orig = mod._kill_process_tree
    mod._kill_process_tree = lambda pid: killed.append(pid)
    try:
        # 用 zhipu 实测过的 api_500 网络错误文案连续 5 次
        for _ in range(_RATE_LIMIT_STREAK_LIMIT):
            runner._on_stderr_line(
                501,
                'message="retryable error matched" service=session.retry '
                "provider=zhipu category=api_500 attempt=2 "
                "message=网络错误，请稍后重试",
            )
    finally:
        mod._kill_process_tree = orig
    assert killed == [501], f"持续 api_500 应触发软中止杀 pid=501，实际杀了 {killed}"
    assert runner._rate_limit_streak_by_pid[501] == _RATE_LIMIT_STREAK_LIMIT


def test_rate_limit_retryable_recovered_not_killed(tmp_path: Path) -> None:
    """retryable 错误后一旦恢复（step_finish 等成功标志），计数清零，不误杀。

    回归护栏：一次性的网络抖动（如 api_500 只出现一两次后恢复）不应触发软中止，
    否则正常生成会被误杀。成功标志（step_finish/message.updated/llm runtime）在恢复后
    清空连续计数。
    """
    runner = OpencodeRunner(config_dir=tmp_path)
    killed: list[int] = []
    import ecat.runner.opencode as mod

    orig = mod._kill_process_tree
    mod._kill_process_tree = lambda pid: killed.append(pid)
    try:
        # 一次 500（网络抖动），然后恢复
        runner._on_stderr_line(
            601,
            'message="retryable error matched" service=session.retry '
            "provider=zhipu category=api_500 attempt=2 message=网络错误",
        )
        # 恢复：成功标志清空计数
        runner._on_stderr_line(601, "message=loop session.id=ses_x step=51")
        runner._on_stderr_line(601, '{"type":"text","text":"ok"}')
        # 再来几次 500（恢复过，从零重新计数，未到阈值）
        for _ in range(3):
            runner._on_stderr_line(
                601,
                'message="retryable error matched" service=session.retry '
                "provider=zhipu category=api_500 attempt=1 message=网络错误",
            )
    finally:
        mod._kill_process_tree = orig
    # 恢复后 3 次 < 5，不该杀
    assert killed == [], f"恢复过的计数应重新累计且未到阈值，实际杀了 {killed}"
    assert runner._rate_limit_streak_by_pid.get(601) in (3, None)


def test_rate_limit_llm_runtime_does_not_reset_streak(tmp_path: Path) -> None:
    """`llm runtime selected`（报错后重试再次发起的标志）不得清空连续计数。

    回归护栏（runs_20260902_1638 iter1 卡死）：deveco 每次 retryable 错误后都会立刻
    重新发起调用，打印 `llm runtime selected`。若把它当"恢复成功"标志，每条 retryable
    都会被它清零，连续计数永远到不了 _RATE_LIMIT_STREAK_LIMIT，软中止失效。实测卡死
    区间 max streak 只到 2。正确行为：llm runtime selected 只是"又试一次"，不算恢复，
    计数应继续累积到阈值触发。
    """
    runner = OpencodeRunner(config_dir=tmp_path)
    killed: list[int] = []
    import ecat.runner.opencode as mod

    orig = mod._kill_process_tree
    mod._kill_process_tree = lambda pid: killed.append(pid)
    try:
        # 模拟真实卡死：每条 retryable 后紧跟 llm runtime selected（再次尝试），无真正恢复
        for _ in range(_RATE_LIMIT_STREAK_LIMIT):
            runner._on_stderr_line(
                701,
                'message="retryable error matched" service=session.retry '
                "provider=zhipu category=api_429 attempt=1 "
                "message=您的账户已达到速率限制，请您控制请求频率",
            )
            # 报错后立刻重新尝试 —— 会打印这条，但它不是"恢复成功"，不能清零计数
            runner._on_stderr_line(
                701,
                "message=stream providerID=zhipu modelID=glm-5.3-flash "
                'message="llm runtime selected" llm.runtime=ai-sdk',
            )
    finally:
        mod._kill_process_tree = orig
    # 即使中间夹了 llm runtime selected，也不该被清零，应累积到阈值触发软中止
    assert killed == [701], (
        f"llm runtime selected 不应清零计数，应在阈值触发软中止；实际杀了 {killed}"
    )
    assert runner._rate_limit_streak_by_pid[701] == _RATE_LIMIT_STREAK_LIMIT
