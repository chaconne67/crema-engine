import json

import pytest

from agent.crema_work_review import finish_review, prepare_review, review_prompt


def snapshot(text, previous=None):
    return prepare_review({"title": "계획", "content": text}, requirements="소요 시간 명시",
                          model="reviewer-model", reviewer="review-chat", previous=previous)


def result(review, verdict, findings, **more):
    return json.dumps(dict(revision=review["revision"], verdict=verdict, findings=findings,
                           evidence=review["content"], **more))


def test_fixed_revision_and_same_reviewer_verify_both_versions():
    initial = snapshot("일정을 정한다.")
    comments = finish_review(initial, actual_model="reviewer-model", output=result(initial, "changes", ["예상 시간을 넣으세요."]))
    assert comments["status"] == "partial"
    final = snapshot("30분 안에 끝낸다.", {**initial, **comments})
    assert initial["content"] in review_prompt(final)
    approved = result(final, "pass", [], previous_evidence=initial["content"])
    assert finish_review(final, actual_model="reviewer-model", output=approved)["status"] == "completed"
    for answer in [result(initial, "pass", []), result(final, "pass", []), result(final, "pass", ["문제"])]:
        assert finish_review(final, actual_model="reviewer-model", output=answer)["status"] == "waiting"
    assert finish_review(final, actual_model="other", output=approved)["status"] == "waiting"
    with pytest.raises(ValueError):
        snapshot(final["content"], final)
    with pytest.raises(ValueError):
        prepare_review({"title": "계획", "content": "초안"}, requirements="기준",
                       model="reviewer-model", reviewer="other-chat", previous=initial)
    with pytest.raises(ValueError):
        prepare_review("/docs/plan.md", requirements="기준", model="reviewer-model", reviewer="chat")


def test_repeated_findings_stop_only_when_the_actual_ground_for_the_objection_is_unchanged():
    initial = snapshot("일정을 정한다. 비용은 미정이다.")
    comments = {"revision": initial["revision"], "verdict": "changes",
                "findings": ["시간을 적어 주세요.", "비용을 적어 주세요."], "evidence": "일정을 정한다."}
    first = finish_review(initial, actual_model="reviewer-model", output=json.dumps(comments))
    second = snapshot("첫 단계에서 일정을 정한다. 비용은 미정이다.", {**initial, **first})
    reply = {**comments, "revision": second["revision"], "findings": list(reversed(comments["findings"])),
             "previous_evidence": "비용은 미정이다."}
    assert finish_review(second, actual_model="reviewer-model", output=json.dumps(reply))["status"] == "waiting"
    reply["evidence"] = "비용은 미정이다."
    assert finish_review(second, actual_model="reviewer-model", output=json.dumps(reply))["status"] == "partial"
