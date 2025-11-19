import asyncio
import aiohttp
import websockets
import json
import pandas as pd
import numpy as np
import time
import hmac
import hashlib
import urllib.parse
from datetime import datetime, timedelta
import logging
from typing import Dict, List, Optional, Tuple
import talib
import traceback

# Import your pattern detection class
from pattern_detector import UltraPatternDetector, UltraPatternResult

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('crypto_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class CryptoPatternBot:
    def __init__(self):
        # API Configuration
        self.binance_api_key = ''
        self.binance_api_secret = ''
        self.telegram_token = ''
        self.telegram_channel = ''
        
        # Binance endpoints
        self.binance_base_url = 'https://api.binance.com'
        self.binance_ws_url = 'wss://stream.binance.com:9443/ws/'
        
        # Timeframes
        self.timeframes = {
            'short': ['1m', '5m'],
            'mid': ['30m', '1h'], 
            'long': ['4h', '1d']
        }
        
        # Data storage
        self.market_data = {}
        self.top_symbols = []
        self.pattern_detector = UltraPatternDetector()
        
        # Analysis results
        self.analysis_results = {}
        self.last_alerts = {}
        
        # Session for HTTP requests
        self.session = None
        
    async def start(self):
        """Start the bot"""
        logger.info("Starting Crypto Pattern Bot...")
        
        # Create aiohttp session
        self.session = aiohttp.ClientSession()
        
        try:
            # Get top 100 symbols by volume
            await self.get_top_symbols()
            
            # Initialize data storage
            await self.initialize_data_storage()
            
            # Start data collection and analysis
            await asyncio.gather(
                self.collect_market_data(),
                self.analyze_patterns(),
                self.monitor_alerts()
            )
            
        except Exception as e:
            logger.error(f"Error starting bot: {e}")
            logger.error(traceback.format_exc())
        finally:
            if self.session:
                await self.session.close()
    
    async def get_top_symbols(self):
        """Get top 100 symbols by 24h volume"""
        try:
            url = f"{self.binance_base_url}/api/v3/ticker/24hr"
            
            async with self.session.get(url) as response:
                data = await response.json()
            
            # Filter USDT pairs and sort by volume
            usdt_pairs = [
                ticker for ticker in data 
                if ticker['symbol'].endswith('USDT') and 
                float(ticker['quoteVolume']) > 1000000  # Min $1M volume
            ]
            
            # Sort by quote volume (descending) and take top 100
            usdt_pairs.sort(key=lambda x: float(x['quoteVolume']), reverse=True)
            self.top_symbols = [ticker['symbol'] for ticker in usdt_pairs[:100]]
            
            logger.info(f"Selected {len(self.top_symbols)} top volume symbols")
            logger.info(f"Top 10: {self.top_symbols[:10]}")
            
        except Exception as e:
            logger.error(f"Error getting top symbols: {e}")
            # Fallback to popular pairs
            self.top_symbols = [
                'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'ADAUSDT', 'XRPUSDT',
                'SOLUSDT', 'DOTUSDT', 'DOGEUSDT', 'AVAXUSDT', 'LINKUSDT'
            ]
    
    async def initialize_data_storage(self):
        """Initialize data storage for all symbols and timeframes"""
        for symbol in self.top_symbols:
            self.market_data[symbol] = {}
            for timeframe_group in self.timeframes.values():
                for tf in timeframe_group:
                    self.market_data[symbol][tf] = {
                        'timestamps': [],
                        'opens': [],
                        'highs': [],
                        'lows': [],
                        'closes': [],
                        'volumes': [],
                        'last_update': 0
                    }
        
        # Get initial historical data
        await self.get_historical_data()
    
    async def get_historical_data(self):
        """Get historical data for all symbols and timeframes"""
        logger.info("Fetching historical data...")
        
        for symbol in self.top_symbols:
            for timeframe_group in self.timeframes.values():
                for tf in timeframe_group:
                    try:
                        await self.fetch_klines(symbol, tf, limit=500)
                        await asyncio.sleep(0.1)  # Rate limiting
                    except Exception as e:
                        logger.error(f"Error fetching historical data for {symbol} {tf}: {e}")
    
    async def fetch_klines(self, symbol: str, timeframe: str, limit: int = 500):
        """Fetch kline data from Binance"""
        try:
            url = f"{self.binance_base_url}/api/v3/klines"
            params = {
                'symbol': symbol,
                'interval': timeframe,
                'limit': limit
            }
            
            async with self.session.get(url, params=params) as response:
                data = await response.json()
            
            if symbol not in self.market_data:
                self.market_data[symbol] = {}
            if timeframe not in self.market_data[symbol]:
                self.market_data[symbol][timeframe] = {
                    'timestamps': [],
                    'opens': [],
                    'highs': [],
                    'lows': [],
                    'closes': [],
                    'volumes': [],
                    'last_update': 0
                }
            
            # Process kline data
            timestamps = []
            opens = []
            highs = []
            lows = []
            closes = []
            volumes = []
            
            for kline in data:
                timestamps.append(int(kline[0]))
                opens.append(float(kline[1]))
                highs.append(float(kline[2]))
                lows.append(float(kline[3]))
                closes.append(float(kline[4]))
                volumes.append(float(kline[5]))
            
            # Update data
            self.market_data[symbol][timeframe] = {
                'timestamps': timestamps,
                'opens': opens,
                'highs': highs,
                'lows': lows,
                'closes': closes,
                'volumes': volumes,
                'last_update': time.time()
            }
            
        except Exception as e:
            logger.error(f"Error fetching klines for {symbol} {timeframe}: {e}")
    
    async def collect_market_data(self):
        """Collect real-time market data via WebSocket"""
        while True:
            try:
                # Create WebSocket streams for all symbols and timeframes
                streams = []
                for symbol in self.top_symbols:
                    symbol_lower = symbol.lower()
                    for timeframe_group in self.timeframes.values():
                        for tf in timeframe_group:
                            streams.append(f"{symbol_lower}@kline_{tf}")
                
                # Split streams into chunks (Binance has limit)
                stream_chunks = [streams[i:i+200] for i in range(0, len(streams), 200)]
                
                # Start WebSocket connections for each chunk
                tasks = []
                for chunk in stream_chunks:
                    task = asyncio.create_task(self.websocket_stream(chunk))
                    tasks.append(task)
                
                # Wait for any task to complete (shouldn't happen in normal operation)
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                
                # Cancel pending tasks
                for task in pending:
                    task.cancel()
                
                logger.warning("WebSocket connection ended, reconnecting...")
                await asyncio.sleep(5)
                
            except Exception as e:
                logger.error(f"Error in data collection: {e}")
                await asyncio.sleep(10)
    
    async def websocket_stream(self, streams: List[str]):
        """Handle WebSocket stream for given streams"""
        try:
            stream_url = f"{self.binance_ws_url}stream?streams=" + "/".join(streams)
            
            async with websockets.connect(stream_url) as websocket:
                logger.info(f"Connected to WebSocket with {len(streams)} streams")
                
                async for message in websocket:
                    try:
                        data = json.loads(message)
                        await self.process_kline_data(data)
                    except Exception as e:
                        logger.error(f"Error processing WebSocket message: {e}")
                        
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            raise
    
    async def process_kline_data(self, data: dict):
        """Process incoming kline data"""
        try:
            if 'data' not in data:
                return
            
            kline_data = data['data']
            if kline_data['e'] != 'kline':
                return
            
            kline = kline_data['k']
            symbol = kline['s']
            timeframe = kline['i']
            
            if symbol not in self.market_data:
                return
            
            if timeframe not in self.market_data[symbol]:
                return
            
            # Only process closed candles
            if not kline['x']:  # x = is_closed
                return
            
            # Update data
            data_dict = self.market_data[symbol][timeframe]
            
            data_dict['timestamps'].append(int(kline['t']))
            data_dict['opens'].append(float(kline['o']))
            data_dict['highs'].append(float(kline['h']))
            data_dict['lows'].append(float(kline['l']))
            data_dict['closes'].append(float(kline['c']))
            data_dict['volumes'].append(float(kline['v']))
            
            # Keep only last 1000 candles
            if len(data_dict['closes']) > 1000:
                for key in ['timestamps', 'opens', 'highs', 'lows', 'closes', 'volumes']:
                    data_dict[key] = data_dict[key][-1000:]
            
            data_dict['last_update'] = time.time()
            
        except Exception as e:
            logger.error(f"Error processing kline data: {e}")
    
    async def analyze_patterns(self):
        """Analyze patterns for all symbols"""
        while True:
            try:
                analysis_tasks = []
                
                for symbol in self.top_symbols:
                    if symbol in self.market_data:
                        task = asyncio.create_task(self.analyze_symbol_patterns(symbol))
                        analysis_tasks.append(task)
                
                # Process in batches to avoid overwhelming
                batch_size = 20
                for i in range(0, len(analysis_tasks), batch_size):
                    batch = analysis_tasks[i:i+batch_size]
                    await asyncio.gather(*batch, return_exceptions=True)
                    await asyncio.sleep(1)  # Brief pause between batches
                
                # Wait 60 seconds before next analysis cycle
                await asyncio.sleep(60)
                
            except Exception as e:
                logger.error(f"Error in pattern analysis: {e}")
                await asyncio.sleep(30)
    
    async def analyze_symbol_patterns(self, symbol: str):
        """Analyze patterns for a specific symbol"""
        try:
            if symbol not in self.market_data:
                return
            
            symbol_results = {
                'short': {},
                'mid': {},
                'long': {},
                'timestamp': time.time(),
                'current_price': 0
            }
            
            # Get current price
            current_price = 0
            for tf_group in self.timeframes.values():
                for tf in tf_group:
                    if (symbol in self.market_data and 
                        tf in self.market_data[symbol] and 
                        len(self.market_data[symbol][tf]['closes']) > 0):
                        current_price = self.market_data[symbol][tf]['closes'][-1]
                        break
                if current_price > 0:
                    break
            
            if current_price == 0:
                return
            
            symbol_results['current_price'] = current_price
            
            # Analyze each timeframe group
            for group_name, timeframes in self.timeframes.items():
                group_patterns = {}
                
                for tf in timeframes:
                    if (tf in self.market_data[symbol] and 
                        len(self.market_data[symbol][tf]['closes']) >= 100):
                        
                        data = self.market_data[symbol][tf]
                        
                        # Skip if data is too old
                        if time.time() - data['last_update'] > 300:  # 5 minutes
                            continue
                        
                        try:
                            # Run pattern detection on this timeframe
                            patterns = await self.detect_patterns_for_timeframe(
                                symbol, tf, data, current_price
                            )
                            
                            if patterns:
                                group_patterns[tf] = patterns
                                
                        except Exception as e:
                            logger.error(f"Error analyzing {symbol} {tf}: {e}")
                
                symbol_results[group_name] = group_patterns
            
            # Store results
            self.analysis_results[symbol] = symbol_results
            
            # Generate predictions
            await self.generate_predictions(symbol, symbol_results)
            
        except Exception as e:
            logger.error(f"Error analyzing symbol {symbol}: {e}")
    
    async def detect_patterns_for_timeframe(self, symbol: str, timeframe: str, 
                                          data: dict, current_price: float) -> List[UltraPatternResult]:
        """Detect patterns for a specific timeframe"""
        try:
            opens = np.array(data['opens'])
            highs = np.array(data['highs'])
            lows = np.array(data['lows'])
            closes = np.array(data['closes'])
            volumes = np.array(data['volumes'])
            
            if len(closes) < 100:
                return []
            
            # YOUR PATTERN DETECTION CODE GOES HERE
            # This is where you integrate your pattern detection methods
            # from the script you provided
            
            all_patterns = []
            
            # Detect all your pattern categories
            perfect_patterns = self.pattern_detector._detect_perfect_patterns_stable(
                opens, highs, lows, closes, volumes, current_price, timeframe
            )
            all_patterns.extend(perfect_patterns)
            
            most_perfect_patterns = self.pattern_detector._most_perfect_patterns_stable(
                opens, highs, lows, closes, volumes, current_price, timeframe
            )
            all_patterns.extend(most_perfect_patterns)
            
            classic_patterns = self.pattern_detector._detect_classic_patterns_stable(
                opens, highs, lows, closes, volumes, current_price, timeframe
            )
            all_patterns.extend(classic_patterns)
            
            harmonic_patterns = self.pattern_detector._detect_harmonic_patterns_stable(
                highs, lows, closes, current_price, timeframe
            )
            all_patterns.extend(harmonic_patterns)
            
            elliott_patterns = self.pattern_detector._detect_elliott_wave_patterns_stable(
                closes, current_price, timeframe
            )
            all_patterns.extend(elliott_patterns)
            
            wyckoff_patterns = self.pattern_detector._detect_wyckoff_patterns_stable(
                opens, highs, lows, closes, volumes, current_price, timeframe
            )
            all_patterns.extend(wyckoff_patterns)
            
            volume_patterns = self.pattern_detector._detect_volume_patterns_stable(
                closes, volumes, current_price, timeframe
            )
            all_patterns.extend(volume_patterns)
            
            fibonacci_patterns = self.pattern_detector._detect_fibonacci_patterns_stable(
                highs, lows, closes, current_price, timeframe
            )
            all_patterns.extend(fibonacci_patterns)
            
            candlestick_patterns = self.pattern_detector._detect_candlestick_patterns_stable(
                opens, highs, lows, closes, current_price, timeframe
            )
            all_patterns.extend(candlestick_patterns)
            
            oscillator_patterns = self.pattern_detector._detect_oscillator_patterns_stable(
                opens, highs, lows, closes, current_price, timeframe
            )
            all_patterns.extend(oscillator_patterns)
            
            moving_patterns = self.pattern_detector._detect_moving_patterns_stable(
                opens, highs, lows, closes, volumes, current_price, timeframe
            )
            all_patterns.extend(moving_patterns)
            
            volatility_patterns = self.pattern_detector._detect_volatility_patterns_stable(
                opens, highs, lows, closes, volumes, current_price, timeframe
            )
            all_patterns.extend(volatility_patterns)
            
            # Add combination patterns
            combination_patterns = self.pattern_detector._detect_combination_patterns_stable(
                opens, highs, lows, closes, volumes, current_price, timeframe, all_patterns
            )
            all_patterns.extend(combination_patterns)
            
            godlike_patterns = self.pattern_detector._detect_godlike_patterns_stable(
                opens, highs, lows, closes, volumes, current_price, timeframe, all_patterns
            )
            all_patterns.extend(godlike_patterns)
            
            legendary_patterns = self.pattern_detector._detect_legendary_patterns_stable(
                opens, highs, lows, closes, volumes, current_price, timeframe, all_patterns
            )
            all_patterns.extend(legendary_patterns)
            
            master_patterns = self.pattern_detector._detect_master_patterns_stable(
                opens, highs, lows, closes, volumes, current_price, timeframe, all_patterns
            )
            all_patterns.extend(master_patterns)
            
            blockchain_patterns = self.pattern_detector._detect_blockchain_patterns_stable(
                opens, highs, lows, closes, volumes, current_price, timeframe, all_patterns
            )
            all_patterns.extend(blockchain_patterns)
            
            cross_patterns = self.pattern_detector._detect_cross_patterns_stable(
                opens, highs, lows, closes, volumes, current_price, timeframe, all_patterns
            )
            all_patterns.extend(cross_patterns)
            
            real_patterns = self.pattern_detector._detect_real_patterns_stable(
                opens, highs, lows, closes, volumes, current_price, timeframe, all_patterns
            )
            all_patterns.extend(real_patterns)
            
            quantum_patterns = self.pattern_detector._detect_quantum_patterns_stable(
                opens, highs, lows, closes, volumes, current_price, timeframe, all_patterns
            )
            all_patterns.extend(quantum_patterns)
            
            microstructure_patterns = self.pattern_detector._detect_microstructur_patterns_stable(
                opens, highs, lows, closes, volumes, current_price, timeframe, all_patterns
            )
            all_patterns.extend(microstructure_patterns)
            
            seasonal_patterns = self.pattern_detector._detect_seasonal_patterns_stable(
                opens, highs, lows, closes, volumes, current_price, timeframe, all_patterns
            )
            all_patterns.extend(seasonal_patterns)
            
            # Filter patterns with minimum confidence
            filtered_patterns = [
                pattern for pattern in all_patterns 
                if hasattr(pattern, 'confidence') and pattern.confidence >= 10.0
            ]
            
            return filtered_patterns
            
        except Exception as e:
            logger.error(f"Error detecting patterns for {symbol} {timeframe}: {e}")
            return []
    
    async def generate_predictions(self, symbol: str, results: dict):
        """Generate predictions for short, mid, long term"""
        try:
            predictions = {
                'short': {'confidence': 0, 'direction': 'NEUTRAL', 'patterns': []},
                'mid': {'confidence': 0, 'direction': 'NEUTRAL', 'patterns': []},
                'long': {'confidence': 0, 'direction': 'NEUTRAL', 'patterns': []}
            }
            
            current_price = results['current_price']
            
            for term in ['short', 'mid', 'long']:
                if term not in results or not results[term]:
                    continue
                
                bullish_patterns = []
                bearish_patterns = []
                total_confidence = 0
                pattern_count = 0
                
                # Collect all patterns for this term
                for tf, patterns in results[term].items():
                    for pattern in patterns:
                        pattern_count += 1
                        total_confidence += pattern.confidence
                        
                        # Determine direction
                        if (hasattr(pattern, 'target_price') and 
                            hasattr(pattern, 'entry_price')):
                            if pattern.target_price > pattern.entry_price:
                                bullish_patterns.append(pattern)
                            else:
                                bearish_patterns.append(pattern)
                        elif 'BULLISH' in pattern.name or 'BULL' in pattern.name:
                            bullish_patterns.append(pattern)
                        elif 'BEARISH' in pattern.name or 'BEAR' in pattern.name:
                            bearish_patterns.append(pattern)
                
                if pattern_count > 0:
                    avg_confidence = total_confidence / pattern_count
                    
                    # Determine overall direction
                    bullish_score = sum(p.confidence for p in bullish_patterns)
                    bearish_score = sum(p.confidence for p in bearish_patterns)
                    
                    if bullish_score > bearish_score * 1.2:
                        direction = 'BULLISH'
                        confidence = min(95, avg_confidence * (bullish_score / (bullish_score + bearish_score + 1)))
                        relevant_patterns = bullish_patterns
                    elif bearish_score > bullish_score * 1.2:
                        direction = 'BEARISH'
                        confidence = min(95, avg_confidence * (bearish_score / (bullish_score + bearish_score + 1)))
                        relevant_patterns = bearish_patterns
                    else:
                        direction = 'NEUTRAL'
                        confidence = avg_confidence * 0.7
                        relevant_patterns = bullish_patterns + bearish_patterns
                    
                    predictions[term] = {
                        'confidence': confidence,
                        'direction': direction,
                        'patterns': relevant_patterns[:5]  # Top 5 patterns
                    }
            
            # Check if any prediction meets alert criteria
            await self.check_alert_criteria(symbol, predictions, current_price)
            
        except Exception as e:
            logger.error(f"Error generating predictions for {symbol}: {e}")
    
    async def check_alert_criteria(self, symbol: str, predictions: dict, current_price: float):
        """Check if predictions meet alert criteria"""
        try:
            for term, prediction in predictions.items():
                if (prediction['confidence'] > 19 and 
                    prediction['direction'] != 'NEUTRAL' and
                    len(prediction['patterns']) > 0):
                    
                    # Check if we've already sent this alert recently
                    alert_key = f"{symbol}_{term}_{prediction['direction']}"
                    current_time = time.time()
                    
                    if (alert_key in self.last_alerts and 
                        current_time - self.last_alerts[alert_key] < 3600):  # 1 hour cooldown
                        continue
                    
                    # Generate alert
                    await self.send_telegram_alert(symbol, term, prediction, current_price)
                    self.last_alerts[alert_key] = current_time
                    
        except Exception as e:
            logger.error(f"Error checking alert criteria for {symbol}: {e}")
    
    async def send_telegram_alert(self, symbol: str, term: str, prediction: dict, current_price: float):
        """Send alert to Telegram"""
        try:
            # Get best pattern for entry/targets
            best_pattern = max(prediction['patterns'], key=lambda p: p.confidence)
            
            # Calculate targets
            entry_price = getattr(best_pattern, 'entry_price', current_price)
            stop_loss = getattr(best_pattern, 'stop_loss', current_price * 0.95 if prediction['direction'] == 'BULLISH' else current_price * 1.05)
            target_price = getattr(best_pattern, 'target_price', current_price * 1.1 if prediction['direction'] == 'BULLISH' else current_price * 0.9)
            
            # Calculate multiple targets
            if prediction['direction'] == 'BULLISH':
                tp1 = entry_price + (target_price - entry_price) * 0.3
                tp2 = entry_price + (target_price - entry_price) * 0.6
                tp3 = target_price
            else:
                tp1 = entry_price - (entry_price - target_price) * 0.3
                tp2 = entry_price - (entry_price - target_price) * 0.6
                tp3 = target_price
            
            # Format message
            direction_emoji = "🚀" if prediction['direction'] == 'BULLISH' else "🔻"
            term_emoji = {"short": "⚡", "mid": "🎯", "long": "📈"}[term]
            
            message = f"""
{direction_emoji} **{symbol}** {term_emoji} {term.upper()} TERM SIGNAL
    
💰 **HARGA SAAT INI:** ${current_price:.6f}
🎯 **ENTRY TERBAIK:** ${entry_price:.6f}
🛡️ **STOP LOSS:** ${stop_loss:.6f}

🎯 **TP 1:** ${tp1:.6f}
🎯 **TP 2:** ${tp2:.6f}  
🎯 **TP 3:** ${tp3:.6f}

📊 **PATTERN:** {best_pattern.name}
📈 **CONFIDENCE:** {prediction['confidence']:.1f}%
⭐ **GRADE:** {getattr(best_pattern, 'pattern_grade', 'N/A')}

#{symbol} #{term.upper()}TERM #{prediction['direction']}
            """.strip()
            
            # Send to Telegram
            await self.send_telegram_message(message)
            
            logger.info(f"Alert sent for {symbol} {term} {prediction['direction']} - {prediction['confidence']:.1f}%")
            
        except Exception as e:
            logger.error(f"Error sending Telegram alert: {e}")
    
    async def send_telegram_message(self, message: str):
        """Send message to Telegram channel"""
        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            data = {
                'chat_id': self.telegram_channel,
                'text': message,
                'parse_mode': 'Markdown',
                'disable_web_page_preview': True
            }
            
            async with self.session.post(url, data=data) as response:
                if response.status == 200:
                    logger.info("Telegram message sent successfully")
                else:
                    logger.error(f"Failed to send Telegram message: {response.status}")
                    
        except Exception as e:
            logger.error(f"Error sending Telegram message: {e}")
    
    async def monitor_alerts(self):
        """Monitor and send periodic updates"""
        while True:
            try:
                await asyncio.sleep(300)  # Check every 5 minutes
                
                # Send summary if needed
                active_signals = 0
                for symbol, results in self.analysis_results.items():
                    if time.time() - results.get('timestamp', 0) < 600:  # Recent results
                        active_signals += 1
                
                if active_signals > 0:
                    logger.info(f"Monitoring {active_signals} active symbols")
                
            except Exception as e:
                logger.error(f"Error in monitoring: {e}")
                await asyncio.sleep(60)

# Run the bot
async def main():
    bot = CryptoPatternBot()
    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
