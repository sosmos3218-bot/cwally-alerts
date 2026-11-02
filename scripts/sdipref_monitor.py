#!/usr/bin/env python3
"""[SDI-PREF] Same-session KRX closes; inclusive 50/61.5% zone-entry alerts.
Existing k203780 19:00 slot; no expiry, no weekly repeats. First run seeds, no signal.
Modes auto, dry (no publish/state), selftest, install (one labelled send, no state).
"""
from __future__ import annotations
import copy
import json
import re
import sys
import urllib.request
from datetime import datetime
from decimal import Decimal
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import KST, is_trading_day, session_day
from ntfy_util import publish, resolve_topic, resolve_server
CFG_PATH = ROOT / 'configs/sdipref.json'
STATE_PATH = ROOT / 'state/sdipref.json'
TAG = '[SDI-PREF]'


def rows(code):
    url = f'https://finance.daum.net/api/quote/A{code}/days?symbolCode=A{code}&page=1&perPage=5&pagination=true'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': f'https://finance.daum.net/quotes/A{code}'})
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = json.loads(resp.read().decode('utf-8'))
    out = {}
    for r in raw.get('data') or []:
        day = str(r.get('date') or '')[:10]
        value = r.get('tradePrice')
        if str(r.get('symbolCode')) != 'A'+code:
            raise ValueError('종목 코드 불일치')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) or value is None:
            continue
        price = Decimal(str(value))
        if not price.is_finite() or price <= 0:
            raise ValueError('가격 오류')
        out[day] = price
    if not out:
        raise ValueError('KRX 일별 시세 없음')
    return out


def same_session(common, pref, day):
    if day not in common or day not in pref:
        raise ValueError(f'{day} 양 종목 KRX 종가 미확인. 지난 거래일이나 NXT로 대체하지 않음')
    c, p = Decimal(str(common[day])), Decimal(str(pref[day]))
    if not c.is_finite() or not p.is_finite() or c <= 0 or p <= 0:
        raise ValueError('양 종목 종가 오류')
    return c, p, (Decimal(1)-p/c)*100


def zone(discount, cfg):
    if discount <= Decimal(str(cfg['low_discount_pct'])):
        return 'LOW'
    if discount >= Decimal(str(cfg['high_discount_pct'])):
        return 'HIGH'
    return 'MIDDLE'


def evaluate(state, day, c, p, d, cfg):
    new = copy.deepcopy(state)
    if state.get('last_date', '') >= day:
        return new, None
    z = zone(d, cfg)
    previous = state.get('zone')
    event = z if previous is not None and z != previous and z in {'LOW', 'HIGH'} else None
    new.update(last_date=day, common_close=str(c), preferred_close=str(p), discount_pct=str(d), zone=z)
    return new, event


def message(day, c, p, d, event):
    low = event == 'LOW'
    head = '50% 이하 진입, 상대가치 이점 재점검' if low else '61.5% 이상 진입, 할인 확대 재점검'
    title = f'{TAG} 006405 {head}'
    body = '\n'.join([
        '이게 뭐 알림: 삼성SDI 보통주 006400과 우선주 006405의 동일 거래일 KRX 종가 괴리율 감시.',
        f'지금 결과/상태: {day} KRX 정규장 종가 보통주 {c:,.0f}원, 우선주 {p:,.0f}원. 괴리율 {d:.4f}%. 경계는 반올림 전 값으로 판정. 출처 Daum KRX 일별.',
        '지금 할 일: '+('할인이 줄었습니다. 우선주에서 보통주로 교체할 실익을 수수료와 함께 비교하세요.' if low else '할인이 커졌습니다. 실적 훼손과 유동성을 먼저 확인하고 우선주 상대가치를 검토하세요.'),
        '나중에: 평일 19시에 확인. 50% 이하 또는 61.5% 이상 구간 진입 때만 발송하고 벗어나면 재무장. 같은 구간 유지 중 반복 발송 없음.',
        '익절/줄일 때: 괴리율 축소만 보지 말고 실제 손익, 비중과 AMPC 및 정산이익 제외 실적을 함께 확인하세요.',
        '하지 말 것: 할인 확대를 자동 매수 신호로 보거나 NXT 가격을 섞지 마세요. 2027년까지 무배당 정책이며, 우선주는 지정가 주문을 쓰세요.'
    ])
    return title, body


