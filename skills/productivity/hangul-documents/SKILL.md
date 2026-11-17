---
name: hangul-documents
description: "서버판에서 HWP·HWPX의 표를 읽거나 공문 양식을 채우고 번역할 때 쓴다."
---

# 한글 문서

읽기는 `document_read`다. `blocks`의 `rows/cols/cells`와 셀의 `row_span/col_span`을 함께 읽어 병합·빈 칸을 보존한다. 답에 표를 만들 때 행·열 관계를 유지하고 파일 이름·위치·원문을 근거로 붙인다.

새 HWPX는 `document_write`의 `.hwpx` 이름과 `content.paragraphs`, `content.tables`를 준다. 받은 양식은 `source`, `sha256`, `content.fill={라벨 경로:값}`으로 채운다. 라벨 오른쪽 칸은 `성명 > right`처럼 지정한다. 중복되거나 없는 라벨은 도구가 거절하므로 새 양식으로 대체하지 말고 실제 표를 다시 읽는다.

본문 편집·번역은 `content.translations={text_units ID:새 글}`로 모든 글 조각에 대응시킨다. 문단 맥락을 함께 보고 번역하며 바꾸지 않을 글은 원문을 유지한다. 받은 HWP는 고친 뒤 HWPX로 저장한다. 변환에서 지원하지 못한 개체나 서식은 숨기지 않는다. 서버 재읽기와 한컴에서 실제 연 확인을 구분한다.

본 제품은 한컴의 HWP 문서 파일(.hwp) 공개 문서를 참고하여 개발하였습니다.

한글·한컴·HWP·HWPX는 한컴의 상표이며 이 기능은 한컴과의 제휴를 뜻하지 않는다.
