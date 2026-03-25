# 22B Strategy Engine

**Binance Futures 자동매매 봇 — 페이퍼 트레이딩 & 실전 매매 지원**

> 🚧 **현재 기본 모드**: OBSERVE / LIMITED (페이퍼 트레이딩)
>
> ⚠️ **면책 조항**: 이 소프트웨어는 교육·연구 목적으로 공개됩니다.
> 암호화폐 선물 거래는 원금 전액 손실 위험이 있으며,
> 이 봇 사용으로 발생하는 금전적 손실에 대해 개발자는 책임을 지지 않습니다.

---

## 목차

1. [프로젝트 개요](#1-프로젝트-개요)
2. [주요 기능](#2-주요-기능)
3. [아키텍처](#3-아키텍처)
4. [설치 및 실행](#4-설치-및-실행)
5. [환경변수 설정](#5-환경변수-설정)
6. [대시보드 가이드](#6-대시보드-가이드)
7. [전략 파이프라인](#7-전략-파이프라인)
8. [기본 전략 3종](#8-기본-전략-3종)
9. [전략 개발 가이드](#9-전략-개발-가이드)
10. [이미지 패턴 전략](#10-이미지-패턴-전략-신기능)
11. [페이퍼 성과 분석](#11-페이퍼-성과-분석)
12. [운영 모드](#12-운영-모드)
13. [안전장치](#13-안전장치)
14. [기술 스택](#14-기술-스택)
15. [기여 방법](#15-기여-방법)
16. [라이선스](#16-라이선스)

---

## 1. 프로젝트 개요

22B Strategy Engine은 Binance Futures 선물 거래를 위한 **풀스택 자동매매 시스템**입니다.

```
Binance WebSocket ──► DataStore (Memory + SQLite)
                              │
                    ┌─────────▼──────────┐
                    │  RegimeDetector    │  BTC 시장 국면 판단
                    └─────────┬──────────┘
                              │
               ┌──────────────▼──────────────────────────┐
               │        StrategyManager v1.3              │
               │  Signal → Opportunity → Score → Top-N   │
               └──────────────┬──────────────────────────┘
                              │
              ┌───────────────┴──────────────┐
              │                              │
   ┌──────────▼──────────┐      ┌────────────▼────────────┐
   │   PaperRecorder      │      │  Executor (ACTIVE only) │
   │   (LIMITED 모드)     │      │  Binance Futures API    │
   └─────────────────────┘      └─────────────────────────┘
              │
   FastAPI Dashboard (:8000) ◄── WebSocket 실시간 Push
```

**핵심 특징:**
- **4단계 운영 모드**: OBSERVE → LIMITED(페이퍼) → ACTIVE(실전) → BLOCKED
- **Opportunity 파이프라인**: 신호를 11개 규칙으로 스코어링 후 Top-N만 실행
- **AI 분석**: OpenClaw(Claude) 기반 일일·주간 자동 리뷰
- **이미지 패턴 전략**: 차트 이미지 업로드 → AI 분석 → 전략 자동 생성
- **안전장치**: Kill Switch, 자동 건강도 모니터링, 위험 관리 엔진

---

## 2. 주요 기능

### 거래 엔진

| 기능 | 설명 |
|------|------|
| 실시간 시세 수집 | Binance WebSocket으로 캔들·티커·펀딩레이트·미결제약정 수신 |
| 시장 국면 감지 | BTC 기준 상승/하락/횡보 레짐 자동 분류 (EMA50, ATR, RSI, BB Bandwidth) |
| Opportunity 파이프라인 | 신호 → 정규화 → 스코어링(11개 규칙, 최대 20점) → 우선순위 큐 → Top-N 실행 |
| 포트폴리오 제약 | 최대 동시 포지션 2개, 같은 방향 1개 제한 |
| 자동 TP/SL | 진입 시 자동 계산, 매 사이클 모니터링 |
| Reconciler | 5분마다 Binance 실시간↔내부 DB 대조 검증 |
| 런타임 파라미터 | 재시작 없이 대시보드에서 전략 파라미터 수정 |

### AI 분석 (OpenClaw 연결 시)

| 기능 | 주기 | 설명 |
|------|------|------|
| 시장 해석 | 레짐 전환 시 | AI가 전환 이유·신호·전략 추천 분석 |
| 일일 리뷰 | 매일 22:00 UTC | 전략 성과·시장 요약 자동 생성 |
| 주간 리뷰 | 매주 일요일 00:00 UTC | 심층 분석 + 전략 승격/강등 추천 |
| 이미지 패턴 분석 | 사용자 요청 시 | 차트 이미지 → 조건 JSON 자동 추출 |

### 대시보드 (http://localhost:8000)

- **실시간 WebSocket**: 틱 데이터, 신호, 포지션 변경 즉시 반영
- **30개 이상 REST API**: Swagger UI `/api/docs`에서 확인
- **페이퍼 성과 시뮬레이션**: $1,000 기준 승률·손익비·최종 잔고 계산
- **이미지 패턴 패널**: 드래그앤드롭 업로드 → AI 분석 → 저장

---

## 3. 아키텍처

```
blockchain-tranding/
│
├── bot/                              핵심 봇 엔진
│   ├── main.py                       진입점 (Engine 클래스)
│   ├── config.py                     .env 설정 관리
│   │
│   ├── data/
│   │   ├── collector.py              Binance WebSocket + REST 수집
│   │   ├── store.py                  DataStore (메모리 캐시 + SQLite)
│   │   ├── replay_account.py         가상 계좌 (백테스트용)
│   │   └── validation_*.py           오프라인 검증 데이터 로더
│   │
│   ├── regime/
│   │   ├── detector.py               RegimeDetector — BTC 시장 국면
│   │   └── fast_layer.py             FastLayer — 단기 5종 보조 신호
│   │
│   ├── strategies/
│   │   ├── _base.py                  StrategyBase 추상 클래스, Signal 데이터클래스
│   │   ├── manager.py                StrategyManager — v1.3 Opportunity 파이프라인
│   │   ├── overreaction_reversal.py  전략 1: RSI 되돌림
│   │   ├── volatility_expansion_breakout.py  전략 2: 볼린저 스퀴즈 돌파
│   │   ├── early_trend_capture.py    전략 3: 초기 추세 포착
│   │   ├── image_pattern_strategy.py 이미지 패턴 전략 (AI 분석 기반)
│   │   ├── condition_evaluator.py    16종 기술적 조건 평가 엔진
│   │   ├── signal_bus.py             신호 라우팅·필터링
│   │   ├── opportunity.py            Opportunity 정규화
│   │   ├── scoring.py                스코어링 엔진 (11개 규칙, 최대 20점)
│   │   ├── opportunity_queue.py      TTL 1h 우선순위 큐
│   │   ├── paper_recorder.py         페이퍼 포지션 관리
│   │   ├── strategy_health.py        건강도 모니터링 + 자동 일시정지
│   │   ├── approval_manager.py       Level 1/2/3 승인 체계
│   │   └── params_store.py           런타임 파라미터 저장소
│   │
│   ├── execution/
│   │   ├── executor.py               Binance Futures 주문 실행 (HMAC-SHA256)
│   │   ├── state_machine.py          주문 상태 추적
│   │   ├── risk_manager.py           위험 검증
│   │   ├── kill_switch.py            Soft/Hard 긴급 정지
│   │   ├── reconciler.py             5분 주기 거래소↔DB 대조
│   │   └── portfolio_constraints.py  포트폴리오 제약
│   │
│   ├── ai/
│   │   ├── claude_client.py          OpenClaw AI 클라이언트 (텍스트 + 이미지 비전)
│   │   ├── regime_interpreter.py     AI 시장 국면 해석
│   │   ├── daily_reviewer.py         일일 리뷰 자동화
│   │   ├── weekly_reviewer.py        주간 리뷰 자동화
│   │   └── backtest_reporter.py      백테스트 리포트 생성
│   │
│   └── notifications/
│       └── telegram.py               Telegram 알림
│
├── dashboard/
│   ├── app.py                        FastAPI 앱 (30+ 엔드포인트)
│   ├── templates/index.html          대시보드 HTML (Jinja2)
│   └── static/
│       ├── js/dashboard.js           WebSocket 클라이언트 + UI
│       └── css/style.css             다크 테마 스타일
│
├── db/
│   └── schema.py                     SQLite DDL + 마이그레이션
│
├── .env.example                      환경변수 템플릿
├── requirements.txt                  Python 패키지 목록
└── start.bat                         Windows 실행 스크립트
```

---

## 4. 설치 및 실행

### 요구사항

- Python 3.12+
- Binance 계정 (테스트넷 또는 실전)
- (선택) Telegram 봇 토큰
- (선택) OpenClaw AI 서버 — AI 분석 및 이미지 패턴 기능에 필요

### 설치

```bash
# 1. 저장소 클론
git clone https://github.com/sinmb79/straregy-consol.git
cd straregy-consol

# 2. 가상환경 생성 (권장)
python -m venv venv
source venv/bin/activate      # Linux / Mac
venv\Scripts\activate         # Windows

# 3. 패키지 설치
pip install -r requirements.txt

# 4. 환경변수 파일 생성
cp .env.example .env
# .env 파일을 열어 Binance API 키 등 입력
```

### 실행

```bash
# Python으로 직접 실행
python -m bot.main

# Windows 배치 파일
start.bat
```

대시보드: `http://localhost:8000`
API 문서: `http://localhost:8000/api/docs`

### 처음 시작 권장 순서

```
1. .env에서 BINANCE_TESTNET=true, SYSTEM_MODE=OBSERVE 설정
2. python -m bot.main 실행
3. 대시보드에서 시장 국면·신호 정상 동작 확인
4. Settings 패널에서 모드를 LIMITED로 변경 (페이퍼 트레이딩)
5. 충분한 검증 후 ACTIVE 모드로 전환
```

---

## 5. 환경변수 설정

`.env.example` 을 복사하여 `.env`를 만든 후 설정합니다.

```bash
cp .env.example .env
```

### 필수

| 변수 | 설명 | 예시 |
|------|------|------|
| `BINANCE_API_KEY` | Binance API 키 | `abc123...` |
| `BINANCE_API_SECRET` | Binance API 시크릿 | `xyz789...` |
| `BINANCE_TESTNET` | 테스트넷 사용 여부 | `true` |
| `SYSTEM_MODE` | 초기 운영 모드 | `OBSERVE` |

### 선택

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `TRACKED_SYMBOLS` | `BTCUSDT,ETHUSDT` | 추적 심볼 (쉼표 구분) |
| `CANDLE_INTERVALS` | `1h,4h` | 캔들 수집 주기 |
| `DASHBOARD_HOST` | `0.0.0.0` | 대시보드 바인드 주소 |
| `DASHBOARD_PORT` | `8000` | 대시보드 포트 |
| `TELEGRAM_BOT_TOKEN` | (없음) | Telegram 알림 토큰 |
| `TELEGRAM_CHAT_ID` | (없음) | Telegram 채널 ID |
| `AI_ENABLED` | `true` | AI 분석 기능 활성화 |
| `OPENCLAW_BASE_URL` | `http://127.0.0.1:18789` | OpenClaw 서버 주소 |
| `OPENCLAW_AGENT_ID` | `main` | OpenClaw 에이전트 ID |
| `DAILY_REVIEW_HOUR` | `22` | 일일 리뷰 실행 시각 (UTC 기준 시) |
| `WEEKLY_REVIEW_DAY` | `6` | 주간 리뷰 요일 (0=월 ~ 6=일) |
| `DB_PATH` | `./data/trading.db` | SQLite DB 파일 경로 |
| `LOG_LEVEL` | `INFO` | 로그 레벨 |

> ⚠️ `.env` 파일은 절대 git에 커밋하지 마세요. `.gitignore`에 이미 포함되어 있습니다.

---

## 6. 대시보드 가이드

### 헤더 바

| 항목 | 설명 |
|------|------|
| **Mode** | 현재 운영 모드 배지 |
| **Exchange** | Binance WebSocket 연결 상태 |
| **Balance** | 계좌 잔고 (ACTIVE 모드 실시간) |
| **Today P&L** | 오늘 손익 |
| **Regime** | 현재 시장 국면 |
| **KILL SWITCH** | 긴급 전체 정지 버튼 |

### 패널 구성

| 패널 | 내용 |
|------|------|
| **System Status** | 봇·Binance WS·AI·Telegram 연결 상태 + 가동 시간 |
| **Market Regime** | BTC 시장 국면 + 8개 지표 (EMA50, ATR%, RSI, 펀딩레이트 등) |
| **Panel 1: Live Indicators** | 추적 심볼별 실시간 지표 카드 |
| **Panel 2: Strategy Signals** | 전략 신호 실시간 스트림 (WebSocket) |
| **Panel 3: Positions** | LIVE / PAPER 오픈 포지션 |
| **Panel 4: Strategy Board** | 전략별 승률·손익비·MDD·기대값·건강도 |
| **Paper Trading Performance** | $1,000 기준 성과 시뮬레이션 패널 |
| **Panel 5: Trade Log** | 종료 거래 내역 (전략·기간·모드 필터) |
| **Panel 6: Regime & AI Analysis** | AI 시장 해석·추천·주간 리뷰 |
| **이미지 패턴 전략** | 차트 이미지 업로드 → AI 분석 → 전략 저장 |
| **Settings** | 모드 변경·Kill Switch·AI 토글·심볼 관리·파라미터 수정 |

### 주요 API 엔드포인트

```
GET  /api/snapshot              전체 상태 스냅샷
GET  /api/signals               최근 신호 목록
GET  /api/strategy-stats        전략별 통계 (승률, 손익비 등)
GET  /api/trade-log             거래 로그
GET  /api/paper-performance     페이퍼 성과 시뮬레이션
GET  /api/live-positions        실시간 포지션
GET  /api/regime                현재 시장 국면
POST /api/kill-switch           긴급 정지
POST /api/kill-switch/reset     긴급 정지 해제
POST /api/image-pattern/analyze 이미지 패턴 AI 분석
POST /api/image-pattern/save    분석 결과를 전략으로 저장
GET  /api/image-patterns        저장된 패턴 전략 목록
WS   /ws/live                   실시간 WebSocket 스트림
```

전체 엔드포인트: `http://localhost:8000/api/docs` (Swagger UI)

---

## 7. 전략 파이프라인

```
각 전략.compute(store, regime) → List[Signal]
              │
              ▼
    OpportunityNormalizer
    symbol, side, entry_price, tp, sl 정규화
              │
              ▼
    ScoringEngine — 11개 규칙, 최대 20점
    ┌─ 레짐 호환성     (+3)
    ├─ 펀딩레이트 상태 (+2)
    ├─ 거래량 상태     (+2)
    ├─ 전략 신뢰도     (+3)
    ├─ 변동성 상태     (+2)
    └─ 기타 6개 규칙   (+8)
              │
              ▼
    OpportunityQueue — TTL 1시간, 점수순 정렬
              │
              ▼
    Top-N 필터 (min_score ≥ 8, 심볼 티어별)
              │
        ┌─────┴─────┐
        │           │
   PaperRecorder  Executor
   (LIMITED)      (ACTIVE)
```

---

## 8. 기본 전략 3종

### overreaction_reversal (RSI 되돌림)

**타입**: Reversal | **허용 레짐**: BTC_BEARISH, BTC_SIDEWAYS, HIGH_VOLATILITY

- **매수 조건**: RSI(14) < 28 + RSI 반등 중 + 최근 3봉 내 3% 이상 급락 + 가격 > EMA200×0.90 + 펀딩 ≤ 0.0001
- **매도 조건**: RSI(14) > 72 + RSI 하락 중 + 최근 3봉 내 3% 이상 급등 + 가격 < EMA200×1.10 + 펀딩 ≥ -0.0001

### volatility_expansion_breakout (볼린저 스퀴즈 돌파)

**타입**: Breakout | **허용 레짐**: BTC_BULLISH, BTC_SIDEWAYS, ALT_ROTATION

- **매수 조건**: 볼린저 밴드 스퀴즈 해소 + 상단 돌파 + 거래량 급증 + RSI 50~75
- **매도 조건**: 볼린저 밴드 스퀴즈 해소 + 하단 붕괴 + 거래량 급증 + RSI 25~50

### early_trend_capture (초기 추세 포착)

**타입**: Trend | **허용 레짐**: BTC_BULLISH, BTC_BEARISH, ALT_ROTATION

- **매수 조건**: EMA50 > EMA200 + 최근 골든크로스 발생 + 가격이 두 EMA 위 + RSI 40~65
- **매도 조건**: EMA50 < EMA200 + 최근 데드크로스 발생 + 가격이 두 EMA 아래 + RSI 35~60

---

## 9. 전략 개발 가이드

`StrategyBase`를 상속하고 `compute()` 메서드만 구현하면 됩니다.

### 최소 구현 예시

```python
# bot/strategies/my_strategy.py
from bot.strategies._base import Signal, StrategyBase
from bot.data.store import DataStore
from typing import List

class MyStrategy(StrategyBase):
    name          = "my_strategy"       # 고유 전략명 (소문자·언더스코어)
    category      = "trend"             # reversal | breakout | trend | custom
    regime_filter = ["BTC_BULLISH"]     # 허용할 레짐 목록

    def compute(self, store: DataStore, regime: dict) -> List[Signal]:
        from bot.config import get_config
        import pandas as pd

        signals    = []
        regime_str = regime.get("regime", "UNKNOWN")

        for symbol in get_config().tracked_symbols:
            candles = store.get_candles(symbol, "1h", limit=50)
            if not candles or len(candles) < 20:
                continue

            df    = pd.DataFrame(candles).sort_values("ts").reset_index(drop=True)
            close = df["c"].astype(float)
            price = float(close.iloc[-1])

            # 예시 조건: 종가가 20봉 최고가를 돌파
            if price > float(close.iloc[:-1].max()):
                signals.append(Signal(
                    strategy   = self.name,
                    symbol     = symbol,
                    action     = "BUY",
                    mode       = "PAPER",
                    confidence = 0.72,
                    regime     = regime_str,
                    tp         = round(price * 1.030, 8),   # +3.0% TP
                    sl         = round(price * 0.985, 8),   # -1.5% SL
                    reason     = f"20봉 최고가 돌파: {price:.2f}",
                ))

        return signals
```

### StrategyManager에 등록

```python
# bot/strategies/manager.py  (12번째 줄 근처)
from bot.strategies.my_strategy import MyStrategy

class StrategyManager:
    def __init__(self, store):
        ...
        self._strategies = [
            OverreactionReversalStrategy(),
            VolatilityExpansionBreakoutStrategy(),
            EarlyTrendCaptureStrategy(),
            MyStrategy(),                    # ← 추가
            self._image_pattern_strategy,
        ]
```

### 런타임 파라미터 사용 (대시보드에서 수정 가능)

```python
# 재시작 없이 대시보드에서 수정 가능한 파라미터
tp_pct  = self.get_param("tp_pct",  0.03)   # 기본 3%
sl_pct  = self.get_param("sl_pct",  0.015)  # 기본 1.5%
rsi_thr = self.get_param("rsi_threshold", 30)
```

### Signal 필드 참조

| 필드 | 타입 | 설명 |
|------|------|------|
| `strategy` | str | 전략 이름 |
| `symbol` | str | 거래쌍 (예: `"BTCUSDT"`) |
| `action` | str | `"BUY"` \| `"SELL"` \| `"SKIP"` |
| `mode` | str | `"PAPER"` \| `"LIVE"` |
| `confidence` | float | 신뢰도 0.0 ~ 1.0 |
| `regime` | str | 현재 시장 국면 |
| `tp` | float \| None | Take-Profit 가격 |
| `sl` | float \| None | Stop-Loss 가격 |
| `reason` | str | 신호 발생 이유 (대시보드 표시용) |

---

## 10. 이미지 패턴 전략 (신기능)

차트 이미지를 AI에게 분석시켜 자동으로 매매 전략을 생성합니다.

### 사용 방법

```
1. 차트 캡처 — TradingView, Binance, 또는 기타 차트 도구에서 스크린샷
2. 패턴 표시 — 캡처 이미지에 선·화살표·텍스트로 패턴 표시
              예: "지지선", "여기서 BUY", "RSI 다이버전스 발생"
3. 업로드    — 대시보드 "이미지 패턴 전략" 패널에서 이미지 업로드
4. AI 분석   — 심볼·타임프레임·설명 입력 후 [AI 분석 시작] 클릭
5. 검토·저장 — AI가 추출한 조건 목록 확인 후 [전략으로 저장]
6. 자동 실행 — 저장된 패턴은 매 사이클마다 실시간으로 감지
```

### 지원 조건 타입 (20종)

| 조건 타입 | 설명 |
|-----------|------|
| `rsi_below` / `rsi_above` | RSI 임계값 조건 |
| `rsi_recovering` / `rsi_falling` | RSI 방향 조건 |
| `price_near_level` | 특정 가격 레벨 근처 |
| `price_breakout_above` / `price_breakdown_below` | 레벨 돌파/붕괴 |
| `price_above_ema` / `price_below_ema` | EMA 위/아래 |
| `bollinger_squeeze` / `bollinger_expansion` | 볼린저 밴드 수축/확장 |
| `volume_spike` | 거래량 급증 |
| `macd_cross_bullish` / `macd_cross_bearish` | MACD 크로스 |
| `funding_below` / `funding_above` | 펀딩레이트 조건 |
| `candle_hammer` / `candle_doji` | 캔들 형태 |
| `candle_engulfing_bullish` / `candle_engulfing_bearish` | 장악형 캔들 |

> AI 분석 기능은 OpenClaw AI 서버 연결이 필요합니다.
> 미연결 시 `"Vision analysis unavailable"` 오류가 표시됩니다.

---

## 11. 페이퍼 성과 분석

대시보드 "Paper Trading Performance" 패널에서 확인합니다.

### 계산 방식

- **기준 자본**: 기본 $1,000 (패널 입력창에서 변경 가능)
- **거래당 배분**: `기준 자본 ÷ 최대 동시 포지션 수(2) = $500`
- **거래 손익 ($)**: `$500 × (pnl% / 100)` — 비복리(고정 배분) 방식
- **최대 낙폭**: 고점 대비 최대 하락 금액

### 표시 지표

| 지표 | 설명 |
|------|------|
| 승률 (Win Rate) | 수익 거래 / 전체 거래 수 |
| 총 거래수 | 종료된 페이퍼 포지션 수 |
| 순 손익 ($) | 총 달러 손익 |
| 최종 잔고 | 기준 자본 + 순손익 |
| 손익비 (Profit Factor) | 총수익 / 총손실 (≥ 1.5 권장) |
| 최대 낙폭 (Max DD) | 최대 연속 손실 금액 |
| 기대값 | 거래당 기대 수익 ($) |

---

## 12. 운영 모드

| 모드 | 설명 | 실제 주문 |
|------|------|----------|
| `OBSERVE` | 관찰 전용 — 신호 생성·통계 수집만 | ✗ |
| `LIMITED` | **페이퍼 트레이딩** — 가상 포지션만 기록 | ✗ |
| `ACTIVE` | **실전 매매** — Binance에 실제 주문 | ✓ |
| `BLOCKED` | 전체 차단 — 모든 기능 정지 | ✗ |

**권장 진입 순서**: `OBSERVE` → `LIMITED`(페이퍼 검증 충분히) → `ACTIVE`

모드 변경:
1. 대시보드 Settings 패널 → 매매 모드 버튼 클릭
2. 또는 REST API: `POST /api/settings { "system_mode": "LIMITED" }`

---

## 13. 안전장치

### Kill Switch

즉각 발동 방법:
- 대시보드 헤더의 빨간 **KILL SWITCH** 버튼
- API 호출:
  ```bash
  curl -X POST http://localhost:8000/api/kill-switch \
    -H "Content-Type: application/json" \
    -d '{"reason": "긴급 정지", "authorized_by": "operator"}'
  ```

| 타입 | 동작 |
|------|------|
| Soft Kill | 신규 진입 차단, 기존 포지션은 SL/TP로 보호 유지 |
| Hard Kill | 모든 활동 즉시 중단 |

### 전략 건강도 자동 모니터링

`StrategyHealthEngine`이 자동으로 전략 성과를 추적합니다:

- 최근 10거래 손익비 < 0.5 → 자동 일시정지
- 최근 20거래 MDD > -15% → 자동 일시정지
- 건강도 회복 시 자동 재개

### 포트폴리오 제약

```
최대 동시 오픈 포지션  : 2개
동일 방향(LONG or SHORT): 1개
같은 전략+심볼 중복    : 차단
신호 ID 중복 진입      : 차단 (idempotency)
```

### Reconciler

5분마다 Binance 실시간 포지션과 내부 DB를 자동 대조하여
불일치 발생 시 로그·Telegram 알림을 보냅니다.

---

## 14. 기술 스택

| 분야 | 기술 |
|------|------|
| 언어 | Python 3.12+ |
| 웹 프레임워크 | FastAPI + uvicorn (ASGI) |
| 비동기 | asyncio, websockets |
| 데이터 분석 | pandas, numpy |
| 데이터베이스 | SQLite (WAL 모드) |
| HTTP 클라이언트 | httpx |
| AI 연동 | OpenAI SDK (OpenClaw 호환, 이미지 비전 지원) |
| 알림 | python-telegram-bot |
| 설정 관리 | python-dotenv |
| 프론트엔드 | Vanilla JS + WebSocket (의존성 없음) |

---

## 15. 기여 방법

1. Fork 후 Feature Branch 생성: `git checkout -b feature/my-strategy`
2. 변경사항 커밋
3. Pull Request 생성 — 전략 성과 데이터 또는 테스트 결과 첨부 권장

### 새 전략 기여 체크리스트

- [ ] `StrategyBase` 상속 및 `compute()` 구현
- [ ] `name`, `category`, `regime_filter` 클래스 속성 설정
- [ ] `StrategyManager._strategies` 목록에 등록
- [ ] 페이퍼 트레이딩으로 최소 30거래 성과 검증
- [ ] 손익비 ≥ 1.2, 승률 ≥ 45% 이상
- [ ] `get_param()`을 통한 런타임 파라미터 지원

---

## 16. 라이선스

MIT License — 자유롭게 사용, 수정, 배포 가능합니다.
상업적 사용 시 원본 저장소 링크를 유지해 주세요.

```
MIT License

Copyright (c) 2025 22B Strategy Engine Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

*22B Strategy Engine — Built for learning, not for guaranteed profits.*
