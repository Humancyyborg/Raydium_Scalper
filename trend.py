
import aiohttp
import logging
from decimal import Decimal
from typing import List, Dict
import asyncio

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('scanner.log')
    ]
)

class TokenScanner:
    def __init__(self):
        self.base_url = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
        self.headers = {"accept": "application/json"}
        self.min_liquidity = 100000  # Minimum liquidity in USD
        self.min_volume = 500000    # Minimum volume in USD
        self.logger = logging.getLogger(__name__)

    async def get_trending_tokens(self) -> List[Dict]:
        params = {
            "include": "address",
            "page": 1,
            "duration": "1h"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.base_url, params=params, headers=self.headers) as response:
                    if response.status == 200:
                        data = await response.json()
                        return self.analyze_pools(data['data'])
                    return []
        except Exception as e:
            self.logger.error(f"Error fetching trending tokens: {e}")
            return []

    def analyze_pools(self, pools_data: List[Dict]) -> List[Dict]:
        analyzed_pools = []
        
        for pool in pools_data:
            try:
                attributes = pool['attributes']
                relationships = pool['relationships']
                
                # Extract base token address
                base_token = relationships['base_token']['data']['id'].split('_')[1]
                
                # Basic filters
                volume_1h = Decimal(attributes['volume_usd']['h1'])
                liquidity = Decimal(attributes['reserve_in_usd'])
                price_change = Decimal(attributes['price_change_percentage']['h1'])
                
                # Skip tokens with non-positive price change
                if price_change <= 0:
                    continue
                
                if volume_1h < self.min_volume or liquidity < self.min_liquidity:
                    continue
                
                # Calculate score
                transactions = attributes['transactions']['h1']
                buy_sell_ratio = transactions['buys'] / transactions['sells'] if transactions['sells'] != 0 else 1
                
                score = self.calculate_token_score(
                    volume_1h=volume_1h,
                    price_change=price_change,
                    buy_sell_ratio=Decimal(str(buy_sell_ratio)),
                    total_tx=transactions['buys'] + transactions['sells']
                )
                
                analyzed_pools.append({
                    'address': base_token,
                    'name': attributes['name'],
                    'score': float(score),
                    'volume_1h': float(volume_1h),
                    'liquidity': float(liquidity),
                    'price_change_1h': float(price_change),
                    'buy_sell_ratio': buy_sell_ratio,
                    'base_token_price': float(attributes['base_token_price_usd']),
                    'created_at': attributes['pool_created_at']
                })
                
            except Exception as e:
                self.logger.error(f"Error analyzing pool: {e}")
                continue
        
        return sorted(analyzed_pools, key=lambda x: x['score'], reverse=True)[:5]

    def calculate_token_score(self, volume_1h: Decimal, price_change: Decimal, 
                            buy_sell_ratio: Decimal, total_tx: int) -> Decimal:
        try:
            volume_score = float(volume_1h / Decimal('1000000'))  # Normalize by dividing by 1M
            price_change_score = float(price_change)  # Use actual positive price change
            tx_score = total_tx / 1000  # Normalize by dividing by 1K
            buy_sell_score = float(buy_sell_ratio)
            
            # Adjusted weights: 40% price change, 30% volume, 20% tx count, 10% buy/sell ratio
            score = (
                price_change_score * 0.3 +
                volume_score * 0.4 +
                tx_score * 0.2 +
                buy_sell_score * 0.1
            )
            
            return Decimal(str(score))
            
        except Exception as e:
            self.logger.error(f"Error calculating score: {e}")
            return Decimal('0')

async def main():
    scanner = TokenScanner()
    while True:
        try:
            tokens = await scanner.get_trending_tokens()
            print("\n=== TOP TRENDING TOKENS ===")
            for i, token in enumerate(tokens, 1):
                print(f"\n#{i} {token['name']}")
                print(f"Address: {token['address']}")
                print(f"Price: ${token['base_token_price']:.8f}")
                print(f"Volume (1h): ${token['volume_1h']:,.2f}")
                print(f"Price Change (1h): {token['price_change_1h']}%")
                print(f"Liquidity: ${token['liquidity']:,.2f}")
                print(f"Buy/Sell Ratio: {token['buy_sell_ratio']:.2f}")
                print(f"Score: {token['score']:.2f}")
                print("-" * 50)
            
            await asyncio.sleep(300)  # Wait 5 minutes before next scan
            
        except Exception as e:
            print(f"Error in main loop: {e}")
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())



