import sqlite3
import pandas as pd
import os
import sys

# Add parent directory to path to import config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DB_PATH, TABLE_NAME

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

class StrategyManager:
    def __init__(self):
        self.db_path = DB_PATH
        self.table_name = TABLE_NAME

    def get_signals(self, strategy_id="golden_rsi"):
        """Scans the database based on the selected strategy."""
        signals = []
        try:
            conn = sqlite3.connect(self.db_path)
            symbols = pd.read_sql(f"SELECT DISTINCT symbol FROM {self.table_name}", conn)['symbol'].tolist()
            
            for symbol in symbols:
                df = pd.read_sql(
                    f"SELECT * FROM {self.table_name} WHERE symbol = ? ORDER BY trade_date ASC", 
                    conn, 
                    params=(symbol,)
                )
                
                if strategy_id == "golden_rsi":
                    sig = self._logic_golden_rsi(df, symbol)
                elif strategy_id == "sma_cross":
                    sig = self._logic_sma_cross(df, symbol)
                else:
                    sig = None
                
                if sig:
                    signals.append(sig)
            
            conn.close()
        except Exception as e:
            print(f"Strategy Scan Error: {e}")
            
        return signals

    def _logic_golden_rsi(self, df, symbol):
        """Golden RSI Logic: Buy pullbacks in a bull trend."""
        if len(df) < 200: return None
        df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()
        df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
        df['rsi14'] = calculate_rsi(df['close'], 14)
        
        latest = df.iloc[-1]
        if latest['close'] > latest['ema200'] and latest['close'] < latest['ema20'] and latest['rsi14'] < 40:
            return {
                "symbol": symbol,
                "price": float(latest['close']),
                "metric": f"RSI: {round(latest['rsi14'], 1)}",
                "trend": "Bullish Pullback",
                "strategy": "Golden RSI"
            }
        return None

    def _logic_sma_cross(self, df, symbol):
        """SMA Cross Logic: Momentum breakout (20 cross 50)."""
        if len(df) < 50: return None
        df['sma20'] = df['close'].rolling(window=20).mean()
        df['sma50'] = df['close'].rolling(window=50).mean()
        
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        
        # Bullish Cross: 20 was below 50, now 20 is above 50
        if prev['sma20'] <= prev['sma50'] and latest['sma20'] > latest['sma50']:
            return {
                "symbol": symbol,
                "price": float(latest['close']),
                "metric": "SMA 20/50 Cross",
                "trend": "Momentum Breakout",
                "strategy": "SMA Crossover"
            }
        return None

if __name__ == "__main__":
    manager = StrategyManager()
    print(f"Scanning Golden RSI...")
    print(manager.get_signals("golden_rsi"))
    print(f"Scanning SMA Cross...")
    print(manager.get_signals("sma_cross"))
