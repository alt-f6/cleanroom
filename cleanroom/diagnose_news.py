from dotenv import load_dotenv
load_dotenv()
import os
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

client = NewsClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
response = client.get_news(NewsRequest(symbols="AAPL", limit=2))

print("response type:", type(response))
print("public attrs/methods:", [a for a in dir(response) if not a.startswith("_")])
print()

if hasattr(response, "data"):
    print("response.data type:", type(response.data))
    print("response.data:", response.data)
    print()

if hasattr(response, "df"):
    try:
        print("response.df:")
        print(response.df)
        print()
    except Exception as e:
        print("response.df failed:", e)

# NewsSet may be a Mapping keyed by symbol (like bars/quotes) - try that shape
try:
    keys = list(response.keys())
    print("response.keys():", keys)
    if keys:
        first_key = keys[0]
        val = response[first_key]
        print(f"response['{first_key}'] type:", type(val))
        print(f"response['{first_key}']:", val)
except Exception as e:
    print(".keys() approach failed:", e)

# Try raw_data mode as a fallback comparison
print()
print("--- retrying with raw_data=True ---")
raw_client = NewsClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], raw_data=True)
raw_response = raw_client.get_news(NewsRequest(symbols="AAPL", limit=2))
print("raw response type:", type(raw_response))
print("raw response:", raw_response)