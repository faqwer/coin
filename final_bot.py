import time
import pyupbit
import datetime
import requests
import os
import logging
import traceback
import signal
import sys
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

# =========================================================
# [1. 설정 및 로그 초기화]
# =========================================================

# 1-1. 환경 변수 로드 (파일명 setting.env 확인 필수)
load_dotenv("setting.env") 

access = os.getenv("UPBIT_ACCESS")
secret = os.getenv("UPBIT_SECRET")
my_token = os.getenv("TELEGRAM_TOKEN")
my_chat_id = os.getenv("TELEGRAM_CHAT_ID")

# 1-2. 로그 설정 (로그파일 5MB * 3개 보관)
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler = RotatingFileHandler('autotrade.log', maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
handler.setFormatter(log_formatter)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(handler)

# 콘솔(nohup.out)에도 출력되게 설정
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(log_formatter)
logger.addHandler(stream_handler)

def send_telegram(message):
    if not my_token or not my_chat_id: return
    url = f"https://api.telegram.org/bot{my_token}/sendMessage"
    data = {"chat_id": my_chat_id, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        logger.error(f"텔레그램 전송 실패: {e}")

# =========================================================
# [2. 시스템 종료 신호 감지기 (pkill 대응)]
# =========================================================
def sigterm_handler(signum, frame):
    msg = "🛑 시스템 명령(pkill)으로 봇이 종료되었습니다."
    logger.info(msg)
    send_telegram(msg)
    sys.exit(0)

# SIGTERM 신호 등록
signal.signal(signal.SIGTERM, sigterm_handler)

# =========================================================
# [3. 메인 시스템]
# =========================================================
try:
    # --- 키 확인 ---
    if not access or not secret:
        raise Exception("API 키가 로드되지 않았습니다. setting.env 파일을 확인하세요.")

    # --- 매매 설정 ---
    tickers = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL"]
    k_value = 0.5
    trailing_stop_rate = 0.02
    
    # --- 객체 초기화 ---
    upbit = pyupbit.Upbit(access, secret)
    daily_data = {} 
    holding_status = {}
    high_prices = {} 

    # --- 보조 함수들 ---
    def update_daily_data(ticker):
        try:
            df = pyupbit.get_ohlcv(ticker, interval="day", count=5)
            if df is None or len(df) < 5: return None
            
            yesterday = df.iloc[-2]
            today_open = df.iloc[-1]['open']
            target = today_open + (yesterday['high'] - yesterday['low']) * k_value
            prev_closes = df['close'].iloc[-5:-1]
            ma5_prev_sum = prev_closes.sum()
            start_time = df.index[-1]
            
            return {'target': target, 'ma5_prev_sum': ma5_prev_sum, 'start_time': start_time}
        except Exception as e:
            logger.error(f"[{ticker}] 데이터 갱신 중 에러: {e}")
            return None

    def get_current_ma5(ticker, current_price):
        if ticker not in daily_data: return 0
        return (daily_data[ticker]['ma5_prev_sum'] + current_price) / 5

    def get_balance_api(ticker):
        try:
            balances = upbit.get_balances()
            for b in balances:
                if b['currency'] == ticker:
                    if b['balance'] is not None:
                        return float(b['balance'])
            return 0
        except:
            return 0

    # --- 봇 시작 알림 ---
    logger.info("✅ 자동매매 봇 시작 (최종 완성본)")
    send_telegram(f"🚀 봇 시스템 시작\n대상: {tickers}")

    # --- 초기 데이터 로드 ---
    for t in tickers:
        data = update_daily_data(t)
        if data:
            daily_data[t] = data
            symbol = t.split("-")[1]
            
            # 잔고 확인 및 상태 동기화
            bal = get_balance_api(symbol)
            curr_p = pyupbit.get_current_price(t)
            if curr_p and bal * curr_p > 5000:
                holding_status[t] = True
                logger.info(f" - [{t}] 보유 중")
            else:
                holding_status[t] = False

    # =========================================================
    # [4. 무한 루프]
    # =========================================================
    while True:
        try:
            now = datetime.datetime.now()
            
            # 1. [날짜 변경 체크 및 데이터 갱신]
            if tickers[0] in daily_data:
                ref_time = daily_data[tickers[0]]['start_time']
                # 기준 시간보다 하루 + 10초 지났으면 갱신
                if now > ref_time + datetime.timedelta(days=1, seconds=10):
                    logger.info("📅 날짜 변경 -> 데이터 갱신")
                    for t in tickers:
                        new_data = update_daily_data(t)
                        if new_data:
                            daily_data[t] = new_data
                            symbol = t.split("-")[1]
                            # 날짜 바뀌면 고점 초기화
                            if symbol in high_prices: del high_prices[symbol]
                    send_telegram("✅ 일일 데이터 갱신 완료 (새로운 목표가 적용)")

            # 2. [종목별 매매 로직]
            for ticker in tickers:
                if ticker not in daily_data: continue
                symbol = ticker.split("-")[1]
                t_data = daily_data[ticker]
                target_price = t_data['target']
                start_time = t_data['start_time']
                end_time = start_time + datetime.timedelta(days=1)
                
                current_price = pyupbit.get_current_price(ticker)
                if current_price is None: continue

                ma5 = get_current_ma5(ticker, current_price)
                is_holding = holding_status.get(ticker, False)

                # [Phase A] 장 중 (09:00:00 ~ 익일 08:59:50)
                if start_time < now < end_time - datetime.timedelta(seconds=10):
                    
                    # (1) 매수 시도
                    if not is_holding:
                        if current_price > target_price and current_price > ma5:
                            krw = get_balance_api("KRW")
                            if krw > 5000:
                                # 현재 보유 종목 수 계산
                                current_holding_count = sum(1 for t in tickers if holding_status.get(t, False))
                                slots_left = len(tickers) - current_holding_count
                                
                                if slots_left > 0:
                                    # 자금 분할 (수수료/슬리피지 0.999)
                                    buy_amt = krw / slots_left * 0.999
                                    if buy_amt > 5000:
                                        res = upbit.buy_market_order(ticker, buy_amt)
                                        # 체결 대기
                                        time.sleep(1) 
                                        
                                        # 매수 성공 검증
                                        if get_balance_api(symbol) * current_price > 5000:
                                            holding_status[ticker] = True
                                            high_prices[symbol] = current_price
                                            
                                            # ✅ 매수 성공 상세 메시지
                                            msg = (f"✅ [매수 성공] {symbol}\n"
                                                   f"매수가: {current_price:,.0f}원\n"
                                                   f"목표가: {target_price:,.0f}원 (돌파)")
                                            logger.info(msg)
                                            send_telegram(msg)
                                        else:
                                            # ❌ 매수 실패 메시지
                                            msg = (f"❌ [매수 실패] {symbol}\n"
                                                   f"원인: 잔고 부족 또는 주문 취소됨")
                                            logger.warning(msg)
                                            send_telegram(msg)

                    # (2) 매도 시도 (트레일링 스탑)
                    if is_holding:
                        # 고점 갱신
                        if symbol not in high_prices or current_price > high_prices[symbol]:
                            high_prices[symbol] = current_price
                        
                        highest = high_prices[symbol]
                        stop_price = highest * (1 - trailing_stop_rate)
                        
                        # 하락 시 매도
                        if current_price < stop_price:
                            bal = get_balance_api(symbol)
                            if bal > 0:
                                upbit.sell_market_order(ticker, bal)
                                time.sleep(1)
                                
                                # 매도 확인
                                if get_balance_api(symbol) * current_price < 5000:
                                    holding_status[ticker] = False
                                    
                                    # ✅ 트레일링 스탑 상세 메시지
                                    msg = (f"📉 [트레일링 스탑 매도] {symbol}\n"
                                           f"원인: 고점 대비 -2% 하락\n"
                                           f"최고가: {highest:,.0f}원\n"
                                           f"매도가: {current_price:,.0f}원")
                                    logger.info(msg)
                                    send_telegram(msg)
                
                # [Phase B] 장 마감 직전 청산
                else:
                    if is_holding:
                        bal = get_balance_api(symbol)
                        if bal > 0:
                            upbit.sell_market_order(ticker, bal)
                            time.sleep(1)
                            holding_status[ticker] = False
                            msg = f"🏁 [{symbol}] 장 마감 강제 청산 완료"
                            logger.info(msg)
                            send_telegram(msg)
                
                time.sleep(0.2)
            time.sleep(0.5)

        except Exception as e:
            # 일시적 에러는 무시하고 계속 진행
            logger.error(f"⚠️ 루프 에러: {e}")
            time.sleep(1)

# =========================================================
# [5. 예외 처리]
# =========================================================
except KeyboardInterrupt:
    logger.info("사용자 명령(Ctrl+C)으로 종료됨")
    
except Exception as e:
    # 심각한 에러 발생 시
    err_trace = traceback.format_exc()
    logger.critical(f"💀 봇 비정상 종료!\n{err_trace}")
    send_telegram(f"💀 [긴급] 봇이 죽었습니다!\n에러: {e}")
