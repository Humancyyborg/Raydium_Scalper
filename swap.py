import aiohttp
import asyncio
import logging
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
import base64
from typing import Optional, Dict
from decimal import Decimal
import time
from enum import Enum
import requests

class TradeAction(Enum):
    BUY = "buy"
    SELL = "sell"

# Rate limiter to ensure we stay within 300 requests per minute
class RateLimiter:
    def __init__(self, max_requests: int, period: int):
        self.max_requests = max_requests
        self.period = period
        self.timestamps = []

    def __call__(self):
        now = time.time()
        self.timestamps = [t for t in self.timestamps if now - t < self.period]
        if len(self.timestamps) >= self.max_requests:
            time_to_wait = self.period - (now - self.timestamps[0])
            time.sleep(time_to_wait)
        self.timestamps.append(time.time())

# Initialize rate limiter
rate_limiter = RateLimiter(max_requests=300, period=60)

class TradingBot:
    def __init__(self, private_key: str, rpc_url: str, swap_url: str, api_key: str, sol_mint: str):
        self.setup_logging()
        
        # Initialize wallet
        try:
            self.keypair = Keypair.from_base58_string(private_key.strip())
            self.wallet_address = str(self.keypair.pubkey())
        except Exception as e:
            raise ValueError(f"Invalid private key: {str(e)}")

        # Configuration
        self.rpc_url = rpc_url
        self.swap_url = swap_url
        self.api_key = api_key
        self.sol_mint = sol_mint
        
        # State management
        self.current_position = None
        self.position_open_time = None
        self.blacklisted_tokens = set()
        self.buy_price = Decimal(0)
        self.session = None
        self.semaphore = asyncio.Semaphore(5)  # Concurrent request limiter
        self.cooldown_end_time = 0  # Track cooldown period after sells

        # Statistics
        self.trades_executed = 0
        self.total_profit = Decimal(0)

    def setup_logging(self):
        self.logger = logging.getLogger(__name__)
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)

    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=25),
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self.session

    async def execute_trade(self, token_address: str, action: TradeAction) -> bool:
        """Execute a trade with precise balance calculation and cooldown management"""
        try:
            async with self.semaphore:
                session = await self.get_session()
                
                if action == TradeAction.BUY:
                    # Check cooldown status
                    current_time = time.time()
                    if current_time < self.cooldown_end_time:
                        remaining = self.cooldown_end_time - current_time
                        self.logger.info(f"Cooldown active. Next buy allowed in {remaining:.1f} seconds.")
                        return False
                    
                    # Calculate 50% of SOL balance
                    sol_balance = await self.get_sol_balance()
                    if sol_balance < Decimal('0.0001'):
                        self.logger.error("Insufficient SOL balance")
                        return False

                    trade_amount = sol_balance * Decimal('0.5')
                    params = {
                        'fromMint': self.sol_mint,
                        'toMint': token_address,
                        'amount': str(int(trade_amount * 10**9)),
                        'slippage': 2000,
                        'priorityMicroLamports': str(int(Decimal('0.0001') * 10**9)),
                        'owner': self.wallet_address,
                        'provider': 'raydium'
                    }
                else:
                    # Get precise token balance information
                    token_info = await self.get_token_account_info(token_address)
                    if not token_info:
                        self.logger.error("No token account found")
                        return False

                    raw_amount = token_info['raw_amount']
                    if raw_amount <= 0:
                        self.logger.error("No token balance to sell")
                        return False

                    params = {
                        'fromMint': token_address,
                        'toMint': self.sol_mint,
                        'amount': str(raw_amount),
                        'slippage': 2000,  # 20% slippage for volatile exits
                        'priorityMicroLamports': str(int(Decimal('0.0001') * 10**9)),
                        'owner': self.wallet_address,
                        'provider': 'raydium'
                    }

                # Get swap transaction
                async with session.get(
                    self.swap_url,
                    params=params,
                    headers={"X-API-KEY": self.api_key}
                ) as response:
                    swap_data = await response.json()
                    if not swap_data.get("success"):
                        error_msg = swap_data.get('message', 'Unknown error')
                        self.logger.error(f"Swap API error: {error_msg} | Response: {swap_data}")
                        return False

                    # Sign and send transaction
                    transaction = VersionedTransaction.from_bytes(base64.b64decode(swap_data["data"]["base64Transaction"]))
                    signed_tx = VersionedTransaction(transaction.message, [self.keypair])

                    async with session.post(
                        self.rpc_url,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "sendTransaction",
                            "params": [
                                base64.b64encode(bytes(signed_tx)).decode('utf-8'),
                                {"encoding": "base64", "skipPreflight": True}
                            ]
                        }
                    ) as tx_response:
                        result = await tx_response.json()
                        if 'error' in result:
                            self.logger.error(f"Transaction failed: {result['error']['message']}")
                            return False

                        if action == TradeAction.BUY:
                            self.buy_price = await self.get_token_price(token_address)
                            if not self.buy_price:
                                self.logger.error("Failed to verify buy price")
                                return False

                            self.current_position = token_address
                            self.position_open_time = time.time()
                            self.trades_executed += 1
                            self.logger.info(f"Bought {token_address} at ${self.buy_price:.6f}")
                        else:
                            sell_price = await self.get_token_price(token_address)
                            profit = (sell_price - self.buy_price) * token_info['ui_amount']
                            self.total_profit += profit
                            self.logger.info(f"Sold {token_address} | Profit: ${profit:.4f}")
                            self.current_position = None
                            self.position_open_time = None
                            # Activate 5-second cooldown after successful sell
                            self.cooldown_end_time = time.time() + 25

                        return True

        except Exception as e:
            self.logger.error(f"Trade execution failed: {str(e)}", exc_info=True)
            return False

    async def get_token_account_info(self, token_address: str) -> Optional[dict]:
        """Get precise token balance information from chain"""
        try:
            session = await self.get_session()
            async with session.post(
                self.rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        self.wallet_address,
                        {"mint": token_address},
                        {"encoding": "jsonParsed"}
                    ]
                }
            ) as response:
                data = await response.json()
                if not data['result']['value']:
                    return None

                token_amount = data['result']['value'][0]['account']['data']['parsed']['info']['tokenAmount']
                return {
                    'raw_amount': int(token_amount['amount']),
                    'decimals': int(token_amount['decimals']),
                    'ui_amount': Decimal(str(token_amount['uiAmount']))
                }
        except Exception as e:
            self.logger.error(f"Token account info check failed: {str(e)}")
            return None

    async def get_sol_balance(self) -> Decimal:
        """Get current SOL balance in decimal format"""
        try:
            session = await self.get_session()
            async with session.post(
                self.rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getBalance",
                    "params": [self.wallet_address]
                }
            ) as response:
                data = await response.json()
                return Decimal(data['result']['value']) / Decimal(1e9)
        except Exception as e:
            self.logger.error(f"SOL balance check failed: {str(e)}")
            return Decimal(0)

    async def get_token_price(self, token_address: str) -> Decimal:
        """Get current token price from Jupiter API"""
        try:
            session = await self.get_session()
            async with session.get(
                "https://api.jup.ag/price/v2",
                params={'ids': token_address}
            ) as response:
                data = await response.json()
                return Decimal(str(data['data'][token_address]['price']))
        except Exception as e:
            self.logger.error(f"Price check failed: {str(e)}")
            return Decimal(0)

    async def fetch_usd_market_cap(self, token_address: str) -> Optional[Decimal]:
        """Fetch market cap from DexScreener API"""
        try:
            # Use asyncio.to_thread to run synchronous requests in a thread
            market_cap = await asyncio.to_thread(self._fetch_market_cap_sync, token_address)
            return Decimal(str(market_cap)) if market_cap is not None else None
        except Exception as e:
            self.logger.error(f"Market cap check failed: {str(e)}")
            return None

    def _fetch_market_cap_sync(self, token_address: str) -> Optional[float]:
        """Synchronous helper function to fetch market cap"""
        url = f"https://api.dexscreener.com/token-pairs/v1/solana/{token_address}"
        rate_limiter()  # Apply rate limiting
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list) and data:
                return data[0].get('marketCap')
        except Exception as e:
            self.logger.error(f"Error fetching market cap for {token_address}: {str(e)[:100]}")
        return None

    async def monitor_token(self, token_address: str):
        """Enhanced position monitoring with sell retries"""
        while self.current_position == token_address:
            try:
                current_price = await self.get_token_price(token_address)
                if not current_price:
                    await asyncio.sleep(2)
                    continue

                # Calculate profit percentage
                profit_pct = ((current_price - self.buy_price) / self.buy_price) * 100

                # Determine exit condition
                exit_reason = None
                if profit_pct >= 20:
                    exit_reason = f"20% profit target (+{profit_pct:.2f}%)"
                elif profit_pct <= -70:
                    exit_reason = f"10% stop loss ({profit_pct:.2f}%)"
                elif time.time() - self.position_open_time > 7200:
                    exit_reason = "1 hour hold timeout"

                if exit_reason:
                    self.logger.info(f"Exit triggered: {exit_reason}")
                    retry_count = 0
                    while retry_count < 3 and self.current_position:
                        success = await self.execute_trade(token_address, TradeAction.SELL)
                        if success:
                            break
                        retry_count += 1
                        self.logger.info(f"Retrying sell ({retry_count}/3)...")
                        await asyncio.sleep(2)
                    break

                await asyncio.sleep(2)
            except Exception as e:
                self.logger.error(f"Monitoring error: {str(e)}")
                await asyncio.sleep(5)

    async def close(self):
        """Cleanup resources"""
        if self.session and not self.session.closed:
            await self.session.close()


#https://gist.github.com/Humancyyborg/e663d66beec1f44942c10d31b27817d7

#screen -S cortexV1



