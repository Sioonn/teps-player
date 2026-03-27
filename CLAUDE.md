# CLAUDE.md

## Project Overview

TEPS(영어능력검정시험) 리스닝 문제를 웹에서 문제별로 재생하고 학습할 수 있는 단일 페이지 웹 플레이어.
GitHub Pages로 배포 중 (https://sioonn.github.io/teps-player/).

## Tech Stack

- **Frontend**: 단일 `index.html` (vanilla JS, CSS 포함)
- **Audio Processing**: `split.py` (Python, pydub + ffmpeg)
- **Database**: Supabase (PostgreSQL) — marks, highlights 저장
- **Deployment**: GitHub Pages (정적), Render (Flask server — 선택적)

## Structure

```
mp3/                    — 원본 MP3 파일 (gitignore됨)
script/<test_name>/     — 분할된 세그먼트 MP3 파일들
index.html              — 웹 플레이어 (모든 CSS/JS 포함)
manifest.json           — 테스트 목록 및 파일 정보
scripts.json            — 문제별 스크립트 텍스트 (테스트별로 구분)
split.py                — MP3 silence 분할 CLI 도구
server.py               — Flask 서버 (업로드/처리용, Render 배포)
requirements.txt        — Python 의존성 (pydub, flask, gunicorn)
Procfile                — Render 배포용
robots.txt              — 검색엔진 차단
```

## TEPS 문제 구조 (Test2, Test3 기준)

- 총 40문제, Part 1(Q1-10), Part 2(Q11-20), Part 3(Q21-30), Part 4(Q31-40)
- Q1-36: 각각 독립된 세그먼트
- Q37-38: 하나의 지문을 공유 → `37_38.mp3`로 병합
- Q39-40: 하나의 지문을 공유 → `39_40.mp3`로 병합
- 최종 파일 수: 38개 (01.mp3 ~ 36.mp3 + 37_38.mp3 + 39_40.mp3)
- **주의**: 모든 MP3가 이 구조는 아님 (예: jungseok_1은 다른 구조)

## manifest.json 형식

```json
{
  "tests": [
    {
      "name": "test2",
      "displayName": "Test 2",
      "path": "script/test2",
      "files": [
        { "file": "01.mp3", "label": "01" },
        { "file": "37_38.mp3", "label": "37-38" }
      ]
    }
  ]
}
```

- `files` 배열 사용 (questionCount 아님)
- index.html 내 EMBEDDED_MANIFEST도 동기화 필요

## scripts.json 형식

```json
{
  "test2": { "4": "스크립트...", "5": "스크립트..." },
  "test3": { "1": "스크립트...", "2": "스크립트..." },
  "jungseok_1": { "1": "스크립트...", "2": "스크립트..." }
}
```

- 최상위 키: 테스트 이름 (manifest의 name과 일치)
- 값 키: 문제 번호 (문자열)

## Supabase 설정

- **URL**: `https://hypvsxmnezflzkghervs.supabase.co`
- **Anon Key**: index.html에 하드코딩됨
- **테이블**:
  - `marks` — 문제 마크 (더블클릭 노란 표시). 컬럼: `key TEXT PRIMARY KEY`
  - `highlights` — 스크립트 텍스트 하이라이트. 컬럼: `id UUID PK, script_key TEXT, start_offset INT, end_offset INT`

## 새 MP3 추가 파이프라인

새로운 MP3 파일이 `mp3/` 에 추가되면 아래 순서대로 수행:

### 1. MP3 분할
```bash
python3 split.py mp3/<filename>.mp3 --min-silence-len 3000 --threshold -40
```
- 출력: `script/<test_name>/01.mp3, 02.mp3, ...`
- 약 2~5분 소요 (파일 크기에 따라)

### 2. 세그먼트 수 확인 및 후처리

**TEPS 구조 (Test2, Test3 등 40문제):**
- split_on_silence가 보통 45개 raw segment 생성
- 세그먼트 길이를 확인하여 후처리:
  - 01~36: 그대로 유지
  - 37번 이후 짧은(~5s) 세그먼트들은 인접 세그먼트와 병합
  - 일반적으로: 37+38+39+40 → `37_38.mp3`, 41+42+43+44 → `39_40.mp3`
  - 마지막 짧은 세그먼트(45번 등) 제거 (trailing silence)
- 최종 38개 파일

**비-TEPS 구조 (jungseok 등):**
- split 결과를 그대로 사용
- 마지막 세그먼트가 ~7s 이하면 trailing으로 판단하여 제거 고려

### 3. manifest.json 업데이트
- split.py가 `questionCount`로 넣으므로 → `files` 배열로 수동 교체
- 병합된 파일(37_38.mp3 등)의 label은 `"37-38"` 형식

```python
# manifest 업데이트 패턴
files = []
for i in range(1, 37):
    files.append({"file": f"{i:02d}.mp3", "label": f"{i:02d}"})
files.append({"file": "37_38.mp3", "label": "37-38"})
files.append({"file": "39_40.mp3", "label": "39-40"})
```

### 4. index.html EMBEDDED_MANIFEST 업데이트
```python
# manifest.json → index.html 내 MANIFEST 블록 동기화
# MANIFEST_START ~ MANIFEST_END 사이 교체
```

### 5. 스크립트 추가 (선택)
- `all_scripts_*.txt` 파일이 있으면 파싱하여 `scripts.json`에 추가
- 테스트 이름을 키로, 문제번호를 하위 키로 사용
- Q37-38, Q39-40 등 공유 지문이 있으면 passage + 개별 문제를 합쳐서 저장

### 6. Git commit & push
```bash
git add script/<test_name>/ manifest.json index.html scripts.json
git commit -m "Add <test_name> MP3 split segments and update manifest"
git push
```
- GitHub Pages에 1~2분 후 반영
- 반영 안 되면 Ctrl+Shift+R (캐시 초기화)

## 주요 기능 참고

- **문제 재생**: 문제 버튼 클릭 → 해당 세그먼트 재생
- **마크**: 더블클릭 → 노란 표시 (Supabase marks 테이블)
- **하이라이트**: 스크립트 텍스트 드래그 → 형광펜 (Supabase highlights 테이블)
- **배속**: 위/아래 방향키 또는 ▲▼ 버튼 (0.1 단위)
- **Seek**: 좌/우 방향키 또는 -3/+3 버튼
- **Auto advance**: 체크 시 다음 문제 자동 재생
- **Silence markers**: 재생바에 회색으로 묵음 구간 표시 (Web Audio API)
- **모바일**: 햄버거 메뉴 (헤더 내), 2줄 재생바, 100dvh 레이아웃
