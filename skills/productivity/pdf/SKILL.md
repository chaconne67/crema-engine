---
name: pdf
description: "서버판에서 PDF·스캔의 근거를 읽거나 PDF를 만들고 고칠 때 쓴다."
metadata:
  license: MIT
---

# PDF

읽기는 `document_read` 한 경로다. `needs_vision`이 있으면 반환된 쪽 이미지를 `vision_analyze`로 읽어 표의 행·열 관계를 보존한 전사를 만든다. `ocr=[{page,text}]`로 같은 원본을 다시 읽어 저장한다. 알아볼 수 없는 값은 추측하지 말고 사용자에게 알린다. 파일 이름·쪽 번호·원문을 답의 근거로 쓴다.

만들기와 고치기는 읽은 내용에서 요청한 새 원고를 작성해 기존 `pdf_save`에 정적 HTML과 `.pdf` 이름을 준다. 표는 HTML 표로, 그림은 `data:` 이미지로 넣는다. 외부 URL·로컬 파일·스크립트는 읽지 않는다. 원본을 덮지 않고 새 파일로 저장한다. 받은 PDF의 서식 그대로 편집하는 기능으로 안내하지 않는다.

결과를 `document_read`로 다시 읽고 빠진 표·글을 확인한다. `download_url`을 답에 링크한다. 읽기·쓰기 방식은 Hermes MIT PDF 스킬을 참고했으며 서버에서는 승인된 공통 PDF 도구만 사용한다.
