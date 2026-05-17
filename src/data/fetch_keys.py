from py_clob_client_v2 import ClobClient
import os

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,  # Polygon mainnet
    key="0x010564b0228ce5d7773659271a9a154d8c42966ba4c134a6267aa3587a9f25aa"
)

# Creates new credentials or derives existing ones
credentials = client.create_or_derive_api_key()

print(credentials)
# {
#     "apiKey": "550e8400-e29b-41d4-a716-446655440000",
#     "secret": "base64EncodedSecretString",
#     "passphrase": "randomPassphraseString"
# }
from py_clob_client_v2 import ClobClient, OrderArgs, PartialCreateOrderOptions
from py_clob_client_v2.order_builder.constants import BUY
import os

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key="0x010564b0228ce5d7773659271a9a154d8c42966ba4c134a6267aa3587a9f25aa",
    creds=credentials,  # Generated from L1 auth, API credentials enable L2 methods
    signature_type=3,  # POLY_1271, explained below
    funder="0x5f48a6AECFF273A212CE89704A2b897734f10280"
)

print(client.signer)