def selftest():
    cfg = {'low_discount_pct': 50, 'high_discount_pct': 61.5}
    for d, z in [('50', 'LOW'), ('50.00001', 'MIDDLE'), ('61.49999', 'MIDDLE'), ('61.5', 'HIGH')]:
        assert zone(Decimal(d), cfg) == z
    s, e = evaluate({}, '2026-10-28', 100, 40, Decimal(60), cfg); assert e is None
    s, e = evaluate(s, '2026-10-29', 100, 38.5, Decimal('61.5'), cfg); assert e == 'HIGH'
    old = copy.deepcopy(s)
    s, e = evaluate(s, '2026-10-29', 100, 38, Decimal(62), cfg); assert e is None and s == old
    s, e = evaluate(s, '2026-10-30', 100, 38, Decimal(62), cfg); assert e is None
    s, e = evaluate(s, '2026-11-02', 100, 40, Decimal(60), cfg); assert e is None
    s, e = evaluate(s, '2026-11-03', 100, 38, Decimal(62), cfg); assert e == 'HIGH'
    s, e = evaluate(s, '2026-11-04', 100, 50, Decimal(50), cfg); assert e == 'LOW'
    s, e = evaluate(s, '2026-11-05', 100, 51, Decimal(49), cfg); assert e is None
    for a, b in [({'2026-10-29': 100}, {'2026-10-28': 40}), ({'2026-10-28': 100}, {'2026-10-28': 40})]:
        try:
            same_session(a, b, '2026-10-29')
        except ValueError:
            pass
        else:
            raise AssertionError('date mismatch not rejected')
    c, p, d = same_session({'2026-10-29': 538000}, {'2026-10-29': 220000}, '2026-10-29')
    assert Decimal(59) < d < Decimal(60)
    assert not is_trading_day(datetime(2026, 10, 31).date())
    print('SELFTEST PASS: inclusive boundaries, seed, daily dedupe, rearm, direct zone switch, mismatched/stale dates, KRX pair, holiday')


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'auto'
    if mode == 'selftest':
        selftest(); return
    if mode not in {'auto', 'dry', 'install'}:
        raise SystemExit('unknown mode')
    cfg = json.loads(CFG_PATH.read_text())
    if mode == 'install':
        title = TAG+' 006405 설치 확인 | 매매 신호 아님'
        body = '\n'.join([
            '이게 뭐 알림: 삼성SDI 보통주/우선주 종가 괴리율 알림 설치 확인.',
            '지금 결과/상태: 같은 날 KRX 종가로 50% 이하 또는 61.5% 이상 진입을 감시하도록 연결했습니다. 실전 조건 충족 알림이 아닙니다.',
            '지금 할 일: 알림 수신만 확인하세요. 매매하지 마세요.',
            '나중에: 기존 평일 19시 슬롯. 첫 실행은 기준만 기록하고 이후 구간 진입 때 1회. 공시 점검은 별도 기존 [SDI] 4분기 확장에 연결합니다.',
            '익절/줄일 때: 설치 확인으로 비중을 바꾸지 마세요.',
            '하지 말 것: NXT 시세 혼용, 시장가 주문, 할인 확대만으로 자동 매수.'
        ])
        result = publish(resolve_topic('port'), title, body, 3, ['memo'], resolve_server(cfg['ntfy_server']), config_name='sdipref')
        print('INSTALL RESULT', result); return
    now = datetime.now(KST)
    day = session_day(now)
    if not is_trading_day(day) or day.isoformat() < cfg['start_date']:
        print('non-session; silent'); return
    if now.date() == day and (now.hour, now.minute) < (15, 40):
        print('not a completed KRX session; silent'); return
    dry = mode == 'dry'
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    try:
        c, p, d = same_session(rows(cfg['ticker_common']), rows(cfg['ticker']), day.isoformat())
        new, event = evaluate(state, day.isoformat(), c, p, d, cfg)
    except Exception as exc:
        print(type(exc).__name__, str(exc))
        if dry:
            print('DRY RUN: no ntfy, no state'); return
        if state.get('ops_day') != day.isoformat():
            publish('cwally-ops', TAG+' 006405 종가 조회 실패', f'{day} 동일 거래일 KRX 종가 확인 실패: {exc}. 매매 신호 아님. 데이터 상태는 갱신하지 않습니다.', 3, ['warning'], resolve_server(cfg['ntfy_server']), config_name='sdipref')
            state['ops_day'] = day.isoformat()
            STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2)+'\n')
        return
    print(f'{day} KRX common={c}, preferred={p}, discount={d:.4f}%, zone={new.get("zone")}, event={event}, seed={not bool(state.get("last_date"))}')
    if event:
        title, body = message(day.isoformat(), c, p, d, event)
        if dry:
            print(title, body, sep='\n')
        else:
            publish('cwally-port', title, body, 4, ['chart_with_upwards_trend'], resolve_server(cfg['ntfy_server']), config_name='sdipref')
    if dry:
        print('DRY RUN: no ntfy, no state'); return
    if new != state:
        new['updated_at'] = now.isoformat(timespec='seconds')
        STATE_PATH.write_text(json.dumps(new, ensure_ascii=False, indent=2)+'\n')


if __name__ == '__main__':
    main()
