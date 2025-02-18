
import asyncio
import logging
import json
import requests
import os
from dotenv import load_dotenv
from swap import TradingBot, TradeAction
from trend import TokenScanner

# Load environment variables
load_dotenv()

def fetch_pair_data(mint_address: str):
    """
    Fetch pair data from the dexscreener API using the token's mint address.
    """
    url = f"https://api.dexscreener.com/token-pairs/v1/solana/{mint_address}"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()  # Expected to be a list of pairs
    else:
        raise Exception(f"Error fetching data from {url}")

def predict_upward_movement(pair, debug=False):
    """
    Score the coin based on short-term metrics for volume and m5 buy/sell ratio.

    Key metrics considered:
      - m5 Transaction Imbalance
      - 5-Minute Volume

    Returns:
      - bool: Prediction if upward movement is likely
      - float: Score indicating confidence in prediction
    """
    score = 0
    details = {}

    # 1. Transaction Imbalance: Focus on m5
    try:
        m5_buys = pair["txns"]["m5"]["buys"]
        m5_sells = pair["txns"]["m5"]["sells"]
    except KeyError:
        return False, "Missing transaction data for m5"

    # Avoid division by zero
    m5_ratio = m5_buys / (m5_sells if m5_sells > 0 else 1)
    details["m5_ratio"] = m5_ratio

    if m5_ratio >= 1.3:
        score += 1

    # 2. Short-Term Volume: Use the 5-minute volume for immediate activity
    m5_volume = pair.get("volume", {}).get("m5", 0)
    details["m5_volume"] = m5_volume

    SHORT_TERM_VOLUME_THRESHOLD = 100000  # Example threshold value
    if m5_volume >= SHORT_TERM_VOLUME_THRESHOLD:
        score += 2

    if debug:
        print("Debug Details:", json.dumps(details, indent=2))

    THRESHOLD_SCORE = 3
    prediction = score >= THRESHOLD_SCORE
    return prediction, score

async def listen_for_events(bot: TradingBot, scanner: TokenScanner):
    """Main event loop for processing trading signals"""

    while True:
        try:
            # Skip if already in a position
            if bot.current_position:
                await asyncio.sleep(3)
                continue

            # Fetch trending tokens
            tokens = await scanner.get_trending_tokens()
            top_tokens = tokens[:1]
            if not top_tokens:
                bot.logger.info("No trending tokens found. Retrying in 10 seconds...")
                await asyncio.sleep(10)
                continue

            for top_token in top_tokens:
                token_address = top_token['address']
                bot.logger.info(f"Evaluating {top_token['name']} ({token_address})")

                if token_address in bot.blacklisted_tokens:
                    bot.logger.info(f"Already traded {token_address}. Skipping...")
                    continue

                # Market cap filter
                market_cap = await bot.fetch_usd_market_cap(token_address)
                if market_cap is None:
                    bot.logger.info(f"Couldn't fetch market cap for {token_address}")
                    continue

                if market_cap > 40_000_000 or market_cap < 500_000:
                    bot.logger.info(f"Skipping {token_address} (MC: ${market_cap:,.2f})")
                    continue

                # === Final Check: Pair data prediction based on mint address ===
                try:
                    loop = asyncio.get_event_loop()
                    pair_data = await loop.run_in_executor(None, fetch_pair_data, token_address)
                    if not pair_data:
                        bot.logger.info(f"No pair data found for {token_address}")
                        continue

                    prediction, score = predict_upward_movement(pair_data[0], debug=True)
                    if not prediction:
                        bot.logger.info(
                            f"Skipping {token_address} - Unfavorable pair data prediction (Score: {score})"
                        )
                        continue

                except Exception as e:
                    bot.logger.error(f"Error during pair data prediction for {token_address}: {str(e)}")
                    continue
                # === End of final check ===

                # Execute trade for new trending tokens
                trade_result = await bot.execute_trade(token_address, TradeAction.BUY)
                if trade_result:
                    bot.current_position = token_address
                    bot.blacklisted_tokens.add(token_address)
                    asyncio.create_task(bot.monitor_token(token_address))
                    break  # Stop evaluating after successful trade

            await asyncio.sleep(10)

        except Exception as e:
            bot.logger.error(f"Event loop error: {str(e)}")
            await asyncio.sleep(5)

async def main():
    try:
        # Load configuration from .env
        private_key = os.getenv("PRIVATE_KEY")
        if not private_key:
            raise ValueError("Missing PRIVATE_KEY in .env")

        # Initialize components
        bot = TradingBot(
            private_key=private_key,
            rpc_url=os.getenv("RPC_URL"),
            swap_url=os.getenv("SPIDER_SWAP_URL"),
            api_key=os.getenv("SPIDER_SWAP_API_KEY"),
            sol_mint=os.getenv("SOL_ADDRESS")
        )
        scanner = TokenScanner()

        # Start main loop
        await listen_for_events(bot, scanner)

    except Exception as e:
        logging.error(f"Fatal error: {str(e)}")
    finally:
        if 'bot' in locals():
            await bot.close()

if __name__ == "__main__":
    asyncio.run(main())







