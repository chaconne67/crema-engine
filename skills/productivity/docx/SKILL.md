---
name: docx
description: "서버판에서 워드 문서를 읽거나 만들고 본문·번역을 고칠 때 쓴다."
metadata:
  license: MIT
---

# 워드

`document_read`로 원문과 표를 읽는다. 원문을 인용할 때 파일 이름과 `blocks.location`을 함께 적는다.

새 워드는 `document_write`의 `filename`을 `.docx`로, `content`를 `paragraphs=[글]`, `tables=[행목록]`으로 준다. 받은 파일의 본문 편집·번역은 `source`, 읽기 결과의 `sha256`, `content.translations={text_units ID:새 글}`을 준다. 모든 ID에 대응하는 글을 전달한다. 바꾸지 않을 글은 원문을 그대로 넣는다. 각 ID는 서식이 나뉜 글 조각이므로 문단·표 맥락을 함께 보고 자연스럽게 번역한다. 서식·그림·문서 관계는 도구가 유지한다.

저장 후 다시 읽어 요청한 내용과 표가 남았는지 확인한다. `download_url`을 답에 링크한다. 프로그램에서 연 결과와 서버 재읽기 결과를 구분한다.

읽기·쓰기 방식의 출처는 Hermes MIT 사무 문서 스킬이며 서버의 정해진 도구를 사용한다.
