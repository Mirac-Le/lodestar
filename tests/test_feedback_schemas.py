"""Feedback Pydantic schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lodestar.web.schemas import (
    FeedbackAutoCapture,
    FeedbackFormBug,
    FeedbackFormFeature,
    FeedbackSubmitRequest,
)


def test_bug_form_requires_core_fields() -> None:
    with pytest.raises(ValidationError):
        FeedbackFormBug(
            title="太短",  # <10
            involved_person_ids=[1],
            want_to_do="x", did="x", actual="x", expected="x",
            history="new",
        )


def test_feature_form_user_story_must_match_when_then() -> None:
    with pytest.raises(ValidationError):
        FeedbackFormFeature(
            title="按城市筛选联系人列表",
            involved_person_ids=[1],
            user_story="我想要按城市筛选",
            acceptance=["- 能看到城市下拉"],
        )
    FeedbackFormFeature(
        title="按城市筛选联系人列表",
        involved_person_ids=[1],
        user_story="当我在搜索框旁边的时候，我希望能选一个城市筛选",
        acceptance=["- 城市下拉里有上海、北京"],
    )


def test_acceptance_must_have_at_least_one_bullet() -> None:
    with pytest.raises(ValidationError):
        FeedbackFormFeature(
            title="按城市筛选联系人列表",
            involved_person_ids=[1],
            user_story="当X的时候我希望Y",
            acceptance=[],
        )


def test_submit_request_dispatches_by_type() -> None:
    req = FeedbackSubmitRequest(
        type="bug",
        form={
            "title": "搜索漏人的情况这里描述",
            "involved_person_ids": [1, 2],
            "want_to_do": "找人",
            "did": "搜关键词",
            "actual": "没出来",
            "expected": "应出来",
            "history": "recent",
        },
        submitter="王磊（@wanglei）",
        severity="daily",
        auto_capture=FeedbackAutoCapture(
            mount_slug="me",
            view_mode="intent",
            search_active=True,
            query="测试",
            detail_person_id=None,
            active_path_key=None,
            direct_overrides=[],
            indirect_targets=[1, 2],
            contacted_targets=[],
            api_trace=[],
            error_buffer=[],
            frontend_version="20260423-x",
            user_agent="Mozilla/5.0",
            viewport="1920x1080",
        ),
        screenshots=[{
            "filename": "scr1.png",
            "content_type": "image/png",
            "data_base64": "iVBORw0KGgo=",
        }],
    )
    assert req.type == "bug"
    assert req.form.title == "搜索漏人的情况这里描述"
