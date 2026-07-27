from __future__ import annotations

from app.social.revalidation import evaluate_group_reply_revalidation


def _source(**overrides: object) -> dict[str, object]:
    return {
        "id": 10,
        "message_id": "question-1",
        "sender_wxid": "wxid_requester",
        "content": "上海明天天气怎么样？",
        "is_self_sent": False,
        **overrides,
    }


def _message(item_id: int, **overrides: object) -> dict[str, object]:
    return {
        "id": item_id,
        "message_id": f"message-{item_id}",
        "sender_wxid": f"wxid_member_{item_id}",
        "content": "普通消息",
        "is_self_sent": False,
        "metadata": {},
        **overrides,
    }


def test_peer_answer_must_be_tied_to_the_source_question() -> None:
    result = evaluate_group_reply_revalidation(
        source=_source(),
        newer_observations=[
            _message(
                11,
                content="明天上海有雨，记得带伞。",
                metadata={"quote": {"refer_msg_svr_id": "question-1"}},
            )
        ],
        participation_status="may_reply",
    )

    assert result.context_available is True
    assert result.valid_member_answer_exists is True
    assert result.topic_changed is False
    assert "valid_member_answer_after_source" in result.reason_codes


def test_unrelated_chatter_never_suppresses_a_direct_call() -> None:
    result = evaluate_group_reply_revalidation(
        source=_source(),
        newer_observations=[
            _message(11, content="午饭吃什么"),
            _message(12, content="我去楼下看看"),
            _message(13, content="给我带杯咖啡"),
        ],
        participation_status="must_reply",
    )

    assert result.valid_member_answer_exists is False
    assert result.topic_changed is False
    assert result.superseded_by_newer_message is False


def test_unrelated_update_from_requester_does_not_supersede_direct_call() -> None:
    result = evaluate_group_reply_revalidation(
        source=_source(),
        newer_observations=[
            _message(
                11,
                sender_wxid="wxid_requester",
                content="午饭改成吃面吧",
            )
        ],
        participation_status="must_reply",
    )

    assert result.valid_member_answer_exists is False
    assert result.superseded_by_newer_message is False
    assert result.reason_codes == ("source_still_current",)


def test_two_party_unrelated_tail_is_a_topic_shift_for_soft_reply() -> None:
    result = evaluate_group_reply_revalidation(
        source=_source(),
        newer_observations=[
            _message(11, sender_wxid="wxid_a", content="午饭吃什么"),
            _message(12, sender_wxid="wxid_b", content="去楼下吃面吧"),
        ],
        participation_status="may_reply",
    )

    assert result.topic_changed is True
    assert result.valid_member_answer_exists is False


def test_newer_addressed_message_from_requester_fences_old_must_reply() -> None:
    result = evaluate_group_reply_revalidation(
        source=_source(),
        newer_observations=[
            _message(
                11,
                sender_wxid="wxid_requester",
                content="@机器人 不用查天气了",
                bot_addressed=True,
            )
        ],
        participation_status="must_reply",
    )

    assert result.superseded_by_newer_message is True
    assert result.topic_changed is False


def test_missing_source_is_fail_closed() -> None:
    result = evaluate_group_reply_revalidation(
        source=None,
        newer_observations=[],
        participation_status="must_reply",
    )

    assert result.context_available is False
    assert result.reason_codes == ("source_observation_missing",)
