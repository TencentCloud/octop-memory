"""Repeat the labeled entity recall comparison through the public prompt pipeline.

Run with python -m evals.recall.entity_cases [--dsn DEDICATED_TEST_DSN].
Prints per-question atom IDs, ranks, and abstention errors for both corpus sizes.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter_ns

from octop_memory import Memory
from octop_memory.domain.alias import normalize_alias
from octop_memory.pipeline.recall import recall_for_prompt
from octop_memory.storage.backends.sqlite import SqliteMemoryBackend
from octop_memory.types import Alias, AtomCard, Entity

NOW = datetime(2026, 11, 19, 12, tzinfo=UTC)
NAMES = {
    "h": ("Hermes", "信使"),
    "s": ("星河计划", "银河组"),
    "n": ("Northwind Ops", "北风"),
    "y": ("叶青",),
    "u": ("个人偏好",),
}
FACTS = [
    ("h1", "h", "回滚方案采用上一版镜像。", 90),
    ("h2", "h", "告警升级由平台组值班经理接手。", 70),
    ("h3", "h", "附件上限为二十兆字节。", 89),
    ("s1", "s", "回滚方案通过蓝绿切换恢复流量。", 80),
    ("s2", "s", "采购预算封顶八万元。", 61),
    ("s3", "s", "签字人是财务主管。", 47),
    ("n1", "n", "回滚方案是恢复磁盘快照。", 88),
    ("n2", "n", "服务日志保留九十天。", 76),
    ("n3", "n", "发票寄往杭州办公室。", 54),
    ("y1", "y", "叶青负责 Hermes 的验收签字。", 56),
    ("y2", "y", "星河计划的交接材料由叶青归档。", 55),
    ("u1", "u", "周报默认导出 Markdown。", 83),
    ("u2", "u", "代码审查提醒只走邮件。", 64),
    ("u3", "u", "会议预留十分钟休息。", 53),
    ("h4", "h", "容量评审批准增加两个副本。", 7),
    ("h5", "h", "新版客户端完成灰度发布。", 1),
    ("s4", "s", "容量评审决定暂缓扩容。", 21),
    ("s5", "s", "搜索服务完成压测。", 2),
    ("n4", "n", "权限审批由项目经理签字。", 3),
]
# The question label is a target atom ID. None indicates that no fact answers it.
# The final field identifies an explicitly named or thread-resolved project.
QUESTIONS = [
    ("点名查细节", "Hermes 的回滚方案是什么", "h1", None, "h"),
    ("点名查细节", "Hermes 告警升级找谁", "h2", None, "h"),
    ("点名查细节", "星河计划的回滚方案是什么", "s1", None, "s"),
    ("点名查细节", "星河计划采购预算有多少", "s2", None, "s"),
    ("点名查细节", "Northwind Ops 的服务日志保留多久", "n2", None, "n"),
    ("点名查细节", "Northwind Ops 的权限审批谁签字", "n4", None, "n"),
    ("点名查细节", "Hermes 新版客户端进展如何", "h5", None, "h"),
    ("别名", "信使的附件上限是多少", "h3", None, "h"),
    ("别名", "银河组的签字人是谁", "s3", None, "s"),
    ("别名", "北风的回滚方案是什么", "n1", None, "n"),
    ("别名", "银河组搜索服务压测完成了吗", "s5", None, "s"),
    ("只说主题", "周报默认导出什么格式", "u1", None, None),
    ("只说主题", "代码审查提醒怎么发", "u2", None, None),
    ("只说主题", "会议预留多久休息", "u3", None, None),
    ("只说主题", "发票寄往哪个办公室", "n3", None, None),
    ("只说主题", "告警升级谁接手", "h2", None, None),
    ("跨实体", "Hermes 的验收签字谁负责", "y1", None, "h"),
    ("跨实体", "星河计划的交接材料归谁", "y2", None, "s"),
    ("指代或时间", "那个项目的回滚方案呢", "h1", "h", "h"),
    ("指代或时间", "那个项目的服务日志保留多久", "n2", "n", "n"),
    ("指代或时间", "2026-11-12 容量评审结果", "h4", None, None),
    ("指代或时间", "2026-10-29 容量评审结果", "s4", None, None),
    ("换说法", "信使传文件最多能有多大", "h3", None, "h"),
    ("换说法", "开会中间能歇几分钟", "u3", None, None),
    ("换说法", "北风留存运行记录几个月", "n2", None, "n"),
    ("换说法", "每周的工作总结用哪种文档", "u1", None, None),
    ("无答案", "Hermes 的机房门牌号是多少", None, None, "h"),
    ("无答案", "星河计划的报销截止日期是哪天", None, None, "s"),
    ("无答案", "Northwind Ops 的采购预算封顶多少", None, None, "n"),
    ("无答案", "信使的发票寄往哪里", None, None, "h"),
    ("无答案", "我常坐的航班号是多少", None, None, None),
    ("无答案", "那个项目的负责人电话是多少", None, "h", "h"),
]


def seed(mem, extra):
    for eid, names in NAMES.items():
        kind = "Person" if eid == "y" else "Project" if eid in ("h", "s", "n") else "Fact"
        mem.add_entity(Entity(eid, kind, names[0], list(names[1:]), 0, NOW))
        for alias in names:
            mem.add_alias(Alias(normalize_alias(alias), eid, kind, "user", NOW))
    facts = list(FACTS)
    topics = ["日志巡检", "接口测试", "告警阈值", "产物归档", "证书轮换", "交接清单", "容量监控", "客户端测试"]
    for eid in ("h", "s", "n"):
        for i in range(extra):
            facts.append(
                (f"{eid}-extra-{i}", eid, f"{topics[i % len(topics)]}的第{i + 1}批记录已核对。", 0.1 + i * 0.14)
            )
    for aid, eid, body, days in facts:
        when = NOW - timedelta(days=days)
        raw = mem.add_raw(body, event_type="user_message")
        mem.add_atom(
            AtomCard(
                id=aid,
                entity_id=eid,
                candidate_id="cand-" + aid,
                raw_event_ids=[raw.id],
                assertion=body,
                verbatim_quote=body,
                quote_event_id=raw.id,
                search_terms=[NAMES[eid][0]],
                occurred_at=when,
                confidence="high",
                importance="medium",
                created_at=when,
            )
        )
    return {aid: eid for aid, eid, _, _ in facts}


def timed_replay(memory, runs):
    """Warm database, uncached prompt recall. Context setup is outside the timed region."""
    samples = []
    for sweep in range(runs + 1):
        indexes = list(range(len(QUESTIONS)))
        random.Random(481 + sweep).shuffle(indexes)
        for index in indexes:
            _, question, _, ctx, _ = QUESTIONS[index]
            thread = f"timed-{index}" if ctx else None
            if ctx:
                memory.upsert_active_entity(thread, ctx, source="manual", when=NOW)
            start = perf_counter_ns()
            recall_for_prompt(memory, question, thread_id=thread, now=NOW)
            elapsed = (perf_counter_ns() - start) / 1_000_000
            if sweep:
                samples.append(elapsed)
    return {
        "samples": len(samples),
        "p50_ms": statistics.median(samples),
        "p95_ms": sorted(samples)[math.ceil(len(samples) * 0.95) - 1],
        "samples_ms": samples,
    }


def evaluate(memory, extra, timing_runs=0):
    assert memory.vector_index is None
    owners = seed(memory, extra)
    referenced_projects = {"y1": "h", "y2": "s"}
    rows = []
    for index, (group, question, gold, ctx, project) in enumerate(QUESTIONS):
        thread = f"q{index}" if ctx else None
        if ctx:
            memory.upsert_active_entity(thread, ctx, source="manual", when=NOW)
        snippets = recall_for_prompt(memory, question, thread_id=thread, now=NOW).snippets
        hits = [s.source_id for s in snippets]
        off_project = [
            aid for aid in hits if project and owners[aid] != project and referenced_projects.get(aid) != project
        ]
        rows.append(
            dict(group=group, question=question, gold=gold, project=project, hits=hits, off_project=off_project)
        )
    report = dict(
        atoms=len(owners),
        project_questions=sum(r["project"] is not None for r in rows),
        questions_with_off_project_hits=sum(bool(r["off_project"]) for r in rows),
        top5=sum(r["gold"] is not None and r["gold"] in r["hits"][:5] for r in rows),
        top1=sum(r["hits"][:1] == [r["gold"]] for r in rows),
        false_injections=sum(not r["gold"] and bool(r["hits"]) for r in rows),
        questions=rows,
    )
    if timing_runs:
        report["latency"] = timed_replay(memory, timing_runs)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", help="Dedicated PostgreSQL test database (creates synthetic namespaces).")
    parser.add_argument("--timing-runs", type=int, default=0, help="Timed shuffled sweeps per corpus after warmup.")
    parser.add_argument("--extra-per-project", type=int, nargs="+", default=[0, 32], help="Distractors per project.")
    args = parser.parse_args()
    if min(args.extra_per_project) < 0 or args.timing_runs < 0:
        parser.error("counts must be non-negative")
    reports = []
    with tempfile.TemporaryDirectory() as temp:
        for noise in args.extra_per_project:
            ns = f"replay_{uuid.uuid4().hex[:12]}"
            if args.dsn:
                from octop_memory.storage.backends.postgres import PostgresMemoryBackend

                backend = PostgresMemoryBackend(namespace=ns, dsn=args.dsn)
            else:
                backend = SqliteMemoryBackend(namespace=ns, db_path=Path(temp) / f"{ns}.sqlite")
            try:
                reports.append(evaluate(Memory(namespace=ns, backend=backend), noise, args.timing_runs))
            finally:
                try:
                    if args.dsn:
                        backend.purge_namespace()  # Only the randomly generated replay namespace.
                finally:
                    backend.close()
    print(json.dumps(reports, ensure_ascii=False, indent=2))
