---
name: xlsx
description: "서버판에서 엑셀·CSV를 읽거나 경비·연락처 표를 만들고 고칠 때 쓴다."
metadata:
  license: MIT
---

# 엑셀

서버판의 실행 도구는 `document_read`와 `document_write`다. 셸 스크립트를 사용자에게 시키지 않는다.

원본을 읽고 `blocks`의 시트·셀 위치를 확인한다. 새 문서는 `document_write(filename, content)`에 `content.sheets=[{name,cells:{셀주소:값}}]`를 준다. 기존 파일은 `source`와 읽기 결과의 `sha256`도 주며 바꿀 셀만 전달한다.

계산 결과는 계산식으로 남겨 사용자가 입력을 바꾸면 다시 계산되게 한다. 입력값 파랑·수식 검정은 도구가 적용한다. 받은 서식·도형을 다시 만들지 않고 원본에서 편집한다. CSV는 한국어를 보존해 읽으며 엑셀 파일로 저장한다.

저장 후 `document_read`로 수식·입력값·합계 범위·표를 살펴본다. `recalculated=false`는 검산 성공이 아니다. 사용자에게 Excel에서 열 때 재계산이 필요하다고 알린다. 수식 오류나 불완전한 결과는 완료로 보고하지 않는다. 성공한 `download_url`을 답에 링크한다.

읽기·쓰기 방식의 출처는 Hermes MIT 사무 문서 스킬이다. 서버 계정 경계에 맞게 정해진 문서 도구로 옮겼다.
