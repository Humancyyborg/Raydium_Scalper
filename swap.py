#swap.py

import aiohttp
import asyncio
import logging
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
import base64
from typing import Optional, Tuple
from decimal import Decimal
import time
import requests
from enum import Enum

class TradeAction(Enum):
    BUY = "buy"
    SELL = "sell"

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
        self.semaphore = asyncio.Semaphore(5)
        self.cooldown_end_time = 0

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
        #rate_limiter()  # Apply rate limiting
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list) and data:
                return data[0].get('marketCap')
        except Exception as e:
            self.logger.error(f"Error fetching market cap for {token_address}: {str(e)[:100]}")
        return None
    

    def get_associated_token_address(self, mint_address: str) -> Pubkey:
        """Compute the associated token account address using Solders types."""
        try:
            owner_pubkey = Pubkey.from_string(self.wallet_address)
            mint_pubkey = Pubkey.from_string(mint_address)
            token_program_id = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")  # TOKEN_PROGRAM_ID
            associated_token_program_id = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")  # ASSOCIATED_TOKEN_PROGRAM_ID
            
            associated_token_address, _ = Pubkey.find_program_address(
                [
                    bytes(owner_pubkey),
                    bytes(token_program_id),
                    bytes(mint_pubkey),
                ],
                associated_token_program_id
            )
            return associated_token_address
        except Exception as e:
            self.logger.error(f"ATA computation failed: {str(e)}")
            raise

    async def get_token_account_info(self, token_address: str, max_retries: int = 5) -> Optional[dict]:
        """Get token balance by directly checking the associated token account."""
        retry_delay = 1.5
        
        try:
            ata_pubkey = self.get_associated_token_address(token_address)
        except Exception as e:
            self.logger.error(f"Could not compute ATA: {str(e)}")
            return None

        for attempt in range(max_retries):
            try:
                session = await self.get_session()
                async with session.post(
                    self.rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getAccountInfo",
                        "params": [
                            str(ata_pubkey),
                            {"encoding": "jsonParsed"}
                        ]
                    }
                ) as response:
                    data = await response.json()
                    
                    if 'result' not in data:
                        self.logger.warning(f"RPC response error (attempt {attempt+1}/{max_retries})")
                        await asyncio.sleep(retry_delay * (attempt + 1))
                        continue

                    account_info = data['result']['value']
                    if not account_info:
                        return None  # ATA doesn't exist

                    if account_info['data']['program'] != 'spl-token':
                        self.logger.warning("Invalid account type")
                        return None

                    token_data = account_info['data']['parsed']['info']['tokenAmount']
                    return {
                        'raw_amount': int(token_data['amount']),
                        'decimals': int(token_data['decimals']),
                        'ui_amount': Decimal(token_data['uiAmount'])
                    }
            except Exception as e:
                self.logger.warning(f"Balance check failed (attempt {attempt+1}): {str(e)}")
                await asyncio.sleep(retry_delay * (attempt + 1))
        
        self.logger.error("Max retries reached for token balance check")
        return None

    async def verify_token_balance(self, token_address: str, min_retries: int = 3) -> Tuple[bool, Optional[dict]]:
        """Verify balance with enhanced consistency checks."""
        confirmations = 0
        last_info = None
        
        for _ in range(min_retries):
            token_info = await self.get_token_account_info(token_address)
            if not token_info:
                await asyncio.sleep(1.5)
                continue
            
            if token_info['raw_amount'] <= 0:
                self.logger.warning("Zero balance detected")
                return False, None
            
            if last_info and token_info['raw_amount'] == last_info['raw_amount']:
                confirmations += 1
            else:
                confirmations = 0
            
            last_info = token_info
        
        return confirmations >= 2, last_info

    async def execute_trade(self, token_address: str, action: TradeAction, 
                           priority_multiplier: Decimal = Decimal('1'),
                           slippage_bps: int = 2000) -> bool:
        max_retries = 3
        retry_delay = 0.5
        
        for attempt in range(max_retries + 1):
            try:
                async with self.semaphore:
                    session = await self.get_session()
                    
                    if action == TradeAction.BUY:
                        current_time = time.time()
                        if current_time < self.cooldown_end_time:
                            remaining = self.cooldown_end_time - current_time
                            self.logger.info(f"Cooldown active. Next buy allowed in {remaining:.1f} seconds.")
                            return False
                        
                        sol_balance = await self.get_sol_balance()
                        if sol_balance < Decimal('0.0001'):
                            self.logger.error("Insufficient SOL balance")
                            return False

                        trade_amount = sol_balance * Decimal('0.1')
                        params = {
                            'fromMint': self.sol_mint,
                            'toMint': token_address,
                            'amount': str(int(trade_amount * 10**9)),
                            'slippage': 3000,
                            'priorityMicroLamports': str(int(Decimal('0.0001') * 10**9)),
                            'owner': self.wallet_address,
                            'provider': 'raydium'
                        }
                    else:
                        is_valid, token_info = await self.verify_token_balance(token_address)
                        if not is_valid or not token_info:
                            self.logger.error("Failed to verify token balance for sale")
                            return False

                        raw_amount = token_info['raw_amount']
                        if raw_amount <= 0:
                            self.logger.error("No token balance to sell")
                            return False

                        params = {
                            'fromMint': token_address,
                            'toMint': self.sol_mint,
                            'amount': str(raw_amount),
                            'slippage': slippage_bps,
                            'priorityMicroLamports': str(int(Decimal('0.0001') * 10**9 * priority_multiplier)),
                            'owner': self.wallet_address,
                            'provider': 'raydium'
                        }

                    async with session.get(
                        self.swap_url,
                        params=params,
                        headers={"X-API-KEY": self.api_key}
                    ) as response:
                        swap_data = await response.json()
                        if not swap_data.get("success"):
                            error_msg = swap_data.get('message', 'Unknown error')
                            self.logger.error(f"Swap API error: {error_msg}")
                            print(swap_data)
                            return False

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
                                    {
                                        "encoding": "base64",
                                        "skipPreflight": False,
                                        "maxRetries": 3
                                    }
                                ]
                            }
                        ) as tx_response:
                            result = await tx_response.json()
                            if 'error' in result:
                                error_msg = result['error'].get('message', '')
                                if any(msg in error_msg for msg in ['Blockhash not found', 'Transaction was not confirmed']):
                                    self.logger.warning(f"Transient error detected, retrying ({attempt}/{max_retries})")
                                    await asyncio.sleep(retry_delay * (attempt + 1))
                                    continue
                                
                                self.logger.error(f"Transaction failed: {error_msg}")
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
                                if sell_price and token_info:
                                    profit = (sell_price - self.buy_price) * token_info['ui_amount']
                                    self.total_profit += profit
                                    self.logger.info(f"Sold {token_address} | Profit: ${profit:.4f}")
                                self.current_position = None
                                self.position_open_time = None
                                self.cooldown_end_time = time.time() + 15

                            return True

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < max_retries:
                    self.logger.warning(f"Network error: {str(e)}, retrying ({attempt}/{max_retries})")
                    await asyncio.sleep(retry_delay * (attempt + 1))
                    continue
                self.logger.error(f"Network failure after {max_retries} retries: {str(e)}")
                return False
            except Exception as e:
                self.logger.error(f"Trade execution failed: {str(e)}", exc_info=True)
                return False
        return False

    async def get_sol_balance(self) -> Decimal:
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

    async def monitor_token(self, token_address: str):
        while self.current_position == token_address:
            try:
                current_price = await self.get_token_price(token_address)
                if not current_price:
                    await asyncio.sleep(2)
                    continue

                profit_pct = ((current_price - self.buy_price) / self.buy_price) * 100
                time_held = time.time() - self.position_open_time

                exit_reason = None
                if profit_pct >= 15:
                    exit_reason = f"Profit target reached (+{profit_pct:.2f}%)"
                elif time_held >= 20 * 60:
                    exit_reason = "20-minute hold reached"

                if exit_reason:
                    self.logger.info(f"Exit triggered: {exit_reason}")
                    
                    is_valid, token_info = await self.verify_token_balance(token_address)
                    if not is_valid:
                        self.logger.error("Failed to verify token balance for sale")
                        await asyncio.sleep(2)
                        is_valid, token_info = await self.verify_token_balance(token_address)
                        if not is_valid:
                            self.logger.error("Token balance verification failed after retry")
                            self.current_position = None
                            break

                    retry_count = 0
                    max_sell_retries = 4
                    multipliers = [1.0, 1.5, 2.0, 3.0]
                    slippages = [2000, 3000, 4000, 5000]

                    while retry_count < max_sell_retries and self.current_position:
                        success = await self.execute_trade(
                            token_address,
                            TradeAction.SELL,
                            priority_multiplier=Decimal(str(multipliers[retry_count])),
                            slippage_bps=slippages[retry_count]
                        )
                        if success:
                            break

                        is_valid, _ = await self.verify_token_balance(token_address)
                        if not is_valid:
                            self.logger.error("Lost token balance during sell attempts")
                            self.current_position = None
                            break

                        retry_count += 1
                        if retry_count < max_sell_retries:
                            self.logger.info(
                                f"Retrying sell with {multipliers[retry_count]}x priority fee "
                                f"and {slippages[retry_count]/100}% slippage ({retry_count}/{max_sell_retries})..."
                            )
                            await asyncio.sleep(1.5 ** retry_count)
                    break

                await asyncio.sleep(2)
            except Exception as e:
                self.logger.error(f"Monitoring error: {str(e)}")
                await asyncio.sleep(5)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()