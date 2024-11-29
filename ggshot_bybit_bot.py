import json
import time
import logging
from pybit.unified_trading import HTTP
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import ta
import threading
import queue
from pybit.unified_trading import WebSocket
# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trading_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

class GGShotBot:
    def __init__(self, config_path='config.json'):
        self.load_config(config_path)
        self.trading_pairs = [
            "BTCUSDT",   # BTC/USDT - 1h, 30m, 15m, 5m
            "ETHUSDT",   # ETH/USDT - 1h, 30m, 15m, 5m
            "SOLUSDT"    # SOL/USDT - 1h
        ]
        
        # Таймфреймы для каждой пары
        self.timeframes = {
            "BTCUSDT": ["60", "30", "15", "5"],  # 1h, 30m, 15m, 5m
            "ETHUSDT": ["60", "30", "15", "5"],  # 1h, 30m, 15m, 5m
            "SOLUSDT": ["60"]                    # 1h
        }
        
        self.positions = {}
        self.order_queue = queue.Queue()
        
        # Хранилище исторических данных для каждого символа и таймфрейма
        self.historical_data = {}
        for symbol in self.trading_pairs:
            self.historical_data[symbol] = {}
            for timeframe in self.timeframes[symbol]:
                self.historical_data[symbol][timeframe] = pd.DataFrame()
        
        self.max_candles = 100  # Максимальное количество свечей для хранения
        
        # Инициализируем time_offset
        self.time_offset = 0
        
        # Создаем HTTP клиент для получения исторических данных
        self.http_client = HTTP(
            testnet=self.config.get('test_mode', True),
            api_key=self.config['api_key'],
            api_secret=self.config['api_secret'],
            recv_window=20000  # Используем стандартное значение
        )
        
        # Синхронизируем время с сервером
        self.time_offset = self._get_time_offset()
        logging.info(f"Time offset with server: {self.time_offset} ms")
        
        # Получаем и выводим текущий баланс
        self.get_balance()
        
        # Получаем исторические данные перед запуском WebSocket
        self.load_historical_data()
        self.setup_websocket()

    def load_config(self, config_path):
        with open(config_path, 'r') as file:
            self.config = json.load(file)

    def load_historical_data(self):
        """Загружает исторические данные для всех торговых пар и таймфреймов"""
        for symbol in self.trading_pairs:
            for timeframe in self.timeframes[symbol]:
                logging.info(f"Loading historical data for {symbol} on {timeframe}m timeframe")
                try:
                    # Получаем текущее время с учетом смещения
                    current_timestamp = self.get_current_timestamp()
                    
                    # Получаем исторические данные
                    response = self.http_client.get_kline(
                        category="spot",
                        symbol=symbol,
                        interval=timeframe,
                        limit=self.max_candles,
                        timestamp=current_timestamp
                    )
                    
                    if response['retCode'] == 0 and len(response['result']['list']) > 0:
                        # Преобразуем данные в DataFrame
                        df = pd.DataFrame(response['result']['list'], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
                        
                        # Конвертируем timestamp в числовой формат перед преобразованием в datetime
                        df['timestamp'] = pd.to_numeric(df['timestamp'], errors='coerce')
                        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                        df = df.sort_values('timestamp')
                        
                        # Конвертируем строковые значения в числовые
                        for col in ['open', 'high', 'low', 'close', 'volume', 'turnover']:
                            df[col] = pd.to_numeric(df[col], errors='coerce')
                        
                        self.historical_data[symbol][timeframe] = df
                        logging.info(f"Loaded {len(df)} historical candles for {symbol} {timeframe}m from {df['timestamp'].iloc[0]}")
                    else:
                        logging.error(f"Error in response for {symbol} {timeframe}m: {response}")
                        
                except Exception as e:
                    logging.error(f"Error loading historical data for {symbol} {timeframe}m: {e}")

    def setup_websocket(self):
        """Инициализация WebSocket соединения"""
        # Публичный WebSocket для получения рыночных данных
        self.ws_public = WebSocket(
            testnet=self.config.get('test_mode', True),
            channel_type="spot"
        )
        
        # Приватный WebSocket для получения данных аккаунта
        self.ws_private = WebSocket(
            testnet=self.config.get('test_mode', True),
            api_key=self.config['api_key'],
            api_secret=self.config['api_secret'],
            channel_type="private"  # Используем private для приватных каналов
        )
        
        # Подписываемся на обновления для каждой пары и таймфрейма
        for symbol in self.trading_pairs:
            for timeframe in self.timeframes[symbol]:
                try:
                    self.ws_public.kline_stream(
                        symbol=symbol,
                        interval=timeframe,
                        callback=self.handle_kline_update
                    )
                except Exception as e:
                    logging.error(f"Error subscribing to kline stream for {symbol} {timeframe}m: {e}")
        
        # Подписываемся на приватные каналы
        try:
            self.ws_private.wallet_stream(callback=self.handle_wallet_update)
            self.ws_private.order_stream(callback=self.handle_order_update)
            logging.info("Successfully subscribed to private channels")
        except Exception as e:
            logging.error(f"Error subscribing to private channels: {e}")
            logging.warning("Bot will continue without private data updates")

    def update_historical_data(self, symbol, timeframe, new_candle_df):
        """Обновляет исторические данные для символа и таймфрейма"""
        try:
            if symbol not in self.historical_data or timeframe not in self.historical_data[symbol]:
                self.historical_data[symbol][timeframe] = new_candle_df
                return
                
            df = self.historical_data[symbol][timeframe]
            
            # Убеждаемся, что timestamp в обоих DataFrame имеет одинаковый тип
            if not pd.api.types.is_datetime64_any_dtype(new_candle_df['timestamp']):
                new_candle_df['timestamp'] = pd.to_datetime(new_candle_df['timestamp'])
            if not pd.api.types.is_datetime64_any_dtype(df['timestamp']):
                df['timestamp'] = pd.to_datetime(df['timestamp'])
            
            # Добавляем новые данные
            df = pd.concat([df, new_candle_df])
            
            # Удаляем дубликаты и сортируем по времени
            df = df.drop_duplicates(subset=['timestamp'], keep='last')
            df = df.sort_values('timestamp')
            
            # Оставляем только последние max_candles свечей
            if len(df) > self.max_candles:
                df = df.tail(self.max_candles)
            
            self.historical_data[symbol][timeframe] = df
            logging.info(f"Updated historical data for {symbol} {timeframe}m. Total candles: {len(df)}")
            
        except Exception as e:
            logging.error(f"Error updating historical data for {symbol} {timeframe}m: {e}")

    def handle_kline_update(self, message):
        """Обработчик обновлений свечей"""
        try:
            if not isinstance(message, dict) or 'data' not in message or 'topic' not in message:
                logging.error(f"Invalid message format: {message}")
                return
                
            topic_parts = message['topic'].split('.')
            if len(topic_parts) != 3:
                logging.error(f"Invalid topic format: {message['topic']}")
                return
            
            timeframe = topic_parts[1]
            symbol = topic_parts[2]
            
            kline_data = message['data']
            if not isinstance(kline_data, list) or len(kline_data) == 0:
                logging.error(f"No kline data in message")
                return
                
            candle = kline_data[0]
            
            # Преобразуем timestamp в числовой формат перед созданием DataFrame
            timestamp = int(candle['timestamp'])
            
            # Создаем DataFrame из данных свечи
            new_candle = {
                'timestamp': timestamp,  # Сохраняем как числовое значение
                'open': float(candle['open']),
                'high': float(candle['high']),
                'low': float(candle['low']),
                'close': float(candle['close']),
                'volume': float(candle['volume']),
                'turnover': float(candle['turnover'])
            }
            
            df = pd.DataFrame([new_candle])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            
            # Обновляем исторические данные
            if symbol in self.historical_data and timeframe in self.historical_data[symbol]:
                current_df = self.historical_data[symbol][timeframe]
                
                # Объединяем данные
                combined_df = pd.concat([current_df, df])
                
                # Удаляем дубликаты и сортируем
                combined_df = combined_df.drop_duplicates(subset=['timestamp'], keep='last')
                combined_df = combined_df.sort_values('timestamp')
                
                # Оставляем только последние max_candles свечей
                if len(combined_df) > self.max_candles:
                    combined_df = combined_df.tail(self.max_candles)
                
                self.historical_data[symbol][timeframe] = combined_df
                logging.info(f"Updated historical data for {symbol} {timeframe}m. Total candles: {len(combined_df)}")
            else:
                self.historical_data[symbol][timeframe] = df
            
            # Проверяем условия для торговли
            if self.should_trade(symbol, timeframe, self.historical_data[symbol][timeframe]):
                self.execute_trade(symbol)
                
        except Exception as e:
            logging.error(f"Error in handle_kline_update: {e}")

    def handle_wallet_update(self, message):
        """Обработчик обновлений кошелька"""
        try:
            logging.info(f"Wallet update: {message}")
        except Exception as e:
            logging.error(f"Error in handle_wallet_update: {e}")

    def handle_order_update(self, message):
        """Обработчик обновлений ордеров"""
        try:
            logging.info(f"Order update: {message}")
        except Exception as e:
            logging.error(f"Error in handle_order_update: {e}")

    def calculate_indicators(self, df):
        """Рассчитывает индикаторы для торговой стратегии"""
        try:
            # Настройки из оригинального кода GGshot для BTC/USDT 1h
            IN1 = 2100
            IN2 = 8.0
            TP1 = 2.3
            TP2 = 4.6
            TP3 = 6.9
            TP4 = 13.8
            SL = 2.3
            
            # Рассчитываем EMA
            df['ema_short'] = ta.trend.ema_indicator(df['close'], window=8)
            df['ema_medium'] = ta.trend.ema_indicator(df['close'], window=13)
            df['ema_long'] = ta.trend.ema_indicator(df['close'], window=21)
            
            # Рассчитываем RSI
            df['rsi'] = ta.momentum.rsi(df['close'], window=14)
            
            # Рассчитываем уровни поддержки и сопротивления
            df['highest_high'] = df['high'].rolling(window=IN1).max()
            df['lowest_low'] = df['low'].rolling(window=IN1).min()
            
            # Рассчитываем ATR для динамических уровней TP и SL
            df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14)
            
            return df
            
        except Exception as e:
            logging.error(f"Error calculating indicators: {e}")
            return None

    def get_signal(self, df):
        """Определяет торговый сигнал на основе стратегии"""
        try:
            if len(df) < 2:  # Нужно минимум 2 свечи для сравнения
                return None, None
                
            current = df.iloc[-1]  # Текущая свеча
            previous = df.iloc[-2]  # Предыдущая свеча
            
            # Проверяем тренд
            uptrend = (current['ema_short'] > current['ema_medium'] > current['ema_long'])
            downtrend = (current['ema_short'] < current['ema_medium'] < current['ema_long'])
            
            # Условия для длинной позиции
            long_conditions = [
                uptrend,  # Восходящий тренд
                current['rsi'] > 30,  # RSI не перепродан
                current['close'] > current['ema_short'],  # Цена выше короткой EMA
                previous['close'] < previous['ema_short'],  # Предыдущая цена была ниже EMA
            ]
            
            # Условия для короткой позиции
            short_conditions = [
                downtrend,  # Нисходящий тренд
                current['rsi'] < 70,  # RSI не перекуплен
                current['close'] < current['ema_short'],  # Цена ниже короткой EMA
                previous['close'] > previous['ema_short'],  # Предыдущая цена была выше EMA
            ]
            
            # Рассчитываем уровни для позиции
            if all(long_conditions):
                entry_price = current['close']
                stop_loss = entry_price * (1 - 0.023)  # SL 2.3%
                take_profit = entry_price * (1 + 0.023)  # TP 2.3%
                return "long", {
                    "entry_price": entry_price,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit
                }
                
            elif all(short_conditions):
                entry_price = current['close']
                stop_loss = entry_price * (1 + 0.023)  # SL 2.3%
                take_profit = entry_price * (1 - 0.023)  # TP 2.3%
                return "short", {
                    "entry_price": entry_price,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit
                }
                
            return None, None
            
        except Exception as e:
            logging.error(f"Error getting trading signal: {e}")
            return None, None

    def should_trade(self, symbol, timeframe, df):
        """Проверяет условия для торговли на конкретном таймфрейме"""
        try:
            # Проверяем, достаточно ли данных
            if len(df) < 50:  # Нужно минимум 50 свечей для расчета индикаторов
                return False
                
            # Рассчитываем индикаторы
            df_with_indicators = self.calculate_indicators(df.copy())
            if df_with_indicators is None:
                return False
                
            # Получаем сигнал
            signal, levels = self.get_signal(df_with_indicators)
            if signal is None:
                return False
                
            # Проверяем, есть ли уже открытая позиция
            if symbol in self.positions:
                return False
                
            # Сохраняем уровни для исполнения ордера
            self.positions[symbol] = {
                "signal": signal,
                "levels": levels,
                "timeframe": timeframe  # Сохраняем таймфрейм, на котором получен сигнал
            }
            
            # Логируем только когда нашли сигнал
            logging.info(f"\n{'='*50}")
            logging.info(f"🎯 Trading signal detected for {symbol} on {timeframe}m timeframe")
            logging.info(f"Signal type: {'🟢 LONG' if signal == 'long' else '🔴 SHORT'}")
            logging.info(f"Entry price: {levels['entry_price']:.2f}")
            logging.info(f"Stop loss: {levels['stop_loss']:.2f}")
            logging.info(f"Take profit: {levels['take_profit']:.2f}")
            logging.info(f"{'='*50}")
            
            return True
            
        except Exception as e:
            logging.error(f"Error in should_trade: {e}")
            return False

    def get_balance(self):
        """Получает и выводит текущий баланс аккаунта"""
        try:
            current_timestamp = self.get_current_timestamp()
            balance_response = self.http_client.get_wallet_balance(
                accountType="UNIFIED",
                coin="USDT",
                timestamp=current_timestamp
            )
            
            if balance_response['retCode'] == 0:
                balance = float(balance_response['result']['list'][0]['coin'][0]['walletBalance'])
                available = float(balance_response['result']['list'][0]['coin'][0]['availableToWithdraw'])
                
                logging.info(f"\n{'='*50}")
                logging.info(f"💰 Баланс аккаунта:")
                logging.info(f"Общий баланс USDT: {balance:.2f}")
                logging.info(f"Доступно USDT: {available:.2f}")
                logging.info(f"{'='*50}\n")
                
                return balance
            else:
                logging.error(f"Ошибка при получении баланса: {balance_response}")
                return None
                
        except Exception as e:
            logging.error(f"Ошибка при получении баланса: {e}")
            return None

    def _get_time_offset(self):
        """Получает разницу во времени между локальным временем и временем сервера"""
        try:
            # Делаем несколько попыток для более точного определения
            offsets = []
            for _ in range(5):
                start_time = int(time.time() * 1000)
                server_time = self.http_client.get_server_time()
                end_time = int(time.time() * 1000)
                
                if 'time' in server_time:
                    server_timestamp = int(server_time['time'])
                    # Берем среднее между началом и концом запроса
                    local_timestamp = (start_time + end_time) // 2
                    offsets.append(server_timestamp - local_timestamp)
                time.sleep(0.1)  # Небольшая пауза между запросами
                
            # Берем медианное значение для исключения выбросов
            return int(np.median(offsets)) if offsets else 0
        except Exception as e:
            logging.error(f"Error getting server time: {e}")
            return 0

    def get_current_timestamp(self):
        """Возвращает текущее время с учетом смещения от серверного времени"""
        return int(time.time() * 1000) + self.time_offset

    def calculate_position_size(self, symbol, entry_price):
        """Рассчитывает размер позиции на основе баланса"""
        try:
            # Добавляем смещение времени к текущему времени
            current_timestamp = self.get_current_timestamp()
            
            # Получаем баланс USDT с синхронизированным временем
            balance_response = self.http_client.get_wallet_balance(
                accountType="UNIFIED",
                coin="USDT",
                recv_window=60000,  # Увеличиваем окно приема до 60 секунд
                timestamp=current_timestamp
            )
            
            if balance_response['retCode'] != 0:
                logging.error(f"Failed to get balance: {balance_response}")
                return None
                
            available_balance = float(balance_response['result']['list'][0]['coin'][0]['walletBalance'])
            
            # Используем 2% от баланса для каждой сделки
            position_value = available_balance * 0.02
            
            # Рассчитываем количество монет
            quantity = position_value / entry_price
            
            # Округляем до 6 знаков после запятой
            quantity = round(quantity, 6)
            
            return quantity
            
        except Exception as e:
            logging.error(f"Error calculating position size: {e}")
            return None

    def execute_trade(self, symbol):
        """Исполняет торговый сигнал"""
        try:
            if symbol not in self.positions:
                logging.error(f"No position data for {symbol}")
                return
                
            position_data = self.positions[symbol]
            signal = position_data["signal"]
            levels = position_data["levels"]
            
            # Рассчитываем размер позиции
            quantity = self.calculate_position_size(symbol, levels['entry_price'])
            if not quantity:
                logging.error("Failed to calculate position size")
                return
                
            # Получаем текущее время с учетом смещения
            current_timestamp = self.get_current_timestamp()
                
            # Создаем основной ордер
            try:
                logging.info(f"\n{'='*50}")
                logging.info(f"🚀 Executing trade for {symbol}")
                
                main_order = self.http_client.place_order(
                    category="spot",
                    symbol=symbol,
                    side="Buy" if signal == "long" else "Sell",
                    orderType="Market",
                    qty=str(quantity),
                    isLeverage=0,
                    orderFilter="Order",
                    timestamp=current_timestamp
                )
                
                if main_order['retCode'] != 0:
                    logging.error(f"❌ Failed to place main order: {main_order}")
                    return
                    
                logging.info(f"✅ Main order placed successfully")
                
                # Обновляем timestamp для следующего запроса
                current_timestamp = self.get_current_timestamp()
                
                # Создаем ордер стоп-лосс
                sl_order = self.http_client.place_order(
                    category="spot",
                    symbol=symbol,
                    side="Sell" if signal == "long" else "Buy",
                    orderType="StopLimit",
                    qty=str(quantity),
                    price=str(levels['stop_loss']),
                    stopPrice=str(levels['stop_loss']),
                    isLeverage=0,
                    orderFilter="StopOrder",
                    triggerDirection=2 if signal == "long" else 1,
                    timestamp=current_timestamp
                )
                
                if sl_order['retCode'] == 0:
                    logging.info(f"✅ Stop Loss order placed")
                else:
                    logging.error(f"❌ Failed to place stop loss order: {sl_order}")
                
                # Обновляем timestamp для следующего запроса
                current_timestamp = self.get_current_timestamp()
                
                # Создаем ордер тейк-профит
                tp_order = self.http_client.place_order(
                    category="spot",
                    symbol=symbol,
                    side="Sell" if signal == "long" else "Buy",
                    orderType="Limit",
                    qty=str(quantity),
                    price=str(levels['take_profit']),
                    isLeverage=0,
                    orderFilter="Order",
                    timeInForce="GoodTillCancel",
                    timestamp=current_timestamp
                )
                
                if tp_order['retCode'] == 0:
                    logging.info(f"✅ Take Profit order placed")
                else:
                    logging.error(f"❌ Failed to place take profit order: {tp_order}")
                
                logging.info(f"\n📊 Trade Summary:")
                logging.info(f"Symbol: {symbol}")
                logging.info(f"Direction: {'🟢 LONG' if signal == 'long' else '🔴 SHORT'}")
                logging.info(f"Quantity: {quantity}")
                logging.info(f"Entry: {levels['entry_price']:.2f}")
                logging.info(f"Stop Loss: {levels['stop_loss']:.2f}")
                logging.info(f"Take Profit: {levels['take_profit']:.2f}")
                logging.info(f"{'='*50}\n")
                
            except Exception as e:
                logging.error(f"Error placing orders: {e}")
                
            finally:
                # Очищаем данные позиции
                del self.positions[symbol]
            
        except Exception as e:
            logging.error(f"Error executing trade: {e}")

    def run(self):
        """Запуск бота"""
        try:
            logging.info("Starting bot...")
            
            last_balance_check = 0
            balance_check_interval = 300  # Проверяем баланс каждые 5 минут
            
            # Запускаем бесконечный цикл для поддержания работы WebSocket
            while True:
                current_time = time.time()
                
                # Проверяем баланс каждые 5 минут
                if current_time - last_balance_check >= balance_check_interval:
                    self.get_balance()
                    last_balance_check = current_time
                
                time.sleep(1)
                
        except KeyboardInterrupt:
            logging.info("Shutting down bot...")
            self.ws_public.exit()
            self.ws_private.exit()
        except Exception as e:
            logging.error(f"Error in main loop: {e}")
            self.ws_public.exit()
            self.ws_private.exit()

if __name__ == "__main__":
    bot = GGShotBot()
    bot.run()
