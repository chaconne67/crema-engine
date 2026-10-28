"""Crema: the approval box says in Korean what a held command would do (주인님 2026-10-28).

The engine names a dangerous command in English (``tools/approval_detection.py`` DANGEROUS_PATTERNS and the
execution findings); that name stays the approval's ``pattern_key`` and what the smart judge reads. Only the
``description`` of the ``approval.request`` event the app shows is put into Korean here
(gateway/platforms/api_server_runs.py). Several findings come joined with "; ". Words already in Korean (a mail
to send) are left as they are; an English name this table does not know reads as a dangerous command in general,
so the box never shows English.
"""

from __future__ import annotations

import re

GENERAL = "위험할 수 있는 명령"

KOREAN = {
    "delete in root path": "최상위 경로의 파일 지우기",
    "recursive delete": "폴더째 지우기",
    "recursive delete (long flag)": "폴더째 지우기",
    "recursive delete (flags after operands)": "폴더째 지우기",
    "Windows cmd destructive delete": "명령 프롬프트로 파일 지우기",
    "Windows PowerShell destructive delete": "PowerShell로 파일 지우기",
    "PowerShell encoded command execution": "숨겨 쓴 PowerShell 명령 실행",
    "PowerShell destructive delete (Remove-Item)": "PowerShell로 파일 지우기",
    "Windows destructive delete (recursive/quiet switch)": "폴더째 또는 묻지 않고 파일 지우기",
    "pipe remote content to PowerShell (iwr | iex)": "인터넷에서 받은 내용을 바로 PowerShell로 실행",
    "execute remote content via Invoke-Expression": "인터넷에서 받은 내용을 바로 실행",
    "force kill processes (taskkill /F)": "프로그램 강제 종료",
    "force kill processes (Stop-Process -Force)": "프로그램 강제 종료",
    "format filesystem (Format-Volume)": "드라이브 포맷",
    "wipe disk (Clear-Disk)": "디스크 전체 지우기",
    "disk partitioning (diskpart)": "디스크 파티션 바꾸기",
    "format drive (format.com)": "드라이브 포맷",
    "wipe free space (cipher /w)": "빈 공간 완전 지우기",
    "grant Everyone access (icacls)": "모든 사용자에게 접근 권한 주기",
    "reset ACLs recursively (icacls /reset)": "폴더 전체의 접근 권한 초기화",
    "delete volume shadow copies (vssadmin)": "Windows 복원 지점 지우기",
    "delete backups (wbadmin)": "Windows 백업 지우기",
    "modify boot configuration (bcdedit /set)": "부팅 설정 바꾸기",
    "registry delete (reg delete)": "레지스트리 항목 지우기",
    "registry value delete (Remove-ItemProperty -Force)": "레지스트리 값 지우기",
    "force stop service (Stop-Service -Force)": "Windows 서비스 강제 중지",
    "stop/delete service (sc)": "Windows 서비스 중지 또는 삭제",
    "access to SSH keys (Windows path)": "SSH 키 파일 열기",
    "access to Hermes secrets (Windows path)": "엔진 비밀값 파일 열기",
    "world/other-writable permissions": "누구나 고칠 수 있게 권한 바꾸기",
    "recursive world/other-writable (long flag)": "폴더 전체를 누구나 고칠 수 있게 권한 바꾸기",
    "recursive chown to root": "폴더 전체의 소유자를 관리자로 바꾸기",
    "recursive chown to root (long flag)": "폴더 전체의 소유자를 관리자로 바꾸기",
    "format filesystem": "드라이브 포맷",
    "disk copy": "디스크 통째로 복사",
    "write to block device": "디스크에 직접 쓰기",
    "SQL DROP": "데이터베이스 표나 데이터베이스 지우기",
    "SQL DELETE without WHERE": "데이터베이스 표의 모든 행 지우기",
    "SQL TRUNCATE": "데이터베이스 표 비우기",
    "overwrite system config": "시스템 설정 파일 덮어쓰기",
    "stop/restart system service": "시스템 서비스 중지 또는 다시 시작",
    "kill all processes": "모든 프로그램 종료",
    "force kill processes": "프로그램 강제 종료",
    "force kill processes (killall -KILL)": "프로그램 강제 종료",
    "force kill processes (killall -s KILL)": "프로그램 강제 종료",
    "kill processes by regex (killall -r)": "이름이 비슷한 프로그램 한꺼번에 종료",
    "fork bomb": "컴퓨터를 멈추게 하는 명령",
    "pipe remote content to shell": "인터넷에서 받은 내용을 바로 실행",
    "execute remote script via process substitution": "인터넷에서 받은 스크립트를 바로 실행",
    "execute remote content via command substitution": "인터넷에서 받은 내용을 바로 실행",
    "cloud metadata endpoint access (instance credentials)": "클라우드 서버 인증 정보 읽기",
    "pipe decoded content to shell (possible command obfuscation)": "숨겨 쓴 명령을 풀어서 실행",
    "pipe xxd-decoded content to shell (possible command obfuscation)": "숨겨 쓴 명령을 풀어서 실행",
    "pipe tr-transformed output to shell (possible command obfuscation)": "숨겨 쓴 명령을 풀어서 실행",
    "pipe openssl-decoded content to shell (possible command obfuscation)": "숨겨 쓴 명령을 풀어서 실행",
    "overwrite system file via tee": "시스템 파일 덮어쓰기",
    "overwrite system file via redirection": "시스템 파일 덮어쓰기",
    "overwrite project env/config via tee": "프로젝트 설정·비밀값 파일 덮어쓰기",
    "overwrite project env/config via redirection": "프로젝트 설정·비밀값 파일 덮어쓰기",
    "xargs with rm": "찾은 파일을 한꺼번에 지우기",
    "find -exec/-execdir rm": "찾은 파일을 한꺼번에 지우기",
    "find dynamic shell word may expand to destructive flag": "찾은 파일을 지울 수 있는 명령",
    "find -delete": "찾은 파일을 한꺼번에 지우기",
    "dynamic shell word may expand to arbitrary program execution flag": "다른 프로그램을 실행할 수 있는 명령",
    "stop/restart hermes gateway (kills running agents)": "Crema 엔진 중지 또는 다시 시작",
    "hermes update (restarts gateway, kills running agents)": "Crema 엔진 업데이트(엔진이 다시 시작됨)",
    "docker with remote daemon redirect (-H/--host)": "다른 컴퓨터의 Docker 조작",
    "docker with daemon redirect (--context: alternate daemon)": "다른 Docker 환경 조작",
    "docker context use (switches default daemon for future commands)": "기본 Docker 환경 바꾸기",
    "podman with remote daemon redirect (--url/--connection/--identity)": "다른 컴퓨터의 Podman 조작",
    "podman remote mode (-r/--remote: remote daemon)": "다른 컴퓨터의 Podman 조작",
    "docker/podman daemon redirect via environment (DOCKER_HOST/CONTAINER_HOST)": "다른 컴퓨터의 컨테이너 조작",
    "docker compose restart/stop/kill/down (container lifecycle)": "컨테이너 중지·종료·다시 시작",
    "docker restart/stop/kill (container lifecycle)": "컨테이너 중지·종료·다시 시작",
    "start gateway outside systemd (use 'systemctl --user restart hermes-gateway')": "Crema 엔진을 따로 켜기",
    "kill hermes/gateway process (self-termination)": "Crema 엔진 종료",
    "kill process via pgrep/pidof expansion (self-termination)": "이름으로 찾은 프로그램 종료",
    "kill process via backtick pgrep/pidof expansion (self-termination)": "이름으로 찾은 프로그램 종료",
    "stop/restart hermes launchd service (kills running agents)": "Crema 엔진 중지 또는 다시 시작",
    "copy/move file into system config path": "시스템 설정 폴더에 파일 넣기",
    "overwrite project env/config file": "프로젝트 설정·비밀값 파일 덮어쓰기",
    "copy/move file into sensitive credential/SSH/shell-rc path": "인증·SSH·셸 설정 폴더에 파일 넣기",
    "in-place edit of sensitive credential/SSH/shell-rc path": "인증·SSH·셸 설정 파일 고치기",
    "in-place edit of sensitive credential/SSH/shell-rc path (long flag)": "인증·SSH·셸 설정 파일 고치기",
    "in-place edit of sensitive credential/SSH/shell-rc path (perl/ruby)": "인증·SSH·셸 설정 파일 고치기",
    "in-place edit of system config": "시스템 설정 파일 고치기",
    "in-place edit of system config (long flag)": "시스템 설정 파일 고치기",
    "in-place edit of Hermes config/env": "Crema 엔진 설정·비밀값 파일 고치기",
    "in-place edit of Hermes config/env (long flag)": "Crema 엔진 설정·비밀값 파일 고치기",
    "in-place edit of Hermes config/env (perl/ruby)": "Crema 엔진 설정·비밀값 파일 고치기",
    "shell execution via heredoc": "명령 안에 쓴 스크립트 실행",
    "git reset --hard (destroys uncommitted changes)": "커밋하지 않은 변경을 모두 버리기",
    "git force push (rewrites remote history)": "원격 저장소 기록 덮어쓰기(강제 푸시)",
    "git force push short flag (rewrites remote history)": "원격 저장소 기록 덮어쓰기(강제 푸시)",
    "git clean with force (deletes untracked files)": "Git에 없는 파일 지우기",
    "git branch force delete": "브랜치 강제 삭제",
    "git branch force delete (long flags)": "브랜치 강제 삭제",
    "git branch force delete (long flags, force-first)": "브랜치 강제 삭제",
    "chmod +x followed by immediate execution": "파일을 실행할 수 있게 바꾸고 바로 실행",
    "sudo with privilege flag (stdin/askpass/shell/list)": "관리자 권한으로 실행",
    "sudo with combined-flag privilege escalation": "관리자 권한으로 실행",
    "package manager uninstall": "프로그램 패키지 지우기",
    "command parser limit exceeded": "너무 길거나 복잡해서 살펴보지 못한 명령",
    "stop/restart hermes gateway via shell-spliced verb (kills running agents)": "Crema 엔진 중지 또는 다시 시작",
    "script execution via -e/-c flag": "명령 안에 쓴 스크립트 실행",
    "script execution via heredoc": "명령 안에 쓴 스크립트 실행",
    "shell command via -c/-lc flag": "명령 안에 쓴 셸 명령 실행",
}

_HANGUL = re.compile(r"[가-힣]")


def korean_description(text: str) -> str:
    """The approval's description for the user: each "; "-joined English name in Korean, once each."""
    if not text or _HANGUL.search(text):
        return text
    words = []
    for name in text.split("; "):
        word = KOREAN.get(name.strip(), GENERAL)
        if word not in words:
            words.append(word)
    return " · ".join(words)
