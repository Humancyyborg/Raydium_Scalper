
import asyncio
import logging
import json
import requests
import os
from dotenv import load_dotenv
from swapReg import TradingBot, TradeAction
from trend import TokenScanner

# Load environment variables
load_dotenv()


def is_near_psychological_zone(market_cap: float, threshold_percentage: float = 0.20) -> bool:
    """
    Check if market cap is within threshold_percentage of any psychological zone.
    Psychological zones are dynamically generated based on the order of magnitude of the market cap.
    """
    if market_cap <= 0:
        return False

    # Calculate the order of magnitude of the market cap
    magnitude = 10 ** (int(market_cap).bit_length() - 1) if market_cap >= 1 else 1

    # Generate psychological levels dynamically
    psychological_levels = [
        magnitude,           # e.g., 1,000,000
        magnitude * 2,       # e.g., 2,000,000
        magnitude * 5,       # e.g., 5,000,000
        magnitude * 10,      # e.g., 10,000,000
        magnitude * 20,      # e.g., 20,000,000
        magnitude * 50,      # e.g., 50,000,000
        magnitude * 100,     # e.g., 100,000,000
    ]

    for level in psychological_levels:
        lower_bound = level * (1 - threshold_percentage)
        upper_bound = level * (1 + threshold_percentage)
        if lower_bound <= market_cap <= upper_bound:
            return True
    return False


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
    Score the coin based on short-term metrics for a memecoin.

    Key metrics considered:
      - Transaction Imbalance (m5 and h1 buy/sell ratios)
      - Short-Term Price Change (m5 and h1)
      - 5-Minute Volume (to gauge immediate trading activity)
      - Liquidity Check (to ensure market depth for execution)

    The higher the score, the higher the confidence that the coin may achieve a 20%
    upward move in the near term.
    """
    score = 0
    details = {}

    # 1. Transaction Imbalance: Focus on m5 and h1
    try:
        m5_buys = pair["txns"]["m5"]["buys"]
        m5_sells = pair["txns"]["m5"]["sells"]
        h1_buys = pair["txns"]["h1"]["buys"]
        h1_sells = pair["txns"]["h1"]["sells"]
    except KeyError:
        return False, "Missing transaction data"

    # Avoid division by zero
    m5_ratio = m5_buys / (m5_sells if m5_sells > 0 else 1)
    h1_ratio = h1_buys / (h1_sells if h1_sells > 0 else 1)
    details["m5_ratio"] = m5_ratio
    details["h1_ratio"] = h1_ratio

    if m5_ratio >= 1.5:
        score += 1
    if h1_ratio >= 1.2:
        score += 1

    # 2. Price Change Momentum: Look at m5 and h1 price changes
    price_change_m5 = pair.get("priceChange", {}).get("m5", 0)
    price_change_h1 = pair.get("priceChange", {}).get("h1", 0)
    details["price_change_m5"] = price_change_m5
    details["price_change_h1"] = price_change_h1

    if price_change_m5 > 0:
        score += 1
    if price_change_h1 > 0:
        score += 1

    # 3. Short-Term Volume: Use the 5-minute volume for immediate activity
    m5_volume = pair.get("volume", {}).get("m5", 0)
    details["m5_volume"] = m5_volume

    SHORT_TERM_VOLUME_THRESHOLD = 300000  # Example threshold value
    if m5_volume >= SHORT_TERM_VOLUME_THRESHOLD:
        score += 1

    # 4. Liquidity Check: Ensure sufficient liquidity exists
    liquidity_usd = pair.get("liquidity", {}).get("usd", 0)
    details["liquidity_usd"] = liquidity_usd
    LIQUIDITY_THRESHOLD = 110000  
    if liquidity_usd >= LIQUIDITY_THRESHOLD:
        score += 1

    if debug:
        print("Debug Details:", json.dumps(details, indent=2))

    THRESHOLD_SCORE = 4
    prediction = score >= THRESHOLD_SCORE
    return prediction, score


async def listen_for_events(bot: TradingBot, scanner: TokenScanner):
    """Main event loop for processing trading signals"""
    initial_token_processed = False  # Flag to track if the initial token has been processed

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

                if market_cap > 20_000_000 or market_cap < 700_000:
                    bot.logger.info(f"Skipping {token_address} (MC: ${market_cap:,.2f})")
                    continue

                # Check if near psychological zone
                if is_near_psychological_zone(market_cap):
                    bot.logger.info(
                        f"Skipping {token_address} - Too close to psychological zone "
                        f"(MC: ${market_cap:,.2f})"
                    )
                    continue

                # Blacklist the initial trending token (without any further checks) as per the original pattern
                if not initial_token_processed:
                    bot.logger.info(f"Blacklisting initial token {token_address} without trading.")
                    bot.blacklisted_tokens.add(token_address)
                    initial_token_processed = True
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
