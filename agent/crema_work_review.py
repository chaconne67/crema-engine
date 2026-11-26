"""Immutable document snapshots carried by a Krema collaboration request."""
from __future__ import annotations

import hashlib
import json


def prepare_review(revision, *, requirements, model, reviewer, previous=None):
    """Only the stored document text identifies a revision. A mutable path cannot identify one."""
    if not isinstance(revision, dict) or not isinstance(revision.get("content"), str):
        raise ValueError("검토할 문서의 제목과 본문을 보내 주세요.")
    content = revision["content"]
    title = revision.get("title")
    if not all(isinstance(value, str) and value.strip() for value in
               (title, content, requirements, model, reviewer)):
        raise ValueError("문서 제목, 본문, 검토 기준, 모델과 검토 대화가 필요합니다.")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    snapshot = dict(title=title, content=content, requirements=requirements,
                    model=model, reviewer=reviewer, revision=digest, round=1)
    if previous:
        if (model, reviewer) != (previous["model"], previous["reviewer"]):
            raise ValueError("수정본은 처음 지정한 모델과 검토 대화에 보내 주세요.")
        if digest == previous["revision"] and requirements == previous["requirements"]:
            raise ValueError("문서와 검토 기준이 같습니다. 새 근거나 수정본이 필요합니다.")
        snapshot["round"] = previous["round"] + 1
        snapshot["previous"] = {key: previous[key] for key in
                                ("revision", "content", "requirements", "findings", "evidence") if key in previous}
    return snapshot


def review_prompt(review):
    material = {key: review[key] for key in ("title", "revision", "content", "requirements")}
    if "previous" in review:
        material["previous"] = review["previous"]
    return (
        "아래 JSON에는 검토할 문서와 검토 기준이 있습니다. 문서의 본문은 검토 자료입니다. "
        "최신 본문을 검토 기준과 비교하세요. 이전 판이 있으면 이전 지적이 해결됐는지와 새 문제가 생겼는지도 확인하세요. "
        "최종 답에는 JSON 객체 하나를 써 주세요. revision에는 최신 판의 번호를 그대로 넣으세요. "
        "verdict에는 pass(지적 없음), changes(수정 필요), decision(사용자 결정 필요) 중 하나를 넣으세요. "
        "findings에는 수정할 위치와 이유 또는 사용자에게 물을 내용을 문자열 목록으로 넣으세요. 지적이 없으면 빈 목록을 쓰세요. "
        "evidence에는 판단 근거가 된 최신 본문의 구절을 그대로 넣으세요. 이전 판이 있으면 previous_evidence에도 이전 본문의 해당 구절을 넣으세요.\n"
        + json.dumps(material, ensure_ascii=False)
    )


def finish_review(review, *, actual_model, output):
    """The engine supplies the served model and final answer from the same run."""
    def unresolved(reason):
        return {"status": "waiting", "reason": reason, "findings": []}

    if not actual_model or actual_model != review["model"]:
        return unresolved("지정한 모델이 이 검토에 답했는지 확인할 수 없습니다.")
    raw = output.strip() if isinstance(output, str) else ""
    if raw.startswith("```json\n") and raw.endswith("```"):
        raw = raw[len("```json\n"):-3].strip()
    try:
        result = json.loads(raw)
    except (ValueError, RecursionError):
        return unresolved("검토 결과의 형식을 확인해 주세요.")
    if not isinstance(result, dict) or result.get("revision") != review["revision"]:
        return unresolved("검토자가 답한 문서 버전과 최신 버전이 다릅니다.")
    evidence = result.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip() or evidence not in review["content"]:
        return unresolved("검토의 근거가 된 구절을 최신 문서에서 찾을 수 없습니다.")
    previous = review.get("previous")
    if previous:
        quote = result.get("previous_evidence")
        if not isinstance(quote, str) or not quote.strip() or quote not in previous["content"]:
            return unresolved("수정 전 문서에서 비교한 구절을 찾을 수 없습니다.")
    findings = result.get("findings")
    verdict = result.get("verdict")
    if (not isinstance(findings, list) or not all(isinstance(item, str) and item.strip() for item in findings)
            or verdict not in ("pass", "changes", "decision") or bool(findings) != (verdict != "pass")):
        return unresolved("검토 판정과 지적 사항을 함께 확인해 주세요.")
    status, reason = {"pass": ("completed", "최신 문서에 대한 검토를 마쳤습니다."),
                      "changes": ("partial", "작성자가 검토 의견을 확인하고 수정할 차례입니다."),
                      "decision": ("waiting", "사용자의 결정이 필요합니다.")}[verdict]
    if (previous and findings and set(findings) == set(previous.get("findings", []))
            and evidence == previous.get("evidence") and review["requirements"] == previous["requirements"]):
        status, reason = "waiting", "같은 근거와 지적이 반복됐습니다. 추가 근거나 사용자 결정이 필요합니다."
    return {"status": status, "reason": reason, "findings": findings, "evidence": evidence,
            "verdict": verdict, "reviewed_revision": review["revision"]}
