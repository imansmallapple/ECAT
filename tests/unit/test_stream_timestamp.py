"""stream.jsonl 的时间戳注入。

事件本身不带时间，落盘后排查"哪一步慢了"只能靠文件 mtime 猜。注入必须满足两条：
产物仍是合法 JSONL（下游脚本照常 json.loads），且原有字节逐字保留。
"""

from __future__ import annotations

import json
import time

from ecat.runner.base import _stamp


def test_stamp_adds_time_and_elapsed_as_leading_keys():
    t0 = time.monotonic() - 1.5
    line = '{"type":"assistant","message":{"content":[]},"uuid":"ab12"}\n'
    out = _stamp(line, t0)

    assert out.startswith('{"_ecat_time":"'), "时间要放最前，head -c 就能看到"
    ev = json.loads(out)
    assert ev["type"] == "assistant" and ev["uuid"] == "ab12"   # 原字段不受影响
    assert ev["_ecat_elapsed_s"] >= 1.5
    # 秒级精度：不带小数秒，带时区
    assert "." not in ev["_ecat_time"].split("T")[1].split("+")[0]
    assert out.endswith("\n")


def test_stamp_preserves_the_original_bytes():
    """插入点之后逐字保留 —— 大行不做 json 往返，正是为了不重排字段和空白。"""
    line = '{ "type":"result" ,  "result":"done" }'
    out = _stamp(line, time.monotonic())
    assert out.endswith(' "type":"result" ,  "result":"done" }')


def test_stamp_skips_lines_that_would_break():
    now = time.monotonic()
    # 空对象：插进去会留下尾逗号
    assert _stamp("{}", now) == "{}"
    assert _stamp("{}\n", now) == "{}\n"
    # 非 JSON / 空行
    assert _stamp("not json", now) == "not json"
    assert _stamp("", now) == ""
    assert _stamp("\n", now) == "\n"
    # 数组不是事件对象
    assert _stamp('[1,2]', now) == '[1,2]'


def test_stamped_stream_is_still_valid_jsonl():
    t0 = time.monotonic()
    lines = [
        '{"type":"system","subtype":"init","session_id":"s1"}\n',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"x"}]}}\n',
        '{"type":"result","result":"final"}\n',
    ]
    out = "".join(_stamp(x, t0) for x in lines)
    evs = [json.loads(x) for x in out.splitlines()]
    assert [e["type"] for e in evs] == ["system", "assistant", "result"]
    assert all("_ecat_time" in e and "_ecat_elapsed_s" in e for e in evs)
    # 单调不减 —— 相对耗时可以直接做差
    el = [e["_ecat_elapsed_s"] for e in evs]
    assert el == sorted(el)
