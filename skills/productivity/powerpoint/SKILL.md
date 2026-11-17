---
name: powerpoint
description: "서버판에서 PPT를 읽거나 만들고 서식을 유지해 번역할 때 쓴다."
metadata:
  license: MIT
---

# 발표 자료

원본은 `document_read`로 읽는다. 새 PPT는 `document_write`의 `.pptx` 이름과 `content.slides=[{title,body}]`로 만든다. 기존 PPT의 편집·번역은 `source`, `sha256`, `content.translations={text_units ID:새 글}`로 모든 글 조각에 대응시킨다. 슬라이드·도형·표·노트의 문맥을 함께 보고 번역하며 바꾸지 않을 글은 원문을 유지한다.

발표 PDF를 만들 때는 발표 원고를 정적 HTML로 구성하고 `pdf_save`를 쓴다. 슬라이드마다 고정 크기의 한 페이지를 만들고 마지막 장 외에는 `break-after:page`를 준다. 글을 자동 요약해 장수를 채우지 않는다. Chromium으로 실제 출력된 PDF를 다시 읽어 장수·원고·표가 맞는지 확인한다. 받은 PPT를 화면 그대로 PDF로 변환했다고 안내하려면 해당 프로그램의 렌더링 확인이 필요하다.

성공한 `download_url`을 답에 링크한다. Hermes MIT 발표 자료 스킬의 읽기·쓰기 원칙을 서버의 정해진 도구에 맞춰 적용한다.
