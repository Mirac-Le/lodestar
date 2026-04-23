"""Render a feedback ticket to markdown."""

from __future__ import annotations

from lodestar.web.feedback_markdown import render_ticket_md


def _sample_payload_bug() -> dict:
    return {
        "ticket_id": "FB-20260423-0001",
        "type": "bug",
        "status": "open",
        "severity": "daily",
        "submitter": "王磊（飞书 @wanglei）",
        "created_at": "2026-04-23 14:32:11",
        "form": {
            "title": "查「帮孩子上海上学」时董淑佳没出现",
            "involved_person_ids": [47],
            "want_to_do": "找人帮客户孩子上学",
            "did": "1. 搜索框输入\n2. 按回车",
            "actual": "只出了 3 人，董淑佳不在",
            "expected": "董淑佳应该在列表里",
            "why_expected": "她儿子在 UWC，bio 写了上海教育",
            "history": "recent",
        },
        "auto_capture": {
            "mount_slug": "me",
            "view_mode": "intent",
            "search_active": True,
            "query": "帮孩子上海上学",
            "detail_person_id": None,
            "active_path_key": "t-0-23",
            "direct_overrides": [],
            "indirect_targets": [23, 99, 104],
            "contacted_targets": [],
            "api_trace": [],
            "error_buffer": [],
            "frontend_version": "20260423-x",
            "user_agent": "Mozilla/5.0",
            "viewport": "1920x1080",
        },
        "db_snapshot": [{
            "person": {
                "id": 47, "name": "董淑佳", "bio": "教育·副校长",
                "tags": ["上海教育资源"], "skills": [],
                "companies": [], "cities": ["上海"],
                "notes": None, "is_wishlist": False,
            },
            "me_edge": {
                "strength": 3, "frequency": "yearly",
                "context": "老同事介绍",
            },
            "neighbors": [{
                "id": 23, "name": "王昌尧", "strength": 5,
                "frequency": "monthly",
            }],
        }],
        "screenshots": [],
    }


def test_render_bug_has_frontmatter() -> None:
    md = render_ticket_md(_sample_payload_bug())
    assert md.startswith("---\n")
    assert "ticket_id: FB-20260423-0001" in md
    assert "type: bug" in md


def test_render_bug_has_prompt_section_for_ai() -> None:
    md = render_ticket_md(_sample_payload_bug())
    assert "@assistant" in md
    assert "设计" in md


def test_render_bug_has_all_form_sections() -> None:
    md = render_ticket_md(_sample_payload_bug())
    for section in ("标题", "涉及的人", "你想干什么",
                    "你做了什么", "看到了什么", "期望什么",
                    "历史对比", "影响程度"):
        assert section in md, f"missing section: {section}"


def test_render_embeds_db_snapshot() -> None:
    md = render_ticket_md(_sample_payload_bug())
    assert "董淑佳" in md
    assert "strength=3" in md
