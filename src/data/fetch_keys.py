import sys
import argparse
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

def fetch_api_credentials(private_key: str, host: str = "https://clob.polymarket.com", chain_id: int = 137) -> ApiCreds:
    """
    Fetches or derives L1/L2 API credentials (API key, secret, passphrase) 
    for the Polymarket CLOB using the provided wallet private key.
    """
    try:
        # Initialize client with just the private key
        client = ClobClient(
            host=host,
            key=private_key,
            chain_id=chain_id
        )
        
        # Derive or create new API credentials
        print("Authenticating and deriving L1/L2 API credentials...")
        creds = client.create_or_derive_api_creds()
        
        return creds
    except Exception as e:
        print(f"Error fetching API credentials: {str(e)}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Fetch Polymarket CLOB API Credentials from Private Key")
    parser.add_argument("--pk", required=True, help="Wallet Private Key (0x...)")
    parser.add_argument("--host", default="https://clob.polymarket.com", help="CLOB Host URL")
    parser.add_argument("--chain-id", type=int, default=137, help="Chain ID (137 for Polygon Mainnet)")
    
    args = parser.parse_args()
    
    creds = fetch_api_credentials(args.pk, args.host, args.chain_id)
    
    if creds:
        print("\n" + "="*50)
        print("SUCCESS! API Credentials Derived")
        print("="*50)
        print(f"API Key:        {creds.api_key}")
        print(f"API Secret:     {creds.api_secret}")
        print(f"API Passphrase: {creds.api_passphrase}")
        print("="*50)
        print("Copy these values into your config/live.yaml or .env file.")

if __name__ == "__main__":
    main()
