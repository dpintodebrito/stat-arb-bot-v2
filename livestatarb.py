#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Statistical Arbitrage Paper Trading Bot - Bybit Futures Production
Réplication exacte de la stratégie avec simulation locale des trades
+ Notifications Discord via webhook
"""

import time
import asyncio
import logging
import json
import os
import threading
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.dates import DateFormatter
import matplotlib.dates as mdates

# Installation requise : pip install pybit pandas numpy matplotlib websocket-client requests
try:
    from pybit.unified_trading import HTTP
    import websocket
except ImportError:
    print("❌ Installer dependencies: pip install pybit pandas numpy matplotlib websocket-client requests")
    exit(1)

# ============================================================================
# CONFIGURATION & DATACLASSES
# ============================================================================

@dataclass
class TradingConfig:
    """Configuration du bot de trading"""
    # API Bybit Production
    api_key: str = ""  # Lecture seule pour récupérer les infos de marché
    api_secret: str = ""  # Lecture seule
    testnet: bool = False  # Production pour données temps réel
    
    # Discord Webhook
    discord_webhook_url: str = "https://discord.com/api/webhooks/1400361724028129300/6ox13i6F_I1--ZgwkUkTCPTKB2C1yT9zQCJJEa4D6J6biO5AdJn9gjoEnU_AojMeJcBk"  # URL du webhook Discord
    
    # Paires de trading
    symbol_1: str = "DOTUSDT"
    symbol_2: str = "KSMUSDT"
    
    # Paramètres stratégie (identiques à l'original)
    window: int = 100
    entry_threshold: float = 2.0
    exit_threshold: float = 0.5
    hedge_window: int = 60
    extreme_zscore_threshold: float = 3.5
    
    # Paramètres paper trading
    initial_balance: float = 10000.0  # USD virtuels
    position_size_usd: float = 100.0
    max_positions: int = 1
    trading_fee: float = 0.001  # 0.1% par trade
    
    # Timing
    data_interval: str = "15"  # minutes pour données historiques
    loop_interval: int = 30  # secondes entre analyses
    discord_status_interval: int = 3600  # 1 heure en secondes
    
    # Logs
    log_level: str = "INFO"
    log_to_file: bool = True
    
    # Performance tracking
    performance_file: str = "paper_trading_results.json"


@dataclass
class MarketData:
    """Données de marché"""
    timestamp: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class PaperPosition:
    """Position virtuelle"""
    symbol: str
    side: str  # "Buy" ou "Sell"
    size: float
    entry_price: float
    entry_time: datetime
    entry_value: float
    fees_paid: float = 0.0
    
    def get_unrealized_pnl(self, current_price: float) -> float:
        """Calcule le PnL non réalisé"""
        if self.side == "Buy":
            return (current_price - self.entry_price) * self.size
        else:
            return (self.entry_price - current_price) * self.size


@dataclass
class PaperPortfolio:
    """Portefeuille virtuel"""
    cash: float
    positions: Dict[str, PaperPosition] = field(default_factory=dict)
    total_fees: float = 0.0
    realized_pnl: float = 0.0
    trades_count: int = 0
    winning_trades: int = 0
    max_portfolio_value: float = 0.0
    min_portfolio_value: float = float('inf')
    portfolio_history: List[Tuple[datetime, float]] = field(default_factory=list)
    
    def get_portfolio_value(self, current_prices: Dict[str, float]) -> float:
        """Calcule la valeur totale du portefeuille"""
        total_value = self.cash
        
        for symbol, position in self.positions.items():
            if symbol in current_prices:
                unrealized_pnl = position.get_unrealized_pnl(current_prices[symbol])
                total_value += position.entry_value + unrealized_pnl
        
        return total_value
    
    def update_history(self, current_prices: Dict[str, float]):
        """Met à jour l'historique de performance"""
        portfolio_value = self.get_portfolio_value(current_prices)
        self.portfolio_history.append((datetime.now(), portfolio_value))
        
        # Mettre à jour min/max
        self.max_portfolio_value = max(self.max_portfolio_value, portfolio_value)
        self.min_portfolio_value = min(self.min_portfolio_value, portfolio_value)


# ============================================================================
# DISCORD NOTIFIER
# ============================================================================

class DiscordNotifier:
    """Gestionnaire des notifications Discord"""
    
    def __init__(self, webhook_url: str, logger: logging.Logger):
        self.webhook_url = webhook_url
        self.logger = logger
        self.enabled = bool(webhook_url)
        
        if not self.enabled:
            self.logger.warning("Webhook Discord non configuré - notifications désactivées")
    
    def send_message(self, content: str, embed: Dict = None) -> bool:
        """Envoie un message Discord"""
        if not self.enabled:
            return False
        
        try:
            payload = {"content": content}
            if embed:
                payload["embeds"] = [embed]
            
            response = requests.post(
                self.webhook_url,
                json=payload,
                timeout=10
            )
            
            if response.status_code == 204:
                self.logger.debug("Message Discord envoyé avec succès")
                return True
            else:
                self.logger.error(f"Erreur envoi Discord: {response.status_code}")
                return False
                
        except Exception as e:
            self.logger.error(f"Erreur envoi Discord: {e}")
            return False
    
    def send_status_update(self, initial_balance: float, current_value: float, 
                          realized_pnl: float, trades_count: int, win_rate: float):
        """Envoie une mise à jour du statut du portefeuille"""
        if not self.enabled:
            return
        
        total_return = current_value - initial_balance
        return_pct = (total_return / initial_balance) * 100
        
        embed = {
            "title": "📊 Statut du Portefeuille",
            "color": 0x00ff00 if total_return >= 0 else 0xff0000,
            "timestamp": datetime.now().isoformat(),
            "fields": [
                {
                    "name": "💰 Capital Initial",
                    "value": f"${initial_balance:,.2f}",
                    "inline": True
                },
                {
                    "name": "💰 Capital Actuel",
                    "value": f"${current_value:,.2f}",
                    "inline": True
                },
                {
                    "name": "📈 PnL Total",
                    "value": f"${total_return:+,.2f} ({return_pct:+.2f}%)",
                    "inline": True
                },
                {
                    "name": "📊 PnL Réalisé",
                    "value": f"${realized_pnl:+,.2f}",
                    "inline": True
                },
                {
                    "name": "🎯 Trades",
                    "value": f"{trades_count}",
                    "inline": True
                },
                {
                    "name": "✅ Win Rate",
                    "value": f"{win_rate:.1f}%",
                    "inline": True
                }
            ],
            "footer": {
                "text": "Statistical Arbitrage Bot"
            }
        }
        
        self.send_message("", embed)
    
    def send_position_opened(self, symbol_1: str, symbol_2: str, signal: int, 
                           qty_1: float, qty_2: float, price_1: float, price_2: float):
        """Notifie l'ouverture d'une position"""
        if not self.enabled:
            return
        
        signal_text = "LONG Spread" if signal == 1 else "SHORT Spread"
        color = 0x00ff00 if signal == 1 else 0xff0000
        
        embed = {
            "title": "🚀 Position Ouverte",
            "color": color,
            "timestamp": datetime.now().isoformat(),
            "fields": [
                {
                    "name": "📊 Type de Position",
                    "value": signal_text,
                    "inline": False
                },
                {
                    "name": f"{'📈' if signal == 1 else '📉'} {symbol_1}",
                    "value": f"{'BUY' if signal == 1 else 'SELL'} {qty_1:.3f} @ ${price_1:.4f}",
                    "inline": True
                },
                {
                    "name": f"{'📉' if signal == 1 else '📈'} {symbol_2}",
                    "value": f"{'SELL' if signal == 1 else 'BUY'} {qty_2:.3f} @ ${price_2:.4f}",
                    "inline": True
                },
                {
                    "name": "⏰ Timestamp",
                    "value": datetime.now().strftime("%H:%M:%S"),
                    "inline": False
                }
            ],
            "footer": {
                "text": "Statistical Arbitrage Bot"
            }
        }
        
        self.send_message("", embed)
    
    def send_position_closed(self, positions: Dict[str, PaperPosition], 
                           current_prices: Dict[str, float], total_pnl: float):
        """Notifie la fermeture d'une position"""
        if not self.enabled:
            return
        
        color = 0x00ff00 if total_pnl >= 0 else 0xff0000
        pnl_emoji = "💰" if total_pnl >= 0 else "💸"
        
        fields = []
        for symbol, position in positions.items():
            current_price = current_prices.get(symbol, position.entry_price)
            pnl = position.get_unrealized_pnl(current_price)
            
            fields.extend([
                {
                    "name": f"📊 {symbol}",
                    "value": f"{position.side} {position.size:.3f}",
                    "inline": True
                },
                {
                    "name": "💵 Entrée → Sortie",
                    "value": f"${position.entry_price:.4f} → ${current_price:.4f}",
                    "inline": True
                },
                {
                    "name": f"{pnl_emoji} PnL",
                    "value": f"${pnl:+.2f}",
                    "inline": True
                }
            ])
        
        fields.append({
            "name": "💎 PnL Total",
            "value": f"${total_pnl:+.2f}",
            "inline": False
        })
        
        fields.append({
            "name": "⏰ Timestamp",
            "value": datetime.now().strftime("%H:%M:%S"),
            "inline": False
        })
        
        embed = {
            "title": "🔒 Position Fermée",
            "color": color,
            "timestamp": datetime.now().isoformat(),
            "fields": fields,
            "footer": {
                "text": "Statistical Arbitrage Bot"
            }
        }
        
        self.send_message("", embed)
    
    def send_bot_started(self, symbol_1: str, symbol_2: str, initial_balance: float):
        """Notifie le démarrage du bot"""
        if not self.enabled:
            return
        
        embed = {
            "title": "🤖 Bot Démarré",
            "color": 0x0099ff,
            "timestamp": datetime.now().isoformat(),
            "fields": [
                {
                    "name": "📊 Paires Tradées",
                    "value": f"{symbol_1} / {symbol_2}",
                    "inline": True
                },
                {
                    "name": "💰 Capital Initial",
                    "value": f"${initial_balance:,.2f}",
                    "inline": True
                },
                {
                    "name": "📈 Mode",
                    "value": "Paper Trading",
                    "inline": True
                }
            ],
            "footer": {
                "text": "Statistical Arbitrage Bot"
            }
        }
        
        self.send_message("🚀 **Statistical Arbitrage Bot démarré!**", embed)
    
    def send_bot_stopped(self, final_value: float, total_return: float, trades_count: int):
        """Notifie l'arrêt du bot"""
        if not self.enabled:
            return
        
        return_pct = (total_return / (final_value - total_return)) * 100
        color = 0x00ff00 if total_return >= 0 else 0xff0000
        
        embed = {
            "title": "🛑 Bot Arrêté",
            "color": color,
            "timestamp": datetime.now().isoformat(),
            "fields": [
                {
                    "name": "💰 Capital Final",
                    "value": f"${final_value:,.2f}",
                    "inline": True
                },
                {
                    "name": "📈 PnL Total",
                    "value": f"${total_return:+,.2f} ({return_pct:+.2f}%)",
                    "inline": True
                },
                {
                    "name": "🎯 Total Trades",
                    "value": f"{trades_count}",
                    "inline": True
                }
            ],
            "footer": {
                "text": "Statistical Arbitrage Bot"
            }
        }
        
        self.send_message("🏁 **Trading session terminée**", embed)


# ============================================================================
# SETUP LOGGING
# ============================================================================

def setup_logging(config: TradingConfig) -> logging.Logger:
    """Configure le système de logging"""
    logger = logging.getLogger("PaperTradingBot")
    logger.setLevel(getattr(logging, config.log_level))
    
    # Supprimer handlers existants
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s', 
        datefmt='%H:%M:%S'
    )
    
    # Console handler (WARNING+ seulement pour minimiser les prints)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # File handler
    if config.log_to_file:
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        file_handler = logging.FileHandler(
            log_dir / f"paper_trading_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        )
        file_handler.setLevel(getattr(logging, config.log_level))
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


# ============================================================================
# WEBSOCKET CLIENT BYBIT
# ============================================================================

class BybitWebSocketClient:
    """Client WebSocket pour données temps réel Bybit"""
    
    def __init__(self, config: TradingConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        
        # WebSocket URL pour production
        self.ws_url = "wss://stream.bybit.com/v5/public/linear"
        
        # Données temps réel
        self.current_prices = {}
        self.price_updates = {}
        self.last_update_time = {}
        
        # WebSocket
        self.ws = None
        self.ws_thread = None
        self.is_connected = False
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 10
        
        # Subscriptions
        self.subscriptions = [
            f"tickers.{config.symbol_1}",
            f"tickers.{config.symbol_2}"
        ]
    
    def connect(self):
        """Établit la connexion WebSocket"""
        try:
            self.ws = websocket.WebSocketApp(
                self.ws_url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close
            )
            
            # Lancer dans un thread séparé
            self.ws_thread = threading.Thread(target=self.ws.run_forever)
            self.ws_thread.daemon = True
            self.ws_thread.start()
            
            # Attendre la connexion
            for _ in range(50):  # 5 secondes max
                if self.is_connected:
                    break
                time.sleep(0.1)
            
            if not self.is_connected:
                raise Exception("Impossible de se connecter au WebSocket")
                
            self.logger.info("WebSocket connecté avec succès")
            return True
            
        except Exception as e:
            self.logger.error(f"Erreur connexion WebSocket: {e}")
            return False
    
    def _on_open(self, ws):
        """Callback d'ouverture de connexion"""
        self.is_connected = True
        self.reconnect_attempts = 0
        
        # S'abonner aux tickers
        subscribe_msg = {
            "op": "subscribe",
            "args": self.subscriptions
        }
        
        ws.send(json.dumps(subscribe_msg))
        self.logger.info(f"Abonnement aux symboles: {self.subscriptions}")
    
    def _on_message(self, ws, message):
        """Traite les messages reçus"""
        try:
            data = json.loads(message)
            
            # Ignorer les messages de confirmation
            if data.get('success') or data.get('op') == 'subscribe':
                return
            
            # Traiter les données de ticker
            if data.get('topic', '').startswith('tickers.'):
                self._process_ticker_data(data)
                
        except Exception as e:
            self.logger.error(f"Erreur traitement message WebSocket: {e}")
    
    def _process_ticker_data(self, data):
        """Traite les données de ticker"""
        try:
            ticker_data = data.get('data', {})
            symbol = ticker_data.get('symbol')
            
            if symbol and symbol in [self.config.symbol_1, self.config.symbol_2]:
                price = float(ticker_data.get('lastPrice', 0))
                
                if price > 0:
                    self.current_prices[symbol] = price
                    self.last_update_time[symbol] = datetime.now()
                    
                    self.logger.debug(f"Prix mis à jour {symbol}: {price}")
                    
        except Exception as e:
            self.logger.error(f"Erreur traitement ticker: {e}")
    
    def _on_error(self, ws, error):
        """Callback d'erreur"""
        self.logger.error(f"Erreur WebSocket: {error}")
    
    def _on_close(self, ws, close_status_code, close_msg):
        """Callback de fermeture"""
        self.is_connected = False
        self.logger.warning("WebSocket fermé")
        
        # Tentative de reconnexion
        if self.reconnect_attempts < self.max_reconnect_attempts:
            self.reconnect_attempts += 1
            self.logger.info(f"Tentative de reconnexion {self.reconnect_attempts}/{self.max_reconnect_attempts}")
            time.sleep(5)
            self.connect()
    
    def get_current_prices(self) -> Dict[str, float]:
        """Retourne les prix actuels"""
        # Vérifier que les prix sont récents (< 60 secondes)
        now = datetime.now()
        valid_prices = {}
        
        for symbol, price in self.current_prices.items():
            last_update = self.last_update_time.get(symbol)
            if last_update and (now - last_update).total_seconds() < 60:
                valid_prices[symbol] = price
        
        return valid_prices
    
    def close(self):
        """Ferme la connexion WebSocket"""
        if self.ws:
            self.ws.close()
        self.is_connected = False


# ============================================================================
# CLIENT BYBIT POUR DONNÉES HISTORIQUES
# ============================================================================

class BybitHistoricalClient:
    """Client pour récupérer les données historiques"""
    
    def __init__(self, config: TradingConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        
        # Client HTTP (lecture seule)
        self.client = HTTP(
            testnet=config.testnet,
            api_key=config.api_key if config.api_key else None,
            api_secret=config.api_secret if config.api_secret else None,
        )
        
        # Cache pour éviter trop d'appels API
        self._last_klines_fetch = {}
        self._cache_duration = 60  # 1 minute
    
    def get_klines(self, symbol: str, limit: int = 200) -> List[MarketData]:
        """Récupère les données OHLCV historiques"""
        cache_key = f"{symbol}_{limit}"
        now = time.time()
        
        # Vérifier cache
        if (cache_key in self._last_klines_fetch and 
            now - self._last_klines_fetch[cache_key] < self._cache_duration):
            return getattr(self, f'_cached_data_{cache_key}', [])
        
        try:
            response = self.client.get_kline(
                category="linear",
                symbol=symbol,
                interval=self.config.data_interval,
                limit=limit
            )
            
            if response['retCode'] != 0:
                self.logger.error(f"Erreur klines {symbol}: {response['retMsg']}")
                return []
            
            data = []
            for item in response['result']['list']:
                data.append(MarketData(
                    timestamp=datetime.fromtimestamp(int(item[0]) / 1000),
                    symbol=symbol,
                    open=float(item[1]),
                    high=float(item[2]),
                    low=float(item[3]),
                    close=float(item[4]),
                    volume=float(item[5])
                ))
            
            # Trier par timestamp croissant
            data.sort(key=lambda x: x.timestamp)
            
            # Mettre en cache
            self._last_klines_fetch[cache_key] = now
            setattr(self, f'_cached_data_{cache_key}', data)
            
            self.logger.debug(f"Données historiques {symbol}: {len(data)} bougies")
            return data
            
        except Exception as e:
            self.logger.error(f"Erreur récupération klines {symbol}: {e}")
            return []


# ============================================================================
# MOTEUR STRATÉGIE (RÉPLICATION EXACTE)
# ============================================================================

class StatArbStrategy:
    """Stratégie d'arbitrage statistique - réplication exacte"""
    
    def __init__(self, config: TradingConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        
        # Historique des prix (identique à l'original)
        self.price_history_1 = pd.Series(dtype=float)
        self.price_history_2 = pd.Series(dtype=float)
        
        # État de la stratégie
        self.hedge_ratio = 1.0
        self.spread_mean = 0.0
        self.spread_std = 1.0
        self.current_position = 0  # -1, 0, +1
        self.position_entry_time = None
        
        # Métriques
        self.total_signals = 0
        self.successful_trades = 0
    
    def update_data(self, data_1: List[MarketData], data_2: List[MarketData]):
        """Met à jour les données et calcule les indicateurs - IDENTIQUE"""
        if not data_1 or not data_2:
            return
        
        # Convertir en séries pandas
        prices_1 = pd.Series([d.close for d in data_1], 
                            index=[d.timestamp for d in data_1])
        prices_2 = pd.Series([d.close for d in data_2], 
                            index=[d.timestamp for d in data_2])
        
        # Synchroniser les timestamps
        common_index = prices_1.index.intersection(prices_2.index)
        if len(common_index) < self.config.window:
            return
        
        self.price_history_1 = prices_1.reindex(common_index).tail(self.config.window * 2)
        self.price_history_2 = prices_2.reindex(common_index).tail(self.config.window * 2)
        
        # Calculer hedge ratio et spread
        self._calculate_hedge_ratio()
        self._calculate_spread_stats()
    
    def _calculate_hedge_ratio(self):
        """Calcule le hedge ratio par régression - IDENTIQUE"""
        if len(self.price_history_1) < self.config.hedge_window:
            return
        
        try:
            # Prendre les dernières données pour le hedge ratio
            y = self.price_history_1.tail(self.config.hedge_window).values
            x = self.price_history_2.tail(self.config.hedge_window).values
            
            # Régression linéaire simple
            covariance = np.cov(x, y)[0, 1]
            variance_x = np.var(x)
            
            if variance_x > 1e-8:
                self.hedge_ratio = covariance / variance_x
            
            # Sécurité
            if self.hedge_ratio <= 0 or self.hedge_ratio > 10:
                self.hedge_ratio = 1.0
                
        except Exception as e:
            self.logger.error(f"Erreur calcul hedge ratio: {e}")
            self.hedge_ratio = 1.0
    
    def _calculate_spread_stats(self):
        """Calcule les statistiques du spread - IDENTIQUE"""
        if len(self.price_history_1) < self.config.window:
            return
        
        try:
            # Calculer le spread sur la fenêtre
            spread = self.price_history_1 - self.hedge_ratio * self.price_history_2
            spread_window = spread.tail(self.config.window)
            
            self.spread_mean = spread_window.mean()
            self.spread_std = spread_window.std()
            
            if self.spread_std < 1e-8:
                self.spread_std = 1.0
                
        except Exception as e:
            self.logger.error(f"Erreur calcul spread: {e}")
    
    def get_current_signal(self) -> int:
        """Retourne le signal actuel - IDENTIQUE"""
        if len(self.price_history_1) < self.config.window:
            return 0
        
        try:
            # Spread actuel
            current_spread = (self.price_history_1.iloc[-1] - 
                            self.hedge_ratio * self.price_history_2.iloc[-1])
            
            # Z-score
            z_score = (current_spread - self.spread_mean) / self.spread_std
            
            # Log pour debug
            self.logger.debug(f"Z-score: {z_score:.3f}, Spread: {current_spread:.6f}, HR: {self.hedge_ratio:.4f}")
            
            # Gestion des positions existantes
            if self.current_position != 0:
                # Vérifier sortie
                if self.current_position == 1 and z_score > -self.config.exit_threshold:
                    return 0  # Fermer long spread
                elif self.current_position == -1 and z_score < self.config.exit_threshold:
                    return 0  # Fermer short spread
                
                # Stop si z-score extrême
                if abs(z_score) > self.config.extreme_zscore_threshold:
                    self.logger.warning(f"Z-score extrême: {z_score:.2f}")
                    return 0  # Force close
                
                return self.current_position  # Maintenir position
            
            # Nouvelles entrées
            if z_score < -self.config.entry_threshold:
                self.logger.info(f"Signal LONG détecté: Z-score={z_score:.3f}")
                return 1  # Long spread
            elif z_score > self.config.entry_threshold:
                self.logger.info(f"Signal SHORT détecté: Z-score={z_score:.3f}")
                return -1  # Short spread
            
            return 0  # Pas de signal
            
        except Exception as e:
            self.logger.error(f"Erreur calcul signal: {e}")
            return 0
    
    def get_position_sizes(self, current_prices: Dict[str, float]) -> Tuple[float, float]:
        """Calcule les tailles de position - IDENTIQUE"""
        price_1 = current_prices.get(self.config.symbol_1, 0)
        price_2 = current_prices.get(self.config.symbol_2, 0)
        
        if price_1 <= 0 or price_2 <= 0:
            return 0, 0
        
        # Calculer les quantités basées sur la valeur USD target
        total_value = self.config.position_size_usd
        
        # Pour un spread trade, diviser la valeur entre les deux actifs
        value_per_asset = total_value / 2
        
        qty_1 = value_per_asset / price_1
        qty_2 = value_per_asset / price_2 * self.hedge_ratio
        
        # Arrondir
        qty_1 = round(qty_1, 3)
        qty_2 = round(qty_2, 3)
        
        return qty_1, qty_2


# ============================================================================
# PAPER TRADING ENGINE
# ============================================================================

class PaperTradingEngine:
    """Moteur de simulation de trading"""
    
    def __init__(self, config: TradingConfig, logger: logging.Logger, discord_notifier: DiscordNotifier):
        self.config = config
        self.logger = logger
        self.discord_notifier = discord_notifier
        
        # Portefeuille virtuel
        self.portfolio = PaperPortfolio(cash=config.initial_balance)
        self.portfolio.max_portfolio_value = config.initial_balance
        self.portfolio.min_portfolio_value = config.initial_balance
        
        # Historique des trades
        self.trade_history = []
    
    def execute_signal(self, signal: int, current_prices: Dict[str, float], 
                      strategy: StatArbStrategy) -> bool:
        """Exécute un signal en mode simulation"""
        if signal == strategy.current_position:
            return True  # Pas de changement
        
        try:
            # Fermer positions existantes
            if strategy.current_position != 0:
                self._close_positions(current_prices, strategy)
            
            # Ouvrir nouvelles positions
            if signal != 0:
                success = self._open_positions(signal, current_prices, strategy)
                if success:
                    strategy.current_position = signal
                    strategy.position_entry_time = datetime.now()
                    return True
                else:
                    return False
            else:
                strategy.current_position = 0
                strategy.position_entry_time = None
                return True
                
        except Exception as e:
            self.logger.error(f"Erreur exécution signal: {e}")
            return False
    
    def _open_positions(self, signal: int, current_prices: Dict[str, float], 
                       strategy: StatArbStrategy) -> bool:
        """Ouvre les positions virtuelles"""
        qty_1, qty_2 = strategy.get_position_sizes(current_prices)
        
        if qty_1 <= 0 or qty_2 <= 0:
            return False
        
        price_1 = current_prices[self.config.symbol_1]
        price_2 = current_prices[self.config.symbol_2]
        
        # Calculer valeurs et frais
        value_1 = qty_1 * price_1
        value_2 = qty_2 * price_2
        total_value = value_1 + value_2
        total_fees = total_value * self.config.trading_fee
        
        # Vérifier si assez de cash
        if self.portfolio.cash < total_value + total_fees:
            self.logger.warning("Pas assez de cash pour ouvrir les positions")
            return False
        
        now = datetime.now()
        
        if signal == 1:  # Long spread
            # Long symbol_1, Short symbol_2
            pos_1 = PaperPosition(
                symbol=self.config.symbol_1,
                side="Buy",
                size=qty_1,
                entry_price=price_1,
                entry_time=now,
                entry_value=value_1,
                fees_paid=value_1 * self.config.trading_fee
            )
            
            pos_2 = PaperPosition(
                symbol=self.config.symbol_2,
                side="Sell",
                size=qty_2,
                entry_price=price_2,
                entry_time=now,
                entry_value=value_2,
                fees_paid=value_2 * self.config.trading_fee
            )
        else:  # Short spread
            # Short symbol_1, Long symbol_2
            pos_1 = PaperPosition(
                symbol=self.config.symbol_1,
                side="Sell",
                size=qty_1,
                entry_price=price_1,
                entry_time=now,
                entry_value=value_1,
                fees_paid=value_1 * self.config.trading_fee
            )
            
            pos_2 = PaperPosition(
                symbol=self.config.symbol_2,
                side="Buy",
                size=qty_2,
                entry_price=price_2,
                entry_time=now,
                entry_value=value_2,
                fees_paid=value_2 * self.config.trading_fee
            )
        
        # Ajouter positions au portefeuille
        self.portfolio.positions[self.config.symbol_1] = pos_1
        self.portfolio.positions[self.config.symbol_2] = pos_2
        
        # Débiter le cash
        self.portfolio.cash -= total_value + total_fees
        self.portfolio.total_fees += total_fees
        
        # Log
        self.logger.info(f"Positions ouvertes - Signal: {signal}, Value: ${total_value:.2f}, Fees: ${total_fees:.2f}")
        
        # Notification Discord
        self.discord_notifier.send_position_opened(
            self.config.symbol_1, self.config.symbol_2, signal,
            qty_1, qty_2, price_1, price_2
        )
        
        return True
    
    def _close_positions(self, current_prices: Dict[str, float], strategy: StatArbStrategy):
        """Ferme toutes les positions"""
        if not self.portfolio.positions:
            return
        
        total_pnl = 0
        total_fees = 0
        now = datetime.now()
        
        # Copier les positions avant de les fermer (pour notification Discord)
        positions_copy = dict(self.portfolio.positions)
        
        for symbol, position in self.portfolio.positions.items():
            if symbol in current_prices:
                current_price = current_prices[symbol]
                
                # Calculer PnL
                pnl = position.get_unrealized_pnl(current_price)
                
                # Frais de sortie
                exit_value = position.size * current_price
                exit_fees = exit_value * self.config.trading_fee
                
                # Ajouter au cash
                self.portfolio.cash += position.entry_value + pnl - exit_fees
                
                total_pnl += pnl
                total_fees += exit_fees + position.fees_paid
                
                # Enregistrer le trade
                trade_record = {
                    'symbol': symbol,
                    'side': position.side,
                    'size': position.size,
                    'entry_price': position.entry_price,
                    'exit_price': current_price,
                    'entry_time': position.entry_time,
                    'exit_time': now,
                    'pnl': pnl,
                    'fees': exit_fees + position.fees_paid,
                    'duration': (now - position.entry_time).total_seconds() / 60  # minutes
                }
                self.trade_history.append(trade_record)
        
        # Mettre à jour statistiques
        self.portfolio.realized_pnl += total_pnl
        self.portfolio.total_fees += total_fees
        self.portfolio.trades_count += 1
        
        if total_pnl > 0:
            self.portfolio.winning_trades += 1
        
        # Notification Discord avant de vider les positions
        self.discord_notifier.send_position_closed(positions_copy, current_prices, total_pnl)
        
        # Vider les positions
        self.portfolio.positions.clear()
        
        # Log
        self.logger.info(f"Positions fermées - PnL: ${total_pnl:.2f}, Fees: ${total_fees:.2f}")
    
    def update_portfolio_metrics(self, current_prices: Dict[str, float]):
        """Met à jour les métriques du portefeuille"""
        self.portfolio.update_history(current_prices)


# ============================================================================
# BOT PRINCIPAL
# ============================================================================

class PaperTradingBot:
    """Bot principal de paper trading"""
    
    def __init__(self, config: TradingConfig):
        self.config = config
        self.logger = setup_logging(config)
        
        # Discord notifier
        self.discord_notifier = DiscordNotifier(config.discord_webhook_url, self.logger)
        
        # Composants
        self.ws_client = BybitWebSocketClient(config, self.logger)
        self.historical_client = BybitHistoricalClient(config, self.logger)
        self.strategy = StatArbStrategy(config, self.logger)
        self.paper_engine = PaperTradingEngine(config, self.logger, self.discord_notifier)
        
        # État
        self.is_running = False
        self.last_signal = 0
        self.start_time = None
        self.last_discord_status = 0
        
        self.logger.info("Paper Trading Bot initialisé")
    
    async def initialize(self) -> bool:
        """Initialise le bot"""
        try:
            self.start_time = datetime.now()
            
            # Connecter WebSocket
            if not self.ws_client.connect():
                return False
            
            # Attendre quelques secondes pour recevoir les prix
            await asyncio.sleep(3)
            
            # Vérifier qu'on reçoit les prix
            prices = self.ws_client.get_current_prices()
            if len(prices) < 2:
                self.logger.error("Pas assez de prix temps réel reçus")
                return False
            
            # Récupérer données historiques
            data_1 = self.historical_client.get_klines(self.config.symbol_1, 300)
            data_2 = self.historical_client.get_klines(self.config.symbol_2, 300)
            
            if len(data_1) < self.config.window or len(data_2) < self.config.window:
                self.logger.error("Pas assez de données historiques")
                return False
            
            # Initialiser stratégie
            self.strategy.update_data(data_1, data_2)
            
            self.logger.info(f"Bot initialisé - Hedge ratio: {self.strategy.hedge_ratio:.4f}")
            self.logger.info(f"Prix initiaux: {prices}")
            
            # Notification Discord de démarrage
            self.discord_notifier.send_bot_started(
                self.config.symbol_1, 
                self.config.symbol_2, 
                self.config.initial_balance
            )
            
            return True
            
        except Exception as e:
            self.logger.error(f"Erreur initialisation: {e}")
            return False
    
    async def run_cycle(self):
        """Exécute un cycle de trading"""
        try:
            # 1. Récupérer prix temps réel
            current_prices = self.ws_client.get_current_prices()
            
            if len(current_prices) < 2:
                self.logger.debug("Prix temps réel incomplets")
                return
            
            # 2. Mettre à jour données historiques périodiquement
            data_1 = self.historical_client.get_klines(self.config.symbol_1)
            data_2 = self.historical_client.get_klines(self.config.symbol_2)
            
            if data_1 and data_2:
                self.strategy.update_data(data_1, data_2)
            
            # 3. Calculer signal
            signal = self.strategy.get_current_signal()
            
            # 4. Exécuter si changement
            if signal != self.last_signal:
                self.paper_engine.execute_signal(signal, current_prices, self.strategy)
                self.last_signal = signal
                
                # Log du changement
                portfolio_value = self.paper_engine.portfolio.get_portfolio_value(current_prices)
                self.logger.warning(f"🔄 Signal: {signal} | Portfolio: ${portfolio_value:.2f} | Prix: {current_prices}")
            
            # 5. Mettre à jour métriques
            self.paper_engine.update_portfolio_metrics(current_prices)
            
            # 6. Vérifier si il faut envoyer un status Discord (toutes les heures)
            now = time.time()
            if now - self.last_discord_status >= self.config.discord_status_interval:
                self._send_discord_status_update(current_prices)
                self.last_discord_status = now
            
        except Exception as e:
            self.logger.error(f"Erreur cycle: {e}")
    
    def _send_discord_status_update(self, current_prices: Dict[str, float]):
        """Envoie une mise à jour de statut sur Discord"""
        try:
            portfolio_value = self.paper_engine.portfolio.get_portfolio_value(current_prices)
            win_rate = (self.paper_engine.portfolio.winning_trades / 
                       self.paper_engine.portfolio.trades_count * 100) if self.paper_engine.portfolio.trades_count > 0 else 0
            
            self.discord_notifier.send_status_update(
                initial_balance=self.config.initial_balance,
                current_value=portfolio_value,
                realized_pnl=self.paper_engine.portfolio.realized_pnl,
                trades_count=self.paper_engine.portfolio.trades_count,
                win_rate=win_rate
            )
        except Exception as e:
            self.logger.error(f"Erreur envoi status Discord: {e}")
    
    async def run(self):
        """Boucle principale du bot"""
        print("🤖 Paper Trading Bot - Statistical Arbitrage")
        print("=" * 60)
        
        if not await self.initialize():
            print("❌ Échec de l'initialisation")
            return
        
        self.is_running = True
        print("✅ Bot démarré en mode PAPER TRADING")
        print(f"📊 Paires: {self.config.symbol_1} / {self.config.symbol_2}")
        print(f"💰 Balance initiale: ${self.config.initial_balance}")
        print(f"📈 Taille position: ${self.config.position_size_usd}")
        print(f"⚙️  Seuils: ±{self.config.entry_threshold} (entrée), ±{self.config.exit_threshold} (sortie)")
        print(f"🔔 Discord: {'✅ Activé' if self.discord_notifier.enabled else '❌ Désactivé'}")
        print("🚀 Trading en cours... (Ctrl+C pour arrêter)")
        print()
        
        try:
            cycle_count = 0
            last_status_time = time.time()
            
            while self.is_running:
                await self.run_cycle()
                cycle_count += 1
                
                # Status périodique (toutes les 5 minutes)
                if time.time() - last_status_time > 300:
                    self._print_status()
                    last_status_time = time.time()
                
                await asyncio.sleep(self.config.loop_interval)
                
        except KeyboardInterrupt:
            print("\n🛑 Arrêt demandé par utilisateur")
        except Exception as e:
            self.logger.error(f"Erreur fatale: {e}")
            print(f"❌ Erreur fatale: {e}")
        finally:
            await self.stop()
    
    def _print_status(self):
        """Affiche le statut périodique"""
        current_prices = self.ws_client.get_current_prices()
        if current_prices:
            portfolio_value = self.paper_engine.portfolio.get_portfolio_value(current_prices)
            pnl_pct = ((portfolio_value - self.config.initial_balance) / self.config.initial_balance) * 100
            
            print(f"📊 Status: Portfolio=${portfolio_value:.2f} | PnL={pnl_pct:+.2f}% | Trades={self.paper_engine.portfolio.trades_count}")
    
    async def stop(self):
        """Arrête le bot proprement"""
        self.is_running = False
        
        # Fermer positions ouvertes
        current_prices = self.ws_client.get_current_prices()
        if current_prices and self.paper_engine.portfolio.positions:
            self.paper_engine._close_positions(current_prices, self.strategy)
        
        # Fermer WebSocket
        self.ws_client.close()
        
        # Générer rapport final
        final_value, total_return = self._generate_final_report()
        
        # Notification Discord d'arrêt
        self.discord_notifier.send_bot_stopped(
            final_value, total_return, self.paper_engine.portfolio.trades_count
        )
        
        print("✅ Bot arrêté proprement")
    
    def _generate_final_report(self) -> Tuple[float, float]:
        """Génère le rapport final de performance"""
        portfolio = self.paper_engine.portfolio
        current_prices = self.ws_client.get_current_prices()
        
        if not current_prices:
            return self.config.initial_balance, 0.0
        
        # Calculs de performance
        final_value = portfolio.get_portfolio_value(current_prices)
        total_return = final_value - self.config.initial_balance
        total_return_pct = (total_return / self.config.initial_balance) * 100
        
        # Calcul du drawdown
        if portfolio.portfolio_history:
            values = [v for _, v in portfolio.portfolio_history]
            max_drawdown = ((portfolio.max_portfolio_value - portfolio.min_portfolio_value) / 
                           portfolio.max_portfolio_value) * 100
        else:
            max_drawdown = 0
        
        # Win rate
        win_rate = (portfolio.winning_trades / portfolio.trades_count * 100) if portfolio.trades_count > 0 else 0
        
        # Durée de trading
        duration = datetime.now() - self.start_time if self.start_time else timedelta(0)
        
        print("\n" + "="*60)
        print("📊 RAPPORT FINAL DE PERFORMANCE")
        print("="*60)
        print(f"💰 Balance initiale:     ${self.config.initial_balance:,.2f}")
        print(f"💰 Balance finale:       ${final_value:,.2f}")
        print(f"📈 PnL total:            ${total_return:+,.2f} ({total_return_pct:+.2f}%)")
        print(f"💸 Frais totaux:         ${portfolio.total_fees:.2f}")
        print(f"🎯 Trades exécutés:      {portfolio.trades_count}")
        print(f"✅ Trades gagnants:      {portfolio.winning_trades} ({win_rate:.1f}%)")
        print(f"📉 Drawdown max:         {max_drawdown:.2f}%")
        print(f"⏱️  Durée de trading:    {str(duration).split('.')[0]}")
        print(f"🔄 Hedge ratio final:    {self.strategy.hedge_ratio:.4f}")
        
        # Sauvegarder les résultats
        self._save_results(final_value, total_return, total_return_pct, win_rate, max_drawdown)
        
        # Générer graphique
        if portfolio.portfolio_history:
            self._generate_chart()
        
        return final_value, total_return
    
    def _save_results(self, final_value: float, total_return: float, 
                     return_pct: float, win_rate: float, max_drawdown: float):
        """Sauvegarde les résultats dans un fichier JSON"""
        try:
            results = {
                'timestamp': datetime.now().isoformat(),
                'config': {
                    'symbol_1': self.config.symbol_1,
                    'symbol_2': self.config.symbol_2,
                    'initial_balance': self.config.initial_balance,
                    'position_size_usd': self.config.position_size_usd,
                    'entry_threshold': self.config.entry_threshold,
                    'exit_threshold': self.config.exit_threshold,
                    'window': self.config.window,
                    'hedge_window': self.config.hedge_window
                },
                'performance': {
                    'initial_balance': self.config.initial_balance,
                    'final_value': final_value,
                    'total_return': total_return,
                    'return_percentage': return_pct,
                    'total_fees': self.paper_engine.portfolio.total_fees,
                    'trades_count': self.paper_engine.portfolio.trades_count,
                    'winning_trades': self.paper_engine.portfolio.winning_trades,
                    'win_rate': win_rate,
                    'max_drawdown': max_drawdown,
                    'final_hedge_ratio': self.strategy.hedge_ratio
                },
                'trades': self.paper_engine.trade_history
            }
            
            with open(self.config.performance_file, 'w') as f:
                json.dump(results, f, indent=2, default=str)
            
            print(f"📄 Résultats sauvegardés: {self.config.performance_file}")
            
        except Exception as e:
            self.logger.error(f"Erreur sauvegarde résultats: {e}")
    
    def _generate_chart(self):
        """Génère un graphique de performance"""
        try:
            if not self.paper_engine.portfolio.portfolio_history:
                return
            
            # Préparer les données
            times = [t for t, _ in self.paper_engine.portfolio.portfolio_history]
            values = [v for _, v in self.paper_engine.portfolio.portfolio_history]
            
            # Créer le graphique
            plt.figure(figsize=(12, 8))
            
            # Graphique principal
            plt.subplot(2, 1, 1)
            plt.plot(times, values, 'b-', linewidth=2, label='Valeur du portefeuille')
            plt.axhline(y=self.config.initial_balance, color='r', linestyle='--', 
                       alpha=0.7, label='Balance initiale')
            
            plt.title(f'Performance Paper Trading - {self.config.symbol_1}/{self.config.symbol_2}', 
                     fontsize=14, fontweight='bold')
            plt.ylabel('Valeur ($)', fontsize=12)
            plt.legend()
            plt.grid(True, alpha=0.3)
            
            # Calculer les returns
            returns = [(values[i] - self.config.initial_balance) / self.config.initial_balance * 100 
                      for i in range(len(values))]
            
            # Graphique des returns
            plt.subplot(2, 1, 2)
            plt.plot(times, returns, 'g-', linewidth=2, label='Return (%)')
            plt.axhline(y=0, color='k', linestyle='-', alpha=0.3)
            
            plt.ylabel('Return (%)', fontsize=12)
            plt.xlabel('Temps', fontsize=12)
            plt.legend()
            plt.grid(True, alpha=0.3)
            
            # Format des dates
            plt.gca().xaxis.set_major_formatter(DateFormatter('%H:%M'))
            plt.gca().xaxis.set_major_locator(mdates.MinuteLocator(interval=30))
            plt.xticks(rotation=45)
            
            plt.tight_layout()
            
            # Sauvegarder
            chart_filename = f"paper_trading_chart_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            plt.savefig(chart_filename, dpi=300, bbox_inches='tight')
            plt.show()
            
            print(f"📈 Graphique sauvegardé: {chart_filename}")
            
        except Exception as e:
            self.logger.error(f"Erreur génération graphique: {e}")


# ============================================================================
# UTILITAIRES
# ============================================================================

def load_config_from_env() -> TradingConfig:
    """Charge la configuration depuis les variables d'environnement"""
    return TradingConfig(
        api_key=os.getenv('BYBIT_API_KEY', ''),
        api_secret=os.getenv('BYBIT_API_SECRET', ''),
        discord_webhook_url=os.getenv('DISCORD_WEBHOOK_URL', ''),
        testnet=False,  # Production pour données temps réel
        symbol_1=os.getenv('SYMBOL_1', 'DOTUSDT'),
        symbol_2=os.getenv('SYMBOL_2', 'KSMUSDT'),
        initial_balance=float(os.getenv('INITIAL_BALANCE', '10000')),
        position_size_usd=float(os.getenv('POSITION_SIZE', '100')),
        entry_threshold=float(os.getenv('ENTRY_THRESHOLD', '2.0')),
        exit_threshold=float(os.getenv('EXIT_THRESHOLD', '0.5')),
        loop_interval=int(os.getenv('LOOP_INTERVAL', '30'))
    )


def print_startup_info():
    """Affiche les informations de démarrage"""
    print("🤖 Statistical Arbitrage Paper Trading Bot")
    print("=" * 60)
    print("✨ Fonctionnalités:")
    print("   • Réplication exacte de la stratégie StatArb")
    print("   • Données temps réel Bybit WebSocket")
    print("   • Simulation complète des trades (paper trading)")
    print("   • Gestion du portefeuille virtuel avec frais")
    print("   • Notifications Discord en temps réel")
    print("   • Rapport de performance automatique")
    print("   • Graphique de performance matplotlib")
    print("   • Logs détaillés pour debugging")
    print()
    print("🔔 Notifications Discord:")
    print("   • Status du portefeuille toutes les heures")
    print("   • Ouverture/fermeture de positions en temps réel")
    print("   • Démarrage/arrêt du bot")
    print()
    print("⚠️  ATTENTION: Mode simulation uniquement!")
    print("   Aucun ordre réel ne sera envoyé à l'exchange.")
    print()


# ============================================================================
# POINT D'ENTRÉE PRINCIPAL
# ============================================================================

async def main():
    """Point d'entrée principal"""
    print_startup_info()
    
    # Charger configuration
    config = TradingConfig()
    
    # Si pas de clés API, essayer les variables d'environnement
    if not config.api_key:
        env_config = load_config_from_env()
        if env_config.api_key:
            config = env_config
    
    # Note: Les clés API ne sont pas obligatoires pour les données publiques
    if not config.api_key:
        print("ℹ️  Pas de clés API configurées - utilisation des données publiques uniquement")
        print("   Pour accéder aux données privées, configurez:")
        print("   • BYBIT_API_KEY et BYBIT_API_SECRET dans l'environnement")
        print("   • Ou modifiez directement TradingConfig dans le code")
        print()
    
    # Vérifier la configuration Discord
    if not config.discord_webhook_url:
        print("⚠️  Webhook Discord non configuré")
        print("   Pour activer les notifications, configurez:")
        print("   • DISCORD_WEBHOOK_URL dans l'environnement")
        print("   • Ou modifiez directement TradingConfig dans le code")
        print()
    
    print("🔧 Configuration:")
    print(f"   Paires: {config.symbol_1} / {config.symbol_2}")
    print(f"   Balance initiale: ${config.initial_balance:,.2f}")
    print(f"   Taille position: ${config.position_size_usd}")
    print(f"   Seuils: ±{config.entry_threshold} (entrée), ±{config.exit_threshold} (sortie)")
    print(f"   Fenêtre analyse: {config.window} périodes")
    print(f"   Intervalle: {config.loop_interval}s")
    print(f"   Status Discord: {config.discord_status_interval//60} min")
    print(f"   Frais simulation: {config.trading_fee*100:.1f}%")
    print()
    
    # Test de connectivité rapide
    print("🔗 Test de connectivité Bybit...")
    try:
        test_client = HTTP(testnet=False)
        response = test_client.get_server_time()
        if response['retCode'] == 0:
            print("✅ Connexion Bybit réussie!")
        else:
            print(f"❌ Erreur connexion: {response.get('retMsg', 'Inconnue')}")
            return
    except Exception as e:
        print(f"❌ Erreur test connexion: {e}")
        return
    
    # Test du webhook Discord si configuré
    if config.discord_webhook_url:
        print("🔗 Test du webhook Discord...")
        try:
            test_response = requests.post(
                config.discord_webhook_url,
                json={"content": "🧪 Test de connexion - Bot prêt à démarrer!"},
                timeout=5
            )
            if test_response.status_code == 204:
                print("✅ Webhook Discord fonctionnel!")
            else:
                print(f"⚠️  Webhook Discord: code {test_response.status_code}")
        except Exception as e:
            print(f"❌ Erreur test Discord: {e}")
    
    print()
    input("Press Enter to start paper trading... (Ctrl+C to stop anytime)")
    print()
    
    # Créer et lancer le bot
    bot = PaperTradingBot(config)
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Au revoir!")
    except Exception as e:
        print(f"\n❌ Erreur: {e}")
        import traceback
        traceback.print_exc()